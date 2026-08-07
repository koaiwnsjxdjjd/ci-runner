# -*- coding: utf-8 -*-
"""
保命 manager（Survival Mode，运行在 Codespaces）

- 精简核心：只提供管理 API（实例/账号/任务/日志）
- 攻击功能禁用（配额限制）
- 数据从 Releases 读取
"""
import time
import functools

from flask import Flask, request, jsonify

import os
import config
import log
from core import status as core_status
from manager import tasks
from manager import accounts
from manager import instances

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
logger = log.setup_logger("survival")

ATTACK_DISABLED = True


def _check_token():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = (request.args.get("token") or "").strip()
    if not token:
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_token():
            return jsonify(ok=False, error="未授权"), 401
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, role="survival", mode="保命模式", time=time.time())


@app.route("/api/status")
@require_auth
def api_status():
    accts = accounts.list_accounts()
    insts = instances.list_instances()
    return jsonify(ok=True, role="survival", mode="保命模式",
                   accounts=accts, instances=insts,
                   survival={"attack_disabled": ATTACK_DISABLED,
                             "note": "保命模式：攻击禁用，仅核心网关"})


@app.route("/api/accounts", methods=["GET"])
@require_auth
def api_list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@app.route("/api/instances", methods=["GET"])
@require_auth
def api_list_instances():
    return jsonify(ok=True, instances=instances.list_instances())


@app.route("/api/tasks", methods=["GET"])
@require_auth
def api_list_tasks():
    return jsonify(ok=True, tasks=tasks.load_tasks())


@app.route("/api/logs")
@require_auth
def api_logs():
    limit = int(request.args.get("limit", 300))
    limit = max(10, min(limit, 2000))
    level = request.args.get("level")
    logs = log.get_logs(limit=limit, level=level)
    return jsonify(ok=True, logs=logs, stats=log.get_stats())


# 保命模式：写操作禁用
@app.route("/api/instances", methods=["POST"])
@require_auth
def api_create_instance():
    return jsonify(ok=False, error="保命模式：创建实例已禁用（配额限制）"), 503


@app.route("/api/accounts", methods=["POST"])
@require_auth
def api_add_account():
    return jsonify(ok=False, error="保命模式：添加账号已禁用"), 503


@app.route("/api/attack/start", methods=["POST"])
def api_attack_start():
    return jsonify(ok=False, error="保命模式：攻击功能已禁用"), 503


@app.route("/api/attack/status", methods=["GET"])
def api_attack_status():
    return jsonify(ok=False, error="保命模式：攻击功能已禁用"), 503


def run():
    logger.info("=== 保命管理器启动（Survival Mode）===")
    log.request_logger(app)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.SURVIVAL_PORT, app, threaded=True, use_reloader=False)