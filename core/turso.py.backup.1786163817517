# -*- coding: utf-8 -*-
"""
Turso SQLite 云数据库封装（生产级）

替代 GitHub Releases 存储小数据（实例清单/账号配置/任务队列/leader锁/实例配置）。
大数据（文件备份/进程快照/MCP依赖/attacker）继续用 GitHub Releases。

特性：
- 全链路日志（每次操作记录SQL/耗时/结果）
- 3次自动重试（退避1/3/5秒，重试时重新连接）
- 线程安全（单连接 + RLock）
- 向后兼容（Turso不可用时回退到GitHub Releases）
- 通用kv_store表设计（key→JSON value）
- 初始化幂等（表只创建一次）
- 连接断开自动重连
"""
import os
import json
import time
import threading

import log

logger = log.setup_logger("turso")

TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]

_db = None
_db_lock = threading.RLock()
_initialized = False


def _get_db():
    """获取 Turso 数据库连接（单例，线程安全）。返回 connection 或 None。"""
    global _db
    if not TURSO_URL or not TURSO_TOKEN:
        return None
    with _db_lock:
        if _db is not None:
            return _db
        try:
            from libsql_experimental import connect
            t0 = time.time()
            _db = connect(TURSO_URL, auth_token=TURSO_TOKEN)
            logger.info(f"[turso] 连接成功 ({time.time()-t0:.3f}s)")
        except ImportError:
            logger.warning("[turso] libsql_experimental 未安装，Turso 不可用（回退到 Releases）")
            return None
        except Exception as e:
            logger.error(f"[turso] 连接失败: {e}")
            return None
        return _db


def _reset_db():
    """重置连接（重试时调用）"""
    global _db
    with _db_lock:
        _db = None


def _init_tables():
    """初始化数据库表（幂等，只执行一次）。返回 True/False。"""
    global _initialized
    if _initialized:
        return True
    db = _get_db()
    if not db:
        return False
    try:
        t0 = time.time()
        db.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        _initialized = True
        logger.info(f"[turso] 表初始化完成 ({time.time()-t0:.3f}s)")
        return True
    except Exception as e:
        logger.error(f"[turso] 表初始化失败: {e}")
        _reset_db()
        return False


def _execute(sql, params=None, retry=True):
    """
    执行 SQL（带重试和全链路日志）。
    返回 cursor 或 None（失败）。
    """
    if not _init_tables():
        return None
    db = _get_db()
    if not db:
        return None
    sql_preview = sql.replace('\n', ' ').strip()[:80]
    for attempt in range(MAX_RETRIES if retry else 1):
        try:
            t0 = time.time()
            if params:
                cursor = db.execute(sql, params)
            else:
                cursor = db.execute(sql)
            elapsed = time.time() - t0
            logger.info(f"[turso] OK ({elapsed:.3f}s) sql=\"{sql_preview}\" attempt={attempt+1}")
            return cursor
        except Exception as e:
            logger.warning(f"[turso] 失败 attempt={attempt+1}/{MAX_RETRIES} sql=\"{sql_preview}\" err={e}")
            if attempt < MAX_RETRIES - 1 and retry:
                time.sleep(RETRY_DELAYS[attempt])
                _reset_db()
                db = _get_db()
                if not db:
                    return None
                _init_tables()
            else:
                logger.error(f"[turso] 最终失败 sql=\"{sql_preview}\"")
    return None


# ==================== 通用 KV 操作 ====================

def put(key, value):
    """
    存储 key-value（value 自动 JSON 序列化）。
    使用 UPSERT 语义（存在则更新）。
    返回 True/False。
    """
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    now = time.time()
    cursor = _execute(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now)
    )
    ok = cursor is not None
    if ok:
        logger.info(f"[turso] put \"{key}\" ({len(value)} chars) OK")
    else:
        logger.error(f"[turso] put \"{key}\" FAILED")
    return ok


def get(key, default=None):
    """
    读取 key-value（自动 JSON 反序列化）。
    返回值或 default（不存在/失败时）。
    """
    cursor = _execute("SELECT value FROM kv_store WHERE key=?", (key,))
    if cursor is None:
        logger.warning(f"[turso] get \"{key}\" FAILED (返回 default)")
        return default
    rows = cursor.fetchall()
    if not rows:
        logger.info(f"[turso] get \"{key}\" -> 不存在")
        return default
    value = rows[0][0]
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        result = value
    logger.info(f"[turso] get \"{key}\" OK ({len(value)} chars)")
    return result


def delete(key):
    """删除 key。返回 True/False。"""
    cursor = _execute("DELETE FROM kv_store WHERE key=?", (key,))
    ok = cursor is not None
    if ok:
        logger.info(f"[turso] delete \"{key}\" OK")
    else:
        logger.error(f"[turso] delete \"{key}\" FAILED")
    return ok


def list_keys(prefix=""):
    """
    列出所有 key（可选前缀过滤）。
    返回 list[str]（失败时返回空列表）。
    """
    if prefix:
        cursor = _execute("SELECT key FROM kv_store WHERE key LIKE ?", (prefix + "%",))
    else:
        cursor = _execute("SELECT key FROM kv_store")
    if cursor is None:
        return []
    keys = [row[0] for row in cursor.fetchall()]
    logger.info(f"[turso] list_keys prefix=\"{prefix}\" -> {len(keys)} keys")
    return keys


def get_all(prefix=""):
    """
    列出所有匹配前缀的 key-value 对。
    返回 dict（失败时返回空 dict）。
    """
    if prefix:
        cursor = _execute("SELECT key, value FROM kv_store WHERE key LIKE ?", (prefix + "%",))
    else:
        cursor = _execute("SELECT key, value FROM kv_store")
    if cursor is None:
        return {}
    result = {}
    for row in cursor.fetchall():
        key, value = row[0], row[1]
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            result[key] = value
    logger.info(f"[turso] get_all prefix=\"{prefix}\" -> {len(result)} items")
    return result


# ==================== 状态检查 ====================

def is_available():
    """检查 Turso 是否可用（已连接且表已初始化）。"""
    return _get_db() is not None and _init_tables()


def ping():
    """测试连接（执行简单查询）。返回 True/False。"""
    cursor = _execute("SELECT 1")
    return cursor is not None


# ==================== Key 常量 ====================

KEY_INSTANCES = "instances"
KEY_ACCOUNTS = "accounts"
KEY_TASKS = "tasks"
KEY_LEADER = "leader"
KEY_INST_CONFIG_PREFIX = "inst_config:"  # inst_config:{inst_id}


def inst_config_key(inst_id):
    """生成实例配置的 key"""
    return f"{KEY_INST_CONFIG_PREFIX}{inst_id}"
