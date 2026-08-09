# -*- coding: utf-8 -*-
"""
ProcessManager 门面（聚合 scanner/config/backup/restore）

- 提供快照、恢复、状态查询、监控循环、最终快照等高层接口
- 维护运行时进程状态（pid/status）
- 供 worker/app.py 与 process/api.py 调用
"""
import os
import time
import threading

import config
import log
from core import utils
from core import crypto, turso
from worker.process import scanner
from worker.process import config as pconfig
from worker.process import backup as pbackup
from worker.process import restore as prestore

logger = log.setup_logger("process")


class ProcessManager:
    """进程持久化管理器（门面）"""

    def __init__(self, inst_cfg=None):
        self.inst_cfg = inst_cfg
        self.known = {}          # name -> {pid, status, started_at, config}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ensure_dirs()

    def _ensure_dirs(self):
        os.makedirs(config.FILES_DIR, exist_ok=True)
        os.makedirs(config.PROC_DIR, exist_ok=True)
        os.makedirs(config.LOGS_DIR, exist_ok=True)

    # ==================== 快照 ====================
    def snapshot(self, reason="periodic"):
        """扫描并持久化所有用户进程。返回保存数"""
        saved, meta = pbackup.snapshot(reason=reason)
        # 更新运行时状态
        with self._lock:
            for name, m in meta.items():
                self.known[name] = {
                    "name": name, "pid": m.get("pid"), "status": "running",
                    "config": pconfig.load_proc_config(name),
                    "started_at": m.get("saved_at"),
                }
        # 独立上传进程快照（更快，不依赖整体文件备份）
        try:
            self._upload_snapshot()
        except Exception as e:
            logger.error(f"[process] 快照上传失败: {e}")
        return saved

    def _upload_snapshot(self):
        """打包并上传进程快照。优先 Turso，回退 Releases。"""
        if not self.inst_cfg:
            return
        from core import storage
        data = pbackup.pack_processes_tar()
        if not data:
            return
        inst_id = self.inst_cfg.instance_id
        enc_data = crypto.encrypt_bytes(data)
        # 优先 Turso
        turso_ok = False
        try:
            if turso.is_available():
                ok, ver = turso.put_blob(turso.inst_processes_key(inst_id), enc_data)
                if ok:
                    turso_ok = True
                    logger.info(f"[process] 进程快照已存入 Turso ({len(data)} 字节)")
        except Exception as e:
            logger.warning(f"[process] Turso快照上传失败: {e}")
        # 回退 Releases
        if not turso_ok:
            asset = f"inst-{inst_id}.processes.tar.gz.enc"
            size, parts = storage.upload_asset_chunked(asset, data)
            logger.info(f"[process] 进程快照已存入 Releases ({size} 字节, {parts} 分片)")

    def _download_snapshot(self):
        """下载并解包进程快照。优先 Turso，回退 Releases。"""
        if not self.inst_cfg:
            return
        from core import storage
        inst_id = self.inst_cfg.instance_id
        # 优先 Turso
        try:
            if turso.is_available():
                blob = turso.get_blob(turso.inst_processes_key(inst_id))
                if blob:
                    try:
                        raw = crypto.decrypt_bytes(blob)
                        pbackup.unpack_processes_tar(raw)
                        logger.info(f"[process] 进程快照已从 Turso 恢复（{len(raw)} 字节）")
                        return
                    except Exception as e:
                        logger.warning(f"[process] Turso快照解密失败: {e}")
        except Exception as e:
            logger.warning(f"[process] Turso快照下载失败: {e}")
        # 回退 Releases
        asset = f"inst-{inst_id}.processes.tar.gz.enc"
        data = storage.download_asset_chunked(asset)
        if data:
            pbackup.unpack_processes_tar(data)
            logger.info(f"[process] 进程快照已从 Releases 恢复（{len(data)} 字节）")

    def final_snapshot(self):
        """销毁前最终快照"""
        logger.info("[process] 执行最终快照（容器即将销毁）")
        try:
            self.snapshot(reason="final")
        except Exception as e:
            logger.error(f"[process] 最终快照失败: {e}")

    # ==================== 恢复 ====================
    def restore_all(self):
        """恢复并启动所有持久化进程。返回 (restored, failed)"""
        # 优先从独立进程快照 asset 恢复（比整体 files.tar.gz 更精准）
        try:
            self._download_snapshot()
        except Exception as e:
            logger.warning(f"[process] 独立快照下载失败: {e}")
        restored, failed = prestore.restore_all()
        # 更新运行时状态
        with self._lock:
            for name in pconfig.load_manifest():
                cfg = pconfig.load_proc_config(name)
                if cfg is None:
                    logger.warning(f"[process] {name} 无配置，跳过状态更新")
                    continue
                self.known[name] = {
                    "name": name, "status": "running", "config": cfg,
                    "started_at": time.time(),
                }
                # 找到实际 pid（通过命令匹配）
                pid = self._find_pid_by_cmd(cfg)
                if pid:
                    self.known[name]["pid"] = pid
        return restored, failed

    def restore_one(self, name):
        """恢复并启动单个进程"""
        return prestore.restore_one(name)

    def _find_pid_by_cmd(self, cfg):
        """通过命令匹配找到运行中的 pid"""
        cmd = cfg.get("command") or ""
        if not cmd:
            return None
        for proc in scanner.scan_user_processes():
            if proc.cmdline_str() == cmd:
                return proc.pid
        return None

    # ==================== 启停 ====================
    def start(self, name):
        ok, pid = prestore.start_process(name)
        if ok:
            with self._lock:
                self.known[name] = {"name": name, "pid": pid,
                                    "status": "running",
                                    "config": pconfig.load_proc_config(name),
                                    "started_at": time.time()}
        return ok

    def stop(self, name):
        with self._lock:
            entry = self.known.get(name) or {}
            pid = entry.get("pid")
        ok, msg = prestore.stop_process(name, pid=pid)
        if ok:
            with self._lock:
                self.known.pop(name, None)
        return ok, msg

    def restart(self, name):
        self.stop(name)
        time.sleep(1)
        return self.start(name)

    # ==================== 状态 ====================
    def list_processes(self):
        """列出所有持久化进程及运行状态"""
        procs = pconfig.load_manifest()
        result = []
        for name, meta in procs.items():
            cfg = pconfig.load_proc_config(name) or {}
            pid = None
            with self._lock:
                if name in self.known:
                    pid = self.known[name].get("pid")
            running = bool(pid and utils.is_alive(pid))
            result.append({
                "name": name,
                "cmdline": meta.get("cmdline", cfg.get("command", "")),
                "cwd": meta.get("cwd", cfg.get("cwd", "")),
                "size_mb": meta.get("size_mb", 0),
                "files_backed": meta.get("files_backed", True),
                "pid": pid,
                "running": running,
                "auto_restart": cfg.get("auto_restart", True),
                "saved_at": meta.get("saved_at", cfg.get("saved_at")),
            })
        return result

    def get_process_log(self, name, limit=200):
        """读取进程日志"""
        import log as _log
        return _log.read_process_log(name, limit=limit)

    # ==================== 监控 ====================
    def monitor_loop(self):
        """周期快照 + 崩溃自恢复"""
        while not self._stop.is_set():
            try:
                self.snapshot(reason="periodic")
                self._recover_crashed()
            except Exception as e:
                logger.error(f"[process] 监控异常: {e}")
            self._stop.wait(config.PROC_SCAN_INTERVAL)

    def _recover_crashed(self):
        """检测已持久化但崩溃的进程并自动重启"""
        with self._lock:
            names = list(self.known.keys())
        for name in names:
            with self._lock:
                entry = self.known.get(name)
                if not entry:
                    continue
                pid = entry.get("pid")
                auto = (entry.get("config") or {}).get("auto_restart", True)
            if not auto:
                continue
            if pid and not utils.is_alive(pid):
                logger.warning(f"[restore] 进程 {name} 已崩溃(pid={pid})，自动重启")
                delay = (entry.get("config") or {}).get("restart_delay", 3)
                time.sleep(delay)
                self.start(name)

    def start_monitor(self):
        """启动监控线程"""
        t = threading.Thread(target=self.monitor_loop, daemon=True)
        t.start()
        logger.info("[process] 进程监控已启动")

    def shutdown(self):
        """停止监控（用于优雅退出）"""
        self._stop.set()