# -*- coding: utf-8 -*-
"""
Turso 账号池管理（manager 侧）

- 账号池：从 ~/turso_accounts.json 加载，热更新
- 额度计算：实例上报累计，每个账号5GB限制
- 预警：4GB=warning, 5GB=full
- 账号分配：选余量最大的账号给实例
- 健康状态：normal/warning/full/unhealthy
- 配置下发：给实例返回账号列表和状态
- 主管家数据也存Turso（专用key）
"""
import os
import json
import time
import threading

import config
import log
from core import turso, storage

logger = log.setup_logger("turso_pool")

# 额度配置
TURSO_QUOTA = 5 * 1024 * 1024 * 1024  # 5GB
TURSO_WARN_THRESHOLD = 0.8  # 80%预警
TURSO_FULL_THRESHOLD = 0.95  # 95%禁用

# manager专用key（主管家数据存Turso）
KEY_TURSO_POOL = "turso_pool_state"  # 账号池状态（额度、健康）
KEY_TURSO_REPORTS = "turso_reports:"  # 实例上报前缀

# 内存缓存
_pool_state = {}
_reports = {}  # {inst_id: {type, size, account, status, version, error, timestamp}}
_lock = threading.RLock()


def _load_pool_state():
    """从Turso加载账号池状态。"""
    global _pool_state
    if turso.is_available():
        data = turso.get(KEY_TURSO_POOL, default=None)
        if data and isinstance(data, dict):
            _pool_state = data
            logger.info(f"[turso_pool] 账号池状态已从Turso加载: {len(_pool_state)} 个账号")
            return
    _pool_state = {}
    logger.info("[turso_pool] 账号池状态为空，初始化")


def _save_pool_state():
    """保存账号池状态到Turso。"""
    if turso.is_available():
        turso.put(KEY_TURSO_POOL, _pool_state)
    # 同时保存到Releases作为备份
    try:
        storage.save_json_enc("turso_pool.json.enc", _pool_state)
    except Exception:
        pass


def _ensure_account_state(account_name):
    """确保账号状态存在"""
    if account_name not in _pool_state:
        _pool_state[account_name] = {
            "total_size": 0,  # 已用字节
            "status": "normal",  # normal/warning/full/unhealthy
            "last_report": 0,  # 最后上报时间
            "fail_count": 0,  # 连续失败次数
        }


def _update_quota(account_name, size_delta):
    """更新账号额度（增量）"""
    with _lock:
        _ensure_account_state(account_name)
        state = _pool_state[account_name]
        state["total_size"] += size_delta
        state["last_report"] = time.time()
        # 检查预警
        usage = state["total_size"] / TURSO_QUOTA
        if usage >= TURSO_FULL_THRESHOLD:
            old = state["status"]
            state["status"] = "full"
            if old != "full":
                logger.warning(f"[turso_pool] 账号 {account_name} 已满 ({usage*100:.1f}%)")
        elif usage >= TURSO_WARN_THRESHOLD:
            old = state["status"]
            state["status"] = "warning"
            if old != "warning":
                logger.warning(f"[turso_pool] 账号 {account_name} 预警 ({usage*100:.1f}%)")
        else:
            state["status"] = "normal"
        _save_pool_state()


def report(inst_id, backup_type, size, account_name=None, status="ok", version=None, error=""):
    """实例上报存储状态"""
    with _lock:
        report_data = {
            "inst_id": inst_id,
            "type": backup_type,
            "size": size,
            "account": account_name,
            "status": status,
            "version": version,
            "error": error,
            "timestamp": time.time(),
        }
        _reports[inst_id] = _reports.get(inst_id, {})
        _reports[inst_id][backup_type] = report_data
        logger.info(f"[turso_pool] 实例 {inst_id} 上报: type={backup_type} size={size} "
                     f"account={account_name} status={status}")

        # 更新额度
        if account_name and status == "ok" and size > 0:
            _update_quota(account_name, size)

        # 存到Turso
        if turso.is_available():
            turso.put(f"{KEY_TURSO_REPORTS}{inst_id}", _reports[inst_id])


def assign_account(inst_id):
    """为实例分配Turso账号（选余量最大的）。返回账号dict或None。"""
    accounts = turso._load_accounts()
    if not accounts:
        logger.warning("[turso_pool] 无可用Turso账号")
        return None
    with _lock:
        best = None
        best_remaining = 0
        for acct in accounts:
            name = acct.get("name", "?")
            _ensure_account_state(name)
            state = _pool_state[name]
            if state["status"] in ("full", "unhealthy"):
                logger.info(f"[turso_pool] 跳过账号 {name} (status={state['status']})")
                continue
            remaining = TURSO_QUOTA - state["total_size"]
            if remaining > best_remaining:
                best = acct
                best_remaining = remaining
        if best:
            name = best.get("name", "?")
            state = _pool_state.get(name, {})
            logger.info(f"[turso_pool] 为实例 {inst_id} 分配账号 {name} "
                         f"(剩余 {best_remaining/1024/1024/1024:.2f}GB, status={state.get('status','normal')})")
        return best


def get_status():
    """返回所有Turso账号状态（给CLI和API用）"""
    accounts = turso._load_accounts()
    result = []
    for acct in accounts:
        name = acct.get("name", "?")
        _ensure_account_state(name)
        state = _pool_state.get(name, {})
        total = state.get("total_size", 0)
        usage_pct = total / TURSO_QUOTA * 100
        result.append({
            "name": name,
            "url": acct.get("url", ""),
            "token_masked": (acct.get("token", "")[:8] + "..." + acct.get("token", "")[-4:]) if len(acct.get("token", "")) > 12 else "***",
            "used_bytes": total,
            "used_mb": round(total / 1024 / 1024, 2),
            "quota_gb": TURSO_QUOTA / 1024 / 1024 / 1024,
            "usage_pct": round(usage_pct, 1),
            "status": state.get("status", "normal"),
            "last_report": state.get("last_report", 0),
        })
    return result


def get_config():
    """返回Turso配置（给实例轮询用）"""
    accounts = turso._load_accounts()
    return {
        "accounts": [
            {
                "name": a.get("name", "?"),
                "url": a.get("url", ""),
                "token": a.get("token", ""),
                "status": _pool_state.get(a.get("name", "?"), {}).get("status", "normal"),
            }
            for a in accounts
        ],
        "pool_state": _pool_state,
        "timestamp": time.time(),
    }


def mark_unhealthy(account_name):
    """标记账号为unhealthy（连接失败）"""
    with _lock:
        _ensure_account(account_name)
        state = _pool_state[account_name]
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] >= 3:
            state["status"] = "unhealthy"
            logger.warning(f"[turso_pool] 账号 {account_name} 标记为unhealthy (失败{state['fail_count']}次)")
        _save_pool_state()


def mark_healthy(account_name):
    """标记账号为healthy（连接恢复）"""
    with _lock:
        _ensure_account(account_name)
        state = _pool_state[account_name]
        state["fail_count"] = 0
        # 不直接改status，让额度检查决定
        _update_quota(account_name, 0)  # 0增量，只更新状态
        logger.info(f"[turso_pool] 账号 {account_name} 恢复healthy")


def init():
    """初始化（manager启动时调用）"""
    _load_pool_state()
    logger.info("[turso_pool] 账号池管理已启动")
