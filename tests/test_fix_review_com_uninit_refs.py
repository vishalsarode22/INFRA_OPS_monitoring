"""
Review fix 8e: a pipeline thread's stored exception no longer keeps COM
objects (frame locals) alive past CoUninitialize.
"""
import gc
import sys
import traceback
import types
import weakref

import main


class FakeCom:
    pass


def _deep():
    com = FakeCom()                  # a SAP GUI COM object held in a local
    _deep.ref = weakref.ref(com)
    raise RuntimeError("session went away")


def test_release_frame_locals_drops_com_objects_but_keeps_the_trace():
    try:
        _deep()
    except RuntimeError as e:
        stored = e
    assert _deep.ref() is not None                 # kept alive by the traceback
    main._release_frame_locals(stored)
    gc.collect()
    assert _deep.ref() is None
    text = "".join(traceback.format_exception(stored))
    assert "_deep" in text and "session went away" in text


def test_chained_exceptions_are_cleared_too():
    def inner():
        com = FakeCom()
        inner.ref = weakref.ref(com)
        raise KeyError("x")
    try:
        try:
            inner()
        except KeyError as k:
            raise ValueError("outer") from k
    except ValueError as e:
        stored = e
    main._release_frame_locals(stored)
    gc.collect()
    assert inner.ref() is None


def test_watchdog_thread_clears_before_couninitialize(monkeypatch):
    order = []
    fake = types.SimpleNamespace(CoInitialize=lambda: None,
                                 CoUninitialize=lambda: order.append(("uninit", _deep.ref() is None)))
    monkeypatch.setitem(sys.modules, "pythoncom", fake)
    real = main._release_frame_locals

    def spy(exc):
        real(exc)
        gc.collect()
    monkeypatch.setattr(main, "_release_frame_locals", spy)
    monkeypatch.setattr(main, "_run_pipeline_for_system_once", lambda cfg: _deep())
    try:
        main._run_with_watchdog({"name": "X"})
    except RuntimeError:
        pass
    assert order == [("uninit", True)]             # COM object gone before CoUninitialize
