"""A stand-in for offthread.Background, for tests that build a window out of a SimpleNamespace:
`run` does the work on a thread of its own and calls `then` there (no Tk thread to hand it to);
`post` calls at once. `inline=True` does everything on the calling thread, in order."""

from __future__ import annotations

import threading


class FakeWork:
    def __init__(self, inline: bool = False):
        self.inline = inline
        self.threads: list[threading.Thread] = []

    def run(self, work, then=None, *, name: str = "work"):
        if self.inline:
            value = work()
            if then is not None:
                then(value)
            return None

        def go():
            value = work()
            if then is not None:
                then(value)

        thread = threading.Thread(target=go, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def post(self, fn, *args):
        fn(*args)

    def join(self, timeout: float = 5.0):
        for thread in self.threads:
            thread.join(timeout)
