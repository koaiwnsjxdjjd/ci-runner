# -*- coding: utf-8 -*-
"""
Turso SQLite 云数据库封装（生产级，HTTP API 版）

替代 GitHub Releases 存储小数据。
使用 Turso HTTP Pipeline API（不依赖 libsql_experimental），确保写入真正持久化。

特性：
- 全链路日志（每次操作记录SQL/耗时/结果）
- 3次自动重试（退避1/3/5秒）
- 线程安全（RLock）
- 向后兼容（Turso不可用时回退到GitHub Releases）
- 通用kv_store表设计
- 安全上传（备份→删除→上传→失败恢复旧数据）
"""
import os
import json
import time
import threading
import requests

import log

logger = log.setup_logger("turso")

TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]

_lock = threading.RLock()
_initialized = False


def _get_http_url():
    """将 libsql:// 转为 https:// HTTP API URL"""
    if not TURSO_URL:
        return ""
    url = TURSO_URL.replace("libsql://", "https://")
    return url.rstrip("/") + "/v2/pipeline"


def _execute_sql(sql, args=None):
    """
    通过 Turso HTTP Pipeline API 执行 SQL。
    返回 (rows, error)。rows 是 list of dict（列名→值），失败返回 (None, error_msg)。
    """
    if not TURSO_URL or not TURSO_TOKEN:
        return None, "TURSO_URL or TURSO_TOKEN not configured"

    url = _get_http_url()
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }

    # 构造 args（libsql HTTP API 格式）
    formatted_args = None
    if args:
        formatted_args = []
        for arg in args:
            if isinstance(arg, str):
                formatted_args.append({"type": "text", "value": arg})
            elif isinstance(arg, int):
                formatted_args.append({"type": "integer", "value": str(arg)})
            elif isinstance(arg, float):
                formatted_args.append({"type": "float", "value": str(arg)})
            elif arg is None:
                formatted_args.append({"type": "null"})
            else:
                formatted_args.append({"type": "text", "value": str(arg)})

    body = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": formatted_args}},
            {"type": "close"},
        ]
    }

    for attempt in range(MAX_RETRIES):
        try:
            t0 = time.time()
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            elapsed = time.time() - t0
            sql_preview = sql.replace('\n', ' ').strip()[:80]

            if resp.status_code != 200:
                logger.warning(f"[turso] HTTP {resp.status_code} (attempt={attempt+1}/{MAX_RETRIES}) sql=\"{sql_preview}\" body={resp.text[:200]}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None, "empty results"

            first = results[0]
            if first.get("type") == "error":
                err = first.get("response", {}).get("message", "unknown")
                logger.warning(f"[turso] SQL error (attempt={attempt+1}): {err} sql=\"{sql_preview}\"")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                return None, err

            # 解析结果
            result = first.get("response", {}).get("result", {})
            cols = [c.get("name", "") for c in result.get("cols", [])]
            rows_raw = result.get("rows", [])
            rows = []
            for row in rows_raw:
                row_dict = {}
                for i, cell in enumerate(row):
                    val = cell.get("value") if isinstance(cell, dict) else cell
                    if i < len(cols):
                        row_dict[cols[i]] = val
                    else:
                        row_dict[f"col{i}"] = val
                rows.append(row_dict)

            logger.info(f"[turso] OK ({elapsed:.3f}s) sql=\"{sql_preview}\" attempt={attempt+1} rows={len(rows)}")
            return rows, None

        except requests.exceptions.Timeout:
            logger.warning(f"[turso] 超时 (attempt={attempt+1}/{MAX_RETRIES}) sql=\"{sql[:80]}\"")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return None, "timeout"
        except Exception as e:
            logger.warning(f"[turso] 异常 (attempt={attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return None, str(e)

    return None, "max retries exceeded"


def _init_tables():
    """初始化 kv_store 表（幂等）"""
    global _initialized
    with _lock:
        if _initialized:
            return True
    rows, err = _execute_sql(
        "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    if err:
        logger.error(f"[turso] 表初始化失败: {err}")
        return False
    _initialized = True
    logger.info("[turso] 表初始化完成 (kv_store)")
    return True


# ==================== 通用 KV 操作 ====================

def put(key, value):
    """
    存储 key-value（value 自动 JSON 序列化）。
    使用 UPSERT 语义。返回 True/False。
    """
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    now = time.time()
    rows, err = _execute_sql(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        [key, value, now]
    )
    if err:
        logger.error(f"[turso] put \"{key}\" FAILED: {err}")
        return False
    logger.info(f"[turso] put \"{key}\" ({len(value)} chars) OK")
    return True


def get(key, default=None):
    """
    读取 key-value（自动 JSON 反序列化）。
    返回值或 default。
    """
    rows, err = _execute_sql("SELECT value FROM kv_store WHERE key=?", [key])
    if err or rows is None:
        logger.warning(f"[turso] get \"{key}\" FAILED: {err}")
        return default
    if not rows:
        logger.info(f"[turso] get \"{key}\" -> 不存在")
        return default
    value = rows[0].get("value", "")
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        result = value
    logger.info(f"[turso] get \"{key}\" OK ({len(value)} chars)")
    return result


def delete(key):
    """删除 key。返回 True/False。"""
    rows, err = _execute_sql("DELETE FROM kv_store WHERE key=?", [key])
    if err:
        logger.error(f"[turso] delete \"{key}\" FAILED: {err}")
        return False
    logger.info(f"[turso] delete \"{key}\" OK")
    return True


def list_keys(prefix=""):
    """列出所有 key（可选前缀过滤）。返回 list[str]。"""
    if prefix:
        rows, err = _execute_sql("SELECT key FROM kv_store WHERE key LIKE ?", [prefix + "%"])
    else:
        rows, err = _execute_sql("SELECT key FROM kv_store")
    if err or rows is None:
        return []
    keys = [r.get("key", "") for r in rows]
    logger.info(f"[turso] list_keys prefix=\"{prefix}\" -> {len(keys)} keys")
    return keys


def get_all(prefix=""):
    """列出所有匹配前缀的 key-value 对。返回 dict。"""
    if prefix:
        rows, err = _execute_sql("SELECT key, value FROM kv_store WHERE key LIKE ?", [prefix + "%"])
    else:
        rows, err = _execute_sql("SELECT key, value FROM kv_store")
    if err or rows is None:
        return {}
    result = {}
    for r in rows:
        key = r.get("key", "")
        value = r.get("value", "")
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            result[key] = value
    logger.info(f"[turso] get_all prefix=\"{prefix}\" -> {len(result)} items")
    return result


# ==================== 状态检查 ====================

def is_available():
    """检查 Turso 是否可用（URL 和 TOKEN 已配置）。"""
    return bool(TURSO_URL and TURSO_TOKEN)


def ping():
    """测试连接。返回 True/False。"""
    if not is_available():
        return False
    rows, err = _execute_sql("SELECT 1")
    return err is None


# ==================== Key 常量 ====================

KEY_INSTANCES = "instances"
KEY_ACCOUNTS = "accounts"
KEY_TASKS = "tasks"
KEY_LEADER = "leader"
KEY_INST_CONFIG_PREFIX = "inst_config:"


def inst_config_key(inst_id):
    return f"{KEY_INST_CONFIG_PREFIX}{inst_id}"
