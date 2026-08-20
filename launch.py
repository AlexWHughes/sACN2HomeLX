#!/usr/bin/env python3
"""Create a local venv if needed, install deps, then start sACN2HomeLX."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

MIN_VERSION = (3, 10)
PROJECT_DIR = Path(__file__).resolve().parent
REQUIREMENTS = PROJECT_DIR / 'requirements.txt'
VENV_DIR = PROJECT_DIR / '.venv'
STAMP = VENV_DIR / '.requirements.sha256'
APP = PROJECT_DIR / 'app.py'
URL = 'http://127.0.0.1:5001'
PORT = 5001


# Windows STATUS_CONTROL_C_EXIT; Unix 128+SIGINT.
_CTRL_C_EXIT_CODES = frozenset({130, -2, 0xC000013A, 3221225786})


def _should_close_launcher_window() -> bool:
    """True when start.command / start.bat asked us to close the launcher window."""
    flag = os.getenv('SACN2HOMELX_CLOSE_WINDOW', '').lower()
    if flag not in ('1', 'true', 'yes'):
        return False
    if os.getenv('SSH_CONNECTION') or os.getenv('SSH_TTY'):
        return False
    if sys.platform == 'win32':
        return True
    if sys.platform != 'darwin':
        return False
    return os.getenv('TERM_PROGRAM', '') in ('Apple_Terminal', 'iTerm.app')


def _is_clean_quit(code: int) -> bool:
    """Ctrl+C and a normal zero exit both count as a clean quit."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return False
    return value == 0 or (value & 0xFFFFFFFF) in _CTRL_C_EXIT_CODES


def _close_windows_console() -> None:
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    except (AttributeError, OSError):
        return


def _close_macos_terminal() -> None:
    try:
        tty_name = os.ttyname(0)
    except OSError:
        return
    aliases = {tty_name}
    if tty_name.startswith('/dev/'):
        aliases.add(tty_name[5:])
    term = os.getenv('TERM_PROGRAM', '')
    if term == 'Apple_Terminal':
        match = ' or '.join(
            f'tty of selected tab is "{name}"' for name in sorted(aliases)
        )
        script = (
            'tell application "Terminal"\n'
            f'  close (every window whose {match}) saving no\n'
            'end tell'
        )
    else:
        checks = ' or '.join(f'tty of s is "{name}"' for name in sorted(aliases))
        script = (
            'tell application "iTerm"\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      repeat with s in sessions of t\n'
            f'        if {checks} then\n'
            '          tell s to close\n'
            '          return\n'
            '        end if\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell'
        )
    try:
        subprocess.Popen(
            ['osascript', '-e', script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def _close_launcher_window() -> None:
    """Close the Terminal / cmd window that launched this process from start.*."""
    if not _should_close_launcher_window():
        return
    if sys.platform == 'win32':
        _close_windows_console()
        return
    _close_macos_terminal()


def _pause_and_exit(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    try:
        input('Press Enter to close this window...')
    except EOFError:
        pass
    _close_launcher_window()
    raise SystemExit(code)


def _python_version(executable: Sequence[str]) -> Optional[Tuple[int, int]]:
    try:
        output = subprocess.check_output(
            list(executable) + ['-c', 'import json, sys; print(json.dumps(list(sys.version_info[:2])))'],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        parsed = json.loads(output.strip())
        return int(parsed[0]), int(parsed[1])
    except (OSError, subprocess.CalledProcessError, ValueError, TypeError, json.JSONDecodeError, IndexError):
        return None


def _candidates() -> List[List[str]]:
    found: List[List[str]] = []
    if sys.version_info >= MIN_VERSION:
        found.append([sys.executable])
    if os.name == 'nt':
        extras = (
            ['py', '-3.12'],
            ['py', '-3.11'],
            ['py', '-3.10'],
            ['py', '-3'],
            ['python'],
            ['python3'],
        )
    else:
        extras = (['python3'], ['python'])
    for command in extras:
        if command not in found:
            found.append(command)
    return found


def find_python() -> List[str]:
    for command in _candidates():
        version = _python_version(command)
        if version is not None and version >= MIN_VERSION:
            return command
    _pause_and_exit(
        'Python 3.10 or newer is required.\n'
        'Install it from https://www.python.org/downloads/\n'
        'On Windows, tick "Add python.exe to PATH", then try again.'
    )


def venv_python() -> Path:
    if os.name == 'nt':
        return VENV_DIR / 'Scripts' / 'python.exe'
    return VENV_DIR / 'bin' / 'python'


def ensure_venv(creator: Sequence[str]) -> Path:
    python = venv_python()
    existing = _python_version([str(python)]) if python.is_file() else None
    if existing is None or existing < MIN_VERSION:
        print('Creating a local Python environment in .venv ...')
        if VENV_DIR.exists():
            shutil.rmtree(VENV_DIR)
        try:
            subprocess.check_call(list(creator) + ['-m', 'venv', str(VENV_DIR)], cwd=str(PROJECT_DIR))
        except subprocess.CalledProcessError:
            _pause_and_exit('Could not create .venv. Reinstall Python 3.10+ and try again.')
        python = venv_python()
        if not python.is_file():
            _pause_and_exit('Created .venv but could not find its Python executable.')
    return python


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_requirements(python: Path) -> None:
    if not REQUIREMENTS.is_file():
        _pause_and_exit(f'Missing {REQUIREMENTS.name} in {PROJECT_DIR}')
    digest = requirements_hash()
    if STAMP.is_file() and STAMP.read_text(encoding='utf-8').strip() == digest:
        return
    print('Installing packages (first run can take a minute) ...')
    try:
        subprocess.check_call(
            [str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(REQUIREMENTS)],
            cwd=str(PROJECT_DIR),
        )
    except subprocess.CalledProcessError:
        _pause_and_exit('Could not install Python packages. Check your internet connection and try again.')
    STAMP.write_text(digest + '\n', encoding='utf-8')


def open_browser_when_ready() -> None:
    if os.getenv('SACN2HOMELX_OPEN_BROWSER', '1').lower() in ('0', 'false', 'no'):
        return
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', PORT), timeout=0.3):
                webbrowser.open(URL)
                return
        except OSError:
            time.sleep(0.25)


def main() -> None:
    os.chdir(PROJECT_DIR)
    print('sACN2HomeLX')
    print(f'Folder: {PROJECT_DIR}')
    creator = find_python()
    version = _python_version(creator)
    if version is not None:
        print(f'Using Python {version[0]}.{version[1]}')
    python = ensure_venv(creator)
    ensure_requirements(python)
    print(f'Starting the app. Leave this window open.')
    print(f'Your browser should open {URL}')
    print('Press Ctrl+C or close this window to quit.')
    print()
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    try:
        code = subprocess.call([str(python), str(APP)], cwd=str(PROJECT_DIR))
    except KeyboardInterrupt:
        print('\nStopped.')
        code = 0
    if _is_clean_quit(code):
        if code != 0:
            print('\nStopped.')
        code = 0
        _close_launcher_window()
    raise SystemExit(code)


if __name__ == '__main__':
    main()
