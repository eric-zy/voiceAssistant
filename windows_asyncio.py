"""Keep expected Windows Proactor disconnect callbacks out of error logs."""
from contextlib import contextmanager
import logging
import os


def is_expected_disconnect(context):
    error = context.get("exception")
    callback = getattr(context.get("handle"), "_callback", None)
    return (isinstance(error, ConnectionResetError)
            and getattr(error, "winerror", None) == 10054
            and getattr(callback, "__module__", None) == "asyncio.proactor_events"
            and getattr(callback, "__name__", None) == "_call_connection_lost")


@contextmanager
def disconnect_logging(loop, *, windows=None):
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        yield
        return
    previous = loop.get_exception_handler()

    def handler(current_loop, context):
        if is_expected_disconnect(context):
            logging.getLogger("uvicorn.error").debug("Windows peer disconnected during transport cleanup (10054)")
        elif previous is not None:
            previous(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    try:
        yield
    finally:
        if loop.get_exception_handler() is handler:
            loop.set_exception_handler(previous)
