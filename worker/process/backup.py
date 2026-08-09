# -*- coding: utf-8 -*-
"""
进程文件快照与备份（生产级）

- 把进程 cwd 下的项目文件复制到 processes/<name>/app/
- 支持排除可重建目录（node_modules/.git 等）
- 大小限制保护
- 原子打包（先复制到临时目录，完成后替换）
- 全链路日志（记录跳过/失败的文件）
- 解包保留文件权限（filter="tar"）
"""
import os
import io
import shutil
import tarfile

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
    原子操作：先复制到临时目录，完成后替换目标目录。
    返回 (ok, size_mb, cfg)。
    """
    cwd = cfg.get("cwd") or ""
    name = cfg.get("name", "proc")
    if not cwd or not os.path.isdir(cwd):
        logger.warning(f"[backup] {name} 工作目录不存在: {cwd}")
        cfg["files_backed"] = False
        return False, 0, cfg

    dest = os.path.join(pconfig.proc_dir(), name, "app")
    tmp_dest = os.path.join(pconfig.proc_dir(), name, "app.tmp")
    os.makedirs(tmp_dest, exist_ok=True)
    _clear_dir(tmp_dest)

    exclude = set(cfg.get("exclude") or pconfig.DEFAULT_EXCLUDE)
    exclude.add(os.path.basename(pconfig.proc_dir().rstrip("/")))
    if "processes" in cwd.split(os.sep):
        exclude.add("processes")

    count = 0
    skipped = 0
    for root, dirs, files in os.walk(cwd):
        rel = os.path.relpath(root, cwd)
        dirs[:] = [d for d in dirs if d not in exclude and rel not in exclude]
        for d in dirs:
            os.makedirs(os.path.join(tmp_dest, rel, d), exist_ok=True)
        for f in files:
            if rel in exclude:
                continue
            s = os.path.join(root, f)
            d = os.path.join(tmp_dest, rel, f)
            try:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                count += 1
            except Exception as e:
                skipped += 1
                if skipped <= 5:
                    logger.warning(f"[backup] {name} 跳过文件 {f}: {e}")

    size_mb = utils.dir_size_mb(tmp_dest)

    if size_mb > config.PROC_MAX_BACKUP_MB:
        logger.warning(f"[backup] {name} 备份过大({size_mb:.1f}MB)，跳过文件备份")
        shutil.rmtree(tmp_dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        cfg["files_backed"] = False
        cfg["files_count"] = 0
    else:
        # 原子替换：删除旧目录，重命名临时目录
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        try:
            os.rename(tmp_dest, dest)
        except OSError:
            # 跨文件系统 rename 失败，回退到 copy + rmtree
            os.makedirs(dest, exist_ok=True)
            _clear_dir(dest)
            utils.copy_tree(tmp_dest, dest, set())
            shutil.rmtree(tmp_dest, ignore_errors=True)
        cfg["files_backed"] = True
        cfg["files_count"] = count
        cfg["size_mb"] = round(size_mb, 2)
        logger.info(f"[backup] {name} 备份完成: {count} 个文件, {size_mb:.1f}MB"
                    f"{f', 跳过 {skipped} 个' if skipped else ''}")

    pconfig.save_proc_config(cfg)
    return True, size_mb, cfg


def pack_processes_tar():
    """把 processes 目录打包为 gz 字节流"""
    if not os.path.isdir(pconfig.proc_dir()):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(pconfig.proc_dir(), arcname="processes")
    return buf.getvalue()


def unpack_processes_tar(data):
    """从字节流解包 processes 到 files 目录。保留文件权限。"""
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # filter="tar" 保留文件权限（执行权限、读写权限等）
            # filter="data" 会丢弃权限导致恢复的二进制无法执行
            try:
                tar.extractall(path=config.FILES_DIR, filter="tar")
            except TypeError:
                # Python < 3.12 不支持 filter 参数
                tar.extractall(path=config.FILES_DIR)
        logger.info("[backup] 进程快照解包完成（保留权限）")
        return True
    except Exception as e:
        logger.error(f"[backup] 进程快照解包失败: {e}")
        return False


def snapshot(reason="periodic"):
    """
    扫描并备份所有用户进程，写入 manifest。
    按cwd去重（每个项目目录只备份一次，用ghvps.json中的name作为key）。
    返回 (saved_count, processes_meta)。
    """
    from worker.process import scanner
    procs = scanner.scan_user_processes()
    if not procs:
        logger.info("[snapshot] 扫描完成，无用户进程")
        return 0, {}

    saved = 0
    processes_meta = {}
    seen_cwds = set()
    for info in procs:
        # 按cwd去重（同一个项目目录下的多个进程只备份一次）
        if info.cwd in seen_cwds:
            continue
        seen_cwds.add(info.cwd)
        try:
            cfg = pconfig.build_config(info)
            ok, size_mb, cfg = backup_process_files(cfg)
            if ok:
                saved += 1
                # 用cfg["name"]作为key（来自ghvps.json，与配置文件路径一致）
                name = cfg.get("name", info.name)
                processes_meta[name] = {
                    "name": name,
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
