# -*- coding: utf-8 -*-
"""
进程文件快照与备份（单一职责）

- 把进程 cwd 下的项目文件复制到 processes/<name>/app/
- 支持排除可重建目录（node_modules/.git 等）
- 大小限制保护（防止超大项目拖垮备份）
"""
import os
import shutil

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.backup")


def _clear_dir(path):
    """清空目录内容"""
    if not os.path.isdir(path):
        return
    for item in os.listdir(path):
        p = os.path.join(path, item)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except Exception:
                pass


def backup_process_files(cfg):
    """
    备份单个进程的项目文件。
    返回 (ok, size_mb, cfg)。
    """
    cwd = cfg.get("cwd") or ""
    name = cfg.get("name", "proc")
    if not cwd or not os.path.isdir(cwd):
        logger.warning(f"[backup] {name} 工作目录不存在: {cwd}")
        cfg["files_backed"] = False
        return False, 0, cfg

    dest = os.path.join(pconfig.proc_dir(), name, "app")
    os.makedirs(dest, exist_ok=True)
    _clear_dir(dest)

    exclude = set(cfg.get("exclude") or pconfig.DEFAULT_EXCLUDE)
    # 关键：排除持久化目录本身（防止 cwd 含 processes 时无限递归复制）
    exclude.add(os.path.basename(pconfig.proc_dir().rstrip("/")))
    if "processes" in cwd.split(os.sep):
        exclude.add("processes")
    count = utils.copy_tree(cwd, dest, exclude)
    size_mb = utils.dir_size_mb(dest)

    if size_mb > config.PROC_MAX_BACKUP_MB:
        logger.warning(f"[backup] {name} 备份过大({size_mb:.1f}MB)，跳过文件备份")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        cfg["files_backed"] = False
        cfg["files_count"] = 0
    else:
        cfg["files_backed"] = True
        cfg["files_count"] = count
        cfg["size_mb"] = round(size_mb, 2)
    pconfig.save_proc_config(cfg)
    return True, size_mb, cfg


def pack_processes_tar():
    """把 processes 目录打包为 gz 字节流（独立上传用）"""
    import io
    import tarfile
    if not os.path.isdir(pconfig.proc_dir()):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(pconfig.proc_dir(), arcname="processes")
    return buf.getvalue()


def unpack_processes_tar(data):
    """从字节流解包 processes 到 files 目录"""
    import io
    import tarfile
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=config.FILES_DIR, filter="data")
        return True
    except Exception as e:
        logger.error(f"[backup] 进程快照解包失败: {e}")
        return False


def snapshot(reason="periodic"):
    """
    扫描并备份所有用户进程，写入 manifest。
    返回 (saved_count, processes_meta)。
    """
    from worker.process import scanner
    procs = scanner.scan_user_processes()
    if not procs:
        logger.info("[snapshot] 扫描完成，无用户进程")
        return 0, {}

    saved = 0
    processes_meta = {}
    for info in procs:
        try:
            cfg = pconfig.build_config(info)
            ok, size_mb, cfg = backup_process_files(cfg)
            if ok:
                saved += 1
                processes_meta[info.name] = {
                    "name": info.name,
                    "pid": info.pid,
                    "cmdline": info.cmdline_str(),
                    "cwd": info.cwd,
                    "size_mb": round(size_mb, 2),
                    "files_backed": cfg.get("files_backed", True),
                    "saved_at": cfg.get("saved_at"),
                }
        except Exception as e:
            logger.error(f"[snapshot] 备份进程 {info.name} 失败: {e}")

    pconfig.save_manifest(processes_meta, reason=reason)
    logger.info(f"[snapshot] 快照完成: {saved} 个进程持久化（{reason}）")
    return saved, processes_meta