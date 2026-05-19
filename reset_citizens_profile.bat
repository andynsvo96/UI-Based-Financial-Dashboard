@echo off
setlocal

cd /d "%~dp0"

set "PROFILE_DIR=chrome_profiles\citizens"

echo Citizens Profile Reset
echo ======================
echo.
echo Close all Chrome windows that are using the Citizens profile before continuing.
echo This will back up the existing profile folder instead of deleting it.
echo.
pause

if not exist "%PROFILE_DIR%" (
  echo No Citizens profile exists yet.
  mkdir "%PROFILE_DIR%"
  echo Created a fresh Citizens profile folder.
  pause
  exit /b 0
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%i"
set "BACKUP_DIR=chrome_profiles\citizens_backup_%STAMP%"

move "%PROFILE_DIR%" "%BACKUP_DIR%"
if errorlevel 1 (
  echo Failed to move the Citizens profile. Make sure all Citizens Chrome windows are closed.
  pause
  exit /b 1
)

mkdir "%PROFILE_DIR%"
echo Backed up old profile to %BACKUP_DIR%
echo Created a fresh Citizens profile folder.
pause

endlocal
