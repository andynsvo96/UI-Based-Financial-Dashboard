$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

$appUrl = "http://127.0.0.1:5051/"
$venvDir = Join-Path $PSScriptRoot ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$dataDir = Join-Path $PSScriptRoot "data"
$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
$requirementsMarkerPath = Join-Path $dataDir "dashboard-requirements.sha256"
$logPath = Join-Path $dataDir "dashboard-console.log"
$pipOutPath = Join-Path $dataDir "dashboard-pip.out.log"
$pipErrPath = Join-Path $dataDir "dashboard-pip.err.log"
$serverOutPath = Join-Path $dataDir "dashboard-server.out.log"
$serverErrPath = Join-Path $dataDir "dashboard-server.err.log"
$browserProfile = Join-Path $dataDir "dashboard_window_profile"
$serverProcess = $null
$browser = $null

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Set-Content -Path $logPath -Value "[$(Get-Date -Format s)] Financial Dashboard launcher started."

function Write-ConsoleLog {
  param([string]$Message)
  Add-Content -Path $logPath -Value "[$(Get-Date -Format s)] $Message"
}

function Append-TextFile {
  param([string]$Path)
  if (Test-Path -LiteralPath $Path) {
    Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue | Add-Content -Path $logPath
  }
}

function Test-DashboardReady {
  try {
    $response = Invoke-WebRequest -Uri $appUrl -UseBasicParsing -TimeoutSec 2
    return $response.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Test-DashboardCurrent {
  try {
    $response = Invoke-WebRequest -Uri "$($appUrl)api/console" -UseBasicParsing -TimeoutSec 2
    return $response.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Stop-DashboardPortOwner {
  Get-NetTCPConnection -LocalPort 5051 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}

function Stop-DashboardLaunchers {
  $scriptPath = (Join-Path $PSScriptRoot "dashboard_launcher.ps1").ToLowerInvariant()
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.ProcessId -ne $PID -and
      $_.CommandLine -and
      $_.CommandLine.ToLowerInvariant().Contains($scriptPath)
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Stop-DashboardBrowsers {
  Get-DashboardBrowserProcesses |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Find-Browser {
  $command = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }

  $candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
  )

  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }

  return $null
}

function Get-DashboardBrowserProcesses {
  $profileText = $browserProfile.ToLowerInvariant()
  Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($profileText) }
}

try {
  Write-ConsoleLog "Closing any previous dashboard instance."
  $hadPreviousInstance =
    (@(Get-DashboardBrowserProcesses).Count -gt 0) -or
    (@(Get-NetTCPConnection -LocalPort 5051 -State Listen -ErrorAction SilentlyContinue).Count -gt 0)
  Stop-DashboardBrowsers
  Stop-DashboardLaunchers
  Stop-DashboardPortOwner
  if ($hadPreviousInstance) {
    Start-Sleep -Milliseconds 750
  }

  if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-ConsoleLog "Creating virtual environment."
    $venvProcess = Start-Process -FilePath "py.exe" -ArgumentList @("-3", "-m", "venv", $venvDir) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru
    if ($venvProcess.ExitCode -ne 0) {
      throw "Failed to create virtual environment. Exit code $($venvProcess.ExitCode)."
    }
  }

  $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash
  $installedHash = if (Test-Path -LiteralPath $requirementsMarkerPath) {
    (Get-Content -LiteralPath $requirementsMarkerPath -ErrorAction SilentlyContinue | Select-Object -First 1)
  } else {
    ""
  }

  if ($requirementsHash -eq $installedHash) {
    Write-ConsoleLog "Requirements already current."
  } else {
    Write-ConsoleLog "Installing requirements."
    Remove-Item -LiteralPath $pipOutPath, $pipErrPath -Force -ErrorAction SilentlyContinue
    $pipProcess = Start-Process `
      -FilePath $pythonExe `
      -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-r", "requirements.txt") `
      -WorkingDirectory $PSScriptRoot `
      -WindowStyle Hidden `
      -RedirectStandardOutput $pipOutPath `
      -RedirectStandardError $pipErrPath `
      -Wait `
      -PassThru
    Append-TextFile $pipOutPath
    Append-TextFile $pipErrPath
    if ($pipProcess.ExitCode -ne 0) {
      throw "Failed to install requirements. Exit code $($pipProcess.ExitCode)."
    }
    Set-Content -Path $requirementsMarkerPath -Value $requirementsHash
  }

  Write-ConsoleLog "Starting dashboard server."
  Remove-Item -LiteralPath $serverOutPath, $serverErrPath -Force -ErrorAction SilentlyContinue
  $serverProcess = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @("app.py") `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $serverOutPath `
    -RedirectStandardError $serverErrPath `
    -PassThru

  $deadline = (Get-Date).AddSeconds(30)
  while (-not (Test-DashboardReady)) {
    if ((Get-Date) -gt $deadline) {
      Append-TextFile $serverOutPath
      Append-TextFile $serverErrPath
      throw "Server did not respond at $appUrl."
    }
    Start-Sleep -Milliseconds 500
  }

  if ($env:FD_LAUNCHER_SMOKE -eq "1") {
    Write-ConsoleLog "Smoke test mode reached a ready dashboard server."
    $browser = "smoke-test"
    return
  }

  $browser = Find-Browser
  if ($browser) {
    New-Item -ItemType Directory -Force -Path $browserProfile | Out-Null
    Write-ConsoleLog "Opening dashboard app window."
    $browserArgs = @("--app=$appUrl", "--user-data-dir=$browserProfile", "--no-first-run")
    Start-Process -FilePath $browser -ArgumentList $browserArgs | Out-Null

    Start-Sleep -Seconds 3
    while (Get-DashboardBrowserProcesses) {
      Start-Sleep -Seconds 2
    }

    Write-ConsoleLog "Dashboard app window closed."
  } else {
    Write-ConsoleLog "Edge or Chrome was not found. Opening the default browser without automatic close detection."
    Start-Process $appUrl | Out-Null
    return
  }
} catch {
  Write-ConsoleLog "Launcher error: $($_.Exception.GetType().FullName): $($_.Exception.Message)"
  Write-ConsoleLog "Script position: $($_.InvocationInfo.PositionMessage)"
  Start-Process -FilePath "notepad.exe" -ArgumentList "`"$logPath`"" | Out-Null
  return
} finally {
  if ($browser) {
    try {
      Invoke-WebRequest -Uri "$($appUrl)api/shutdown" -Method POST -UseBasicParsing -TimeoutSec 2 | Out-Null
      Write-ConsoleLog "Shutdown request sent to dashboard server."
    } catch {
      Write-ConsoleLog "Shutdown request failed: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds 1
    if ($serverProcess -and -not $serverProcess.HasExited) {
      Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
      Write-ConsoleLog "Stopped launcher-owned server process."
    }
  }
}
