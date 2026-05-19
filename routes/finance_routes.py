from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request


finance_bp = Blueprint("finance", __name__)


def manager():
    return current_app.config["WORKER_MANAGER"]


@finance_bp.post("/api/citizens/setup")
def citizens_setup():
    started, payload = manager().start_citizens_setup()
    return jsonify(payload), 202 if started else 409


@finance_bp.post("/api/citizens/sync")
def citizens_sync():
    payload = request.get_json(silent=True) or {}
    started, payload = manager().start_citizens_sync(payload.get("statement_range"))
    return jsonify(payload), 202 if started else 409


@finance_bp.post("/api/citizens/run")
def citizens_run():
    payload = request.get_json(silent=True) or {}
    started, payload = manager().start_or_continue_citizens_sync(payload.get("statement_range"))
    return jsonify(payload), 202 if started else 409


@finance_bp.post("/api/citizens/mfa-code")
def citizens_mfa_code():
    payload = request.get_json(silent=True) or {}
    started, response = manager().submit_citizens_mfa_code(str(payload.get("code", "")))
    return jsonify(response), 202 if started else 409


@finance_bp.post("/api/citizens/transactions")
def citizens_transactions():
    started, payload = manager().start_citizens_transactions()
    return jsonify(payload), 202 if started else 409


@finance_bp.post("/api/citizens/reset-profile")
def citizens_reset_profile():
    completed, payload = manager().reset_citizens_profile()
    return jsonify(payload), 200 if completed else 409


@finance_bp.get("/api/accounts")
def accounts():
    rows = manager().rows("SELECT * FROM accounts ORDER BY site, display_name")
    return jsonify({"accounts": rows})


@finance_bp.get("/api/balances")
def balances():
    rows = manager().rows(
        """
        SELECT b.*, a.display_name, a.site
        FROM balance_snapshots b
        LEFT JOIN accounts a ON a.id = b.account_id
        ORDER BY b.captured_at DESC
        LIMIT 25
        """
    )
    return jsonify({"balances": rows})


@finance_bp.get("/api/documents")
def documents():
    rows = manager().rows(
        """
        SELECT d.*, a.display_name
        FROM documents d
        LEFT JOIN accounts a ON a.id = d.account_id
        ORDER BY d.downloaded_at DESC
        LIMIT 100
        """
    )
    return jsonify({"documents": rows})


@finance_bp.get("/api/transactions")
def transactions():
    rows = manager().rows(
        """
        SELECT t.*, a.display_name
        FROM transactions t
        LEFT JOIN accounts a ON a.id = t.account_id
        ORDER BY COALESCE(t.transaction_date, t.posted_date, t.captured_at) DESC, t.id DESC
        LIMIT 100
        """
    )
    return jsonify({"transactions": rows})


@finance_bp.get("/api/runs")
def runs():
    rows = manager().rows(
        """
        SELECT id, site, worker, status, started_at, finished_at, message
        FROM sync_runs
        ORDER BY started_at DESC
        LIMIT 25
        """
    )
    return jsonify({"runs": rows})
