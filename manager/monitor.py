# -*- coding: utf-8 -*-
"""
健康监控 + 自动恢复（manager 侧）

- 周期巡检实例健康（HTTP 探活）
- 连续失败自动重启实例
- 账号被封自动清理
- GitHub API 配额预警
"""
import time
import threading
import urllib.request
import urllib.error

import config
import log
from core import ghapi
from core import utils
from manager import instances
from manager import accounts

logger = log.setup_logger("monitor")

_fail_counts = {}


def check_health(host):
    """探活实例，返回 True/False"""
    try:
        req = urllib.request.Request(f"https://{host}/api/health",
                                     headers={"User-Agent": "Mozilla/5.0 (ghbox-monitor)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _account_suspended(account):
    return ghapi.check_account_suspended(account.get("token"))


def _restart_instance(inst):
    """触发实例 worker 重启"""
    account = next((a for a in accounts.load_accounts()
                    if a["name"] == inst.get("account")), None)
    if not account:
        return
    repo = account.get("repo") or config.REPO
    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
    ghapi.gh_request("POST", url, token=account.get("token"),
                     data={"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
    logger.info(f"[monitor] 实例 {inst['id']} 已自动重启")


def _auto_cleanup_account(account):
    """账号被封，自动清理其实例并移除账号"""
    logger.warning(f"[auto-cleanup] 账号 {account['name']} 被封，自动清理")
    insts = instances.list_instances()
    for inst in insts:
        if inst.get("account") == account.get("name") and not inst.get("closed"):
            try:
                instances.close_instance(inst["id"])
            except Exception as e:
                logger.error(f"[auto-cleanup] 关闭 {inst['id']} 失败: {e}")
    try:
        accounts.remove_account(account["name"])
    except Exception as e:
        logger.error(f"[auto-cleanup] 移除账号失败: {e}")


def health_monitor_loop():
    """实例健康巡检"""
    while True:
        time.sleep(60)
        try:
            insts = instances.list_instances()
            changed = False
            for inst in insts:
                if inst.get("status") != "running" or inst.get("closed"):
                    continue
                host = inst.get("hostname")
                if not host:
                    continue
                if check_health(host):
                    _fail_counts[inst["id"]] = 0
                else:
                    n = _fail_counts.get(inst["id"], 0) + 1
                    _fail_counts[inst["id"]] = n
                    logger.warning(f"[monitor] 实例 {inst['id']} 失败 {n}/3")
                    if n >= 3:
                        account = next((a for a in accounts.load_accounts()
                                        if a["name"] == inst.get("account")), None)
                        if account and _account_suspended(account):
                            _auto_cleanup_account(account)
                        else:
                            _restart_instance(inst)
                            inst["status"] = "restarting"
                        _fail_counts[inst["id"]] = 0
                        changed = True
            if changed:
                instances.save_instances(insts)
        except Exception as e:
            logger.error(f"[monitor] 巡检异常: {e}")


def account_monitor_loop():
    """账号被封监控"""
    while True:
        time.sleep(300)
        try:
            for acc in accounts.load_accounts():
                try:
                    if _account_suspended(acc):
                        logger.warning(f"[account-monitor] 账号 {acc['name']} 被封，自动清理")
                        _auto_cleanup_account(acc)
                except Exception:
                    pass
            time.sleep(60)
        except Exception as e:
            logger.error(f"[account-monitor] 异常: {e}")


def api_alert_loop():
    """GitHub API 配额预警"""
    while True:
        time.sleep(300)
        try:
            for acc in accounts.load_accounts():
                try:
                    remaining, limit, _ = ghapi.check_rate_limit(acc.get("token"))
                    if limit > 0 and remaining < limit * 0.2:
                        logger.warning(f"[alert] 账号 {acc['name']} rate limit 剩余 "
                                       f"{remaining}/{limit}（<20%），注意配额")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[alert] 预警检查异常: {e}")


def start_monitors():
    """启动所有监控线程"""
    for fn in (health_monitor_loop, account_monitor_loop, api_alert_loop):
        threading.Thread(target=fn, daemon=True).start()
    logger.info("[monitor] 健康/账号/配额监控已启动")