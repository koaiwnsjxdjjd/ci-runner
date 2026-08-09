# -*- coding: utf-8 -*-
"""
数据/文件持久化（生产级，Turso优先 + Releases备用）

- SQLite 单连接 + 读写锁
- 数据库与 ~/files 目录备份到 Turso（优先）+ GitHub Releases（备用回退）
- 恢复时优先Turso，回退Releases
- 保留文件权限（filter="tar"）
- processes目录恢复后清空（由独立快照恢复最新版本）
- 全链路日志
"""
import io
import os
import json
import time
import tarfile
import sqlite3
import threading
import datetime

import config
import log
from core import storage, crypto, turso
from core import status

logger = log.setup_logger("persistence")

_db_conn = None
_db_lock = threading.RLock()


def _get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
        return _db_conn


def db_execute(sql, params=None):
    with _db_lock:
        try:
            conn = _get_db()
            cur = conn.execute(sql, params or ())
            conn.commit()
            return cur
        except Exception as e:
            logger.error(f"[db] 执行失败: {e}")
            raise


def db_query(sql, params=None):
    with _db_lock:
        try:
            conn = _get_db()
            cur = conn.execute(sql, params or ())
            return cur.fetchall()
        except Exception as e:
            logger.error(f"[db] 查询失败: {e}")
            return []


def create_new_db():
    db_execute("CREATE TABLE IF NOT EXISTS messages "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)")
    db_execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
               (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))


# ==================== 文件打包 ====================
def backup_files_to_bytes():
    """把 ~/files 目录打包为 gz 字节流"""
    if not os.path.isdir(config.FILES_DIR):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(config.FILES_DIR, arcname="files")
    return buf.getvalue()


def restore_files_from_bytes(data):
    """从字节流解包恢复到 home 目录。
    保留文件权限（filter="tar"）。
    清空 processes 目录（由独立快照恢复最新版本）。
    """
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            try:
                tar.extractall(path=os.path.expanduser("~"), filter="tar")
            except TypeError:
                tar.extractall(path=os.path.expanduser("~"))
        os.makedirs(config.FILES_DIR, exist_ok=True)
        # 关键：清除 processes 目录（避免旧版覆盖独立快照）
        import shutil
        if os.path.isdir(config.PROC_DIR):
            shutil.rmtree(config.PROC_DIR, ignore_errors=True)
        os.makedirs(config.PROC_DIR, exist_ok=True)
        logger.info("[persistence] 文件恢复完成（processes目录已清空，由独立快照恢复）")
        return True
    except Exception as e:
        logger.error(f"[persistence] 文件恢复失败: {e}")
        return False


# ==================== 备份/恢复（Turso优先 + Releases备用） ====================
def load_or_create(inst_cfg, token=None, repo=None):
    """
    恢复实例数据（数据库 + 文件）。
    优先从 Turso 恢复，回退到 GitHub Releases。
    返回状态描述字符串。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    status_msg = "新建初始数据库"

    # === 恢复数据库 ===
    db_restored = False
    # 优先 Turso
    try:
        if turso.is_available():
            blob = turso.get_blob(turso.inst_db_key(inst_id))
            if blob:
                try:
                    raw = crypto.decrypt_bytes(blob)
                    with open(config.DB_FILE, "wb") as f:
                        f.write(raw)
                    status_msg = f"从 Turso 恢复数据库（{len(raw)} 字节）"
                    db_restored = True
                    logger.info(status_msg)
                except Exception as e:
                    logger.warning(f"[persistence] Turso数据库解密失败: {e}")
    except Exception as e:
        logger.warning(f"[persistence] Turso数据库恢复失败: {e}")
    # 回退 Releases
    if not db_restored:
        try:
            blob = storage.download_asset_chunked(db_asset, token=tok, repo=repo)
            if blob:
                with open(config.DB_FILE, "wb") as f:
                    f.write(blob)
                status_msg = f"从 Releases 恢复数据库（{len(blob)} 字节）"
                logger.info(status_msg)
            else:
                create_new_db()
                logger.info("[persistence] 无历史数据库，创建初始库")
        except Exception as e:
            logger.error(f"[persistence] 数据库恢复异常: {e}")
            try:
                create_new_db()
            except Exception:
                pass

    # === 恢复文件 ===
    files_restored = False
    # 优先 Turso
    try:
        if turso.is_available():
            fdata = turso.get_blob(turso.inst_files_key(inst_id))
            if fdata:
                try:
                    raw = crypto.decrypt_bytes(fdata)
                    if restore_files_from_bytes(raw):
                        files_restored = True
                        logger.info(f"[persistence] 文件已从 Turso 恢复（{len(raw)} 字节）")
                except Exception as e:
                    logger.warning(f"[persistence] Turso文件解密失败: {e}")
    except Exception as e:
        logger.warning(f"[persistence] Turso文件恢复失败: {e}")
    # 回退 Releases
    if not files_restored:
        try:
            fdata = storage.download_asset_chunked(files_asset, token=tok, repo=repo)
            if fdata:
                if restore_files_from_bytes(fdata):
                    logger.info(f"[persistence] 文件已从 Releases 恢复（{len(fdata)} 字节）")
        except Exception as e:
            logger.error(f"[persistence] 文件恢复异常: {e}")

    os.makedirs(config.FILES_DIR, exist_ok=True)
    return status_msg


def backup_database(inst_cfg=None, token=None, repo=None):
    """备份数据库。优先 Turso，回退 Releases。返回 (size, parts)。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    try:
        with _db_lock:
            conn = _get_db()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
        with open(config.DB_FILE, "rb") as f:
            data = f.read()
        enc_data = crypto.encrypt_bytes(data)
        # 优先 Turso
        turso_ok = False
        try:
            if turso.is_available():
                ok, ver = turso.put_blob(turso.inst_db_key(inst_id), enc_data)
                if ok:
                    turso_ok = True
                    logger.info(f"[backup] 数据库已存入 Turso ({len(data)} 字节)")
        except Exception as e:
            logger.warning(f"[backup] Turso数据库备份失败: {e}")
        # 回退 Releases（upload_asset_chunked内部会加密，所以传原始数据）
        if not turso_ok:
            size, parts = storage.upload_asset_chunked(db_asset, data, token=tok, repo=repo)
            logger.info(f"[backup] 数据库已存入 Releases ({size} 字节, {parts} 分片)")
            return size, parts
        return len(data), 1
    except Exception as e:
        logger.error(f"[backup] 数据库备份失败: {e}")
        raise


def backup_files(inst_cfg=None, token=None, repo=None):
    """备份 ~/files 目录。优先 Turso，回退 Releases。返回 (size, parts) 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    data = backup_files_to_bytes()
    if not data:
        return None
    enc_data = crypto.encrypt_bytes(data)
    # 优先 Turso
    turso_ok = False
    try:
        if turso.is_available():
            ok, ver = turso.put_blob(turso.inst_files_key(inst_id), enc_data)
            if ok:
                turso_ok = True
                logger.info(f"[backup] 文件已存入 Turso ({len(data)} 字节)")
    except Exception as e:
        logger.warning(f"[backup] Turso文件备份失败: {e}")
    # 回退 Releases
    if not turso_ok:
        size, parts = storage.upload_asset_chunked(files_asset, data, token=tok, repo=repo)
        logger.info(f"[backup] 文件已存入 Releases ({size} 字节, {parts} 分片)")
        return size, parts
    return len(data), 1


def save_prev_backup(inst_cfg=None, token=None, repo=None):
    """保存上一版数据库快照（用于回滚）"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    try:
        blob = storage.download_asset_chunked(db_asset, token=tok, repo=repo)
        if blob:
            storage.upload_asset_chunked(f"{db_asset}.bak", blob, token=tok, repo=repo)
            logger.info("[persistence] 已保存上一版数据库快照")
    except Exception as e:
        logger.warning(f"[persistence] 保存快照失败: {e}")


# ==================== 备份循环 ====================
def backup_loop(inst_cfg=None, token=None, repo=None, stop_event=None):
    """周期性备份循环（数据库 + 文件）"""
    while True:
        if stop_event and stop_event.is_set():
            return
        time.sleep(config.BACKUP_INTERVAL)
        try:
            size, parts = backup_database(inst_cfg, token=token, repo=repo)
            logger.info(f"[backup] 数据库备份完成 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 数据库备份失败: {e}")
        try:
            res = backup_files(inst_cfg, token=token, repo=repo)
            if res:
                size, parts = res
                logger.info(f"[backup] 文件备份完成 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 文件备份失败: {e}")
