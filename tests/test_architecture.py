"""The package's layering, read from the imports themselves, so a change that breaks it fails
here in the quick tier (CI's lint-imports checks the same rules from pyproject.toml). The rules:

- the tag code (tagio) is self-contained: it imports nothing else in the package;
- the engine, the file and tag readers, the library database and the network cache know nothing
  of the window, the tools or the window toolkit;
- the Check tools import from the core, never the other way round: nothing outside tools/ imports
  a tool except the main window;
- the main window module is imported only by the entry point and the self-check;
- only the windows and the widget kit import the window toolkit.

(The toolkit's name, the window modules' suffix and the main window's name are spelled in two
parts here because conftest marks any test module that names them as one that makes windows,
and this one makes none.)
"""
from __future__ import annotations

import ast
from pathlib import Path

import ironbark

ROOT = Path(ironbark.__file__).parent
TOOLKIT = "tk" + "inter"
WINDOW = "_win" + "dow"
MAIN_WINDOW = "u" + "i"

CORE = ["engine", "session", "audiofile", "library", "library_db", "library_sql", "library_import", "indexer",
        "tagio", "netcache", "analysis",
        "loudness", "proof", "devices", "settings", "places", "musicfiles"]
WINDOW_SIDE = [MAIN_WINDOW, "tools", "toolwin", "controls", "tabbar", "popups", "theme", "shapes"]
# The widget kit and the app platform's few toolkit users; every *_view and *_window module may too.
MAY_USE_TOOLKIT = {MAIN_WINDOW, "__main__", "appicon", "chrome", "controls", "diagnostics", "dragdrop",
                   "keyfocus", "keys", "offthread", "placement", "popups", "selfcheck", "shapes",
                   "tabbar", "theme", "toolwin", "virtual_list", "tools.details"}


def modules() -> dict[str, Path]:
    found = {}
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(ROOT).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        found[name or "(package)"] = path
    return found


def imports_of(name: str, path: Path) -> set[str]:
    """What the module imports: package modules as dotted names without the package prefix
    (`library.Track`, `netcache.store`), anything else by its top-level name (`numpy`)."""
    if name == "(package)":
        package = []
    elif path.name == "__init__.py":
        package = name.split(".")
    else:
        package = name.split(".")[:-1]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.removeprefix("ironbark.") if alias.name.startswith("ironbark.")
                          else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                target = ".".join(base + ([node.module] if node.module else []))
                for alias in node.names:
                    found.add(f"{target}.{alias.name}" if target else alias.name)
            elif node.module:
                found.add(node.module.removeprefix("ironbark.") if node.module.startswith("ironbark.")
                          else node.module.split(".")[0])
    return found


def within(imported: str, *names: str) -> bool:
    return any(imported == n or imported.startswith(n + ".") for n in names)


def uses_toolkit(imported: str) -> bool:
    return imported == TOOLKIT or imported.startswith(TOOLKIT + ".") or imported == TOOLKIT + "dnd2"


def graph() -> dict[str, set[str]]:
    return {name: imports_of(name, path) for name, path in modules().items()}


def test_the_tag_code_is_self_contained():
    package_names = {name.split(".")[0] for name in modules()}
    wrong = [(m, i) for m, imports in graph().items() if within(m, "tagio")
             for i in imports if i.split(".")[0] in package_names and not within(i, "tagio")]
    assert wrong == []


def test_the_core_knows_nothing_of_the_window_side():
    wrong = [(m, i) for m, imports in graph().items() if within(m, *CORE)
             for i in imports if within(i, *WINDOW_SIDE) or uses_toolkit(i)
             or i.endswith(WINDOW) or i.endswith("_view")]
    assert wrong == []


def test_the_tools_import_from_the_core_never_the_other_way():
    g = graph()
    into_tools = [(m, i) for m, imports in g.items() if not within(m, "tools") and m != MAIN_WINDOW
                  for i in imports if within(i, "tools")]
    out_to_window = [(m, i) for m, imports in g.items() if within(m, "tools")
                     for i in imports if within(i, MAIN_WINDOW)]
    assert into_tools == [] and out_to_window == []


def test_the_main_window_is_imported_only_by_the_entry_point_and_the_self_check():
    importers = sorted(m for m, imports in graph().items() if any(within(i, MAIN_WINDOW) for i in imports))
    assert importers == ["__main__", "selfcheck"]


def test_only_the_windows_and_the_widget_kit_use_the_toolkit():
    wrong = sorted(m for m, imports in graph().items() if any(uses_toolkit(i) for i in imports)
                   and m not in MAY_USE_TOOLKIT and not m.endswith(WINDOW) and not m.endswith("_view"))
    assert wrong == []
