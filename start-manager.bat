@echo off
rem Double-click this script to launch the Typora Themes Manager web console
rem (opens your browser automatically).
rem Note: keep this file pure ASCII with CRLF line endings so cmd parses it
rem correctly on any system code page.

title Typora Themes Manager
cd /d "%~dp0"

rem Probe the py launcher first, then python; both require Python 3.8+
rem (older versions and the Microsoft Store placeholder count as not installed)
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PYCMD=python"
)

if not defined PYCMD (
    echo.
    echo [Error] No usable Python 3.8+ environment was detected.
    echo Please download and install Python from the official site first,
    echo and remember to check "Add python.exe to PATH" during installation:
    echo.
    echo     https://www.python.org/downloads/
    echo.
    echo Opening the download page ...
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

%PYCMD% "theme_manager.py" %*

echo.
echo Manager stopped.
pause
