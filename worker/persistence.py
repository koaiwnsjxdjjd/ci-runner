# -*- coding: utf-8 -*-
"""
数据/文件持久化（生产级）

- SQLite 单连接 + 读写锁（避免并发写冲突）
- 数据库与 ~/files 目录加密备份到 Releases（实例级 asset 隔离）
- 分片上传支持大文件
- 恢复时自动还原数据库与文件
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
from core import storage
from core import status

logger = log.setup_logger("persistence")

# SQLite 单连接 + 锁
_db_conn = None
_db_lock = threading.RLock()  # RLock 支持重入，避免 _get_db() 在 db_execute/db_query 内部死锁


def _get_db():
    """获取单例 SQLite 连接（线程安全）"""
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
        return _db_conn


def db_execute(sql, params=None):
    """执行写操作（带锁）"""
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
    """执行查询（带锁）"""
    with _db_lock:
        try:
            conn = _get_db()
            cur = conn.execute(sql, params or ())
            return cur.fetchall()
        except Exception as e:
            logger.error(f"[db] 查询失败: {e}")
            return []


def create_new_db():
    """初始化数据库表结构"""
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
    """从字节流解包恢复到 home 目录"""
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=os.path.expanduser("~"), filter="data")
        os.makedirs(config.FILES_DIR, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"[persistence] 文件恢复失败: {e}")
        return False


# ==================== 备份/恢复（实例级） ====================
def load_or_create(inst_cfg, token=None, repo=None):
    """
    恢复实例数据（数据库 + 文件）。
    返回状态描述字符串。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    status_msg = "新建初始数据库"
    try:
        blob = storage.download_asset_chunked(db_asset, token=tok, repo=repo)
        if blob:
            with open(config.DB_FILE, "wb") as f:
                f.write(blob)
            status_msg = f"从 Releases 恢复数据库备份（{len(blob)} 字节）"
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
    try:
        fdata = storage.download_asset_chunked(files_asset, token=tok, repo=repo)
        if fdata:
            if restore_files_from_bytes(fdata):
                logger.info(f"[persistence] 文件已恢复（{len(fdata)} 字节）")
    except Exception as e:
        logger.error(f"[persistence] 文件恢复异常: {e}")
    os.makedirs(config.FILES_DIR, exist_ok=True)
    return status_msg


def backup_database(inst_cfg=None, token=None, repo=None):
    """备份数据库到 Releases，返回 (size, parts)"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    try:
        with _db_lock:
            conn = _get_db()
            # 先 checkpoint（WAL 落盘）
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
        with open(config.DB_FILE, "rb") as f:
            data = f.read()
        size, parts = storage.upload_asset_chunked(db_asset, data, token=tok, repo=repo)
        return size, parts
    except Exception as e:
        logger.error(f"[persistence] 数据库备份失败: {e}")
        raise


def backup_files(inst_cfg=None, token=None, repo=None):
    """备份 ~/files 目录，返回 (size, parts) 或 None"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    data = backup_files_to_bytes()
    if not data:
        return None
    size, parts = storage.upload_asset_chunked(files_asset, data, token=tok, repo=repo)
    return size, parts


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
            logger.info(f"[backup] 数据库已加密上传 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 数据库备份失败: {e}")
        try:
            res = backup_files(inst_cfg, token=token, repo=repo)
            if res:
                size, parts = res
                logger.info(f"[backup] 文件已加密上传 {size} 字节 ({parts} 分片)")
        except Exception as e:
            logger.error(f"[backup] 文件备份失败: {e}")