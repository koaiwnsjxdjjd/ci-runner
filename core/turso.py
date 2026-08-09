# -*- coding: utf-8 -*-
"""
Turso 云数据库封装（生产级完整版）

特性：
- 多Turso账号支持（负载均衡、热更新）
- 大文件BLOB存储（<100MB直接存，>=100MB分片50MB）
- 3次重试 + 多账号轮换
- 版本号（updated_at时间戳）
- 全链路日志
- 线程安全（RLock）
"""
import os
import json
import time
import base64
import threading
import requests

import log

logger = log.setup_logger("turso")

# ==================== 配置 ====================
SHARD_SIZE = 50 * 1024 * 1024              # 分片大小 50MB
DIRECT_UPLOAD_THRESHOLD = 100 * 1024 * 1024  # 100MB以下直接上传
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]
ACCOUNTS_FILE = os.path.expanduser("~/turso_accounts.json")

# ==================== 全局状态 ====================
_accounts = []
_accounts_mtime = 0
_lock = threading.RLock()
_initialized = False

# ==================== Key 常量 ====================
KEY_INSTANCES = "instances"
KEY_ACCOUNTS = "accounts"
KEY_TASKS = "tasks"
KEY_LEADER = "leader"
KEY_INST_CONFIG_PREFIX = "inst_config:"
KEY_DB_PREFIX = "inst_db:"
KEY_FILES_PREFIX = "inst_files:"
KEY_PROCESSES_PREFIX = "inst_processes:"
KEY_SHARD_PREFIX = "shard:"
KEY_SHARD_MANIFEST_PREFIX = "shard_manifest:"


def inst_config_key(inst_id):
    return f"{KEY_INST_CONFIG_PREFIX}{inst_id}"

def inst_db_key(inst_id):
    return f"{KEY_DB_PREFIX}{inst_id}"

def inst_files_key(inst_id):
    return f"{KEY_FILES_PREFIX}{inst_id}"

def inst_processes_key(inst_id):
    return f"{KEY_PROCESSES_PREFIX}{inst_id}"


# ==================== 账号管理（热更新） ====================
def _load_accounts():
    """加载Turso账号列表。优先 ~/turso_accounts.json，回退环境变量。热更新。"""
    global _accounts, _accounts_mtime
    # 环境变量模式
    env_url = os.environ.get("TURSO_URL", "")
    env_token = os.environ.get("TURSO_TOKEN", "")
    if not os.path.exists(ACCOUNTS_FILE):
        if env_url and env_token:
            return [{"url": env_url, "token": env_token, "name": "env-default"}]
        return []
    # 检查文件是否变更（热更新）
    try:
        mtime = os.path.getmtime(ACCOUNTS_FILE)
    except Exception:
        return _accounts or ([{"url": env_url, "token": env_token, "name": "env-default"}] if env_url and env_token else [])
    if mtime == _accounts_mtime and _accounts:
        return _accounts
    # 重新加载
    with _lock:
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                data = json.load(f)
            accts = data.get("accounts", [])
            if accts:
                # 验证每个账号（ping测试会在使用时自然发生）
                _accounts = accts
                _accounts_mtime = mtime
                logger.info(f"[turso] 账号列表已热加载: {len(accts)} 个 ({', '.join(a.get('name','?') for a in accts)})")
                return accts
        except Exception as e:
            logger.warning(f"[turso] 账号列表加载失败: {e}")
    if env_url and env_token:
        return [{"url": env_url, "token": env_token, "name": "env-default"}]
    return []


def _get_http_url(account):
    """libsql:// → https:// HTTP API URL"""
    return account["url"].replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"


def _select_account(idx_hint=None):
    """选择账号（轮询负载均衡）"""
    accounts = _load_accounts()
    if not accounts:
        return None
    if idx_hint is not None and idx_hint < len(accounts):
        return accounts[idx_hint]
    return accounts[int(time.time()) % len(accounts)]


# ==================== SQL 执行 ====================
def _execute_sql(account, sql, args=None, timeout=120):
    """
    通过 Turso HTTP Pipeline API 执行 SQL。
    返回 (rows, error)。rows 是 list[dict]，失败返回 (None, error_msg)。
    """
    url = _get_http_url(account)
    headers = {
        "Authorization": f"Bearer {account['token']}",
        "Content-Type": "application/json",
    }
    formatted_args = None
    if args:
        formatted_args = []
        for arg in args:
            if isinstance(arg, str):
                formatted_args.append({"type": "text", "value": arg})
            elif isinstance(arg, int):
                formatted_args.append({"type": "integer", "value": arg})
            elif isinstance(arg, float):
                formatted_args.append({"type": "float", "value": arg})
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
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None, "empty results"
        first = results[0]
        if first.get("type") == "error":
            err = first.get("response", {}).get("message", "unknown")
            return None, err
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
        return rows, None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def _execute_with_retry(sql, args=None, timeout=120):
    """执行SQL（多账号轮换 + 3次重试）"""
    accounts = _load_accounts()
    if not accounts:
        return None, "no turso accounts"
    last_error = None
    for acct_idx, account in enumerate(accounts):
        for attempt in range(MAX_RETRIES):
            rows, err = _execute_sql(account, sql, args, timeout)
            if err is None:
                return rows, None
            last_error = err
            acct_name = account.get("name", "?")
            sql_preview = sql.replace("\n", " ").strip()[:60]
            logger.warning(f"[turso] {acct_name} attempt={attempt+1}/{MAX_RETRIES} err={err} sql=\"{sql_preview}\"")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
    return None, last_error


def _init_tables():
    """初始化kv_store表（幂等）"""
    global _initialized
    if _initialized:
        return True
    _, err = _execute_with_retry(
        "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    if err:
        logger.error(f"[turso] 表初始化失败: {err}")
        return False
    _initialized = True
    logger.info("[turso] 表初始化完成 (kv_store)")
    return True


# ==================== 状态检查 ====================
def is_available():
    """检查Turso是否可用（有账号配置）"""
    return len(_load_accounts()) > 0


def ping():
    """测试连接"""
    rows, err = _execute_with_retry("SELECT 1")
    return err is None


# ==================== 小数据 KV 操作 ====================
def put(key, value):
    """存储key-value（自动JSON序列化）。返回True/False。"""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    now = time.time()
    _, err = _execute_with_retry(
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
    """读取key-value（自动JSON反序列化）。"""
    rows, err = _execute_with_retry("SELECT value FROM kv_store WHERE key=?", [key])
    if err or not rows:
        if err:
            logger.warning(f"[turso] get \"{key}\" FAILED: {err}")
        return default
    value = rows[0].get("value", "")
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        result = value
    logger.info(f"[turso] get \"{key}\" OK ({len(value)} chars)")
    return result


def delete(key):
    """删除key。返回True/False。"""
    _, err = _execute_with_retry("DELETE FROM kv_store WHERE key=?", [key])
    if err:
        logger.error(f"[turso] delete \"{key}\" FAILED: {err}")
        return False
    logger.info(f"[turso] delete \"{key}\" OK")
    return True


def list_keys(prefix=""):
    """列出所有key（可选前缀过滤）。"""
    if prefix:
        rows, _ = _execute_with_retry("SELECT key FROM kv_store WHERE key LIKE ?", [prefix + "%"])
    else:
        rows, _ = _execute_with_retry("SELECT key FROM kv_store")
    if not rows:
        return []
    return [r.get("key", "") for r in rows]


def get_all(prefix=""):
    """列出所有匹配前缀的key-value对。返回dict。"""
    if prefix:
        rows, _ = _execute_with_retry("SELECT key, value FROM kv_store WHERE key LIKE ?", [prefix + "%"])
    else:
        rows, _ = _execute_with_retry("SELECT key, value FROM kv_store")
    if not rows:
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


def get_version(key):
    """获取key的版本号（updated_at时间戳）。"""
    rows, _ = _execute_with_retry("SELECT updated_at FROM kv_store WHERE key=?", [key])
    if not rows:
        return None
    val = rows[0].get("updated_at")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ==================== 大文件 BLOB 存储 ====================
def put_blob(key, data_bytes):
    """
    存储大文件BLOB。
    <100MB：直接存为kv_store中的一行（base64编码）。
    >=100MB：分片存储（每片50MB）。
    返回 (True/False, version_timestamp)。
    """
    if not _init_tables():
        return False, None
    size = len(data_bytes)
    now = time.time()

    if size < DIRECT_UPLOAD_THRESHOLD:
        # 直接存储
        b64 = base64.b64encode(data_bytes).decode()
        ok = put(key, b64)
        if ok:
            # 更新版本号为当前时间
            _execute_with_retry("UPDATE kv_store SET updated_at=? WHERE key=?", [now, key])
            logger.info(f"[turso] put_blob \"{key}\" ({size} bytes) direct OK")
            return True, now
        return False, None

    # 分片存储
    shard_count = (size + SHARD_SIZE - 1) // SHARD_SIZE
    logger.info(f"[turso] put_blob \"{key}\" ({size} bytes, {size/1024/1024:.1f}MB) → {shard_count} shards × {SHARD_SIZE/1024/1024:.0f}MB")

    # 先清理旧分片
    _delete_shards(key)

    # 上传分片
    for i in range(shard_count):
        start = i * SHARD_SIZE
        end = min(start + SHARD_SIZE, size)
        chunk = data_bytes[start:end]
        b64 = base64.b64encode(chunk).decode()
        shard_key = f"{KEY_SHARD_PREFIX}{key}:{i}"
        ok = put(shard_key, b64)
        if not ok:
            logger.error(f"[turso] put_blob shard {i+1}/{shard_count} FAILED")
            return False, None
        logger.info(f"[turso] put_blob shard {i+1}/{shard_count} OK ({len(chunk)} bytes)")

    # 存储manifest
    manifest = {
        "parts": shard_count,
        "total_size": size,
        "shard_size": SHARD_SIZE,
        "updated_at": now,
    }
    manifest_key = f"{KEY_SHARD_MANIFEST_PREFIX}{key}"
    put(manifest_key, json.dumps(manifest, ensure_ascii=False))
    # 也更新manifest的updated_at
    _execute_with_retry("UPDATE kv_store SET updated_at=? WHERE key=?", [now, manifest_key])

    logger.info(f"[turso] put_blob \"{key}\" sharded OK ({shard_count} shards, {size} bytes)")
    return True, now


def get_blob(key):
    """
    读取大文件BLOB。
    先检查分片manifest，有则合并分片，否则直接读取base64解码。
    返回 bytes 或 None。
    """
    if not _init_tables():
        return None

    # 检查分片manifest
    manifest_key = f"{KEY_SHARD_MANIFEST_PREFIX}{key}"
    manifest_raw = get(manifest_key)
    if manifest_raw and isinstance(manifest_raw, str):
        try:
            manifest = json.loads(manifest_raw)
        except Exception:
            manifest = None
    else:
        manifest = manifest_raw if isinstance(manifest_raw, dict) else None

    if manifest and isinstance(manifest, dict) and "parts" in manifest:
        # 分片模式
        parts = int(manifest["parts"])
        total_size = int(manifest.get("total_size", 0))
        logger.info(f"[turso] get_blob \"{key}\" → {parts} shards (total {total_size} bytes)")
        chunks = []
        for i in range(parts):
            shard_key = f"{KEY_SHARD_PREFIX}{key}:{i}"
            b64 = get(shard_key)
            if b64 is None:
                logger.error(f"[turso] get_blob shard {i+1}/{parts} MISSING")
                return None
            if not isinstance(b64, str):
                logger.error(f"[turso] get_blob shard {i+1}/{parts} type error: {type(b64)}")
                return None
            try:
                chunks.append(base64.b64decode(b64))
            except Exception as e:
                logger.error(f"[turso] get_blob shard {i+1}/{parts} decode error: {e}")
                return None
        data = b"".join(chunks)
        logger.info(f"[turso] get_blob \"{key}\" sharded OK ({len(data)} bytes)")
        return data

    # 直接模式
    b64 = get(key)
    if b64 is None:
        return None
    if not isinstance(b64, str):
        return None
    try:
        data = base64.b64decode(b64)
        logger.info(f"[turso] get_blob \"{key}\" direct OK ({len(data)} bytes)")
        return data
    except Exception as e:
        logger.error(f"[turso] get_blob \"{key}\" decode error: {e}")
        return None


def get_blob_version(key):
    """获取BLOB的版本号（updated_at时间戳）。"""
    # 先检查分片manifest
    manifest_key = f"{KEY_SHARD_MANIFEST_PREFIX}{key}"
    ver = get_version(manifest_key)
    if ver is not None:
        return ver
    # 直接模式
    return get_version(key)


def delete_blob(key):
    """删除BLOB（包括分片和manifest）。"""
    _delete_shards(key)
    delete(key)
    delete(f"{KEY_SHARD_MANIFEST_PREFIX}{key}")
    logger.info(f"[turso] delete_blob \"{key}\" OK")


def _delete_shards(key):
    """删除分片数据"""
    manifest_key = f"{KEY_SHARD_MANIFEST_PREFIX}{key}"
    manifest_raw = get(manifest_key)
    if manifest_raw and isinstance(manifest_raw, str):
        try:
            manifest = json.loads(manifest_raw)
        except Exception:
            manifest = None
    else:
        manifest = manifest_raw if isinstance(manifest_raw, dict) else None

    if manifest and isinstance(manifest, dict) and "parts" in manifest:
        parts = int(manifest["parts"])
        for i in range(parts):
            delete(f"{KEY_SHARD_PREFIX}{key}:{i}")
    delete(manifest_key)


# ==================== 账号管理（CLI用） ====================
def add_account(name, url, token):
    """添加Turso账号到配置文件。返回True/False。"""
    accounts = _load_accounts()
    # 去重（按name）
    accounts = [a for a in accounts if a.get("name") != name]
    accounts.append({"name": name, "url": url, "token": token})
    return save_accounts(accounts)


def remove_account(name):
    """删除Turso账号。返回True/False。"""
    accounts = _load_accounts()
    new_list = [a for a in accounts if a.get("name") != name]
    if len(new_list) == len(accounts):
        return False
    return save_accounts(new_list)


def save_accounts(accounts):
    """保存账号列表到配置文件"""
    try:
        data = {"accounts": accounts}
        with open(ACCOUNTS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        global _accounts, _accounts_mtime
        _accounts = accounts
        _accounts_mtime = os.path.getmtime(ACCOUNTS_FILE)
        logger.info(f"[turso] 账号列表已保存: {len(accounts)} 个")
        return True
    except Exception as e:
        logger.error(f"[turso] 保存账号列表失败: {e}")
        return False


def list_accounts():
    """列出所有Turso账号（脱敏）"""
    accounts = _load_accounts()
    result = []
    for a in accounts:
        tok = a.get("token", "")
        masked = (tok[:8] + "..." + tok[-4:]) if len(tok) > 12 else "***"
        result.append({
            "name": a.get("name", "?"),
            "url": a.get("url", ""),
            "token_masked": masked,
        })
    return result


def test_account(name=None):
    """测试账号连接。返回 (ok, detail)"""
    accounts = _load_accounts()
    if not accounts:
        return False, "无Turso账号"
    for a in accounts:
        if name and a.get("name") != name:
            continue
        rows, err = _execute_sql(a, "SELECT 1", timeout=15)
        if err is None:
            return True, f"{a.get('name','?')} 连接正常"
        else:
            return False, f"{a.get('name','?')} 连接失败: {err}"
    return False, f"账号 {name} 不存在"
