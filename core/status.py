# -*- coding: utf-8 -*-
"""
保命状态检测（Survival Status）

- 检测 GitHub Actions 状态（双层检测）
- 检测账号是否被封
- 检测配额健康度
"""
import os
import time
import datetime

import config
import log
from core import ghapi

logger = log.setup_logger("status")

START_TIME = datetime.datetime.now(datetime.timezone.utc)


def elapsed():
    """当前 job 已运行秒数"""
    return int((datetime.datetime.now(datetime.timezone.utc) - START_TIME).total_seconds())


def check_actions():
    """检测 GitHub Actions 状态，返回 (ok, detail)"""
    return ghapi.check_github_status()


def check_account_health(account):
    """检查单个账号健康：未封 + 配额足够。返回 (ok, detail)"""
    try:
        if ghapi.check_account_suspended(account.get("token")):
            return False, {"suspended": True}
        remaining, limit, reset = ghapi.check_rate_limit(account.get("token"))
        ratio = remaining / limit if limit else 0
        if ratio < config.QUOTA_SWITCH_THRESHOLD:
            return False, {"rate_low": True, "remaining": remaining, "limit": limit}
        return True, {"rate_remaining": remaining, "rate_limit": limit}
    except Exception as e:
        return False, {"error": str(e)}