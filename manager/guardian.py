# -*- coding: utf-8 -*-
"""
保命管理器（Survival Guardian，manager 侧）

- 检测 GitHub Actions 故障 → 切换保命模式（Codespaces）
- 保命账号自动选择（健康度最高）
- 配额监控：快用完时无缝切换下一个健康账号
- 探测 Actions 恢复后自动迁回
"""
import time
import threading

import config
import log
from core import ghapi
from core import status as core_status
from core import storage

logger = log.setup_logger("guardian")


class Guardian:
    def __init__(self, accounts_provider=None):
        self.accounts_provider = accounts_provider or (lambda: [])
        self.survival_active = False
        self.current_survival_account = None
        self.survival_codespace = None
        self.last_reason = None
        self._lock = threading.Lock()

    def _get_accounts(self):
        return self.accounts_provider() or []

    def _pick_survival_account(self, exclude=None):
        accounts = self._get_accounts()
        if exclude:
            accounts = [a for a in accounts if a["name"] != exclude]
        return ghapi.select_best_survival_account(accounts)

    def _get_repo_id(self, repo, token):
        status, d = ghapi.gh_request("GET", f"{ghapi.API_BASE}/repos/{repo}", token=token)
        return d.get("id") if status == 200 else 0

    def _create_codespace(self, account):
        """用账号创建保命 Codespace"""
        try:
            repo = account.get("repo") or config.REPO
            url = "https://api.github.com/user/codespaces"
            data = {
                "repository_id": self._get_repo_id(repo, account["token"]),
                "ref": "main",
                "devcontainer_path": config.SURVIVAL_DEVCONTAINER,
                "machine": "basicLinux32gb",
                "geo": "UsWest",
            }
            status, d = ghapi.gh_request("POST", url, token=account["token"], data=data)
            if status in (200, 201, 202):
                logger.info(f"[guardian] Codespace 创建成功: {d.get('name')}")
                return d
            logger.error(f"[guardian] Codespace 创建失败: {status} {d}")
        except Exception as e:
            logger.error(f"[guardian] Codespace 创建异常: {e}")
        return None

    def enter_survival(self):
        """进入保命模式"""
        with self._lock:
            if self.survival_active:
                return
            acc = self._pick_survival_account()
            if not acc:
                logger.error("[guardian] 无健康账号，保命失败（所有账号配额不足）")
                return
            cs = self._create_codespace(acc)
            if not cs:
                logger.error("[guardian] Codespace 创建失败，保命未启动")
                return
            self.survival_active = True
            self.current_survival_account = acc
            self.survival_codespace = cs
            logger.warning(f"[guardian] 进入保命模式（账号 {acc['name']}，"
                           f"Codespace {cs.get('name')}）")

    def check_quota_switch(self):
        """保命账号配额不足时切换"""
        if not self.survival_active or not self.current_survival_account:
            return
        health, detail = ghapi.estimate_account_quota(self.current_survival_account)
        if health >= config.QUOTA_SWITCH_THRESHOLD:
            return
        logger.warning(f"[guardian] 保命账号 {self.current_survival_account['name']} "
                       f"配额不足 (健康度 {health:.2f})，切换账号")
        next_acc = self._pick_survival_account(exclude=self.current_survival_account["name"])
        if not next_acc:
            logger.error("[guardian] 无下一个健康账号，保持当前保命")
            return
        cs = self._create_codespace(next_acc)
        if cs:
            old = self.current_survival_account["name"]
            self.current_survival_account = next_acc
            self.survival_codespace = cs
            logger.info(f"[guardian] 保命账号 {old} → {next_acc['name']} 无缝切换")

    def _trigger_actions_recovery(self):
        """触发 Actions 恢复（manager + 各 worker）"""
        accounts = self._get_accounts()
        try:
            url = f"{ghapi.API_BASE}/repos/{config.MAIN_REPO}/actions/workflows/{config.MANAGER_WORKFLOW}/dispatches"
            ghapi.gh_request("POST", url, data={"ref": "main"})
            logger.info("[guardian] 已触发 manager 恢复")
        except Exception as e:
            logger.error(f"[guardian] 触发 manager 恢复失败: {e}")
        try:
            insts = storage.load_json_enc(config.ASSET_INSTANCES, default=[])
            for inst in insts:
                acc = next((a for a in accounts if a["name"] == inst.get("account")), None)
                if acc and not inst.get("closed"):
                    repo = acc.get("repo") or config.REPO
                    url = f"{ghapi.API_BASE}/repos/{repo}/actions/workflows/{config.WORKER_WORKFLOW}/dispatches"
                    ghapi.gh_request("POST", url, token=acc.get("token"),
                                     data={"ref": "main", "inputs": {"INSTANCE_ID": inst["id"]}})
            logger.info("[guardian] 已触发 worker 恢复")
        except Exception as e:
            logger.error(f"[guardian] 触发 worker 恢复失败: {e}")

    def recover_to_actions(self):
        """Actions 恢复，迁回"""
        with self._lock:
            if not self.survival_active:
                return
            logger.info("[guardian] Actions 已恢复，迁回")
            self._trigger_actions_recovery()
            self.survival_active = False
            self.current_survival_account = None
            self.survival_codespace = None

    def monitor_loop(self):
        while True:
            try:
                ok, reason = core_status.check_actions()
                self.last_reason = reason
                if ok:
                    if self.survival_active:
                        self.recover_to_actions()
                else:
                    if not self.survival_active:
                        logger.warning(f"[guardian] Actions 故障（{reason}），进入保命")
                        self.enter_survival()
                    else:
                        self.check_quota_switch()
            except Exception as e:
                logger.error(f"[guardian] 监控异常: {e}")
            time.sleep(config.SURVIVAL_CHECK_INTERVAL)

    def start(self):
        threading.Thread(target=self.monitor_loop, daemon=True).start()
        logger.info("[guardian] 保命监控已启动")


def create_guardian(accounts_provider=None):
    g = Guardian(accounts_provider)
    g.start()
    return g