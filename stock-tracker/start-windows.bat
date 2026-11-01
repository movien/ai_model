@echo off
rem Stock Tracker launcher for Windows: double-click this file.
rem First run: creates a private Python environment in .venv and installs the 3 packages.
rem Then it opens the dashboard in your browser. Keep this window open while tracking.
setlocal
cd /d "%~dp0"
title Stock Tracker

set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python 3.9 or newer is needed.
  echo  1. Download it from https://www.python.org/downloads/
  echo  2. In the installer, tick "Add python.exe to PATH".
  echo  3. Then double-click this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. This takes a minute...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment in "%~dp0.venv".
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Installing the required packages failed. Check your internet connection and try again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" tracker.py gui %*
echo.
echo Stock Tracker stopped.
pause
