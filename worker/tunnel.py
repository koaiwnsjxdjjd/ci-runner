# -*- coding: utf-8 -*-
"""
Cloudflare 隧道管理（worker 侧）

- 启动固定域名隧道（cloudflared tunnel run --token）
- 连接注册检测
- 多隧道支持（自身 + 用户进程里的其他隧道由进程持久化接管）
"""
import os
import time
import threading
import subprocess

import config
import log
from core import utils

logger = log.setup_logger("tunnel")


class TunnelManager:
    def __init__(self, inst_cfg=None):
        self.inst_cfg = inst_cfg
        self.proc = None
        self.registered = False
        self.url = ""

    def _get_token(self):
        if self.inst_cfg and self.inst_cfg.tunnel_token:
            return self.inst_cfg.tunnel_token
        return config.TUNNEL_TOKEN

    def _get_host(self):
        if self.inst_cfg and self.inst_cfg.tunnel_host:
            return self.inst_cfg.tunnel_host
        return config.TUNNEL_HOST

    def start(self):
        """启动隧道（阻塞读取日志直到注册或进程退出）"""
        token = self._get_token()
        host = self._get_host()
        if not token:
            logger.warning("[tunnel] 无 tunnel token，跳过")
            return False
        self.url = f"https://{host}"
        try:
            self.proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", token],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            logger.info(f"[tunnel] 固定隧道启动: {self.url} (pid={self.proc.pid})")
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if "Registered tunnel connection" in line:
                    self.registered = True
                    logger.info("[tunnel] 连接已注册")
                elif "ERR" in line or "error" in line.lower():
                    logger.warning(f"[tunnel] {line[:200]}")
            logger.warning("[tunnel] 隧道进程退出")
            return self.registered
        except Exception as e:
            logger.error(f"[tunnel] 启动失败: {e}")
            return False

    def start_async(self):
        """异步启动隧道线程"""
        t = threading.Thread(target=self.start, daemon=True)
        t.start()
        return t

    def stop(self):
        """停止隧道"""
        if self.proc:
            try:
                self.proc.terminate()
                time.sleep(1)
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self.registered = False
        logger.info("[tunnel] 隧道已停止")