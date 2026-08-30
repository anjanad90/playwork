@echo off
setlocal
cd /d "%~dp0"

REM Builds release\PlayWork.exe with icon and version info.
REM All input paths are absolute: PyInstaller resolves --icon and
REM --version-file relative to the spec directory, which lives in %TEMP%.

set HERE=%~dp0
set OUT=%HERE%release
set WORK=%TEMP%\playwork-build

where python >nul 2>&1
if errorlevel 1 goto nopython

if not exist "%HERE%playwork.py" goto noscript

echo Installing build tools...
python -m pip install --quiet --upgrade psutil pywin32 pyinstaller
if errorlevel 1 goto fail

set EXTRA=
if exist "%HERE%playwork.ico" (
    set EXTRA=--icon "%HERE%playwork.ico"
) else (
    echo NOTE: playwork.ico not found - building without an icon.
)

if exist "%HERE%version.txt" (
    set EXTRA=%EXTRA% --version-file "%HERE%version.txt"
) else (
    echo NOTE: version.txt not found - building without version info.
)

echo.
echo Building. This takes a minute or two.
echo.
python -m PyInstaller ^
  --onefile --noconsole --clean ^
  --name PlayWork ^
  %EXTRA% ^
  --distpath "%OUT%" ^
  --workpath "%WORK%" ^
  --specpath "%WORK%" ^
  "%HERE%playwork.py"
if errorlevel 1 goto fail

if not exist "%OUT%\PlayWork.exe" goto fail

echo.
echo  Built:  %OUT%\PlayWork.exe
echo.
echo  Move it somewhere permanent (not OneDrive), then make a shortcut.
echo  Settings and history live in %%APPDATA%%\PlayWork and survive rebuilds.
echo.
explorer "%OUT%"
pause
exit /b 0

:noscript
echo.
echo  playwork.py is not in this folder:
echo    %HERE%
echo  Put Build-Release.bat next to playwork.py and run it again.
echo.
pause
exit /b 1

:nopython
echo.
echo  Python was not found. Install it from python.org and tick
echo  "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:fail
echo.
echo  Build failed. The error is above this line.
echo.
echo  Most common causes:
echo    - antivirus blocking PyInstaller - add an exclusion and retry
echo    - a previous PlayWork.exe still running - close it first
echo.
pause
exit /b 1
