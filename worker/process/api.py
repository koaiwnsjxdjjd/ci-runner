# -*- coding: utf-8 -*-
"""
进程管理 API（Flask 蓝图）

- GET    /api/processes                 列出持久化进程
- POST   /api/processes/<name>/restart  重启进程
- POST   /api/processes/<name>/stop     停止进程
- POST   /api/processes/<name>/start    启动进程
- GET    /api/processes/<name>/log      查看进程日志
- POST   /api/processes/snapshot        手动触发快照
"""
from flask import Blueprint, request, jsonify

import log

logger = log.setup_logger("proc.api")

bp = Blueprint("process_api", __name__)

_manager = None


def init_process_api(manager):
    """绑定 ProcessManager 实例"""
    global _manager
    _manager = manager


def _auth_ok(data):
    """校验 token（兼容 body/header/query）"""
    token = ""
    if isinstance(data, dict):
        token = data.get("token", "")
    if not token:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "")
    import config
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


@bp.route("/api/processes", methods=["GET"])
def api_list():
    if not _auth_ok({}):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    return jsonify(ok=True, processes=_manager.list_processes())


@bp.route("/api/processes/snapshot", methods=["POST"])
def api_snapshot():
    data = request.get_json(silent=True) or {}
    if not _auth_ok(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    saved = _manager.snapshot(reason="manual")
    return jsonify(ok=True, saved=saved, msg=f"快照完成，持久化 {saved} 个进程")


@bp.route("/api/processes/<name>/restart", methods=["POST"])
def api_restart(name):
    data = request.get_json(silent=True) or {}
    if not _auth_ok(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    ok = _manager.restart(name)
    return jsonify(ok=ok, msg="已重启" if ok else "重启失败"), (200 if ok else 500)


@bp.route("/api/processes/<name>/stop", methods=["POST"])
def api_stop(name):
    data = request.get_json(silent=True) or {}
    if not _auth_ok(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    ok, msg = _manager.stop(name)
    return jsonify(ok=ok, msg=msg), (200 if ok else 500)


@bp.route("/api/processes/<name>/start", methods=["POST"])
def api_start(name):
    data = request.get_json(silent=True) or {}
    if not _auth_ok(data):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    ok = _manager.start(name)
    return jsonify(ok=ok, msg="已启动" if ok else "启动失败"), (200 if ok else 500)


@bp.route("/api/processes/<name>/log", methods=["GET"])
def api_log(name):
    if not _auth_ok({}):
        return jsonify(ok=False, error="未授权"), 401
    if not _manager:
        return jsonify(ok=False, error="进程管理器未初始化"), 500
    limit = int(request.args.get("limit", 200))
    limit = max(10, min(limit, 2000))
    lines = _manager.get_process_log(name, limit=limit)
    return jsonify(ok=True, name=name, lines=lines)