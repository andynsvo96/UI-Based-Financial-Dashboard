# UI-Based Financial Dashboard

A local Flask application for reviewing Citizens checking account activity in a browser-based dashboard. The app opens at `http://127.0.0.1:5050`, stores captured data in a local SQLite database, and provides controls for setup, syncing balances, downloading statement metadata, capturing recent transactions, MFA continuation, cancellation, and profile reset.

## Features

- Browser dashboard served by Flask with a single HTML UI.
- Citizens checking sync worker powered by Selenium.
- Local SQLite storage for accounts, balance snapshots, documents, transactions, and sync runs.
- API endpoints for dashboard status, Citizens setup, sync, MFA code submission, transaction-only capture, cancellation, and profile reset.
- One-click launcher scripts for Windows and macOS/Linux-style shells.
- Local audit logging designed to avoid storing credentials, tokens, cookies, or session values.

## Repository Contents

- `server.py` - Flask app, SQLite initialization, and worker process management.
- `ui_panel.html` - Browser dashboard UI.
- `routes/` - Flask API routes for finance and system operations.
- `workers/citizens_checking_sync.py` - Selenium-based Citizens checking automation worker.
- `automation_runtime.py` and `automation_audit.py` - Shared runtime and audit helpers.
- `config.py` - Local app paths, Citizens URLs, status constants, and timeout settings.
- `run_financial_dashboard.bat` - Windows launcher.
- `run_financial_dashboard.command` - macOS/Linux-style launcher.
- `requirements.txt` - Python dependencies.

## Requirements

- Python 3.10 or newer.
- Google Chrome installed locally.
- A Citizens online banking login that can be used interactively in the opened browser profile.

## Quick Start

### Windows

Double-click:

```text
run_financial_dashboard.bat
```

The script creates `.venv`, installs dependencies, starts the Flask server, and opens the dashboard.

### macOS or Linux-style Shell

Run:

```bash
chmod +x run_financial_dashboard.command
./run_financial_dashboard.command
```

Or start manually:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

On Windows, the equivalent manual commands are:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

Then open:

```text
http://127.0.0.1:5050
```

## Using the Dashboard

1. Start the dashboard with one of the launcher scripts.
2. Use Citizens Setup to open a dedicated Chrome profile and complete the initial login flow.
3. Use Sync Citizens to capture account data and statement information.
4. Use Transactions to run a transaction-only capture.
5. If Citizens asks for MFA or another manual step, complete it in Chrome or submit the 6-digit code through the dashboard, then continue the run.
6. Use Reset Profile if the saved Citizens Chrome profile needs to be rebuilt.

## Local Data and Privacy

This repository intentionally does not commit runtime financial data or browser state. The following paths are ignored by git:

- `data/`
- `chrome_profiles/`
- `screenshots/`
- `backups/`
- `.venv/`
- `automation_record_log.txt`
- `dashboard_state.json`
- `last_result.json`

The app creates those files and folders locally as needed. Statement PDFs, CSV exports, SQLite databases, browser sessions, and sync logs should stay on the local machine unless you intentionally back them up elsewhere.

## Development Notes

Run a syntax check with:

```bash
python -m compileall automation_audit.py automation_runtime.py config.py server.py routes workers
```

The app is configured for local use only by default:

```python
APP_HOST = "127.0.0.1"
APP_PORT = 5050
APP_DEBUG = False
```

Change those settings carefully if you plan to expose the dashboard outside the local machine.
