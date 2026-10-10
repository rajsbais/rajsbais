@echo off
rem One command to run Keystone on Windows: double-click, or run  start.bat  (options: --memory, --check, --port N). See start.py.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 start.py %* & goto :done)
where python >nul 2>nul && (python start.py %* & goto :done)
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ and run this again.
:done
if errorlevel 1 pause
