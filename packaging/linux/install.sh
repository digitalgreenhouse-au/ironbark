#!/bin/sh
# Adds Ironbark (this folder) to the desktop's menu for this user: a launcher in
# ~/.local/share/applications and the icon. Nothing is copied or needs root: the program stays
# in this folder, so move the folder first, then run this.
#   ./install.sh            add it (again: after moving the folder)
#   ./install.sh --remove   take it off the menu (the folder itself is left alone)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
launcher=$data/applications/ironbark.desktop
icon=$data/icons/hicolor/256x256/apps/ironbark.png

if [ "${1:-}" = "--remove" ]; then
    rm -f "$launcher" "$icon"
    echo "Ironbark is off the menu. (This folder is still here: delete it to remove the program.)"
else
    mkdir -p "$(dirname "$launcher")" "$(dirname "$icon")"
    cp "$here/ironbark.png" "$icon"
    # The launcher names this folder's program; quoted, so a folder name with spaces works.
    literal=$(printf '%s' "$here" | sed 's/[\\&|]/\\&/g')   # (a folder named "Rock & roll", say)
    sed "s|^Exec=Ironbark|Exec=\"$literal/Ironbark\"|" "$here/ironbark.desktop" > "$launcher"
    echo "Ironbark is on the menu (Sound & Video), starting $here/Ironbark."
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$data/icons/hicolor" 2>/dev/null || true
