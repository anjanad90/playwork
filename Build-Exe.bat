@echo off
setlocal
cd /d "%~dp0"

REM Builds PlayWork.exe into %LOCALAPPDATA%\PlayWork so that PyInstaller's
REM temporary build files never touch OneDrive.

set OUT=%LOCALAPPDATA%\PlayWork

where python >nul 2>&1
if errorlevel 1 goto nopython

echo Installing build tools...
python -m pip install --quiet psutil pywin32 pyinstaller
if errorlevel 1 goto fail

echo.
echo Building. This takes a minute.
python -m PyInstaller --onefile --noconsole --name PlayWork ^
  --distpath "%OUT%" --workpath "%TEMP%\playwork-build" ^
  --specpath "%TEMP%\playwork-build" playwork.py
if errorlevel 1 goto fail

echo.
echo Built: %OUT%\PlayWork.exe
echo Right-click it and "Send to - Desktop (create shortcut)".
echo.
explorer "%OUT%"
pause
exit /b 0

:nopython
echo Python was not found. Install it from python.org and tick
echo "Add python.exe to PATH" during setup.
pause
exit /b 1

:fail
echo.
echo Build failed. Scroll up for the error.
pause
exit /b 1
