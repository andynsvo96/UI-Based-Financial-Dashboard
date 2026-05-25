from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, render_template, request

from sync_engine import sync_manager
from statement_parser import (
    DATA_DIR,
    DB_PATH,
    add_scan_folder,
    dashboard_summary,
    add_manual_asset,
    delete_manual_asset,
    delete_scan_folder,
    get_settings,
    import_statements,
    init_db,
    transactions_for_month,
    update_settings,
)


CONSOLE_LOG_PATH = DATA_DIR / "dashboard-console.log"
CONSOLE_MAX_CHARS = 120_000


def console_log_payload() -> dict[str, object]:
    if not CONSOLE_LOG_PATH.exists():
        return {"log": "Console log is waiting for the dashboard launcher.", "size": 0, "path": str(CONSOLE_LOG_PATH)}

    size = CONSOLE_LOG_PATH.stat().st_size
    with CONSOLE_LOG_PATH.open("rb") as handle:
        if size > CONSOLE_MAX_CHARS:
            handle.seek(-CONSOLE_MAX_CHARS, os.SEEK_END)
            prefix = b"... earlier console output trimmed ...\n"
        else:
            prefix = b""
        raw = prefix + handle.read()
    return {
        "log": raw.decode("utf-8", errors="replace"),
        "size": size,
        "path": str(CONSOLE_LOG_PATH),
    }


def stop_server() -> None:
    os._exit(0)


def create_app() -> Flask:
    first_run = not DB_PATH.exists()
    init_db()
    if first_run:
        import_statements()
    app = Flask(__name__)

    @app.errorhandler(ValueError)
    def value_error(error: ValueError):
        return jsonify({"message": str(error)}), 400

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/summary")
    def summary():
        return jsonify(dashboard_summary(request.args.get("institution", "all"), request.args.get("period", "ytd")))

    @app.get("/api/transactions")
    def transactions():
        return jsonify(
            transactions_for_month(
                request.args.get("institution", "all"),
                request.args.get("period", "ytd"),
                request.args.get("month", ""),
            )
        )

    @app.post("/api/import")
    def import_local_statements():
        stats = import_statements()
        return jsonify(stats)

    @app.get("/api/settings")
    def settings():
        return jsonify(get_settings())

    @app.post("/api/settings")
    def save_settings():
        payload = request.get_json(silent=True) or {}
        return jsonify(update_settings(payload))

    @app.post("/api/settings/folders")
    def create_scan_folder():
        payload = request.get_json(silent=True) or {}
        return jsonify(add_scan_folder(payload))

    @app.delete("/api/settings/folders/<int:folder_id>")
    def remove_scan_folder(folder_id: int):
        return jsonify(delete_scan_folder(folder_id))

    @app.post("/api/settings/assets")
    def create_manual_asset():
        payload = request.get_json(silent=True) or {}
        return jsonify(add_manual_asset(payload))

    @app.delete("/api/settings/assets/<int:asset_id>")
    def remove_manual_asset(asset_id: int):
        return jsonify(delete_manual_asset(asset_id))

    @app.get("/api/sync/status")
    def sync_status():
        return jsonify(sync_manager.status())

    @app.get("/api/console")
    def console_log():
        return jsonify(console_log_payload())

    @app.post("/api/shutdown")
    def shutdown():
        threading.Timer(0.25, stop_server).start()
        return jsonify({"status": "stopping", "message": "Dashboard server is shutting down."})

    @app.post("/api/sync/citizens/setup")
    def citizens_sync_setup():
        return jsonify(sync_manager.setup_citizens())

    @app.post("/api/sync/citizens/run")
    def citizens_sync_run():
        started, payload = sync_manager.start_citizens()
        return jsonify(payload), 202 if started else 409

    @app.post("/api/sync/amex/setup")
    def amex_sync_setup():
        return jsonify(sync_manager.setup_amex())

    @app.post("/api/sync/amex/run")
    def amex_sync_run():
        started, payload = sync_manager.start_amex()
        return jsonify(payload), 202 if started else 409

    @app.post("/api/sync/chase/setup")
    def chase_sync_setup():
        return jsonify(sync_manager.setup_chase())

    @app.post("/api/sync/chase/run")
    def chase_sync_run():
        started, payload = sync_manager.start_chase()
        return jsonify(payload), 202 if started else 409

    @app.post("/api/sync/citi/setup")
    def citi_sync_setup():
        return jsonify(sync_manager.setup_citi())

    @app.post("/api/sync/citi/run")
    def citi_sync_run():
        started, payload = sync_manager.start_citi()
        return jsonify(payload), 202 if started else 409

    @app.post("/api/sync/vanguard/setup")
    def vanguard_sync_setup():
        return jsonify(sync_manager.setup_vanguard())

    @app.post("/api/sync/vanguard/run")
    def vanguard_sync_run():
        started, payload = sync_manager.start_vanguard()
        return jsonify(payload), 202 if started else 409

    @app.post("/api/sync/vanguard/code")
    def vanguard_sync_code():
        payload = request.get_json(silent=True) or {}
        return jsonify(sync_manager.submit_vanguard_code(str(payload.get("code", ""))))

    @app.post("/api/sync/citi/code")
    def citi_sync_code():
        payload = request.get_json(silent=True) or {}
        return jsonify(sync_manager.submit_citi_code(str(payload.get("code", ""))))

    @app.post("/api/sync/setup")
    def sync_setup_selected():
        payload = request.get_json(silent=True) or {}
        return jsonify(sync_manager.setup_institutions(payload.get("institutions")))

    @app.post("/api/sync/run")
    def sync_run_selected():
        payload = request.get_json(silent=True) or {}
        started, response_payload = sync_manager.start_many(payload.get("institutions"))
        return jsonify(response_payload), 202 if started else 409

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=False)
