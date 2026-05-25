# UI-Based Financial Dashboard

A local-first financial dashboard for importing statements, syncing live balances, and reviewing cash flow, net worth, rewards, investments, liabilities, and recurring transactions from one browser-based interface.

The current project is a compact Flask application with a single HTML/CSS/JavaScript dashboard, local SQLite storage, statement parsing, and optional browser-assisted account sync workflows.

## Current Structure

```text
.
|-- app.py                    # Flask routes and API endpoints
|-- statement_parser.py       # Statement imports, SQLite schema, summaries, net worth logic
|-- sync_engine.py            # Browser-assisted sync automation and live balance capture
|-- templates/
|   `-- index.html            # Main UI, tabs, charts, settings, console view
|-- dashboard_launcher.pyw    # Windowless Windows launcher
|-- dashboard_launcher.ps1    # Legacy PowerShell launcher
|-- run_dashboard.vbs         # No-terminal launch wrapper
|-- run_dashboard.bat         # Convenience launcher
|-- requirements.txt          # Python dependencies
`-- .gitignore                # Excludes private statements, databases, logs, profiles, and venv files
```

## Features

- Main dashboard with income, expenses, bills, projected net, transactions, accounts, and statement summaries.
- Net worth tracking split into Assets, Rewards, Investments, Liabilities, and Net Worth.
- Rewards and retirement/investment balances from sync data are grouped separately from ordinary assets.
- Manual assets and liabilities can be added from Settings, including rewards and retirement entries.
- Monthly cash flow can be viewed from January forward or with the current/latest month first.
- Spending categories support bar and pie views.
- Sync tab supports selected institution sync workflows and live balance history.
- Console tab shows launcher and server output inside the app instead of relying on a terminal window.
- Windows launch flow can open the dashboard without leaving a visible PowerShell window running.

## Local Runtime Data

The app stores private and generated files locally. These are intentionally excluded from GitHub:

- `Statements/`
- `data/`
- `.venv/`
- SQLite databases
- logs
- browser profiles
- Python cache files

Do not commit bank statements, downloaded account data, browser profiles, database files, or credential material.

## Running Locally

Install dependencies:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the Flask app directly:

```powershell
.\.venv\Scripts\python.exe app.py
```

Or launch the Windows app-style dashboard:

```powershell
.\run_dashboard.bat
```

The dashboard runs at:

```text
http://127.0.0.1:5051/
```

## Notes

This project is intended for personal local use. Sync workflows rely on local browser automation and user-controlled account access. Keep credentials in environment variables or saved local browser profiles only; never commit them.
