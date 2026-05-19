@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PYTHON_EXE="
set "APP_URL=http://127.0.0.1:5050"

echo Financial Dashboard
echo ===================
echo.

py -3 --version >nul 2>&1
if not errorlevel 1 (
  set "PYTHON_EXE=py -3"
)

if not defined PYTHON_EXE (
  python --version >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_EXE=python"
  )
)

if not defined PYTHON_EXE (
  echo Python was not found on PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)

if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" --version >nul 2>&1
  if errorlevel 1 (
    echo Existing virtual environment is not usable on this computer.
    set "BACKUP_DIR=.venv_broken_%RANDOM%"
    ren "%VENV_DIR%" "!BACKUP_DIR!"
    if errorlevel 1 (
      echo Failed to move the broken virtual environment out of the way.
      pause
      exit /b 1
    )
    echo Moved broken virtual environment to !BACKUP_DIR!.
  )
)

if not exist "%VENV_PYTHON%" (
  echo Creating local virtual environment...
  %PYTHON_EXE% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
  )
)

echo Installing or updating dependencies...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
  echo Failed to update pip.
  pause
  exit /b 1
)

"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies from requirements.txt.
  pause
  exit /b 1
)

echo.
echo Starting Financial Dashboard at %APP_URL%
echo Leave this window open while using the app.
echo Press Ctrl+C in this window to stop the server.
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "$url='%APP_URL%'; for ($i = 0; $i -lt 60; $i++) { try { $r = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 1; if ($r.StatusCode -ge 200) { Start-Process $url; break } } catch {}; Start-Sleep -Milliseconds 500 }"
"%VENV_PYTHON%" server.py

endlocal
exit /b 0
