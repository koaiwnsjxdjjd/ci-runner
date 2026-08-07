# -*- coding: utf-8 -*-
"""
Leader 锁（生产级，统一双后端）

- backend="release"：基于 GitHub Releases leader.json 心跳（manager 用）
- backend="manager"：基于 manager HTTP 心跳（worker 用，不占 GitHub 配额）

提供统一的 acquire / heartbeat_loop / follower_loop 接口，
支持降级自管理（manager 不可达时 worker 自动升级为 leader）。
"""
import os
import time
import json
import uuid
import threading
import urllib.request
import urllib.error

import config
import log
from core import storage

logger = log.setup_logger("lock")

JOB_ID = uuid.uuid4().hex[:8]


class LeaderLock:
    """统一 Leader 锁"""

    def __init__(self, backend="release", instance_id=None, token=None):
        self.backend = backend
        self.instance_id = instance_id or config.INSTANCE_ID
        self.token = token
        self.job_id = JOB_ID
        self.is_leader = False
        self.degraded = False
        self.fail_count = 0
        self._on_promote = None
        self.mgr_host = os.environ.get("MANAGER_HOST", "ghvps2.kekeke.cc.cd")

    # ==================== 读/写心跳 ====================
    def _read_heartbeat(self):
        """读取当前心跳。返回 dict 或 None"""
        try:
            if self.backend == "release":
                blob = storage.download_asset(config.ASSET_LEADER, token=self.token)
                if not blob:
                    return None
                return json.loads(blob.decode())
            # manager 后端
            url = f"https://{self.mgr_host}/api/worker/leader?inst_id={self.instance_id}&job_id={self.job_id}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {config.EXEC_TOKEN}",
                "User-Agent": "Mozilla/5.0 (ghbox)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode())
                if d.get("ok") and d.get("is_leader"):
                    return {"job_id": self.job_id, "heartbeat": time.time()}
                return None
        except Exception:
            return None

    def _write_heartbeat(self):
        """写入心跳。返回 True/False"""
        try:
            if self.backend == "release":
                data = json.dumps({"job_id": JOB_ID, "heartbeat": time.time()}).encode()
                storage.upload_asset(config.ASSET_LEADER, data, token=self.token)
                return True
            # manager 后端：心跳上报（带版本号）
            url = f"https://{self.mgr_host}/api/worker/heartbeat"
            payload = json.dumps({
                "inst_id": self.instance_id, "job_id": self.job_id,
                "version": config.CURRENT_SHA or "unknown"}).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.EXEC_TOKEN}",
                "User-Agent": "Mozilla/5.0 (ghbox)"})
            urllib.request.urlopen(req, timeout=10)
            self.fail_count = 0
            return True
        except Exception:
            self.fail_count += 1
            if self.fail_count >= 5:
                self.degraded = True
            return False

    # ==================== 获取锁 ====================
    def acquire(self):
        """尝试成为 leader。返回 is_leader"""
        try:
            if self.backend == "release":
                leader = self._read_heartbeat()
                now = time.time()
                if leader and leader.get("job_id") != JOB_ID and \
                        (now - leader.get("heartbeat", 0)) < config.HEARTBEAT_TIMEOUT:
                    self.is_leader = False
                    return False
                self.is_leader = True
                self._write_heartbeat()
                return True
            # manager 后端：心跳 + 查询 leader 判定
            ok = self._write_heartbeat()
            if ok:
                if self.degraded:
                    self.degraded = False
                    logger.info("[lock] manager 恢复，退出降级模式")
                d = self._read_heartbeat()
                self.is_leader = bool(d and d.get("job_id") == JOB_ID)
                logger.info(f"[lock] {'leader' if self.is_leader else 'follower'}（manager 判定）")
            else:
                # 降级：manager 不可达，自管理为 leader
                self.is_leader = True
                self.degraded = True
                logger.warning("[lock] 降级模式: manager 不可达，自管理（leader）")
            return self.is_leader
        except Exception as e:
            logger.error(f"[lock] acquire 异常: {e}")
            # 异常时保守降级为 leader（保证服务可用）
            self.is_leader = True
            self.degraded = True
            return True

    # ==================== 心跳循环 ====================
    def heartbeat_loop(self):
        """leader 心跳循环（退出条件：不再是 leader）"""
        while True:
            if not self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                ok = self._write_heartbeat()
                if ok and self.degraded:
                    self.degraded = False
                    logger.info("[lock] manager 恢复，退出降级")
            except Exception:
                pass

    def follower_loop(self, on_promote=None):
        """follower 检测升级循环"""
        self._on_promote = on_promote
        while True:
            if self.is_leader:
                return
            time.sleep(config.HEARTBEAT_INTERVAL)
            try:
                if self.backend == "release":
                    leader = self._read_heartbeat()
                    now = time.time()
                    if not leader or (now - leader.get("heartbeat", 0)) >= config.HEARTBEAT_TIMEOUT:
                        if self.acquire():
                            self._fire_promote()
                            return
                else:
                    ok = self._write_heartbeat()
                    if ok:
                        d = self._read_heartbeat()
                        if d and d.get("job_id") == JOB_ID:
                            self.is_leader = True
                            logger.info(f"[lock] follower 升级为 leader: {self.job_id}")
                            self._fire_promote()
                            return
                    elif self.fail_count >= 5:
                        # 降级升级
                        self.is_leader = True
                        self.degraded = True
                        logger.warning("[lock] 降级升级为 leader（manager 不可达）")
                        self._fire_promote()
                        return
            except Exception:
                pass

    def _fire_promote(self):
        if self._on_promote:
            try:
                self._on_promote()
            except Exception as e:
                logger.error(f"[lock] 升级回调异常: {e}")


def create_lock(backend="release", instance_id=None, token=None):
    """工厂方法"""
    return LeaderLock(backend=backend, instance_id=instance_id, token=token)