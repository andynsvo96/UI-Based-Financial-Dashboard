from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, send_file

import config
from automation_audit import log_event
from automation_runtime import read_json, terminate_existing_chrome_processes, write_json
from routes.finance_routes import finance_bp
from routes.system_routes import system_bp


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_directories() -> None:
    for directory in config.REQUIRED_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(config.DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    ensure_directories()
    with get_db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                display_name TEXT NOT NULL,
                account_type TEXT,
                masked_account TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                supports_csv INTEGER NOT NULL DEFAULT 0,
                supports_documents INTEGER NOT NULL DEFAULT 1,
                chrome_profile_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                balance_type TEXT,
                amount REAL,
                currency TEXT NOT NULL DEFAULT 'USD',
                captured_at TEXT NOT NULL,
                source TEXT,
                raw_text TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                site TEXT NOT NULL,
                document_type TEXT,
                statement_name TEXT,
                received_date TEXT,
                account_label TEXT,
                local_path TEXT,
                file_hash TEXT,
                downloaded_at TEXT NOT NULL,
                source_url_optional TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                site TEXT NOT NULL,
                transaction_date TEXT,
                posted_date TEXT,
                description TEXT NOT NULL,
                amount REAL,
                currency TEXT NOT NULL DEFAULT 'USD',
                debit_credit TEXT,
                status TEXT,
                category TEXT,
                source TEXT,
                source_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                raw_text TEXT,
                UNIQUE(account_id, source_hash),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                worker TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT,
                result_json TEXT
            );
            """
        )
        now = utc_now()
        existing = connection.execute(
            "SELECT id FROM accounts WHERE site = ? AND account_type = ?",
            (config.CITIZENS_SITE_KEY, config.CITIZENS_ACCOUNT_TYPE),
        ).fetchone()
        if not existing:
            connection.execute(
                """
                INSERT INTO accounts (
                    site, display_name, account_type, masked_account, enabled,
                    supports_csv, supports_documents, chrome_profile_path,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?, ?)
                """,
                (
                    config.CITIZENS_SITE_KEY,
                    config.CITIZENS_DISPLAY_NAME,
                    config.CITIZENS_ACCOUNT_TYPE,
                    None,
                    str(config.CITIZENS_PROFILE_PATH),
                    now,
                    now,
                ),
            )


def init_state_files() -> None:
    if not config.DASHBOARD_STATE_PATH.exists():
        write_json(
            config.DASHBOARD_STATE_PATH,
            {
                "status": config.STATUS_IDLE,
                "active_worker": None,
                "message": "Idle.",
                "pid": None,
                "updated_at": utc_now(),
            },
        )
    if not config.LAST_RESULT_PATH.exists():
        write_json(
            config.LAST_RESULT_PATH,
            {
                "success": True,
                "status": config.STATUS_IDLE,
                "site": None,
                "worker": None,
                "message": "No sync has run yet.",
                "started_at": None,
                "finished_at": None,
                "data": {"balances": [], "documents": [], "downloads": [], "transactions": [], "transaction_exports": []},
                "errors": [],
                "screenshots": [],
            },
        )
    if not config.AUDIT_LOG_PATH.exists():
        config.AUDIT_LOG_PATH.write_text("", encoding="utf-8")


class WorkerManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.active_worker: str | None = None
        self.active_site: str | None = None
        self.active_run_id: int | None = None
        self.cancel_requested = False

    def status(self) -> dict[str, Any]:
        state = read_json(config.DASHBOARD_STATE_PATH, {})
        last_result = read_json(config.LAST_RESULT_PATH, {})
        return {
            "state": state,
            "last_result": last_result,
            "lock": {"locked": self.lock.locked()},
            "active_worker": self.active_worker,
            "pid": self.process.pid if self.process else None,
        }

    def _set_state(
        self,
        status: str,
        message: str,
        pid: int | None = None,
        active_worker: str | None | object = Ellipsis,
    ) -> None:
        write_json(
            config.DASHBOARD_STATE_PATH,
            {
                "status": status,
                "active_worker": self.active_worker if active_worker is Ellipsis else active_worker,
                "message": message,
                "pid": pid,
                "updated_at": utc_now(),
            },
        )

    def _insert_run(self, site: str, worker: str, status: str, message: str) -> int:
        with get_db_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs (site, worker, status, started_at, message, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (site, worker, status, utc_now(), message, "{}"),
            )
            return int(cursor.lastrowid)

    def _finish_run(self, run_id: int, result: dict[str, Any]) -> None:
        with get_db_connection() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET status = ?, finished_at = ?, message = ?, result_json = ?
                WHERE id = ?
                """,
                (
                    result.get("status", config.STATUS_FAILED),
                    result.get("finished_at") or utc_now(),
                    result.get("message", ""),
                    json.dumps(result, sort_keys=True),
                    run_id,
                ),
            )

    def start_worker(self, site: str, worker: str, args: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
        if not self.lock.acquire(blocking=False):
            return False, {"status": config.STATUS_RUNNING, "message": "Another worker is already running."}

        args = args or []
        script_path = config.BASE_DIR / "workers" / f"{worker}.py"
        if not script_path.exists():
            self.lock.release()
            return False, {"status": config.STATUS_FAILED, "message": f"Worker not found: {worker}"}

        self.active_worker = worker
        self.active_site = site
        self.cancel_requested = False
        run_id = self._insert_run(site, worker, config.STATUS_RUNNING, "Worker started.")
        self.active_run_id = run_id

        command = [sys.executable, str(script_path), *args]
        self.process = subprocess.Popen(
            command,
            cwd=str(config.BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._set_state(config.STATUS_RUNNING, f"{worker} is running.", self.process.pid)
        log_event("worker started", worker=worker, site=site, pid=self.process.pid)

        monitor = threading.Thread(target=self._monitor_worker, args=(self.process, run_id), daemon=True)
        monitor.start()
        return True, {"status": config.STATUS_RUNNING, "message": "Worker started.", "pid": self.process.pid}

    def _monitor_worker(self, process: subprocess.Popen[str], run_id: int) -> None:
        try:
            stdout, stderr = process.communicate(timeout=config.WORKER_TIMEOUT_SECONDS)
            result = read_json(config.LAST_RESULT_PATH, {})

            if self.cancel_requested:
                result = self._cancelled_result()
            elif process.returncode != 0 and result.get("status") not in {
                config.STATUS_WAITING_FOR_LOGIN,
                config.STATUS_WAITING_FOR_MFA,
                config.STATUS_WAITING_FOR_USER_ACTION,
            }:
                result = self._failed_result(stderr or stdout or f"Worker exited with code {process.returncode}.")
        except subprocess.TimeoutExpired:
            process.terminate()
            result = self._failed_result("Worker timed out and was terminated.")

        try:
            self._finish_run(run_id, result)
            self._set_state(result.get("status", config.STATUS_FAILED), result.get("message", ""), None, None)

            status = result.get("status")
            if status in {config.STATUS_WAITING_FOR_LOGIN, config.STATUS_WAITING_FOR_MFA, config.STATUS_WAITING_FOR_USER_ACTION}:
                log_event("user action required", message=result.get("message", ""), worker=self.active_worker)
            elif status == config.STATUS_SUCCESS:
                log_event("worker completed", message=result.get("message", ""), worker=self.active_worker)
            elif status == config.STATUS_CANCELLED:
                log_event("worker cancelled", worker=self.active_worker)
            else:
                log_event("worker failed", message=result.get("message", ""), worker=self.active_worker)
        finally:
            self.process = None
            self.active_worker = None
            self.active_site = None
            self.active_run_id = None
            self.cancel_requested = False
            self.lock.release()

    def _failed_result(self, message: str) -> dict[str, Any]:
        now = utc_now()
        return {
            "success": False,
            "status": config.STATUS_FAILED,
            "site": self.active_site,
            "worker": self.active_worker,
            "message": message[:1000],
            "started_at": None,
            "finished_at": now,
            "data": {"balances": [], "documents": [], "downloads": [], "transactions": [], "transaction_exports": []},
            "errors": [message[:2000]],
            "screenshots": [],
        }

    def _cancelled_result(self) -> dict[str, Any]:
        now = utc_now()
        result = {
            "success": False,
            "status": config.STATUS_CANCELLED,
            "site": self.active_site,
            "worker": self.active_worker,
            "message": "Current run was cancelled.",
            "started_at": None,
            "finished_at": now,
            "data": {"balances": [], "documents": [], "downloads": [], "transactions": [], "transaction_exports": []},
            "errors": [],
            "screenshots": [],
        }
        write_json(config.LAST_RESULT_PATH, result)
        return result

    def start_citizens_setup(self) -> tuple[bool, dict[str, Any]]:
        return self.start_worker(config.CITIZENS_SITE_KEY, "citizens_checking_sync", ["--setup"])

    def _statement_range_arg(self, statement_range: str | None) -> list[str]:
        if statement_range == "all":
            return ["--statement-range", "all"]
        return ["--statement-range", "last60"]

    def start_citizens_sync(self, statement_range: str | None = "last60") -> tuple[bool, dict[str, Any]]:
        return self.start_worker(
            config.CITIZENS_SITE_KEY,
            "citizens_checking_sync",
            self._statement_range_arg(statement_range),
        )

    def start_citizens_transactions(self) -> tuple[bool, dict[str, Any]]:
        return self.start_worker(config.CITIZENS_SITE_KEY, "citizens_checking_sync", ["--transactions-only"])

    def start_or_continue_citizens_sync(self, statement_range: str | None = "last60") -> tuple[bool, dict[str, Any]]:
        last_result = read_json(config.LAST_RESULT_PATH, {})
        if last_result.get("status") == config.STATUS_WAITING_FOR_MFA:
            return self.continue_after_user_action(statement_range)
        if last_result.get("status") in {
            config.STATUS_WAITING_FOR_LOGIN,
            config.STATUS_WAITING_FOR_USER_ACTION,
        }:
            return self.continue_after_user_action(statement_range)
        return self.start_citizens_sync(statement_range)

    def submit_citizens_mfa_code(self, code: str) -> tuple[bool, dict[str, Any]]:
        digits = re.sub(r"\D+", "", code or "")
        if len(digits) != 6:
            return False, {"status": config.STATUS_WAITING_FOR_MFA, "message": "Enter the 6-digit Citizens code."}

        last_result = read_json(config.LAST_RESULT_PATH, {})
        if last_result.get("status") not in {
            config.STATUS_WAITING_FOR_MFA,
            config.STATUS_WAITING_FOR_USER_ACTION,
            config.STATUS_WAITING_FOR_LOGIN,
        }:
            return False, {"status": config.STATUS_IDLE, "message": "Citizens is not waiting for a verification code."}

        write_json(
            config.CITIZENS_MFA_CODE_PATH,
            {
                "code": digits,
                "created_at": utc_now(),
            },
        )
        return self.continue_after_user_action()

    def continue_after_user_action(self, statement_range: str | None = None) -> tuple[bool, dict[str, Any]]:
        last_result = read_json(config.LAST_RESULT_PATH, {})
        if last_result.get("status") not in {
            config.STATUS_WAITING_FOR_LOGIN,
            config.STATUS_WAITING_FOR_MFA,
            config.STATUS_WAITING_FOR_USER_ACTION,
        }:
            return False, {"status": config.STATUS_IDLE, "message": "No login or verification step is waiting."}
        resume_args = []
        resume = last_result.get("data", {}).get("resume", {}) if isinstance(last_result.get("data"), dict) else {}
        if resume.get("transactions_only"):
            resume_args.append("--transactions-only")
        resume_args.extend(self._statement_range_arg(statement_range or resume.get("statement_range")))
        return self.start_worker(config.CITIZENS_SITE_KEY, "citizens_checking_sync", resume_args)

    def cancel(self) -> dict[str, Any]:
        if not self.process or self.process.poll() is not None:
            return {"status": config.STATUS_IDLE, "message": "No active worker to cancel."}
        self.cancel_requested = True
        self.process.terminate()
        self._set_state(config.STATUS_CANCELLED, "Cancelling current run.", self.process.pid)
        return {"status": config.STATUS_CANCELLED, "message": "Cancel requested."}

    def reset_citizens_profile(self) -> tuple[bool, dict[str, Any]]:
        if self.lock.locked():
            return False, {"status": config.STATUS_RUNNING, "message": "A worker is running. Cancel or wait before resetting the profile."}

        terminate_existing_chrome_processes(
            profile_name=config.CITIZENS_PROFILE_NAME,
            debugging_port=config.CITIZENS_DEBUGGING_PORT,
        )
        profile_path = config.CITIZENS_PROFILE_PATH
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = config.CHROME_PROFILES_DIR / f"{config.CITIZENS_PROFILE_NAME}_backup_{stamp}"

        if profile_path.exists():
            shutil.move(str(profile_path), str(backup_path))
            profile_path.mkdir(parents=True, exist_ok=True)
            return True, {
                "status": config.STATUS_IDLE,
                "message": f"Citizens Chrome profile reset. Backup created at {backup_path}. Run Citizens Setup again.",
            }

        profile_path.mkdir(parents=True, exist_ok=True)
        return True, {
            "status": config.STATUS_IDLE,
            "message": "Citizens Chrome profile reset. Run Citizens Setup again.",
        }

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with get_db_connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]


def create_app() -> Flask:
    init_db()
    init_state_files()
    app = Flask(__name__)
    app.config["WORKER_MANAGER"] = WorkerManager()

    @app.get("/")
    def index():
        return send_file(config.BASE_DIR / "ui_panel.html")

    app.register_blueprint(finance_bp)
    app.register_blueprint(system_bp)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=config.APP_HOST, port=config.APP_PORT, debug=config.APP_DEBUG)
