@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto nopython

python -c "import psutil, win32gui" >nul 2>&1
if errorlevel 1 goto install

:launch
start "" pythonw "%~dp0playwork.py"
exit /b 0

:install
echo First run - installing psutil and pywin32...
echo.
python -m pip install psutil pywin32
if errorlevel 1 goto pipfail
echo.
echo Done.
goto launch

:nopython
echo.
echo Python was not found.
echo.
echo Install it from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH" on the first screen.
echo.
pause
exit /b 1

:pipfail
echo.
echo Could not install the dependencies.
echo Try running this file as administrator, or run manually:
echo     python -m pip install --user psutil pywin32
echo.
pause
exit /b 1
