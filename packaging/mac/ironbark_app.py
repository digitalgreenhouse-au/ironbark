"""The packaged app's starting point (PyInstaller needs a script, not a package): the same
start-up as `uv run ironbark`."""

import multiprocessing


def run() -> None:
    # A process started by multiprocessing (a library indexer's or library check's worker) runs
    # its task here and exits, instead of opening a second player — before the player is even
    # imported: a worker must never load Tk.
    multiprocessing.freeze_support()
    from ironbark.__main__ import main

    main()


if __name__ == "__main__":
    run()
