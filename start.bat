@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set SACN2HOMELX_CLOSE_WINDOW=1
setlocal EnableDelayedExpansion

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 launch.py
  set "LAUNCH_EXIT=!ERRORLEVEL!"
  if !LAUNCH_EXIT! neq 0 pause
  exit /b !LAUNCH_EXIT!
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python launch.py
  set "LAUNCH_EXIT=!ERRORLEVEL!"
  if !LAUNCH_EXIT! neq 0 pause
  exit /b !LAUNCH_EXIT!
)

where python3 >nul 2>&1
if %ERRORLEVEL%==0 (
  python3 launch.py
  set "LAUNCH_EXIT=!ERRORLEVEL!"
  if !LAUNCH_EXIT! neq 0 pause
  exit /b !LAUNCH_EXIT!
)

echo Python 3.10 or newer is required.
echo Install it from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH", then try again.
pause
exit /b 1
