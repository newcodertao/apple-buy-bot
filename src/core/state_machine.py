from collections.abc import Callable
from datetime import UTC, datetime

from src.core.models import Platform, State

TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.PREPARING},
    State.PREPARING: {State.WAITING, State.MONITORING},
    State.WAITING: {State.PREPARING, State.MONITORING},
    State.MONITORING: {State.STOCK_FOUND},
    State.STOCK_FOUND: {State.SELECTING_SKU, State.MONITORING},
    State.SELECTING_SKU: {State.ADDING_CART, State.MONITORING},
    State.ADDING_CART: {State.CHECKOUT, State.MONITORING},
    State.CHECKOUT: {State.VERIFYING},
    State.VERIFYING: {State.READY_TO_SUBMIT, State.MONITORING},
    State.WAITING_HUMAN: {
        State.PREPARING, State.WAITING, State.MONITORING, State.STOCK_FOUND,
        State.SELECTING_SKU, State.ADDING_CART, State.CHECKOUT, State.VERIFYING,
    },
    State.READY_TO_SUBMIT: {State.VERIFYING, State.SUBMITTING},
    State.SUBMITTING: {State.SUCCESS},
    State.FAILED: {State.RETRYING, State.STOPPED},
    State.RETRYING: {State.PREPARING, State.MONITORING},
    State.SUCCESS: set(),
    State.STOPPED: set(),
}
for _state in State:
    if _state not in {State.SUCCESS, State.STOPPED, State.FAILED}:
        TRANSITIONS[_state] |= {State.WAITING_HUMAN, State.FAILED, State.STOPPED} - {_state}


class StateMachine:
    def __init__(
        self, platform: Platform, product_id: str,
        recorder: Callable[[dict], None] | None = None,
    ):
        self.platform = Platform(platform)
        self.product_id = product_id
        self.state = State.IDLE
        self.recorder = recorder

    def transition(self, new: State, sku_id: str = "", message: str = "") -> None:
        new = State(new)
        if new not in TRANSITIONS[self.state]:
            raise ValueError(f"Invalid state transition: {self.state.value} -> {new.value}")
        event = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "platform": self.platform.value,
            "product": self.product_id,
            "sku": sku_id,
            "old_state": self.state.value,
            "new_state": new.value,
            "message": message,
        }
        # Persist first: failed audit storage must not silently advance the flow.
        if self.recorder:
            self.recorder(event)
        self.state = new
