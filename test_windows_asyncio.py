from types import SimpleNamespace

import pytest
from windows_asyncio import disconnect_logging, is_expected_disconnect


def reset_context(code=10054, module="asyncio.proactor_events"):
    error = ConnectionResetError("peer disconnected")
    error.winerror = code
    def _call_connection_lost(exc):
        pass
    _call_connection_lost.__module__ = module
    return {"exception":error,"handle":SimpleNamespace(_callback=_call_connection_lost)}


class Loop:
    def __init__(self, previous=None):
        self.handler = previous
        self.errors = []
    def get_exception_handler(self):
        return self.handler
    def set_exception_handler(self, handler):
        self.handler = handler
    def default_exception_handler(self, context):
        self.errors.append(context)


def test_only_transport_reset_is_expected():
    assert is_expected_disconnect(reset_context())
    assert not is_expected_disconnect(reset_context(10053))
    assert not is_expected_disconnect(reset_context(module="app"))
    assert not is_expected_disconnect({"exception":RuntimeError("application error")})


def test_expected_reset_does_not_hide_application_errors():
    loop = Loop()
    with disconnect_logging(loop, windows=True):
        loop.handler(loop,reset_context())
        actual_error = {"exception":RuntimeError("actual failure")}
        loop.handler(loop,actual_error)
    assert loop.errors == [actual_error]
    assert loop.handler is None


def test_previous_handler_is_preserved():
    received = []
    def previous(loop, context):
        received.append(context)
    loop = Loop(previous)
    with disconnect_logging(loop, windows=True):
        context = reset_context(module="app")
        loop.handler(loop,context)
    assert received == [context]
    assert loop.handler is previous


def test_handler_restored_when_lifespan_fails():
    loop = Loop()
    with pytest.raises(ValueError):
        with disconnect_logging(loop, windows=True):
            raise ValueError("startup failed")
    assert loop.handler is None
