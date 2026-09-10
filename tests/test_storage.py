from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from src.core.models import SKU, Platform, State
from src.core.state_machine import StateMachine
from src.storage.database import Database


def test_audit_records_survive_restart_and_reject_untrusted_table(tmp_path):
    path = tmp_path / "audit.db"
    database = Database(path)
    database.initialize()
    run_id = database.create_run(None, "iphone")
    machine = StateMachine(Platform.APPLE, "iphone", lambda e: database.record_event(run_id, e))
    machine.transition(State.PREPARING, "s1", "Preparing")
    sku = SKU(id="s1", platform=Platform.APPLE, product_id="iphone", model="iPhone",
              capacity="512GB", color="黑色", price=Decimal("14999.99"), available=True)
    database.record_stock(run_id, sku, 12.4)
    assert database.record_order(run_id, sku, "DRY_RUN") == 1
    database.finish_run(run_id, "STOPPED")
    database.close()
    reopened = Database(path)
    try:
        reopened.initialize()
        assert reopened.recent("runs")[0]["status"] == "STOPPED"
        assert reopened.recent("events")[0]["new_state"] == "PREPARING"
        assert reopened.recent("stock_checks")[0]["price"] == "14999.99"
        assert reopened.recent("orders")[0]["status"] == "DRY_RUN"
        with pytest.raises(ValueError):
            reopened.recent("orders; DROP TABLE runs")
        with pytest.raises(ValueError):
            reopened.recent("events", -1)
        with pytest.raises(ValueError):
            reopened.record_stock(run_id, sku, float("nan"))
    finally:
        reopened.close()


def test_order_guard_only_one_winner_across_concurrent_connections(tmp_path):
    path = tmp_path / "guard.db"
    primary = Database(path)
    primary.initialize()
    databases = [Database(path) for _ in range(6)]
    barrier = Barrier(len(databases))

    def claim(index):
        barrier.wait(timeout=5)
        return databases[index].claim_order(f"worker-{index}")

    try:
        with ThreadPoolExecutor(max_workers=len(databases)) as executor:
            results = list(executor.map(claim, range(len(databases))))
        assert sum(results) == 1
        owner = primary.guard_status()["owner"]
        assert primary.claim_order(owner) is False
        assert primary.release_order("wrong-owner") is False
        assert primary.release_order(owner) is True
        assert primary.claim_order("new-owner") is True
    finally:
        for database in [primary, *databases]:
            database.close()


@pytest.mark.parametrize("status", ["SUBMITTING", "UNKNOWN", "SUCCESS"])
def test_restart_keeps_uncertain_or_successful_reservation(tmp_path, status):
    path = tmp_path / "guard.db"
    database = Database(path)
    database.initialize()
    assert database.claim_order("first")
    database.mark_submission("first", "SUBMITTING")
    if status != "SUBMITTING":
        database.mark_submission("first", status)
    database.close()
    reopened = Database(path)
    try:
        reopened.initialize()
        assert reopened.guard_status()["status"] == status
        assert not reopened.release_order("first")
        assert not reopened.claim_order("second")
        with pytest.raises(ValueError):
            reopened.mark_submission("first", "SUBMITTING")
        with pytest.raises(ValueError):
            reopened.reconcile_order("first", confirmed_no_order=False)
        if status == "SUCCESS":
            assert not reopened.reconcile_order("first", confirmed_no_order=True)
        else:
            assert reopened.reconcile_order("first", confirmed_no_order=True)
            assert reopened.claim_order("second")
    finally:
        reopened.close()


def test_confirmed_rejection_can_release_but_unknown_cannot(tmp_path):
    database = Database(tmp_path / "guard.db")
    database.initialize()
    try:
        assert database.claim_order("owner")
        with pytest.raises(ValueError):
            database.mark_submission("someone-else", "SUBMITTING")
        database.mark_submission("owner", "SUBMITTING")
        database.mark_submission("owner", "REJECTED")
        assert database.release_order("owner")
        assert database.claim_order("owner")
        database.mark_submission("owner", "SUBMITTING")
        database.mark_submission("owner", "UNKNOWN")
        with pytest.raises(ValueError):
            database.mark_submission("owner", "REJECTED")
        assert not database.release_order("owner")
    finally:
        database.close()
