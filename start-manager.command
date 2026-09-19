#!/usr/bin/env bash
# Launch the Typora Themes Manager web console (opens your browser automatically).
# macOS: double-click this file (if blocked the first time, right-click -> Open);
#        you can also run it in a terminal: bash start-manager.command
# Note: keep this file name pure ASCII -- when launching a .command via double-click,
# Finder->Terminal mangles non-ASCII file names and Terminal then fails with
# "No such file or directory".
# Linux: right-click -> "Run in terminal" (or chmod +x once, then execute directly)

cd "$(dirname "$0")" || exit 1

# Force UTF-8 output encoding so messages never garble in odd shell environments
export PYTHONIOENCODING=utf-8:replace

# Probe python3 / python in order; both require Python 3.8+
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "[Error] No usable Python 3.8+ environment was detected."
    echo "Please download and install Python from the official site first: https://www.python.org/downloads/"
    echo
    echo "Opening the download page ..."
    if command -v open >/dev/null 2>&1; then
        open "https://www.python.org/downloads/"        # macOS
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "https://www.python.org/downloads/"    # Linux
    fi
    printf "Press Enter to exit ..."
    read -r _
    exit 1
fi

"$PY" "theme_manager.py" "$@"

echo
echo "Manager stopped."
printf "Press Enter to close this window ..."
read -r _
exit 0
