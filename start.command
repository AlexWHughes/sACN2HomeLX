#!/bin/bash
cd "$(dirname "$0")"
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
