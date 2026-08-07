# -*- coding: utf-8 -*-
"""
GitHub API 封装（生产级，requests 连接池）

特性：
- 全局连接池复用（requests.Session），避免每次新建 TCP/TLS 连接
- 统一超时、重试、错误处理
- rate limit 查询、账号配额估算、状态检测
"""
import time
import threading
import requests

import config
import log

logger = log.setup_logger("ghapi")

API_BASE = "https://api.github.com"
UPLOAD_BASE = "https://uploads.github.com"

# 全局连接池（线程安全）
_session = None
_session_lock = threading.Lock()
_last_rate_limit = {}   # token -> (ts, remaining, limit, reset)
_rate_lock = threading.Lock()


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({"User-Agent": "Mozilla/5.0 (ghbox)",
                                     "Accept": "application/vnd.github.v3+json"})
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10, pool_maxsize=20, max_retries=2)
            _session.mount("https://", adapter)
        return _session


def gh_request(method, url, token=None, data=None, headers=None, raw=False,
               timeout=60, retries=2):
    """
    通用 GitHub API 请求。
    raw=True 时返回 (status, bytes)，否则返回 (status, json_or_str)。
    自动重试（网络抖动/限流）。
    """
    tok = token or config.GH_TOKEN
    sess = _get_session()
    h = {}
    if tok:
        h["Authorization"] = f"token {tok}"
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = data if isinstance(data, (bytes, str)) else data
    last_status, last_body = 0, None
    for attempt in range(retries + 1):
        try:
            # 关键：dict 用 json= 发送（GitHub API 需要 JSON body），bytes/str 用 data=
            if body is None:
                resp = sess.request(method, url, headers=h, timeout=(10, timeout))
            elif isinstance(body, (bytes, str)):
                resp = sess.request(method, url, data=body, headers=h, timeout=(10, timeout))
            else:
                resp = sess.request(method, url, json=body, headers=h, timeout=(10, timeout))
            last_status = resp.status_code
            if raw:
                last_body = resp.content
                if resp.status_code in (200, 201, 202, 204):
                    return resp.status_code, resp.content
            else:
                try:
                    last_body = resp.json()
                except Exception:
                    last_body = resp.text
                if resp.status_code in (200, 201, 202, 204):
                    return resp.status_code, last_body
            # 非 2xx：若是限流/网络错误则重试
            if resp.status_code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return resp.status_code, last_body
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return 0, "timeout"
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            return 0, str(e)
    return last_status, last_body


# ==================== rate limit ====================
def check_rate_limit(token=None, force=False):
    """
    查询 rate limit，带 60 秒缓存（避免频繁调用刷爆配额）。
    返回 (remaining, limit, reset_ts)。
    """
    tok = token or config.GH_TOKEN
    now = time.time()
    with _rate_lock:
        cached = _last_rate_limit.get(tok)
        if cached and not force and (now - cached[0]) < 60:
            return cached[1], cached[2], cached[3]
    status, d = gh_request("GET", f"{API_BASE}/rate_limit", token=tok, timeout=20)
    if status != 200:
        return 0, 0, 0
    core = d.get("resources", {}).get("core", {})
    remaining, limit, reset = core.get("remaining", 0), core.get("limit", 0), core.get("reset", 0)
    with _rate_lock:
        _last_rate_limit[tok] = (now, remaining, limit, reset)
    return remaining, limit, reset


def estimate_account_quota(account):
    """
    估算账号配额健康度（0~1）。
    基于 rate limit 余量 + 运行中 worker 数。
    返回 (healthy, detail)
    """
    try:
        remaining, limit, _ = check_rate_limit(account.get("token"))
        ratio = remaining / limit if limit else 0
        running = 0
        try:
            repo = account.get("repo") or config.REPO
            url = f"{API_BASE}/repos/{repo}/actions/runs?status=in_progress&per_page=100"
            status, d = gh_request("GET", url, token=account.get("token"), timeout=30)
            if status == 200:
                runs = d.get("workflow_runs", [])
                running = sum(1 for r in runs if config.WORKER_WORKFLOW in r.get("path", ""))
        except Exception:
            pass
        max_c = account.get("max_concurrency", config.DEFAULT_MAX_CONCURRENCY)
        concurrency_ratio = 1 - (running / max_c if max_c else 0)
        health = ratio * 0.6 + concurrency_ratio * 0.4
        return max(0.0, min(1.0, health)), {
            "rate_remaining": remaining, "rate_limit": limit,
            "running": running, "max_concurrency": max_c,
        }
    except Exception as e:
        return 0.0, {"error": str(e)}


def select_best_survival_account(accounts):
    """保命选账号：配额健康度最高且超过阈值"""
    best = None
    best_health = -1
    for acc in accounts:
        health, detail = estimate_account_quota(acc)
        if health > config.QUOTA_SWITCH_THRESHOLD and health > best_health:
            best = acc
            best_health = health
    return best


def check_account_suspended(token=None):
    """检测账号是否被封（403 + suspended）。返回 True/False"""
    tok = token or config.GH_TOKEN
    status, d = gh_request("GET", f"{API_BASE}/user", token=tok, timeout=20)
    if status == 403:
        msg = d.get("message", "") if isinstance(d, dict) else str(d)
        return "suspended" in str(msg).lower()
    return False


# ==================== 状态检测（保命） ====================
def check_github_status():
    """
    检测 GitHub Actions 是否可用。返回 (ok, detail)。
    双层：状态页 + 真实 API 实测兜底。
    """
    try:
        resp = requests.get(config.GITHUB_COMPONENTS_URL, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0 (ghbox)"})
        if resp.status_code == 200:
            d = resp.json()
            page_status = "unknown"
            for c in d.get("components", []):
                if "actions" in c.get("name", "").lower():
                    page_status = c.get("status", "unknown").lower()
                    break
            if page_status in ("operational", "degraded_performance"):
                return True, page_status
            return _probe_actions_real()
    except Exception:
        pass
    return _probe_actions_real()


def _probe_actions_real():
    """真实 API 探测 Actions 是否可用"""
    try:
        status, _ = gh_request("GET", f"{API_BASE}/rate_limit", timeout=20)
        if status not in (200, 403):
            return False, f"api_unreachable:{status}"
    except Exception as e:
        return False, f"api_error:{e}"
    try:
        url = f"{API_BASE}/repos/{config.MAIN_REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
        status, _ = gh_request("POST", url, data={"ref": "main"}, timeout=30)
        if status in (200, 204):
            return True, "probe_dispatch_ok"
        return False, f"probe_dispatch_fail:{status}"
    except Exception:
        return True, "api_ok_dispatch_error"