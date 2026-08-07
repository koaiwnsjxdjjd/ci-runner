# -*- coding: utf-8 -*-
"""
进程配置读写（单一职责）

- ghvps.json 读写（用户自定义项目结构/依赖/排除项）
- manifest.json 读写（进程清单）
- 环境变量读取（安全过滤）
"""
import os
import json
import time

import config
import log

logger = log.setup_logger("process.config")

# 默认备份排除目录（可重建）
DEFAULT_EXCLUDE = config.PROC_BACKUP_EXCLUDE


def proc_dir():
    return config.PROC_DIR


def proc_config_path(name):
    return os.path.join(config.PROC_DIR, name, "ghvps.json")


def manifest_path():
    return os.path.join(config.PROC_DIR, "manifest.json")


# ==================== 环境变量 ====================
# 排除的敏感/易变变量
_SKIP_ENV = (
    "PATH", "HOME", "USER", "SHELL", "PWD", "_", "SHLVL", "LANG", "LC_ALL",
    "TERM", "OLDPWD", "GITHUB_", "RUNNER_", "ACTIONS_", "CI", "GH_TOKEN",
    "DEMO_KEY", "EXEC_TOKEN", "TUNNEL_TOKEN", "CF_", "AWS_", "AZURE_",
)


def read_env(pid):
    """读取进程环境变量（仅保留安全变量）"""
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read().split(b"\x00")
        for item in data:
            if b"=" not in item:
                continue
            k, _, v = item.partition(b"=")
            k = k.decode(errors="replace")
            v = v.decode(errors="replace")
            if not k or any(k.startswith(s) for s in _SKIP_ENV if s.endswith("_")):
                continue
            if k in _SKIP_ENV:
                continue
            env[k] = v
    except Exception:
        pass
    return env


# ==================== 配置组装 ====================
def build_config(info):
    """
    为扫描到的进程组装配置。
    优先读取 cwd 下用户自定义 ghvps.json，否则生成默认配置。
    """
    cwd = info.cwd
    cfg = None
    if cwd and os.path.isdir(cwd):
        user_cfg = os.path.join(cwd, "ghvps.json")
        if os.path.exists(user_cfg):
            try:
                with open(user_cfg) as f:
                    cfg = json.load(f)
            except Exception:
                cfg = None
    if cfg is None:
        cfg = {
            "name": info.name,
            "command": info.cmdline_str(),
            "cwd": cwd or os.path.expanduser("~"),
            "env": read_env(info.pid),
            "install": [],
            "exclude": list(DEFAULT_EXCLUDE),
            "auto_restart": True,
            "restart_delay": 3,
        }
    else:
        cfg.setdefault("name", info.name)
        cfg.setdefault("command", info.cmdline_str())
        cfg.setdefault("cwd", cwd or os.path.expanduser("~"))
        cfg.setdefault("env", read_env(info.pid))
        cfg.setdefault("install", [])
        cfg.setdefault("exclude", list(DEFAULT_EXCLUDE))
        cfg.setdefault("auto_restart", True)
        cfg.setdefault("restart_delay", 3)
    cfg["source_pid"] = info.pid
    cfg["saved_at"] = time.time()
    return cfg


def save_proc_config(cfg):
    """保存进程配置到 processes/<name>/ghvps.json"""
    try:
        d = os.path.join(proc_dir(), cfg["name"])
        os.makedirs(d, exist_ok=True)
        with open(proc_config_path(cfg["name"]), "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存配置失败 {cfg.get('name')}: {e}")
        return False


def load_proc_config(name):
    """读取进程配置，失败返回 None"""
    path = proc_config_path(name)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ==================== manifest 读写 ====================
def save_manifest(processes, reason="periodic"):
    """保存进程清单 manifest.json"""
    manifest = {
        "version": 2,
        "saved_at": time.time(),
        "reason": reason,
        "processes": processes,
    }
    try:
        with open(manifest_path(), "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存清单失败: {e}")
        return False


def load_manifest():
    """读取进程清单，返回 processes dict 或 {}"""
    path = manifest_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            m = json.load(f)
        return m.get("processes", {}) or {}
    except Exception as e:
        logger.error(f"读取清单失败: {e}")
        return {}