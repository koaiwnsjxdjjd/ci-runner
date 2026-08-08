# -*- coding: utf-8 -*-
"""
GitHub Releases 加密存储（生产级）

特性：
- 资产上传/下载/删除（单文件 + 大文件自动分片 + 并发上传）
- 加密存储（AES-256-GCM）
- JSON 对象加密存取
- 空数据保护：防止并发覆盖导致数据丢失
- release 元数据缓存（减少 API 调用）
"""
import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import config
import log
from core import crypto
from core import ghapi

logger = log.setup_logger("storage")

# 分片大小（默认 500MB，可通过环境变量覆盖）
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(500 * 1024 * 1024)))
CHUNK_CONCURRENCY = int(os.environ.get("CHUNK_CONCURRENCY", "5"))

_release_cache = {}
_release_lock = threading.Lock()


# ==================== Release 管理 ====================
def get_release(token=None, repo=None):
    """获取 backup release，返回 dict 或 None"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    url = f"{ghapi.API_BASE}/repos/{repo}/releases/tags/{config.BACKUP_TAG}"
    status, data = ghapi.gh_request("GET", url, token=tok, timeout=30)
    if status != 200:
        logger.debug(f"[storage] get_release {repo} -> {status}")
    return data if status == 200 else None


def ensure_release(token=None, repo=None):
    """确保 backup release 存在，返回 release id"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    cache_key = f"{tok}:{repo}"
    with _release_lock:
        if cache_key in _release_cache:
            return _release_cache[cache_key]
    rel = get_release(token=tok, repo=repo)
    if rel:
        with _release_lock:
            _release_cache[cache_key] = rel["id"]
        return rel["id"]
    url = f"{ghapi.API_BASE}/repos/{repo}/releases"
    data = {"tag_name": config.BACKUP_TAG, "name": "加密备份",
            "body": "AES-256-GCM 加密备份", "draft": False, "prerelease": False}
    status, d = ghapi.gh_request("POST", url, token=tok, data=data, timeout=30)
    if status in (200, 201):
        with _release_lock:
            _release_cache[cache_key] = d.get("id")
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _find_asset(release, name):
    if not release:
        return None
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a
    return None


# ==================== 单文件上传/下载 ====================
def upload_asset(name, data_bytes, token=None, repo=None):
    """上传资产（加密），返回 (size, status)。
    安全上传：先备份旧数据→删除→上传→失败则恢复旧数据。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    rel_id = ensure_release(token=tok, repo=repo)
    rel = get_release(token=tok, repo=repo)
    old = _find_asset(rel, name)
    # 先备份旧资产数据
    old_backup = None
    if old:
        try:
            s, blob = ghapi.gh_request("GET",
                f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{old['id']}",
                token=tok, raw=True,
                headers={"Accept": "application/octet-stream"}, timeout=30)
            if s == 200 and blob:
                old_backup = blob
        except Exception:
            pass
        # 删除旧资产
        ghapi.gh_request("DELETE",
                         f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{old['id']}",
                         token=tok, timeout=30)
    # 上传新资产
    url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={name}"
    status, _ = ghapi.gh_request(
        "POST", url, token=tok, data=data_bytes,
        headers={"Content-Type": "application/octet-stream"}, timeout=180)
    # 上传失败：从备份恢复旧数据（避免数据丢失）
    if status not in (200, 201) and old_backup:
        logger.warning(f"[storage] 上传 {name} 失败({status})，恢复旧数据")
        try:
            ghapi.gh_request("POST", url, token=tok, data=old_backup,
                headers={"Content-Type": "application/octet-stream"}, timeout=180)
        except Exception as e:
            logger.error(f"[storage] 恢复旧数据也失败: {e}")
    return len(data_bytes), status


def download_asset(name, token=None, repo=None):
    """下载资产（原始字节，未解密）。返回 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    rel = get_release(token=tok, repo=repo)
    a = _find_asset(rel, name)
    if not a:
        logger.debug(f"[storage] asset {name} 不存在于 {repo}")
        return None
    logger.debug(f"[storage] 下载 {name} ({a.get('size','?')} bytes) from {repo}")
    status, blob = ghapi.gh_request(
        "GET", f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
        token=tok, raw=True, headers={"Accept": "application/octet-stream"}, timeout=120)
    if status != 200:
        logger.warning(f"[storage] 下载 {name} 失败: {status}")
    return blob if status == 200 else None


def delete_asset(name, token=None, repo=None):
    """删除资产"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    rel = get_release(token=tok, repo=repo)
    a = _find_asset(rel, name)
    if a:
        ghapi.gh_request("DELETE",
                         f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
                         token=tok, timeout=30)


# ==================== 分片上传/下载 ====================
def upload_asset_chunked(name, data_bytes, token=None, repo=None, concurrency=None):
    """
    加密分片上传。小数据走单文件，大数据自动分片并发。
    返回 (size, parts)。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    conc = concurrency or CHUNK_CONCURRENCY
    if len(data_bytes) <= CHUNK_SIZE:
        size, status = upload_asset(name, crypto.encrypt_bytes(data_bytes), token=tok, repo=repo)
        return size, 1
    parts = (len(data_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = [(i, data_bytes[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]) for i in range(parts)]

    def _upload(args):
        i, chunk = args
        upload_asset(f"{name}.part{i}", crypto.encrypt_bytes(chunk), token=tok, repo=repo)
        return i, len(chunk)

    with ThreadPoolExecutor(max_workers=conc) as ex:
        for i, size in ex.map(_upload, chunks):
            logger.info(f"[chunk] {name}.part{i} 上传 {size} 字节")
    upload_asset(f"{name}.manifest",
                 crypto.encrypt_bytes(json.dumps({"parts": parts}).encode()),
                 token=tok, repo=repo)
    return len(data_bytes), parts


def download_asset_chunked(name, token=None, repo=None, concurrency=None):
    """下载并解密（支持分片合并）。返回 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    conc = concurrency or CHUNK_CONCURRENCY
    manifest_blob = download_asset(f"{name}.manifest", token=tok, repo=repo)
    if manifest_blob:
        try:
            manifest = json.loads(crypto.decrypt_bytes(manifest_blob).decode())
            parts = int(manifest["parts"])
            results = [None] * parts

            def _download(i):
                blob = download_asset(f"{name}.part{i}", token=tok, repo=repo)
                return i, crypto.decrypt_bytes(blob) if blob else None

            with ThreadPoolExecutor(max_workers=conc) as ex:
                for i, data in ex.map(_download, range(parts)):
                    results[i] = data
            if any(d is None for d in results):
                raise RuntimeError("部分分片缺失")
            return b"".join(results)
        except Exception as e:
            logger.error(f"[chunk] 分片合并失败: {e}")
            return None
    blob = download_asset(name, token=tok, repo=repo)
    if blob:
        try:
            return crypto.decrypt_bytes(blob)
        except Exception:
            return None
    return None


# ==================== JSON 加密存取 ====================
def save_json_enc(asset_name, obj, token=None, repo=None):
    """加密保存 JSON 对象"""
    return upload_asset(asset_name, crypto.encrypt_json(obj), token=token, repo=repo)


def load_json_enc(asset_name, token=None, repo=None, default=None):
    """读取并解密 JSON 对象，失败返回 default"""
    blob = download_asset(asset_name, token=token, repo=repo)
    if not blob:
        return default
    try:
        return crypto.decrypt_json(blob)
    except Exception:
        return default


# ==================== 数据保护 ====================
def save_json_enc_protected(asset_name, obj, token=None, repo=None):
    """
    带空数据保护的 JSON 保存：
    若 obj 为空且 Releases 已有数据，拒绝覆盖（防止误清数据）。
    返回 True/False。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    if not obj:
        existing = load_json_enc(asset_name, token=tok, repo=repo, default=None)
        if existing is not None:
            logger.warning(f"[protect] 拒绝空数据覆盖 {asset_name}")
            return False
        blob = download_asset(asset_name, token=tok, repo=repo)
        if blob:
            logger.warning(f"[protect] 读取异常，拒绝空覆盖 {asset_name}")
            return False
    save_json_enc(asset_name, obj, token=tok, repo=repo)
    return True


# ==================== 便捷封装（实例级） ====================
def instance_asset_name(instance_id, kind):
    """生成实例专属资产名"""
    if kind == "db":
        return f"inst-{instance_id}.db.enc"
    if kind == "files":
        return f"inst-{instance_id}.files.tar.gz.enc"
    if kind == "leader":
        return f"leader-{instance_id}.json"
    return f"inst-{instance_id}.{kind}.enc"