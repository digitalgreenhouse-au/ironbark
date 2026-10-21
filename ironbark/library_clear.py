"""Settings → Library → Clear the library…: start again with an empty Library. Every library folder
is taken out — the system's Music folder doesn't stand in, as it does before a folder is first
chosen — and everything stored about the music is forgotten, so the Library stays empty until a
folder is added (Add folder…). The music files aren't touched; plays, favourites and quality checks
are kept (they're keyed by the file) for if the music is added back. Asked first.

Rebuild the library… is the other way to start again: the same folders, read afresh."""

from __future__ import annotations

from . import offthread, popups


def ask(parent) -> bool:
    """Asked first, with what goes and what stays. True: go ahead."""
    return popups.confirm(
        parent, "Clear the library?",
        "Ironbark takes every folder out of the Library and forgets everything it has stored about "
        "your music. The Library stays empty until you add a folder.",
        detail=("Your music files aren't touched, and your plays, favourites and quality checks are "
                "kept for if you add the music back."),
        ok="Clear", cancel="Keep it", danger=True)


def clear(win) -> None:
    """The button (`win`: the player window). Asked first; then the settings list no library
    folders, and the library database forgets what it stored — on a worker, as a look under way is
    stopped first. The Library tab and the Library's views follow (they show it empty)."""
    if win.prefs.get("library_empty"):
        win._set_notice("The Library is empty already: Add folder… fills it", 5000)
        return
    win._for_prompt()
    if not ask(win.root):
        return
    win._set_library_folders([])
    index = win._index()

    def work():
        try:
            index.empty()
        finally:
            index.db.release()

    def then(result):
        if isinstance(result, offthread.Error):
            win._set_notice(f"Couldn't clear the library: {result.error}", 8000)
        else:
            win._set_notice("Cleared the library: it's empty until you add a folder", 6000)

    win._set_notice("Clearing the library…", 4000)
    win.work.run(work, then, name="library-clear")
