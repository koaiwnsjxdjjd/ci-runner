# -*- coding: utf-8 -*-
"""
Cloudflare 隧道自动管理（manager 侧）

- 创建固定域名隧道（全自动，无需手动）
- 配置 DNS CNAME
- 删除隧道 + DNS 记录
- 处理「创建时 config 不生效」的坑（单独 PUT configurations）
"""
import json
import time
import urllib.request
import urllib.error

import config
import log

logger = log.setup_logger("tunnels")


def cf_request(method, url, data=None, timeout=30):
    """Cloudflare API 请求，返回 (status, body)"""
    h = {
        "X-Auth-Email": config.CF_EMAIL,
        "X-Auth-Key": config.CF_API_KEY,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, method=method, headers=h)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def create_tunnel(hostname, service_url="http://localhost:8080"):
    """
    创建固定域名隧道并配置 DNS。
    返回 (tunnel_id, tunnel_token)。
    """
    if not all([config.CF_EMAIL, config.CF_API_KEY, config.CF_ACCOUNT_ID]):
        raise RuntimeError("CF 凭证未配置")
    name = "t-" + hostname.split(".")[0]
    # 1. 创建隧道（409时用唯一名称重试）
    for attempt in range(3):
        tunnel_name = name if attempt == 0 else f"{name}-{int(time.time())}"
        status, d = cf_request("POST",
            f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel",
            data={"name": tunnel_name, "config_src": "cloudflare",
                  "config": {"ingress": [{"hostname": hostname, "service": service_url},
                                          {"service": "http_status:404"}]}})
        if status in (200, 201):
            break
        if status == 409 and attempt < 2:
            time.sleep(5)
            continue
        raise RuntimeError(f"创建隧道失败: {status} {d}")
    tid = d["result"]["id"]
    ttoken = d["result"]["token"]
    # 2. 单独更新 ingress（创建时 config 不生效的坑）
    cf_request("PUT",
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel/{tid}/configurations",
        data={"config": {"ingress": [{"hostname": hostname, "service": service_url},
                                     {"service": "http_status:404"}]}})
    # 3. 创建 DNS 记录
    if config.CF_ZONE_ID:
        cf_request("POST",
            f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records",
            data={"type": "CNAME", "name": hostname,
                  "content": f"{tid}.cfargotunnel.com", "proxied": True})
    logger.info(f"[tunnel] 隧道创建成功: {hostname} ({tid})")
    return tid, ttoken


def delete_tunnel(tunnel_id, hostname):
    """删除隧道及 DNS 记录"""
    if config.CF_ZONE_ID:
        status, d = cf_request("GET",
            f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records?name={hostname}")
        if status == 200:
            for rec in d.get("result", []):
                cf_request("DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records/{rec['id']}")
    if config.CF_ACCOUNT_ID:
        cf_request("DELETE",
            f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}")
    logger.info(f"[tunnel] 隧道已删除: {hostname}")
    return {"ok": True}

def create_mcp_tunnel(hostname):
    """创建 MCP 专用隧道（服务指向 3457 端口）"""
    return create_tunnel(hostname, service_url="http://localhost:3457")


def find_tunnel_by_name(name):
    """按名称查找隧道，返回隧道 dict 或 None"""
    if not all([config.CF_EMAIL, config.CF_API_KEY, config.CF_ACCOUNT_ID]):
        return None
    status, d = cf_request("GET",
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel?name={name}")
    if status == 200 and d.get("result"):
        return d["result"][0]
    return None


def delete_tunnel_by_name(hostname):
    """按 hostname 删除隧道（含DNS记录）。
    隧道名称格式: t-{hostname第一段}（如 t-mcp-inst12）
    """
    tunnel_name = "t-" + hostname.split(".")[0]
    t = find_tunnel_by_name(tunnel_name)
    if not t:
        logger.warning(f"[tunnel] 未找到隧道: {tunnel_name}")
        return
    tid = t.get("id")
    if not tid:
        return
    # 删除 DNS 记录
    if config.CF_ZONE_ID:
        status, d = cf_request("GET",
            f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records?name={hostname}")
        if status == 200:
            for rec in d.get("result", []):
                cf_request("DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records/{rec['id']}")
    # 删除隧道
    cf_request("DELETE",
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel/{tid}")
    logger.info(f"[tunnel] 隧道已按名称删除: {hostname}")
