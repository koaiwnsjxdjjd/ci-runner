# -*- coding: utf-8 -*-
"""CLI 公共 HTTP API 客户端（带 token，统一错误处理）"""
import json
import urllib.request
import urllib.error

from cli.common import config


def api(method, url, data=None, timeout=60):
    """请求 API，返回 dict（失败返回 {ok:False, error}）"""
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.TOKEN}",
        "User-Agent": "Mozilla/5.0 (Linux; Android) ghbox-cli",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get(path, timeout=60):
    return api("GET", config.mgr(path), timeout=timeout)


def post(path, data=None, timeout=60):
    return api("POST", config.mgr(path), data=data, timeout=timeout)


def delete(path, timeout=60):
    return api("DELETE", config.mgr(path), timeout=timeout)


def post_url(url, data=None, timeout=60):
    """请求任意 URL（用于直连实例）"""
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.TOKEN}",
        "User-Agent": "Mozilla/5.0 (Linux; Android) ghbox-cli",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method="POST", headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}