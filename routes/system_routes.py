from __future__ import annotations

from flask import Blueprint, current_app, jsonify


system_bp = Blueprint("system", __name__)


def manager():
    return current_app.config["WORKER_MANAGER"]


@system_bp.get("/api/status")
def status():
    return jsonify(manager().status())


@system_bp.post("/api/continue")
def continue_after_login_or_mfa():
    started, payload = manager().continue_after_user_action()
    return jsonify(payload), 202 if started else 409


@system_bp.post("/api/cancel")
def cancel():
    return jsonify(manager().cancel())
