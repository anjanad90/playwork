@echo off
setlocal
cd /d "%~dp0"

echo.
echo  PlayWork - full reset
echo  =====================
echo.
echo  This will:
echo    - stop PlayWork if it is running
echo    - delete settings and history in %APPDATA%\PlayWork
echo    - delete any leftover playwork.json / playwork-log.csv in this folder
echo    - remove PlayWork from Windows startup
echo.
echo  Your playwork.py and .bat files are NOT touched.
echo.
set /p GO=Type YES to continue:
if /i not "%GO%"=="YES" goto cancelled

echo.
echo Stopping PlayWork...
taskkill /f /im PlayWork.exe >nul 2>&1
for /f "tokens=2 delims=," %%P in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
    wmic process where "ProcessId=%%~P" get CommandLine 2>nul | find /i "playwork" >nul && taskkill /f /pid %%~P >nul 2>&1
)

echo Removing settings and history...
if exist "%APPDATA%\PlayWork" rmdir /s /q "%APPDATA%\PlayWork"

echo Removing leftovers from this folder...
del /q "%~dp0playwork.json" 2>nul
del /q "%~dp0playwork-log.csv" 2>nul
del /q "%~dp0playwork.json.moved" 2>nul
del /q "%~dp0playwork-log.csv.moved" 2>nul

echo Removing the startup entry...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PlayWork /f >nul 2>&1

echo.
echo  Done. PlayWork is back to a fresh install.
echo  Next run will open Settings so you can set it up again.
echo.
pause
exit /b 0

:cancelled
echo.
echo  Cancelled. Nothing was changed.
echo.
pause
exit /b 1
