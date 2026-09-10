import pytest

from src.core.models import Platform, State
from src.core.state_machine import StateMachine


def test_purchase_chain_is_recorded_and_terminal():
    events = []
    machine = StateMachine(Platform.APPLE, "iphone", events.append)
    chain = [State.PREPARING, State.WAITING, State.MONITORING, State.STOCK_FOUND,
             State.SELECTING_SKU, State.ADDING_CART, State.CHECKOUT, State.VERIFYING,
             State.READY_TO_SUBMIT, State.VERIFYING, State.READY_TO_SUBMIT,
             State.SUBMITTING, State.SUCCESS]
    for state in chain:
        machine.transition(state, sku_id="s1", message="safe static message")
    assert len(events) == len(chain)
    assert events[0]["old_state"] == "IDLE"
    assert events[-1] | {"timestamp": "ignored"} == {
        "timestamp": "ignored", "platform": "apple", "product": "iphone",
        "sku": "s1", "old_state": "SUBMITTING", "new_state": "SUCCESS",
        "message": "safe static message",
    }
    assert events[-1]["timestamp"].endswith("+00:00")
    with pytest.raises(ValueError, match="Invalid state transition"):
        machine.transition(State.SUBMITTING)


def test_no_submission_from_idle_or_human_takeover():
    machine = StateMachine(Platform.JD, "iphone")
    with pytest.raises(ValueError):
        machine.transition(State.SUBMITTING)
    machine.transition(State.PREPARING)
    machine.transition(State.WAITING_HUMAN)
    with pytest.raises(ValueError):
        machine.transition(State.SUBMITTING)
    machine.transition(State.VERIFYING)
    machine.transition(State.FAILED)
    machine.transition(State.RETRYING)
    machine.transition(State.MONITORING)
    machine.transition(State.STOPPED)
    with pytest.raises(ValueError):
        machine.transition(State.PREPARING)


def test_failed_recorder_does_not_advance_state():
    def broken_recorder(event):
        raise OSError("Simulated full disk")

    machine = StateMachine(Platform.TMALL, "iphone", broken_recorder)
    with pytest.raises(OSError):
        machine.transition(State.PREPARING)
    assert machine.state is State.IDLE
