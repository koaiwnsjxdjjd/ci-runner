# -*- coding: utf-8 -*-
"""
多账号管理（manager 侧）

- 账号配置加密存储到 Releases
- 幂等全自动创建：验证 token → fork → secrets → 报备
- 负载均衡（选并发余量最大的账号）
- 脱敏展示
"""
import time
import base64

from nacl.public import PublicKey, SealedBox

import config
import log
from core import storage
from core import ghapi

logger = log.setup_logger("accounts")


# ==================== 账号存储 ====================
def load_accounts(token=None):
    """读取账号配置（加密）"""
    data = storage.load_json_enc(config.ASSET_ACCOUNTS, token=token, default=[])
    return data if isinstance(data, list) else []


def save_accounts(accounts, token=None):
    """保存账号（空数据保护）"""
    if not accounts:
        existing = load_accounts(token=token)
        if existing:
            logger.warning("[protect] 拒绝空数据覆盖账号配置")
            return
        blob = storage.download_asset(config.ASSET_ACCOUNTS, token=token)
        if blob:
            logger.warning("[protect] 读取异常，拒绝空覆盖账号配置")
            return
    storage.save_json_enc(config.ASSET_ACCOUNTS, accounts, token=token)


def add_account(name, gh_token, repo=None, max_conc=None, token=None):
    """添加/更新账号（仅报备）"""
    accounts = load_accounts(token=token)
    for a in accounts:
        if a.get("name") == name:
            a["token"] = gh_token
            if repo:
                a["repo"] = repo
            if max_conc:
                a["max_concurrency"] = max_conc
            save_accounts(accounts, token=token)
            return {"ok": True, "msg": f"账号 {name} 已更新"}
    accounts.append({
        "name": name,
        "token": gh_token,
        "repo": repo or config.REPO,
        "max_concurrency": max_conc or config.DEFAULT_MAX_CONCURRENCY,
    })
    save_accounts(accounts, token=token)
    return {"ok": True, "msg": f"账号 {name} 已添加"}


def remove_account(name, token=None):
    accounts = load_accounts(token=token)
    new = [a for a in accounts if a.get("name") != name]
    if len(new) == len(accounts):
        return {"ok": False, "msg": f"账号 {name} 不存在"}
    save_accounts(new, token=token)
    return {"ok": True, "msg": f"账号 {name} 已删除"}


def list_accounts(token=None):
    """返回账号列表（脱敏）"""
    accounts = load_accounts(token=token)
    result = []
    for a in accounts:
        tok = a.get("token", "")
        masked = (tok[:6] + "***" + tok[-4:]) if len(tok) > 12 else "***"
        result.append({
            "name": a.get("name"),
            "token_masked": masked,
            "repo": a.get("repo"),
            "max_concurrency": a.get("max_concurrency"),
        })
    return result


# ==================== fork 同步 ====================
def sync_fork(account):
    """把账号 fork 同步到上游最新。返回 True/False"""
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        if repo == config.REPO:
            return True
        url = f"{ghapi.API_BASE}/repos/{repo}/merge-upstream"
        status, d = ghapi.gh_request("POST", url, token=token, data={"branch": "main"})
        ok = status in (200, 201)
        if ok:
            logger.info(f"[sync] 已同步 {repo} 到上游最新")
        else:
            logger.info(f"[sync] {repo} 同步状态: {status} {d.get('message','')}")
        return ok
    except Exception as e:
        logger.error(f"[sync] 同步失败: {e}")
        return False


# ==================== Secrets 配置（libsodium sealed box） ====================
def _set_repo_secret(account_token, repo, secret_name, secret_value):
    """配置仓库 secret（幂等：已配跳过）"""
    try:
        chk = ghapi.gh_request("GET", f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/{secret_name}",
                               token=account_token)
        if chk[0] == 200 and isinstance(chk[1], dict) and chk[1].get("name"):
            return True
        url = f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/public-key"
        status, d = ghapi.gh_request("GET", url, token=account_token)
        if status != 200:
            return False
        key = d["key"]
        key_id = d["key_id"]
        pub = PublicKey(base64.b64decode(key))
        sealed = SealedBox(pub)
        encrypted = sealed.encrypt(str(secret_value).encode())
        encrypted_b64 = base64.b64encode(encrypted).decode()
        url = f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/{secret_name}"
        status, _ = ghapi.gh_request("PUT", url, token=account_token,
                                     data={"encrypted_value": encrypted_b64, "key_id": key_id})
        return status in (200, 201, 204)
    except Exception as e:
        logger.error(f"[secrets] 配置 {secret_name} 失败: {e}")
        return False


def _check_repo_secret(account_token, repo, secret_name):
    status, _ = ghapi.gh_request("GET",
                                f"{ghapi.API_BASE}/repos/{repo}/actions/secrets/{secret_name}",
                                token=account_token)
    return status == 200


def _ensure_repo(account_token, repo_name):
    """确保账号有仓库（不存在则 fork），返回 (full_repo, ok)"""
    status, _ = ghapi.gh_request("GET", f"{ghapi.API_BASE}/repos/{repo_name}",
                                 token=account_token)
    if status == 200:
        return repo_name, True
    logger.info(f"[repo] 账号无仓库，fork 主仓库...")
    status, d = ghapi.gh_request("POST", f"{ghapi.API_BASE}/repos/{config.REPO}/forks",
                                 token=account_token, data={"default_branch_only": True})
    if status not in (200, 202):
        return None, False
    for _ in range(60):
        time.sleep(5)
        status, _ = ghapi.gh_request("GET", f"{ghapi.API_BASE}/repos/{repo_name}",
                                     token=account_token)
        if status == 200:
            logger.info(f"[repo] fork 完成: {repo_name}")
            return repo_name, True
    return None, False


def _wait_workflow_ready(account_token, repo, workflow="worker.yml", timeout=120, max_push=3):
    """等待 workflow 被注册；超时推送空 commit 触发扫描（限制推送次数，防死循环）"""
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows"
    deadline = time.time() + timeout
    push_attempts = 0
    while True:
        # 超过 deadline 且推送次数用尽：退出（不再无限重置超时）
        if time.time() > deadline and push_attempts >= max_push:
            break
        try:
            status, d = ghapi.gh_request("GET", url, token=account_token)
            if status == 200:
                paths = [w.get("path", "") for w in d.get("workflows", [])]
                if any(workflow in p for p in paths):
                    return True
        except Exception:
            pass
        # 超时前 60s 推送空 commit 触发扫描（最多 max_push 次）
        if time.time() > deadline - 60 and push_attempts < max_push:
            try:
                rd = ghapi.gh_request("GET",
                                     f"{ghapi.API_BASE}/repos/{repo}/contents/README.md",
                                     token=account_token)
                if rd[0] == 200 and isinstance(rd[1], dict) and rd[1].get("sha"):
                    sha = rd[1]["sha"]
                    content = rd[1].get("content", "")
                    new_content = base64.b64encode(
                        (base64.b64decode(content).decode(errors="replace") + "\n").encode()).decode()
                    ghapi.gh_request("PUT",
                                    f"{ghapi.API_BASE}/repos/{repo}/contents/README.md",
                                    token=account_token,
                                    data={"message": "trigger workflow scan",
                                          "content": new_content, "sha": sha})
                    logger.info("[repo] 已推送空 commit 触发 workflow 扫描")
            except Exception:
                pass
            push_attempts += 1
            deadline = time.time() + timeout
        time.sleep(5)
    return False


def auto_provision_account(name, account_token, repo=None, max_conc=None, manager_token=None):
    """全自动创建账号（幂等）"""
    # ① 验证 token
    status, user = ghapi.gh_request("GET", f"{ghapi.API_BASE}/user", token=account_token)
    if status != 200:
        return {"ok": False, "error": f"token 无效（{status}）"}
    login = user.get("login", "")
    # ② 确保仓库
    if not repo:
        repo = f"{login}/{config.REPO.split('/')[-1]}"
    repo, ok = _ensure_repo(account_token, repo)
    if not ok:
        return {"ok": False, "error": "仓库准备失败（fork 超时或失败）"}
    # ③ 同步最新代码
    sync_fork({"repo": repo, "token": account_token})
    time.sleep(3)
    # ④ 等待 workflow 注册
    if not _wait_workflow_ready(account_token, repo):
        return {"ok": False, "error": "workflow 注册超时（稍后自动重试）"}
    # ⑤ 配 secrets
    needed = {"GH_TOKEN": account_token, "DEMO_KEY": config.DEMO_KEY,
              "EXEC_TOKEN": config.EXEC_TOKEN}
    all_ok = True
    for sname, sval in needed.items():
        if not _check_repo_secret(account_token, repo, sname):
            if not _set_repo_secret(account_token, repo, sname, sval):
                all_ok = False
                logger.error(f"[secrets] {sname} 配置失败")
    if not all_ok:
        return {"ok": False, "error": "secrets 配置失败（将自动重试）"}
    # ⑥ 报备
    return add_account(name, account_token, repo=repo, max_conc=max_conc, token=manager_token)


# ==================== 负载均衡 ====================
def _account_usage(account, workflow=None):
    """查询账号当前 worker 运行数（并发检测）"""
    try:
        repo = account.get("repo") or config.REPO
        token = account.get("token")
        url = f"{ghapi.API_BASE}/repos/{repo}/actions/runs?status=in_progress&per_page=100"
        status, data = ghapi.gh_request("GET", url, token=token)
        if status != 200:
            return 0
        runs = data.get("workflow_runs", [])
        if workflow:
            return sum(1 for r in runs if workflow in r.get("path", ""))
        return len(runs)
    except Exception:
        return 0


def select_best_account(token=None, workflow=None):
    """负载均衡：选并发余量最大的账号。返回 (account, running) 或 None"""
    accounts = load_accounts(token=token)
    if not accounts:
        return None
    best = None
    for acc in accounts:
        running = _account_usage(acc, workflow=workflow)
        max_c = acc.get("max_concurrency", config.DEFAULT_MAX_CONCURRENCY)
        if running >= max_c:
            continue
        if best is None or (max_c - running) > (best["max_concurrency"] - best["running"]):
            best = {"account": acc, "running": running, "max_concurrency": max_c}
    if best is None:
        return None
    return best["account"], best["running"]