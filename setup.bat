@echo off
REM One-time setup: installs all Python dependencies.
REM Run this once (double-click), then use startup_launcher.vbs to start the app.

cd /d "%~dp0"
echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo Done. Start the app with launch_hidden.vbs
echo To auto-start at login: press Win+R, type shell:startup, and drop a
echo shortcut to launch_hidden.vbs into that folder.
pause
