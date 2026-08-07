# -*- coding: utf-8 -*-
"""
ghbox 全局配置（生产级）

设计要点：
- 所有配置从环境变量读取，提供安全默认值
- 配置分为「静态配置」和「实例配置」两部分
  · 静态配置：进程启动时一次性读取（config 模块级常量）
  · 实例配置：worker 启动时从 Releases 读取，通过 InstanceConfig 对象持有，
    避免全局变量被动态改写（解决旧版 config.ASSET_* 全局可变问题）
- 所有路径集中管理，杜绝硬编码
"""
import os

# ==================== 运行角色 ====================
ROLE = os.environ.get("INSTANCE_ROLE", "worker")          # manager / worker / survival
INSTANCE_ID = os.environ.get("INSTANCE_ID", "worker-1")

# ==================== GitHub ====================
REPO = os.environ.get("REPO", "qqztceghrgji/demo-vps")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
MAIN_REPO = os.environ.get("MAIN_REPO", "qqztceghrgji/demo-vps")
CURRENT_SHA = os.environ.get("CURRENT_SHA", "")

# ==================== 安全 ====================
DEMO_KEY = os.environ.get("DEMO_KEY", "")                 # AES-256-GCM 密钥(hex 64位)
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")             # 远程控制/终端令牌

# ==================== 隧道 ====================
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "ghvps2.kekeke.cc.cd")
CF_EMAIL = os.environ.get("CF_EMAIL", "")
CF_API_KEY = os.environ.get("CF_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "")
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "kekeke.cc.cd")
TUNNEL_PREFIX = os.environ.get("TUNNEL_PREFIX", "ghbox")

# ==================== Releases 存储（资产命名） ====================
BACKUP_TAG = "backup"
ASSET_DB = "demo.db.enc"
ASSET_FILES = "files.tar.gz.enc"
ASSET_LEADER = "leader.json"
ASSET_ACCOUNTS = "accounts.json.enc"
ASSET_INSTANCES = "instances.json.enc"
ASSET_TASKS = "tasks.json.enc"
ASSET_SYSCONFIG = "sysconfig.tar.gz.enc"   # 系统配置备份

# ==================== 主 job 锁 ====================
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))

# ==================== 数据/文件 ====================
DB_FILE = "demo.db"
FILES_DIR = os.path.expanduser(os.environ.get("FILES_DIR", "~/files"))
PROC_DIR = os.path.join(FILES_DIR, "processes")   # 进程持久化目录
SYSCONFIG_DIR = os.path.join(FILES_DIR, "sysconfig")  # 系统配置持久化目录
LOGS_DIR = os.path.join(FILES_DIR, "logs")        # 进程日志目录

# ==================== 服务 ====================
PORT = int(os.environ.get("PORT", "8080"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "300"))

# ==================== 无缝衔接 ====================
PRE_WAKE_SECONDS = int(os.environ.get("PRE_WAKE_SECONDS", "21300"))

# ==================== WSS 终端会话 ====================
SESSION_TTL = int(os.environ.get("SESSION_TTL", "300"))

# ==================== 保命模式 ====================
GITHUB_STATUS_URL = os.environ.get(
    "GITHUB_STATUS_URL", "https://www.githubstatus.com/api/v2/status.json")
GITHUB_COMPONENTS_URL = os.environ.get(
    "GITHUB_COMPONENTS_URL", "https://www.githubstatus.com/api/v2/components.json")
ENABLE_SURVIVAL = os.environ.get("ENABLE_SURVIVAL", "1") == "1"
SURVIVAL_CHECK_INTERVAL = int(os.environ.get("SURVIVAL_CHECK_INTERVAL", "300"))
SURVIVAL_DEVCONTAINER = os.environ.get("SURVIVAL_DEVCONTAINER", ".devcontainer/devcontainer.json")
SURVIVAL_PORT = int(os.environ.get("SURVIVAL_PORT", "8090"))
QUOTA_SWITCH_THRESHOLD = float(os.environ.get("QUOTA_SWITCH_THRESHOLD", "0.15"))
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "20"))
SURVIVAL_TUNNEL_PREFIX = os.environ.get("SURVIVAL_TUNNEL_PREFIX", "surv")

# ==================== 自动更新 ====================
MANAGER_WORKFLOW = os.environ.get("MANAGER_WORKFLOW", "manager.yml")
WORKER_WORKFLOW = os.environ.get("WORKER_WORKFLOW", "worker.yml")

# ==================== 进程持久化（新功能） ====================
PROC_SCAN_INTERVAL = int(os.environ.get("PROC_SCAN_INTERVAL", "300"))   # 周期快照(秒)
PROC_MAX_RETRY = int(os.environ.get("PROC_MAX_RETRY", "3"))             # 恢复失败重试次数
PROC_RETRY_DELAY = [5, 15, 45]                                          # 重试退避
PROC_BACKUP_EXCLUDE = os.environ.get(
    "PROC_BACKUP_EXCLUDE",
    "node_modules,.git,__pycache__,.venv,venv,dist,build,.cache,logs,tmp").split(",")
PROC_MAX_BACKUP_MB = int(os.environ.get("PROC_MAX_BACKUP_MB", "512"))   # 单进程备份上限(MB)

# ==================== 磁盘监控 ====================
DISK_WARN_PERCENT = int(os.environ.get("DISK_WARN_PERCENT", "85"))
DISK_CLEAN_TRIGGER_PERCENT = int(os.environ.get("DISK_CLEAN_TRIGGER_PERCENT", "90"))
DISK_CHECK_INTERVAL = int(os.environ.get("DISK_CHECK_INTERVAL", "600"))


class InstanceConfig:
    """
    实例配置：worker 启动时从账号仓库 Releases 读取实例专属配置，
    并据此覆盖 asset 命名。替代旧代码中「动态改写 config.ASSET_* 全局变量」的做法。

    每个实例的资产名带实例 ID 前缀，实现实例间数据隔离：
      inst-<ID>.db.enc / inst-<ID>.files.tar.gz.enc / leader-<ID>.json
    """

    def __init__(self, instance_id: str, cfg: dict = None):
        self.instance_id = instance_id
        cfg = cfg or {}
        # 实例专属资产命名
        self.asset_db = f"inst-{instance_id}.db.enc"
        self.asset_files = f"inst-{instance_id}.files.tar.gz.enc"
        self.asset_leader = f"leader-{instance_id}.json"
        # 隧道
        self.tunnel_token = cfg.get("tunnel_token") or TUNNEL_TOKEN
        self.tunnel_host = cfg.get("hostname") or TUNNEL_HOST
        self.tunnel_id = cfg.get("tunnel_id") or ""
        self.account = cfg.get("account") or ""
        self.account_repo = cfg.get("account_repo") or ""
        # 原始配置
        self.raw = cfg

    def to_dict(self):
        return {
            "instance_id": self.instance_id,
            "asset_db": self.asset_db,
            "asset_files": self.asset_files,
            "asset_leader": self.asset_leader,
            "tunnel_token": self.tunnel_token,
            "tunnel_host": self.tunnel_host,
            "tunnel_id": self.tunnel_id,
            "account": self.account,
            "account_repo": self.account_repo,
        }