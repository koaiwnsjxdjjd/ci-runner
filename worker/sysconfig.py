# -*- coding: utf-8 -*-
"""
系统配置备份/恢复（合并原 system_backup.py + sysconfig.py + worker 内联，统一为一个模块）

- 备份 Linux 系统关键配置（systemd 服务/内核参数/cron/hosts/环境等）
- 备份到持久化目录 ~/files/sysconfig/，随文件备份一起上传
- 新实例启动时自动还原并拉起自启服务
- 需 sudo（runner 有免密 sudo）
"""
import os
import json
import time
import shutil
import subprocess

import config
import log
from core import utils

logger = log.setup_logger("sysconfig")

# 备份目录（持久化目录内）
SYS_BACKUP_DIR = os.path.join(config.FILES_DIR, "sysconfig")

# 要备份的系统配置（相对 / 的路径）
SYSTEM_PATHS = [
    "/etc/systemd/system",    # systemd 服务单元
    "/etc/sysctl.d",          # 内核参数
    "/etc/sysctl.conf",
    "/etc/cron.d",            # cron 定时任务
    "/etc/crontab",
    "/etc/hosts",
    "/etc/hostname",
    "/etc/profile.d",         # 环境变量
    "/etc/environment",
    "/etc/ssh/sshd_config",
    "/etc/sudoers.d",
]


def _run(cmd):
    """执行 shell 命令（带 sudo），返回 (ok, stdout, stderr)"""
    return utils.run_cmd(cmd, timeout=30)


def backup_system_config():
    """备份系统配置到 ~/files/sysconfig/。返回保存项数"""
    try:
        os.makedirs(SYS_BACKUP_DIR, exist_ok=True)
    except Exception as e:
        logger.error(f"[sysconfig] 创建备份目录失败: {e}")
        return 0
    manifest = {"time": time.time(), "paths": []}
    saved = 0
    for p in SYSTEM_PATHS:
        if not os.path.exists(p):
            continue
        rel = p.lstrip("/")
        target = os.path.join(SYS_BACKUP_DIR, rel)
        try:
            if os.path.isdir(p):
                os.makedirs(target, exist_ok=True)
                ok, _, _ = _run(f"sudo cp -a {p}/. {target}/ 2>/dev/null")
                if ok:
                    manifest["paths"].append(p)
                    saved += 1
            else:
                ok, out, _ = _run(f"sudo cat {p} 2>/dev/null || true")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if out:
                    with open(target, "w") as f:
                        f.write(out)
                    manifest["paths"].append(p)
                    saved += 1
        except Exception as e:
            logger.warning(f"[sysconfig] 备份 {p} 失败: {e}")
    # 保存清单
    try:
        with open(os.path.join(SYS_BACKUP_DIR, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass
    logger.info(f"[sysconfig] 系统配置备份完成，{saved} 项")
    return saved


def restore_system_config():
    """从 ~/files/sysconfig/ 还原系统配置并拉起自启服务。返回还原项数"""
    manifest_file = os.path.join(SYS_BACKUP_DIR, "manifest.json")
    if not os.path.exists(manifest_file):
        return 0
    try:
        with open(manifest_file) as f:
            manifest = json.load(f)
    except Exception as e:
        logger.error(f"[sysconfig] 读取清单失败: {e}")
        return 0
    restored = 0
    for p in manifest.get("paths", []):
        rel = p.lstrip("/")
        src = os.path.join(SYS_BACKUP_DIR, rel)
        if not os.path.exists(src):
            continue
        try:
            if os.path.isdir(src):
                ok, _, _ = _run(f"sudo mkdir -p {p} && sudo cp -a {src}/. {p}/ 2>/dev/null")
            else:
                ok, _, _ = _run(f"sudo mkdir -p {os.path.dirname(p)} && sudo cp -a {src} {p} 2>/dev/null")
            if ok:
                restored += 1
        except Exception as e:
            logger.warning(f"[sysconfig] 还原 {p} 失败: {e}")
    # 重新加载 systemd + 拉起自启服务
    _run("sudo systemctl daemon-reload 2>/dev/null")
    # 恢复 sysctl
    _run("sudo sysctl --system 2>/dev/null")
    # 拉起 systemd 服务单元（.service/.timer）
    sd_src = os.path.join(SYS_BACKUP_DIR, "etc/systemd/system")
    if os.path.isdir(sd_src):
        for f in os.listdir(sd_src):
            if f.endswith((".service", ".timer")):
                _run(f"sudo systemctl enable {f} 2>/dev/null")
                _run(f"sudo systemctl start {f} 2>/dev/null")
    logger.info(f"[sysconfig] 系统配置还原完成，{restored} 项")
    return restored


def backup_sysconfig_tar():
    """把系统配置打包为字节流（用于独立备份到 Releases）"""
    import io
    import tarfile
    if not os.path.isdir(SYS_BACKUP_DIR):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(SYS_BACKUP_DIR, arcname="sysconfig")
    return buf.getvalue()


def restore_sysconfig_from_tar(data):
    """从字节流还原系统配置"""
    import io
    import tarfile
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=config.FILES_DIR, filter="data")
        return True
    except Exception as e:
        logger.error(f"[sysconfig] tar 还原失败: {e}")
        return False