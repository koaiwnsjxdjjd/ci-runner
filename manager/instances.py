# -*- coding: utf-8 -*-
"""
实例管理（manager 侧，增强版：数据自愈）

- 实例清单加密存储
- 创建：选账号 → 同步fork → 建隧道 → 存配置 → 触发 worker
- 关闭：取消 run → 删配置 → 删隧道
- 查询 / 上报
- ★ 数据自愈：
  · ensure_instances_self_heal()：清单缺失/为空时，自动从各账号 fork 扫描重建
  · worker_report 自愈：实例不存在时自动从 worker 配置创建
  · save_instances 增强：旧数据备份到 .bak + 数量骤减保护（防并发覆盖丢数据）
"""
import time
import datetime

import config
import log
from core import storage
from core import crypto
from core import ghapi
from manager import tunnels
from manager import accounts

logger = log.setup_logger("instances")


# ==================== 实例清单 ====================
def load_instances(token=None):
    data = storage.load_json_enc(config.ASSET_INSTANCES, token=token, default=[])
    return data if isinstance(data, list) else []


def save_instances(instances, token=None):
    """
    保存实例清单（增强版数据保护）：
    1. 空数据保护：新数据为空且已有数据 → 拒绝
    2. 数量骤减保护：新数据 < 旧数据一半（且旧数据>2）→ 拒绝（防并发覆盖）
    3. 写前备份旧数据到 .bak
    """
    tok = token or config.GH_TOKEN
    if not instances:
        existing = load_instances(token=tok)
        if existing:
            logger.warning("[protect] 拒绝空数据覆盖实例清单")
            return False
        blob = storage.download_asset(config.ASSET_INSTANCES, token=tok)
        if blob:
            logger.warning("[protect] 读取异常，拒绝空覆盖实例清单")
            return False
    else:
        # 数量骤减保护
        try:
            old = load_instances(token=tok)
            old_count = len(old)
            new_count = len(instances)
            if old_count > 2 and new_count < old_count / 2:
                logger.warning(f"[protect] 拒绝数量骤减覆盖：{old_count}→{new_count}（疑似并发丢数据）")
                return False
        except Exception:
            pass
        # 写前备份旧数据
        try:
            blob = storage.download_asset(config.ASSET_INSTANCES, token=tok)
            if blob:
                storage.upload_asset(f"{config.ASSET_INSTANCES}.bak", blob, token=tok)
        except Exception:
            pass
    storage.save_json_enc(config.ASSET_INSTANCES, instances, token=tok)
    return True


# ==================== 实例清单自愈 ====================
def _scan_worker_configs():
    """
    扫描所有账号 fork 仓库的 inst-*.json.enc，重建实例配置。
    返回 {inst_id: cfg}
    """
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
    return result


def ensure_instances_self_heal(manager_token=None):
    """
    实例清单自愈：若清单缺失或为空，自动从各账号 fork 重建。
    返回重建的实例数；若清单正常则返回 0。
    """
    tok = manager_token or config.GH_TOKEN
    existing = load_instances(token=tok)
    if existing:
        logger.info(f"[heal] 实例清单正常（{len(existing)} 个），无需自愈")
        return 0
    logger.warning("[heal] 实例清单为空/缺失，尝试从账号 fork 自动重建...")
    cfgs = _scan_worker_configs()
    if not cfgs:
        logger.warning("[heal] 未扫描到任何 worker 配置，无法重建")
        return 0
    instances = []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for inst_id, cfg in cfgs.items():
        hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
        mcp_hostname = cfg.get("mcp_hostname", "") or f"mcp-{hostname}"
        instances.append({
            "id": inst_id,
            "hostname": hostname,
            "account": cfg.get("account", ""),
            "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""),
            "mcp_hostname": mcp_hostname,
            "mcp_tunnel_id": cfg.get("mcp_tunnel_id", ""),
            "mcp_url": f"https://{mcp_hostname}" if cfg.get("mcp_tunnel_id") else None,
            "run_id": None,
            "status": "running",
            "url": f"https://{hostname}",
            "closed": False,
            "created_at": now,
        })
    if save_instances(instances, token=tok):
        logger.info(f"[heal] 实例清单已自动重建，共 {len(instances)} 个实例")
        return len(instances)
    return 0


def _next_inst_id(instances):
    nums = []
    for inst in instances:
        try:
            nums.append(int(inst["id"].replace("inst", "")))
        except Exception:
            pass
    return f"inst{max(nums) + 1 if nums else 1}"


# ==================== 账号仓库操作 ====================
def _account_repo_url(repo, path):
    return f"{ghapi.API_BASE}/repos/{repo}{path}"


def _save_instance_config(account, inst_id, payload):
    """把实例配置（含 tunnel token）加密存到账号仓库 Releases"""
    repo = account["repo"]
    token = account["token"]
    asset_name = f"inst-{inst_id}.json.enc"
    url = _account_repo_url(repo, f"/releases/tags/{config.BACKUP_TAG}")
    status, d = ghapi.gh_request("GET", url, token=token)
    if status != 200:
        ghapi.gh_request("POST", _account_repo_url(repo, "/releases"), token=token,
                         data={"tag_name": config.BACKUP_TAG, "name": "实例配置", "body": ""})
        status, d = ghapi.gh_request("GET", url, token=token)
    rel_id = d["id"]
    for a in d.get("assets", []):
        if a.get("name") == asset_name:
            ghapi.gh_request("DELETE", _account_repo_url(repo, f"/releases/assets/{a['id']}"),
                             token=token)
    enc = crypto.encrypt_bytes(json_dumps(payload).encode())
    up_url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={asset_name}"
    ghapi.gh_request("POST", up_url, token=token, data=enc,
                     headers={"Content-Type": "application/octet-stream"})


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def _trigger_worker(account, inst_id):
    """用账号 token 触发该账号仓库的 worker workflow"""
    repo = account["repo"]
    url = _account_repo_url(repo, f"/actions/workflows/{config.WORKER_WORKFLOW}/dispatches")
    status, d = ghapi.gh_request("POST", url, token=account["token"],
                                 data={"ref": "main", "inputs": {"INSTANCE_ID": inst_id}})
    if status not in (200, 204):
        raise RuntimeError(f"触发 worker 失败: {status} {d}")
    time.sleep(4)
    runs_url = _account_repo_url(repo, "/actions/runs?per_page=1")
    status, d = ghapi.gh_request("GET", runs_url, token=account["token"])
    run_id = None
    if status == 200 and d.get("workflow_runs"):
        run_id = d["workflow_runs"][0]["id"]
    return run_id


def _cancel_worker(account, run_id):
    if not run_id:
        return
    repo = account["repo"]
    url = _account_repo_url(repo, f"/actions/runs/{run_id}/cancel")
    ghapi.gh_request("POST", url, token=account["token"])


# ==================== 创建实例 ====================
def create_instance(manager_token=None):
    """创建新工作实例（全自动）"""
    sel = accounts.select_best_account(token=manager_token, workflow=config.WORKER_WORKFLOW)
    if not sel:
        return {"ok": False, "error": "所有账号并发已满，请稍后再试"}
    account, running = sel

    instances = load_instances(token=manager_token)
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

    # 创建 MCP 专用隧道（子域名加 mcp 前缀）
    mcp_hostname = f"mcp-{hostname}"
    mcp_tunnel_id, mcp_tunnel_token = "", ""
    try:
        mcp_tunnel_id, mcp_tunnel_token = tunnels.create_mcp_tunnel(mcp_hostname)
        logger.info(f"[create] MCP 隧道创建成功: {mcp_hostname}")
    except Exception as e:
        logger.warning(f"[create] MCP 隧道创建失败（不影响主功能）: {e}")

    try:
        _save_instance_config(account, inst_id, {
            "inst_id": inst_id,
            "hostname": hostname,
            "tunnel_token": tunnel_token,
            "tunnel_id": tunnel_id,
            "mcp_hostname": mcp_hostname,
            "mcp_tunnel_token": mcp_tunnel_token,
            "mcp_tunnel_id": mcp_tunnel_id,
            "account": account["name"],
            "account_repo": account["repo"],
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
        "id": inst_id,
        "hostname": hostname,
        "account": account["name"],
        "account_repo": account["repo"],
        "tunnel_id": tunnel_id,
        "mcp_hostname": mcp_hostname,
        "mcp_tunnel_id": mcp_tunnel_id,
        "run_id": run_id,
        "status": "starting",
        "url": f"https://{hostname}",
        "mcp_url": f"https://{mcp_hostname}" if mcp_tunnel_token else None,
        "closed": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
        try:
            asset_name = f"inst-{inst_id}.json.enc"
            url = _account_repo_url(account["repo"], f"/releases/tags/{config.BACKUP_TAG}")
            status, d = ghapi.gh_request("GET", url, token=account["token"])
            if status == 200:
                for a in d.get("assets", []):
                    if a.get("name") == asset_name:
                        ghapi.gh_request("DELETE",
                                        _account_repo_url(account["repo"], f"/releases/assets/{a['id']}"),
                                        token=account["token"])
        except Exception:
            pass
    try:
        tunnels.delete_tunnel(inst.get("tunnel_id"), inst.get("hostname"))
    except Exception:
        pass
    # 删除 MCP 隧道
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
    """
    worker 启动后上报 URL 和状态（含自愈）：
    若实例不存在，自动从账号 fork 的 inst-*.json.enc 创建。
    """
    tok = manager_token or config.GH_TOKEN
    instances = load_instances(token=tok)
    inst = next((i for i in instances if i["id"] == inst_id), None)
    if inst:
        inst["url"] = url
        inst["status"] = "running"
        inst["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_instances(instances, token=tok)
        return {"ok": True}
    # 自愈：实例不存在，从账号 fork 扫描创建
    logger.warning(f"[heal] worker {inst_id} 上报但清单中不存在，尝试自愈创建")
    cfgs = _scan_worker_configs()
    cfg = cfgs.get(inst_id)
    if cfg:
        hostname = cfg.get("hostname") or url.replace("https://", "")
        new_inst = {
            "id": inst_id,
            "hostname": hostname,
            "account": cfg.get("account", ""),
            "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""),
            "run_id": None,
            "status": "running",
            "url": url,
            "closed": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        instances.append(new_inst)
        save_instances(instances, token=tok)
        logger.info(f"[heal] 实例 {inst_id} 已自愈创建")
    return {"ok": True}

# ==================== MCP 隧道自动补创建 ====================
def _load_instance_config(account, inst_id):
    """从账号仓库读取实例配置文件"""
    repo = account.get('repo') or config.REPO
    token = account.get('token')
    asset_name = f'inst-{inst_id}.json.enc'
    url = _account_repo_url(repo, f'/releases/tags/{config.BACKUP_TAG}')
    status, d = ghapi.gh_request('GET', url, token=token)
    if status != 200:
        return None
    for a in d.get('assets', []):
        if a.get('name') == asset_name:
            from core import crypto
            status2, blob = ghapi.gh_request('GET',
                f'{ghapi.API_BASE}/repos/{repo}/releases/assets/{a["id"]}',
                token=token, raw=True,
                headers={'Accept': 'application/octet-stream'})
            if status2 == 200 and blob:
                try:
                    return crypto.decrypt_json(blob)
                except Exception:
                    return None
    return None


def ensure_mcp_tunnels(manager_token=None):
    """
    自动确保每个实例有 MCP 隧道。
    策略（优先级从高到低，零中断）：
    1. 实例清单已有 mcp_tunnel_id → 跳过
    2. 从配置文件恢复 MCP 信息 → 更新实例清单（不碰已有隧道）
    3. 创建新隧道 → 保存到配置文件和实例清单
    4. 创建时409（隧道已存在但无token）→ 删除重建（等待10秒避免409）
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
                logger.info(f'[mcp-heal] 从配置文件恢复 {inst["id"]} MCP 信息: {inst["mcp_hostname"]}')
                continue

        # 策略3: 创建新隧道
        try:
            mcp_tid, mcp_ttoken = tunnels.create_mcp_tunnel(mcp_hostname)
        except Exception as e:
            # 409 = 隧道已存在，说明之前创建过，配置文件应该有token
            # 不删除重建（会导致DNS丢失+服务中断），直接跳过
            if 'already have a tunnel' in str(e) or '1013' in str(e):
                logger.info(f'[mcp-heal] {inst["id"]} MCP 隧道已存在，跳过（worker 从配置文件读取 token）')
                continue
            else:
                logger.warning(f'[mcp-heal] {inst["id"]} 创建失败: {e}')
                continue

        inst['mcp_hostname'] = mcp_hostname
        inst['mcp_tunnel_id'] = mcp_tid
        inst['mcp_url'] = f'https://{mcp_hostname}'
        logger.info(f'[mcp-heal] 为 {inst["id"]} 创建 MCP 隧道: {mcp_hostname}')

        # 保存到配置文件
        if account:
            try:
                _save_instance_config(account, inst['id'], {
                    'inst_id': inst['id'],
                    'hostname': hostname,
                    'tunnel_token': inst.get('tunnel_token', ''),
                    'tunnel_id': inst.get('tunnel_id', ''),
                    'mcp_hostname': mcp_hostname,
                    'mcp_tunnel_token': mcp_ttoken,
                    'mcp_tunnel_id': mcp_tid,
                    'account': inst.get('account', ''),
                    'account_repo': inst.get('account_repo', ''),
                })
            except Exception as e:
                logger.warning(f'[mcp-heal] 保存 {inst["id"]} 配置失败: {e}')
        changed = True

    if changed:
        save_instances(instances_list, token=tok)
        logger.info('[mcp-heal] MCP 隧道处理完成')
    return changed
