@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 launch.py
  if errorlevel 1 pause
  exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python launch.py
  if errorlevel 1 pause
  exit /b %ERRORLEVEL%
)

where python3 >nul 2>&1
if %ERRORLEVEL%==0 (
  python3 launch.py
  if errorlevel 1 pause
  exit /b %ERRORLEVEL%
)

echo Python 3.10 or newer is required.
echo Install it from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH", then try again.
pause
exit /b 1
