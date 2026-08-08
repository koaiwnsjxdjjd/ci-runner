# -*- coding: utf-8 -*-
"""
Manager 管理实例（组装层 / Composition Root）

组合模块：
- accounts / instances / tasks / tunnels
- monitor（健康监控）/ guardian（保命）
- core.lock（Leader 锁，release 后端）
"""
import os
import time
import json
import functools
import threading
import subprocess
import urllib.request
import urllib.error

from flask import Flask, request, jsonify

import config
import log
from core import lock as core_lock
from core import status as core_status
from core import ghapi
from manager import tasks
from manager import accounts
from manager import instances
from manager import monitor
from manager import guardian as guardian_mod
from manager import tunnels

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
logger = log.setup_logger("manager")

leader = None
_guardian = None
_worker_heartbeats = {}


# ==================== 任务注册 ====================
@tasks.register_handler("add_account")
def _task_add_account(params, task):
    logger.info(f"[task] 处理账号添加: {params.get('name')}")
    res = accounts.auto_provision_account(
        params.get("name"), params.get("token"),
        repo=params.get("repo"), max_conc=params.get("max_concurrency"))
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "未知错误"))
    logger.info(f"[task] 账号 {params.get('name')} 配置完成")


# ==================== 认证 ====================
def _check_token():
    token = request_auth_token()
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


def request_auth_token():
    from flask import request
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = (request.args.get("token") or "").strip()
    if not token:
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
    return token


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _check_token():
            return jsonify(ok=False, error="未授权，请携带 token"), 401
        return f(*args, **kwargs)
    return wrapper


def _require_leader():
    return bool(leader and leader.is_leader)


def _api_headers():
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {config.EXEC_TOKEN}",
            "User-Agent": "Mozilla/5.0 (ghbox-manager)"}


# ==================== 基础状态 ====================
@app.route("/api/health")
def api_health():
    return jsonify(ok=True, role="manager", job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   leader=leader.is_leader if leader else False)


@app.route("/api/status")
@require_auth
def api_status():
    accts = accounts.list_accounts()
    insts = instances.list_instances()
    now = time.time()
    healthy = sum(1 for hb in _worker_heartbeats.values()
                  if now - hb.get("last_seen", 0) < 180)
    versions = {}
    for hb in _worker_heartbeats.values():
        v = hb.get("version", "unknown")[:8]
        versions[v] = versions.get(v, 0) + 1
    return jsonify(ok=True, role="manager", job=core_lock.JOB_ID,
                   elapsed=core_status.elapsed(),
                   accounts=accts, instances=insts,
                   worker_health={"online": healthy, "versions": versions})


@app.route("/api/overview")
@require_auth
def api_overview():
    """完整数据总览"""
    accts = accounts.list_accounts()
    insts = instances.list_instances()
    now = time.time()
    healthy = sum(1 for hb in _worker_heartbeats.values()
                  if now - hb.get("last_seen", 0) < 180)
    versions = {}
    for hb in _worker_heartbeats.values():
        v = hb.get("version", "unknown")[:8]
        versions[v] = versions.get(v, 0) + 1
    quota = {}
    for acc in accounts.load_accounts():
        health, detail = ghapi.estimate_account_quota(acc)
        quota[acc["name"]] = {"health": round(health, 2), "detail": detail}
    all_tasks = tasks.load_tasks()
    task_stats = {}
    for t in all_tasks:
        task_stats[t.get("status", "unknown")] = task_stats.get(t.get("status", "unknown"), 0) + 1
    return jsonify(
        ok=True, role="manager", job=core_lock.JOB_ID, elapsed=core_status.elapsed(),
        leader=leader.is_leader if leader else False,
        accounts=accts, instances=insts,
        worker_health={"online": healthy, "total": len(insts), "versions": versions},
        quota=quota,
        tasks=task_stats,
        survival={"active": _guardian.survival_active if _guardian else False,
                  "account": _guardian.current_survival_account["name"]
                  if _guardian and _guardian.current_survival_account else None},
        github_status=getattr(_guardian, "last_reason", None),
    )


@app.route("/api/logs")
@require_auth
def api_logs():
    limit = int(request_arg("limit", 300))
    limit = max(10, min(limit, 2000))
    level = request_arg("level")
    module = request_arg("module")
    keyword = request_arg("keyword")
    logs = log.get_logs(limit=limit, level=level, module=module, keyword=keyword)
    return jsonify(ok=True, logs=logs, stats=log.get_stats())


def request_arg(name, default=None):
    from flask import request
    return request.args.get(name, default)


# ==================== 账号管理 ====================
@app.route("/api/accounts", methods=["GET"])
@require_auth
def api_list_accounts():
    return jsonify(ok=True, accounts=accounts.list_accounts())


@app.route("/api/accounts", methods=["POST"])
@require_auth
def api_add_account():
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    data = request_get_json()
    name = (data.get("name") or "").strip()
    token = (data.get("token") or "").strip()
    if not name or not token:
        return jsonify(ok=False, error="name 和 token 必填"), 400
    task = tasks.add_task("add_account", {
        "name": name, "token": token,
        "repo": data.get("repo"), "max_concurrency": data.get("max_concurrency"),
    }, dedup_key=f"add_account:{name}")
    logger.info(f"[api] 账号添加任务入队: {name} ({task['id']})")
    return jsonify(ok=True, msg=f"账号 {name} 配置任务已入队（自动执行，可查询 /api/tasks）",
                   task_id=task["id"])


def request_get_json():
    from flask import request
    return request.get_json(silent=True) or {}


@app.route("/api/accounts/<name>", methods=["DELETE"])
@require_auth
def api_remove_account(name):
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = accounts.remove_account(name)
    logger.info(f"[api] 删除账号 {name}: {res}")
    return jsonify(res)


# ==================== 任务 ====================
@app.route("/api/tasks", methods=["GET"])
@require_auth
def api_list_tasks():
    return jsonify(ok=True, tasks=tasks.load_tasks())


# ==================== 实例管理 ====================
@app.route("/api/instances", methods=["POST"])
@require_auth
def api_create_instance():
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = instances.create_instance()
    logger.info(f"[api] 创建实例: {res.get('msg', res.get('error'))}")
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/instances", methods=["GET"])
@require_auth
def api_list_instances():
    return jsonify(ok=True, instances=instances.list_instances())


@app.route("/api/instances/<inst_id>", methods=["GET"])
@require_auth
def api_get_instance(inst_id):
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    return jsonify(ok=True, instance=inst)


@app.route("/api/instances/<inst_id>", methods=["DELETE"])
@require_auth
def api_close_instance(inst_id):
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    res = instances.close_instance(inst_id)
    monitor._fail_counts.pop(inst_id, None)
    logger.info(f"[api] 关闭实例 {inst_id}: {res.get('msg', res.get('error'))}")
    return jsonify(res)


@app.route("/api/instances/<inst_id>/report", methods=["POST"])
def api_instance_report(inst_id):
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    if not _require_leader():
        return jsonify(ok=False, error="当前为备份节点，写操作被拒绝"), 503
    data = request_get_json()
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    inst["status"] = "running"
    inst["url"] = data.get("url", inst.get("url"))
    inst["last_seen"] = time.time()
    all_insts = instances.load_instances()
    for i in all_insts:
        if i["id"] == inst_id:
            i.update(inst)
            break
    instances.save_instances(all_insts)
    monitor._fail_counts.pop(inst_id, None)
    return jsonify(ok=True)


@app.route("/api/instances/<inst_id>/exec", methods=["POST"])
@require_auth
def api_instance_exec(inst_id):
    data = request_get_json()
    inst = instances.get_instance(inst_id)
    if not inst:
        return jsonify(ok=False, error=f"实例 {inst_id} 不存在"), 404
    host = inst.get("hostname")
    if not host:
        return jsonify(ok=False, error="实例无域名"), 404
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    timeout = int(data.get("timeout", 30))
    timeout = max(1, min(timeout, 600))
    payload = json.dumps({"token": config.EXEC_TOKEN, "cmd": cmd,
                          "timeout": timeout}).encode()
    url = f"https://{host}/api/exec"
    try:
        req = urllib.request.Request(url, data=payload, headers=_api_headers())
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            return jsonify(ok=True, result=json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return jsonify(ok=False, error=f"实例返回 {e.code}: {body[:200]}"), 502
    except Exception as e:
        return jsonify(ok=False, error=f"无法连接实例: {e}"), 502


# ==================== 内部心跳 ====================
@app.route("/api/worker/heartbeat", methods=["POST"])
def api_worker_heartbeat():
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    data = request_get_json()
    inst_id = data.get("inst_id", "")
    job_id = data.get("job_id", "")
    if inst_id:
        version = data.get("version", "unknown")
        _worker_heartbeats[inst_id] = {"job_id": job_id, "last_seen": time.time(),
                                       "version": version}
    return jsonify(ok=True)


@app.route("/api/worker/leader")
def api_worker_leader():
    if not _check_token():
        return jsonify(ok=False, error="未授权"), 401
    inst_id = request_arg("inst_id", "")
    job_id = request_arg("job_id", "")
    hb = _worker_heartbeats.get(inst_id)
    is_leader = bool(hb and hb.get("job_id") == job_id)
    return jsonify(ok=True, is_leader=is_leader, current=hb)


# ==================== 隧道 ====================
def _start_tunnel():
    if not config.TUNNEL_TOKEN:
        return
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", config.TUNNEL_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logger.info(f"[tunnel] 管理实例隧道: https://{config.TUNNEL_HOST}")
        for line in proc.stdout:
            line = line.strip()
            if "Registered tunnel connection" in line:
                logger.info("[tunnel] 连接已注册")
    except Exception as e:
        logger.error(f"[tunnel] 启动失败: {e}")


# ==================== 数据自愈循环 ====================
def _heal_loop():
    """周期性自愈：实例清单重建 + MCP 隧道自动补创建"""
    while True:
        time.sleep(120)
        try:
            if not leader or not leader.is_leader:
                continue
            insts = instances.list_instances()
            if not insts:
                logger.warning("[heal] 实例清单为空，触发自愈重建")
                instances.ensure_instances_self_heal()
            # 自动为缺少 MCP 隧道的实例补创建
            instances.ensure_mcp_tunnels()
        except Exception as e:
            logger.error(f"[heal] 自愈循环异常: {e}")


# ==================== 续命 ====================
def _manager_pre_wake():
    done = False
    while True:
        elapsed = core_status.elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            try:
                url = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                       f"{config.MANAGER_WORKFLOW}/dispatches")
                ghapi.gh_request("POST", url, data={"ref": "main"})
                logger.info(f"[prewake] 已预触发下一个 manager（{elapsed}s）")
            except Exception as e:
                logger.error(f"[prewake] 触发失败: {e}")
            break
        time.sleep(60)


def _auto_update_loop():
    current_sha = config.CURRENT_SHA
    if not current_sha:
        return
    while True:
        time.sleep(600)
        try:
            url = f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/commits/main"
            status, d = ghapi.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != current_sha:
                logger.info(f"[update] 检测到新版本 {latest[:10]}，滚动重启")
                url2 = (f"{ghapi.API_BASE}/repos/{config.REPO}/actions/workflows/"
                        f"{config.MANAGER_WORKFLOW}/dispatches")
                status2, _ = ghapi.gh_request("POST", url2, data={"ref": "main"})
                if status2 not in (200, 204):
                    logger.error(f"[update] 触发新 manager 失败({status2})，继续运行")
                    continue
                time.sleep(60)
                os._exit(0)
        except Exception as e:
            logger.error(f"[update] 检查失败: {e}")


# ==================== 入口 ====================
def run():
    global leader, _guardian
    logger.info(f"=== Manager 启动: {core_lock.JOB_ID} ===")
    leader = core_lock.LeaderLock(backend="release")
    leader.acquire()
    if leader.is_leader:
        threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
        # 实例清单自愈 + MCP 隧道自动补创建（启动时 + 周期性，不阻塞）
        def _startup_heal():
            instances.ensure_instances_self_heal()
            instances.ensure_mcp_tunnels()
        threading.Thread(target=_startup_heal, daemon=True).start()
        threading.Thread(target=_heal_loop, daemon=True).start()
        monitor.start_monitors()
        tasks.recover_pending()
        tasks.start_worker()
        # 只有 leader 启动隧道，避免多实例抢占同一域名
        threading.Thread(target=_start_tunnel, daemon=True).start()
    else:
        def _on_promote():
            # follower 升级为 leader 后，启动全部服务
            if not config.TUNNEL_TOKEN:
                return
            threading.Thread(target=_start_tunnel, daemon=True).start()
            # 实例自愈 + MCP 隧道补创建
            def _promote_heal():
                instances.ensure_instances_self_heal()
                instances.ensure_mcp_tunnels()
            threading.Thread(target=_promote_heal, daemon=True).start()
            threading.Thread(target=_heal_loop, daemon=True).start()
            # 任务执行器 + 监控（之前缺失，导致任务不执行）
            tasks.recover_pending()
            tasks.start_worker()
            monitor.start_monitors()
            logger.info("[lock] follower 升级为 leader，启动隧道+自愈+MCP补创建+任务+监控")
        threading.Thread(target=leader.follower_loop,
                         args=(_on_promote,), daemon=True).start()
    threading.Thread(target=_manager_pre_wake, daemon=True).start()
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    _guardian = guardian_mod.create_guardian(accounts_provider=accounts.load_accounts)
    log.request_logger(app)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.PORT, app, threaded=True, use_reloader=False)