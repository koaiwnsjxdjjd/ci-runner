# -*- coding: utf-8 -*-
"""
MCP 服务管理（worker 侧）

- 确保 MCP 服务代码和依赖可用（优先从 Releases 下载预编译依赖，失败回退 npm install）
- 启动/停止 MCP 服务（node index.js，端口 3457）
- 启动/停止 MCP 专用隧道（mcp-{inst_id}.{domain}）
- 进程持久化兼容（启动前清理旧进程，避免重复）
"""
import os
import io
import time
import shutil
import tarfile
import threading
import subprocess

import config
import log
from core import storage
from worker.tunnel import TunnelManager

logger = log.setup_logger("mcp")

MCP_PORT = int(os.environ.get("MCP_PORT", "3457"))
MCP_SERVER_DIR = os.path.join(config.FILES_DIR, "mcp-server")
MCP_FILES_DIR = os.path.join(config.FILES_DIR, "mcp-files")
MCP_DEPS_ASSET = "mcp-deps.tar.gz.enc"


class McpManager:
    """MCP 服务管理器"""

    def __init__(self, inst_cfg=None):
        self.inst_cfg = inst_cfg
        self.proc = None
        self.tunnel_mgr = None
        self.ready = False

    # ==================== 依赖管理 ====================
    def _copy_server_code(self):
        """从项目目录复制 MCP 服务代码到持久化目录"""
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mcp_src = os.path.join(project_dir, "worker", "mcp-server")
        if not os.path.isdir(mcp_src):
            logger.error("[mcp] 项目中未找到 mcp-server 目录")
            return False
        os.makedirs(MCP_SERVER_DIR, exist_ok=True)
        for fname in ("index.js", "package.json"):
            src = os.path.join(mcp_src, fname)
            dst = os.path.join(MCP_SERVER_DIR, fname)
            if os.path.exists(src):
                shutil.copy2(src, dst)
        logger.info("[mcp] 服务代码已复制")
        return True

    def ensure_server(self):
        """确保 MCP 服务代码和依赖可用。优先 Releases 下载，失败回退 npm install。"""
        node_modules = os.path.join(MCP_SERVER_DIR, "node_modules")
        index_js = os.path.join(MCP_SERVER_DIR, "index.js")

        if os.path.isdir(node_modules) and os.path.exists(index_js):
            logger.info("[mcp] 依赖已存在，跳过安装")
            return True

        self._copy_server_code()

        download_ok = False
        try:
            logger.info("[mcp] 尝试从 Releases 下载预编译依赖...")
            blob = storage.download_asset_chunked(
                MCP_DEPS_ASSET, token=config.GH_TOKEN, repo=config.MAIN_REPO)
            if blob:
                logger.info(f"[mcp] 依赖包下载完成（{len(blob)} 字节），解压中...")
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                    tar.extractall(path=MCP_SERVER_DIR, filter="tar")
                logger.info(f"[mcp] 预编译依赖已恢复（{len(blob)} 字节）")
                if os.path.isdir(node_modules):
                    download_ok = True
                else:
                    logger.warning("[mcp] 解压后 node_modules 不存在，继续 npm install")
            else:
                logger.warning("[mcp] Releases 中无预编译依赖")
        except Exception as e:
            logger.warning(f"[mcp] 下载预编译依赖失败: {e}")
        if download_ok:
            return True

        logger.info("[mcp] 回退到 npm install...")
        try:
            result = subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund", "--loglevel=warn"],
                cwd=MCP_SERVER_DIR,
                capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("[mcp] npm install 完成")
                try:
                    subprocess.run(
                        ["npx", "playwright", "install", "chromium"],
                        cwd=MCP_SERVER_DIR,
                        capture_output=True, text=True, timeout=180)
                    logger.info("[mcp] playwright chromium 安装完成")
                except Exception as e:
                    logger.warning(f"[mcp] playwright install 失败（不影响核心功能）: {e}")
                return os.path.isdir(node_modules)
            else:
                logger.error(f"[mcp] npm install 失败: {result.stderr[:300]}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("[mcp] npm install 超时（5分钟）")
            return False
        except Exception as e:
            logger.error(f"[mcp] npm install 异常: {e}")
            return False

    # ==================== 隧道管理 ====================
    def _get_mcp_tunnel_token(self):
        if self.inst_cfg and self.inst_cfg.raw:
            return self.inst_cfg.raw.get("mcp_tunnel_token", "")
        return ""

    def _get_mcp_hostname(self):
        if self.inst_cfg and self.inst_cfg.raw:
            host = self.inst_cfg.raw.get("mcp_hostname", "")
            if host:
                return host
        if self.inst_cfg and self.inst_cfg.tunnel_host:
            return f"mcp-{self.inst_cfg.tunnel_host}"
        return f"mcp-{config.TUNNEL_HOST}"

    def _start_tunnel(self):
        """启动 MCP 专用隧道"""
        token = self._get_mcp_tunnel_token()
        host = self._get_mcp_hostname()
        if not token:
            logger.warning("[mcp] 无 MCP 隧道 token，跳过隧道启动")
            return False

        class _McpCfg:
            def __init__(self, t, h):
                self.tunnel_token = t
                self.tunnel_host = h

        self.tunnel_mgr = TunnelManager(_McpCfg(token, host))
        self.tunnel_mgr.start_async()
        logger.info(f"[mcp] MCP 隧道已异步启动: https://{host}")
        return True

    # ==================== 旧进程清理 ====================
    def _kill_all_mcp_processes(self):
        """
        杀掉所有 node index.js 进程。
        进程持久化可能恢复了旧 MCP 进程，如果不清理会导致：
        1. 旧进程绑定端口失败（EADDRINUSE）但僵尸进程残留
        2. 端口检查时序问题（旧进程还没绑定端口时新进程已启动）
        """
        try:
            result = subprocess.run(
                ["pgrep", "-f", "node index.js"],
                capture_output=True, text=True, timeout=5)
            killed = []
            for pid_str in result.stdout.split():
                if pid_str:
                    pid = int(pid_str)
                    try:
                        os.kill(pid, 9)
                        killed.append(pid)
                    except Exception:
                        pass
            if killed:
                logger.info(f"[mcp] 启动前清理旧 MCP 进程: {killed}")
                time.sleep(0.5)  # 等待旧进程完全退出
            else:
                logger.info("[mcp] 无旧 MCP 进程需要清理")
        except Exception:
            pass

    # ==================== 服务启动/停止 ====================
    def start(self):
        """启动 MCP 服务（依赖 + 隧道 + node 进程）
        启动前清理所有旧 MCP 进程，确保只有一个实例运行。
        """
        if not self.ensure_server():
            logger.error("[mcp] 依赖准备失败，MCP 服务不启动")
            return False

        # 启动隧道
        self._start_tunnel()

        # 清理所有旧 MCP 进程（进程持久化恢复的）
        self._kill_all_mcp_processes()

        # 启动 MCP 服务
        env = os.environ.copy()
        env["MCP_PORT"] = str(MCP_PORT)
        env["MCP_HOST"] = "0.0.0.0"
        env["MCP_FILES_DIR"] = MCP_FILES_DIR
        mcp_host = self._get_mcp_hostname()
        env["MCP_BASE_URL"] = f"https://{mcp_host}" if mcp_host else f"http://localhost:{MCP_PORT}"
        env["EXEC_TOKEN"] = config.EXEC_TOKEN
        env["HOME"] = os.path.expanduser("~")

        os.makedirs(MCP_FILES_DIR, exist_ok=True)

        try:
            self.proc = subprocess.Popen(
                ["node", "index.js"],
                cwd=MCP_SERVER_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True)
            logger.info(f"[mcp] MCP 服务已启动 (pid={self.proc.pid}, port={MCP_PORT})")
            threading.Thread(target=self._read_output, daemon=True).start()
            self.ready = True
            return True
        except Exception as e:
            logger.error(f"[mcp] MCP 服务启动失败: {e}")
            return False

    def _read_output(self):
        """后台读取 MCP 服务 stdout 日志"""
        if not self.proc:
            return
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    logger.info(f"[mcp:node] {line[:300]}")
        except Exception:
            pass

    def stop(self):
        """停止 MCP 服务和隧道"""
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
                time.sleep(1)
                os.killpg(os.getpgid(self.proc.pid), 9)
            except Exception:
                pass
        self.proc = None
        if self.tunnel_mgr:
            self.tunnel_mgr.stop()
        self.ready = False
        logger.info("[mcp] MCP 服务已停止")

    def status(self):
        """返回 MCP 服务状态"""
        alive = bool(self.proc and self.proc.poll() is None)
        return {
            "ready": self.ready,
            "running": alive,
            "pid": self.proc.pid if alive else None,
            "port": MCP_PORT,
            "hostname": self._get_mcp_hostname(),
            "url": f"https://{self._get_mcp_hostname()}" if self._get_mcp_tunnel_token() else None,
            "deps_ready": os.path.isdir(os.path.join(MCP_SERVER_DIR, "node_modules")),
        }


def ensure_mcp_server(inst_cfg):
    """便捷函数：确保 MCP 服务可用并启动"""
    mgr = McpManager(inst_cfg)
    mgr.start()
    return mgr
