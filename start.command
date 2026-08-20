#!/bin/bash
cd "$(dirname "$0")"
# Finder-launched .command windows stay open after the process exits unless we
# ask Terminal/iTerm to close this tty. launch.py does that on a clean quit.
export SACN2HOMELX_CLOSE_WINDOW=1
if command -v python3 >/dev/null 2>&1; then
  exec python3 launch.py
fi
if command -v python >/dev/null 2>&1; then
  exec python launch.py
fi
echo "Python 3.10 or newer is required."
echo "Install it from https://www.python.org/downloads/ then try again."
read -r -p "Press Enter to close this window..."
exit 1
