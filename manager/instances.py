# -*- coding: utf-8 -*-
"""
实例管理（manager 侧，增强版：Turso + 自愈）

- 实例清单存储到 Turso（优先）+ GitHub Releases（备份双写）
- 实例配置存储到 Turso（优先）+ 账号仓库 Releases（备份双写）
- 创建：选账号 → 同步fork → 建隧道 → 存配置 → 触发 worker
- 关闭：取消 run → 删配置 → 删隧道
- 查询 / 上报
- 数据自愈：清单缺失/为空时自动从配置恢复
- 全链路日志
"""
import time
import json
import datetime

import config
import log
from core import storage, crypto, ghapi, turso
from manager import tunnels
from manager import accounts

logger = log.setup_logger("instances")


# ==================== 实例清单 ====================
def load_instances(token=None):
    """读取实例清单。优先 Turso，回退 Releases。"""
    try:
        if turso.is_available():
            data = turso.get(turso.KEY_INSTANCES, default=None)
            if data is not None:
                logger.info(f"[instances] 从 Turso 加载 ({len(data)} 个)")
                return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[instances] Turso 加载失败，回退 Releases: {e}")
    data = storage.load_json_enc(config.ASSET_INSTANCES, token=token, default=[])
    logger.info(f"[instances] 从 Releases 加载 ({len(data) if isinstance(data, list) else 0} 个)")
    return data if isinstance(data, list) else []


def save_instances(instances, token=None):
    """
    保存实例清单。Turso（主）+ Releases（备份双写）。
    绝对禁止空数据保存。数量骤减保护。
    Turso成功时Releases失败只记info（不记error，减少日志噪音）。
    """
    if not instances:
        logger.warning("[protect] 绝对禁止空数据覆盖实例清单")
        return False
    # 数量骤减保护
    try:
        old = load_instances(token=token)
        old_count = len(old)
        new_count = len(instances)
        if old_count > 2 and new_count < old_count / 2:
            logger.warning(f"[protect] 拒绝数量骤减：{old_count}→{new_count}")
            return False
    except Exception:
        pass
    tok = token or config.GH_TOKEN
    # 优先存 Turso
    turso_ok = False
    try:
        if turso.is_available():
            turso.put(turso.KEY_INSTANCES, instances)
            logger.info(f"[instances] 已存入 Turso ({len(instances)} 个)")
            turso_ok = True
    except Exception as e:
        logger.warning(f"[instances] Turso 保存失败: {e}")
    # Releases 备份双写
    try:
        # .bak 备份仅在 Turso 不可用时做（Turso 可用时跳过以减少 API 调用）
        if not turso_ok:
            blob = storage.download_asset(config.ASSET_INSTANCES, token=tok)
            if blob:
                storage.upload_asset(f"{config.ASSET_INSTANCES}.bak", blob, token=tok)
        storage.save_json_enc(config.ASSET_INSTANCES, instances, token=tok)
        logger.info(f"[instances] 已备份到 Releases ({len(instances)} 个)")
    except Exception as e:
        if turso_ok:
            logger.info(f"[instances] Releases 备份失败（Turso已成功，不影响）: {e}")
        else:
            logger.error(f"[instances] Turso和Releases都失败: {e}")
    return True


# ==================== 实例配置存储 ====================
def _save_instance_config(account, inst_id, payload):
    """保存实例配置。优先 Turso，同时备份到账号仓库 Releases。"""
    # 存 Turso
    try:
        if turso.is_available():
            turso.put(turso.inst_config_key(inst_id), payload)
            logger.info(f"[config] 实例 {inst_id} 配置已存入 Turso")
    except Exception as e:
        logger.warning(f"[config] Turso 保存 {inst_id} 失败: {e}")
    # 同时存账号仓库 Releases（备份，worker 从这里读取）
    try:
        repo = account["repo"]
        token = account["token"]
        asset_name = f"inst-{inst_id}.json.enc"
        url = f"{ghapi.API_BASE}/repos/{repo}/releases/tags/{config.BACKUP_TAG}"
        status, d = ghapi.gh_request("GET", url, token=token)
        if status != 200:
            ghapi.gh_request("POST", f"{ghapi.API_BASE}/repos/{repo}/releases", token=token,
                             data={"tag_name": config.BACKUP_TAG, "name": "实例配置", "body": ""})
            status, d = ghapi.gh_request("GET", url, token=token)
        rel_id = d["id"]
        for a in d.get("assets", []):
            if a.get("name") == asset_name:
                ghapi.gh_request("DELETE", f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
                                 token=token)
        enc = crypto.encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode())
        up_url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={asset_name}"
        ghapi.gh_request("POST", up_url, token=token, data=enc,
                         headers={"Content-Type": "application/octet-stream"})
        logger.info(f"[config] 实例 {inst_id} 配置已备份到 Releases ({repo})")
    except Exception as e:
        logger.warning(f"[config] Releases 备份 {inst_id} 失败: {e}")


def _load_instance_config(account, inst_id):
    """读取实例配置。优先 Turso，回退 Releases。"""
    # 优先 Turso
    try:
        if turso.is_available():
            data = turso.get(turso.inst_config_key(inst_id), default=None)
            if data is not None:
                logger.info(f"[config] 从 Turso 读取 {inst_id} 配置")
                return data
    except Exception as e:
        logger.warning(f"[config] Turso 读取 {inst_id} 失败: {e}")
    # 回退到 Releases
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        asset_name = f"inst-{inst_id}.json.enc"
        url = f"{ghapi.API_BASE}/repos/{repo}/releases/tags/{config.BACKUP_TAG}"
        status, d = ghapi.gh_request("GET", url, token=token)
        if status != 200:
            return None
        for a in d.get("assets", []):
            if a.get("name") == asset_name:
                status2, blob = ghapi.gh_request("GET",
                    f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
                    token=token, raw=True,
                    headers={"Accept": "application/octet-stream"})
                if status2 == 200 and blob:
                    try:
                        return crypto.decrypt_json(blob)
                    except Exception:
                        return None
    except Exception as e:
        logger.warning(f"[config] Releases 读取 {inst_id} 失败: {e}")
    return None


# ==================== 实例清单自愈 ====================
def _scan_worker_configs():
    """
    扫描所有实例配置。优先 Turso，回退 Releases。
    返回 {inst_id: cfg}
    """
    # 优先从 Turso 读取所有实例配置
    try:
        if turso.is_available():
            all_configs = turso.get_all(turso.KEY_INST_CONFIG_PREFIX)
            if all_configs:
                logger.info(f"[heal] 从 Turso 扫描到 {len(all_configs)} 个实例配置")
                result = {}
                for key, cfg in all_configs.items():
                    inst_id = key.replace(turso.KEY_INST_CONFIG_PREFIX, "")
                    if inst_id and isinstance(cfg, dict):
                        cfg.setdefault("account", "")
                        cfg.setdefault("account_repo", "")
                        result[inst_id] = cfg
                return result
    except Exception as e:
        logger.warning(f"[heal] Turso 扫描失败，回退 Releases: {e}")
    # 回退到 Releases 扫描
    result = {}
    for acc in accounts.load_accounts():
        repo = acc.get("repo") or config.REPO
        token = acc.get("token")
        if not token:
            continue
        try:
            rel = storage.get_release(token=token, repo=repo)
            if not rel:
                continue
            for asset in rel.get("assets", []):
                name = asset.get("name", "")
                if name.startswith("inst-") and name.endswith(".json.enc"):
                    blob = storage.download_asset(name, token=token, repo=repo)
                    if blob:
                        try:
                            cfg = crypto.decrypt_json(blob)
                            inst_id = cfg.get("inst_id")
                            if inst_id:
                                cfg.setdefault("account", acc.get("name"))
                                cfg.setdefault("account_repo", repo)
                                result[inst_id] = cfg
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[heal] 扫描账号 {acc.get('name')} 失败: {e}")
    logger.info(f"[heal] 从 Releases 扫描到 {len(result)} 个实例配置")
    return result


def ensure_instances_self_heal(manager_token=None):
    """
    实例清单自愈（完整版）：
    1. 清单为空 → 从配置全部重建
    2. 清单不完整 → 只恢复缺失的
    3. 清单完整 → 跳过
    """
    tok = manager_token or config.GH_TOKEN
    existing = load_instances(token=tok)
    cfgs = _scan_worker_configs()

    if not cfgs:
        if existing:
            logger.info(f"[heal] 实例清单正常（{len(existing)} 个），无配置文件可扫描")
            return 0
        logger.warning("[heal] 实例清单为空且无配置文件，无法自愈")
        return 0

    if not existing:
        logger.warning("[heal] 实例清单为空/缺失，从配置文件全部重建...")
        instances = []
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for inst_id, cfg in cfgs.items():
            hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
            mcp_hostname = cfg.get("mcp_hostname", "") or f"mcp-{hostname}"
            instances.append({
                "id": inst_id, "hostname": hostname,
                "account": cfg.get("account", ""), "account_repo": cfg.get("account_repo", ""),
                "tunnel_id": cfg.get("tunnel_id", ""), "tunnel_token": cfg.get("tunnel_token", ""),
                "mcp_hostname": mcp_hostname, "mcp_tunnel_id": cfg.get("mcp_tunnel_id", ""),
                "mcp_url": f"https://{mcp_hostname}" if cfg.get("mcp_tunnel_id") else None,
                "run_id": None, "status": "running", "url": f"https://{hostname}",
                "closed": False, "created_at": now,
            })
        if save_instances(instances, token=tok):
            logger.info(f"[heal] 实例清单已全部重建，共 {len(instances)} 个实例")
            return len(instances)
        return 0

    existing_ids = {i.get("id") for i in existing if not i.get("closed")}
    missing_ids = set(cfgs.keys()) - existing_ids

    if not missing_ids:
        logger.info(f"[heal] 实例清单正常且完整（{len(existing)} 个），无需自愈")
        return 0

    logger.warning(f"[heal] 实例清单缺少 {len(missing_ids)} 个实例: {missing_ids}")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for inst_id in missing_ids:
        cfg = cfgs[inst_id]
        hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
        mcp_hostname = cfg.get("mcp_hostname", "") or f"mcp-{hostname}"
        existing.append({
            "id": inst_id, "hostname": hostname,
            "account": cfg.get("account", ""), "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""), "tunnel_token": cfg.get("tunnel_token", ""),
            "mcp_hostname": mcp_hostname, "mcp_tunnel_id": cfg.get("mcp_tunnel_id", ""),
            "mcp_url": f"https://{mcp_hostname}" if cfg.get("mcp_tunnel_id") else None,
            "run_id": None, "status": "running", "url": f"https://{hostname}",
            "closed": False, "created_at": now,
        })
        logger.info(f"[heal] 恢复缺失实例: {inst_id}")

    if save_instances(existing, token=tok):
        logger.info(f"[heal] 已恢复 {len(missing_ids)} 个缺失实例，清单共 {len(existing)} 个")
    return len(missing_ids)


# ==================== 账号仓库操作 ====================
def _next_inst_id(instances):
    """生成下一个实例ID"""
    nums = []
    for inst in instances:
        try:
            nums.append(int(inst["id"].replace("inst", "")))
        except Exception:
            pass
    return f"inst{max(nums) + 1 if nums else 1}"


def _trigger_worker(account, inst_id):
    """用账号 token 触发该账号仓库的 worker workflow"""
    repo = account["repo"]
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    status, d = ghapi.gh_request("POST", url, token=account["token"],
                                 data={"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
    if status not in (200, 204):
        raise RuntimeError(f"触发 worker 失败: {status} {d}")
    time.sleep(4)
    runs_url = f"{ghapi.API_BASE}/repos/{repo}/actions/runs?per_page=1"
    status, d = ghapi.gh_request("GET", runs_url, token=account["token"])
    run_id = None
    if status == 200 and d.get("workflow_runs"):
        run_id = d["workflow_runs"][0]["id"]
    return run_id


def _cancel_worker(account, run_id):
    if not run_id:
        return
    repo = account["repo"]
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/runs/{run_id}/cancel"
    ghapi.gh_request("POST", url, token=account["token"])


# ==================== 创建实例 ====================
def create_instance(manager_token=None, account_name=None):
    """创建新工作实例（全自动，支持指定账号）"""
    if account_name:
        accounts_list = accounts.load_accounts(token=manager_token)
        account = next((a for a in accounts_list if a["name"] == account_name), None)
        if not account:
            return {"ok": False, "error": f"账号 {account_name} 不存在"}
        running = accounts._account_usage(account, workflow=config.WORKER_WORKFLOW)
    else:
        sel = accounts.select_best_account(token=manager_token, workflow=config.WORKER_WORKFLOW)
        if not sel:
            return {"ok": False, "error": "所有账号并发已满，请稍后再试"}
        account, running = sel

    instances = load_instances(token=manager_token)
    if not instances:
        time.sleep(3)
        instances = load_instances(token=manager_token)
        if not instances:
            return {"ok": False, "error": "实例清单加载失败（可能是API故障），请稍后重试"}
    inst_id = _next_inst_id(instances)
    hostname = f"{inst_id}.{config.BASE_DOMAIN}"

    try:
        accounts.sync_fork(account)
        time.sleep(2)
    except Exception as e:
        logger.error(f"[create] fork 同步异常（继续）: {e}")

    try:
        tunnel_id, tunnel_token = tunnels.create_tunnel(hostname)
    except Exception as e:
        return {"ok": False, "error": f"创建隧道失败: {e}"}

    mcp_hostname = f"mcp-{hostname}"
    mcp_tunnel_id, mcp_tunnel_token = "", ""
    try:
        mcp_tunnel_id, mcp_tunnel_token = tunnels.create_mcp_tunnel(mcp_hostname)
        logger.info(f"[create] MCP 隧道创建成功: {mcp_hostname}")
    except Exception as e:
        logger.warning(f"[create] MCP 隧道创建失败（不影响主功能）: {e}")

    try:
        _save_instance_config(account, inst_id, {
            "inst_id": inst_id, "hostname": hostname,
            "tunnel_token": tunnel_token, "tunnel_id": tunnel_id,
            "mcp_hostname": mcp_hostname, "mcp_tunnel_token": mcp_tunnel_token,
            "mcp_tunnel_id": mcp_tunnel_id,
            "account": account["name"], "account_repo": account["repo"],
        })
    except Exception as e:
        tunnels.delete_tunnel(tunnel_id, hostname)
        return {"ok": False, "error": f"保存实例配置失败: {e}"}

    try:
        run_id = _trigger_worker(account, inst_id)
    except Exception as e:
        tunnels.delete_tunnel(tunnel_id, hostname)
        return {"ok": False, "error": f"触发 worker 失败: {e}"}

    inst = {
        "id": inst_id, "hostname": hostname,
        "account": account["name"], "account_repo": account["repo"],
        "tunnel_id": tunnel_id, "mcp_hostname": mcp_hostname, "mcp_tunnel_id": mcp_tunnel_id,
        "run_id": run_id, "status": "starting", "url": f"https://{hostname}",
        "mcp_url": f"https://{mcp_hostname}" if mcp_tunnel_token else None,
        "closed": False, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    instances.append(inst)
    save_instances(instances, token=manager_token)
    logger.info(f"[create] 实例 {inst_id} 创建中，地址 https://{hostname}")
    return {"ok": True, "instance": inst, "msg": f"实例 {inst_id} 创建中，地址 https://{hostname}"}


# ==================== 关闭实例 ====================
def close_instance(inst_id, manager_token=None):
    instances = load_instances(token=manager_token)
    inst = next((i for i in instances if i["id"] == inst_id), None)
    if not inst:
        return {"ok": False, "error": f"实例 {inst_id} 不存在"}

    account = next((a for a in accounts.load_accounts(token=manager_token)
                    if a["name"] == inst.get("account")), None)
    if account:
        _cancel_worker(account, inst.get("run_id"))
        # 从 Releases 删除配置文件
        try:
            asset_name = f"inst-{inst_id}.json.enc"
            url = f"{ghapi.API_BASE}/repos/{account['repo']}/releases/tags/{config.BACKUP_TAG}"
            status, d = ghapi.gh_request("GET", url, token=account["token"])
            if status == 200:
                for a in d.get("assets", []):
                    if a.get("name") == asset_name:
                        ghapi.gh_request("DELETE",
                                        f"{ghapi.API_BASE}/repos/{account['repo']}/releases/assets/{a['id']}",
                                        token=account["token"])
        except Exception:
            pass
    # 从 Turso 删除配置
    try:
        if turso.is_available():
            turso.delete(turso.inst_config_key(inst_id))
    except Exception:
        pass
    try:
        tunnels.delete_tunnel(inst.get("tunnel_id"), inst.get("hostname"))
    except Exception:
        pass
    if inst.get("mcp_tunnel_id"):
        try:
            tunnels.delete_tunnel(inst["mcp_tunnel_id"], inst.get("mcp_hostname", f"mcp-{inst.get('hostname','')}"))
        except Exception:
            pass

    inst["closed"] = True
    inst["status"] = "closed"
    save_instances(instances, token=manager_token)
    logger.info(f"[close] 实例 {inst_id} 已关闭")
    return {"ok": True, "msg": f"实例 {inst_id} 已关闭"}


# ==================== 查询 / 上报 ====================
def list_instances(manager_token=None):
    return load_instances(token=manager_token)


def get_instance(inst_id, manager_token=None):
    instances = load_instances(token=manager_token)
    return next((i for i in instances if i["id"] == inst_id), None)


def worker_report(inst_id, url, manager_token=None):
    """worker 上报 URL 和状态（含自愈）"""
    tok = manager_token or config.GH_TOKEN
    instances = load_instances(token=tok)
    inst = next((i for i in instances if i["id"] == inst_id), None)
    if inst:
        inst["url"] = url
        inst["status"] = "running"
        inst["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_instances(instances, token=tok)
        return {"ok": True}
    # 自愈：实例不存在，从配置扫描创建
    logger.warning(f"[heal] worker {inst_id} 上报但清单中不存在，尝试自愈创建")
    cfgs = _scan_worker_configs()
    cfg = cfgs.get(inst_id)
    if cfg:
        hostname = cfg.get("hostname") or url.replace("https://", "")
        new_inst = {
            "id": inst_id, "hostname": hostname,
            "account": cfg.get("account", ""), "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""), "run_id": None,
            "status": "running", "url": url, "closed": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        instances.append(new_inst)
        save_instances(instances, token=tok)
        logger.info(f"[heal] 实例 {inst_id} 已自愈创建")
    return {"ok": True}


# ==================== MCP 隧道自动补创建 ====================
def ensure_mcp_tunnels(manager_token=None):
    """
    自动确保每个实例有 MCP 隧道。四级策略（零中断）。
    1. 清单已有 mcp_tunnel_id → 跳过
    2. 从配置恢复 MCP 信息 → 更新清单
    3. 创建新隧道 → 保存到配置和清单
    4. 409 → 跳过（不删除已有隧道）
    """
    tok = manager_token or config.GH_TOKEN
    instances_list = load_instances(token=tok)
    changed = False
    for inst in instances_list:
        if inst.get('closed'):
            continue
        if inst.get('mcp_tunnel_id'):
            continue
        hostname = inst.get('hostname', '')
        if not hostname:
            continue
        mcp_hostname = f'mcp-{hostname}'

        # 策略2: 从配置文件恢复
        account = next((a for a in accounts.load_accounts(token=tok)
                       if a['name'] == inst.get('account')), None)
        if account:
            cfg = _load_instance_config(account, inst['id'])
            if cfg and cfg.get('mcp_tunnel_id') and cfg.get('mcp_tunnel_token'):
                inst['mcp_hostname'] = cfg.get('mcp_hostname', mcp_hostname)
                inst['mcp_tunnel_id'] = cfg['mcp_tunnel_id']
                inst['mcp_url'] = f'https://{inst["mcp_hostname"]}'
                changed = True
                logger.info(f'[mcp-heal] 从配置恢复 {inst["id"]} MCP: {inst["mcp_hostname"]}')
                continue

        # 策略3: 创建新隧道
        try:
            mcp_tid, mcp_ttoken = tunnels.create_mcp_tunnel(mcp_hostname)
        except Exception as e:
            if 'already have a tunnel' in str(e) or '1013' in str(e):
                logger.info(f'[mcp-heal] {inst["id"]} MCP 隧道已存在，跳过')
                continue
            else:
                logger.warning(f'[mcp-heal] {inst["id"]} 创建失败: {e}')
                continue

        inst['mcp_hostname'] = mcp_hostname
        inst['mcp_tunnel_id'] = mcp_tid
        inst['mcp_url'] = f'https://{mcp_hostname}'
        logger.info(f'[mcp-heal] 为 {inst["id"]} 创建 MCP 隧道: {mcp_hostname}')

        if account:
            try:
                existing = _load_instance_config(account, inst['id']) or {}
                existing['mcp_hostname'] = mcp_hostname
                existing['mcp_tunnel_token'] = mcp_ttoken
                existing['mcp_tunnel_id'] = mcp_tid
                _save_instance_config(account, inst['id'], existing)
            except Exception as e:
                logger.warning(f'[mcp-heal] 保存 {inst["id"]} 配置失败: {e}')
        changed = True

    if changed:
        save_instances(instances_list, token=tok)
        logger.info('[mcp-heal] MCP 隧道处理完成')
    return changed
