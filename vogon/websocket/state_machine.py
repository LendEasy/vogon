"""Call-level state machine.

CallState models the overall WebSocket call lifecycle.
"""
from enum import Enum


class CallState(str, Enum):
    CONNECTING  = "connecting"   # WebSocket handshake in progress
    ACTIVE      = "active"       # STT open, greeting sent, workflow running
    COMPLETING  = "completing"   # Booking confirmed, wrapping up
    DONE        = "done"         # Terminal — call ended normally
    ERROR       = "error"        # Terminal — call ended with error


VALID_TRANSITIONS: dict[CallState, set[CallState]] = {
    CallState.CONNECTING: {CallState.ACTIVE, CallState.ERROR, CallState.DONE},
    CallState.ACTIVE:     {CallState.COMPLETING, CallState.ERROR, CallState.DONE},
    CallState.COMPLETING: {CallState.DONE, CallState.ERROR},
    CallState.DONE:       set(),
    CallState.ERROR:      set(),
}


class StateMachine:
    def __init__(self) -> None:
        self.state: CallState = CallState.CONNECTING

    def transition(self, new_state: CallState) -> bool:
        if new_state in VALID_TRANSITIONS.get(self.state, set()):
            self.state = new_state
            return True
        return False

    def is_terminal(self) -> bool:
        return self.state in (CallState.DONE, CallState.ERROR)
