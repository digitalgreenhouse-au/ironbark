"""offthread.Background: one answer that raises is reported, and every other answer still arrives."""

from __future__ import annotations

import gc
import time

import pytest

from ironbark import offthread


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.withdraw()
    yield r
    r.destroy()
    del r
    gc.collect()   # (on this thread: Tk objects freed on another one abort Python)


def _pump(root, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def test_an_answer_that_raises_is_reported_and_the_others_still_arrive(root):
    errors = []
    root.report_callback_exception = lambda kind, value, trace: errors.append(value)
    work = offthread.Background(root)
    got = []

    def broken(_value):
        raise ValueError("a slip in one callback")

    work.post(broken, None)
    work.post(got.append, "waiting behind it")
    _pump(root, 0.3)
    work.run(lambda: "later", then=got.append, name="later")
    _pump(root, 0.6)
    work.close()
    assert got == ["waiting behind it", "later"]
    assert [type(e) for e in errors] == [ValueError]


def test_nothing_runs_once_closed(root):
    work = offthread.Background(root)
    got = []
    work.close()
    work.post(got.append, "after closing")
    _pump(root, 0.3)
    assert got == [] and work._job is None
