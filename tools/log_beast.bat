@echo off
setlocal
cd /d "%~dp0"

rem Records everything from the Pi's Beast feed (Mode S, ADS-B, Mode A/C) to a .csv.gz
rem using beast_to_csv.py, which must be in the same folder as this file.

rem Your receiver's address (IP or hostname):
set "HOST=192.168.1.50"
set "PORT=30005"

if not exist "beast_to_csv.py" (
    echo beast_to_csv.py was not found next to this file.
    pause
    exit /b 1
)

set "RECEIVER="
set /p "RECEIVER=Receiver name (e.g. my-site, Enter to skip): "

:askmin
set "MINUTES="
set /p "MINUTES=Minutes to record (Enter = until Ctrl+C): "
if not defined MINUTES goto asknote
echo %MINUTES%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo Please enter a whole number of minutes, or just press Enter.
    goto askmin
)

:asknote
set "NOTE="
set /p "NOTE=Note - what you see/hear (avoid & and quote marks): "

where py >nul 2>&1
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

set "ARGS=--port %PORT%"
if defined MINUTES set "ARGS=%ARGS% --minutes %MINUTES%"
if defined RECEIVER set "ARGS=%ARGS% --receiver "%RECEIVER%""

echo.
if defined MINUTES (
    echo Recording %HOST%:%PORT% for %MINUTES% minutes. Ctrl+C stops early.
) else (
    echo Recording %HOST%:%PORT%. Press Ctrl+C to stop.
)
echo If Windows asks "Terminate batch job (Y/N)?" afterwards, the file is already saved.
echo.
%PY% beast_to_csv.py %HOST% %ARGS% --note "%NOTE%"

echo.
pause
