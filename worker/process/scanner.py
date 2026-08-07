# -*- coding: utf-8 -*-
"""
进程扫描与识别（单一职责）

- 扫描 /proc 下所有进程
- 过滤系统进程 / 自身进程 / 自身隧道 / 攻击进程
- 识别自身 worker 进程 PID 集合（供隧道判定）

识别规则（基于实测验证）：
  排除：内核线程 / 系统服务(黑名单) / GitHub Runner 基础设施
       / 自身进程(app.py|worker.py|manager.py|survival.py)
       / 自身 worker 直接子进程(cloudflared tunnel 且 PPID==自身worker)
       / 攻击进程(attacker)
  保留：其余全部（用户进程、用户自起的 cloudflared/ngrok 等）
"""
import os
import pwd

import log

logger = log.setup_logger("proc.scan")

# 自身入口（cmdline 关键词）
SELF_ENTRIES = ("app.py", "worker.py", "manager.py", "survival.py", "ghbox/")

# 系统进程黑名单（cmdline 关键词）
SYSTEM_BLACKLIST = (
    "systemd", "/sbin/init", "init ", "journald", "udevd", "resolved", "networkd", "dbus-daemon",
    "polkitd", "systemd-logind", "rsyslogd", "cron", "chronyd", "haveged",
    "hv_kvp", "sshd", "agetty", "getty", "modemmanager", "multipathd",
    "udisks", "snapd", "containerd", "kubelet", "flanneld", "kube-proxy",
    "Runner.Listener", "Runner.Worker", "hosted-compute-agent", "provjobd",
    "networkd-dispatcher", "sd-pam", "systemd --user", "atd", "irqbalance",
    "acpid", "crond", "dbus", "polkit", "unattended-upgr", "apt", "dpkg",
    "cloud-init", "waagent", "walinuxagent", "agent", "msft", "azure",
    "tuned", "rhsm", "auditd", "rsyslog", "syslog", "haveged", "irq",
    "kthreadd", "ksoftirqd", "migration", "cpuhp", "rcu_", "kworker",
    "runner/work/_temp", "perl", "systemctl", "sudo ", "sudo -", "bash -e",
    "watchdogd", "kswapd", "scsi_eh", "nvme", "hv_balloon", "kcompactd",
    "khugepaged", "ksmd", "oom_reaper", "kauditd", "khungtaskd", "kdevtmpfs",
    "ecryptfs", "idle_inject", "cpuhp", "migration", "perf", "trace",
)


class ProcessInfo:
    """单个用户进程的信息"""

    def __init__(self, pid, ppid, user, cmdline, exe, cwd):
        self.pid = pid
        self.ppid = ppid
        self.user = user
        self.cmdline = cmdline          # 完整命令行（list）
        self.exe = exe
        self.cwd = cwd
        self.name = self._gen_name()

    def _gen_name(self):
        """生成进程名（cwd 最后一段 + 首命令，保证可读）"""
        base = ""
        if self.cwd and self.cwd != "/":
            base = os.path.basename(self.cwd.rstrip("/"))
        cmd0 = self.cmdline[0] if self.cmdline else "proc"
        cmd_base = os.path.basename(cmd0).split(".")[0] if cmd0 else "proc"
        if base and base not in cmd_base:
            return f"{base}-{cmd_base}"
        return cmd_base or f"proc-{self.pid}"

    def cmdline_str(self):
        return " ".join(self.cmdline)

    def to_dict(self):
        return {
            "pid": self.pid, "ppid": self.ppid, "user": self.user,
            "cmdline": self.cmdline, "exe": self.exe, "cwd": self.cwd,
            "name": self.name,
        }


def _read_proc(pid):
    """读取单个进程信息，失败返回 None"""
    try:
        pid = int(pid)
    except Exception:
        return None
    # cmdline
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        cmdline = raw.split()
    except Exception:
        return None
    if not cmdline:
        return None  # 内核线程
    # ppid
    ppid = 0
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
            ppid = int(parts[3]) if len(parts) > 3 else 0
    except Exception:
        pass
    # user
    user = ""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    try:
                        user = pwd.getpwuid(uid).pw_name
                    except Exception:
                        user = str(uid)
                    break
    except Exception:
        pass
    # exe / cwd
    exe = ""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except Exception:
        pass
    cwd = ""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        pass
    return ProcessInfo(pid, ppid, user, cmdline, exe, cwd)


def find_self_worker_pids():
    """
    识别自身 ghbox 进程 PID 集合（app.py/worker.py/manager.py/survival.py）。
    返回 set[int]
    """
    pids = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            info = _read_proc(pid)
            if not info:
                continue
            cmd = info.cmdline_str()
            if any(e in cmd for e in SELF_ENTRIES):
                pids.add(info.pid)
    except Exception as e:
        log.setup_logger("process.scan").warning(f"识别自身进程失败: {e}")
    return pids


def is_system(info, worker_pids):
    """判断是否系统进程（需过滤）"""
    cmd = info.cmdline_str().lower()
    if cmd.startswith("["):
        return True  # 内核线程
    for entry in SELF_ENTRIES:
        if entry in cmd:
            return True  # 自身入口
    for kw in SYSTEM_BLACKLIST:
        if kw.lower() in cmd:
            return True
    # 自身 worker 直接子进程：cloudflared 隧道（PPID 是自身 worker）
    if "cloudflared" in cmd and info.ppid in worker_pids:
        return True
    # 攻击进程
    if "attacker" in cmd:
        return True
    return False


def scan_user_processes():
    """
    扫描并返回用户进程列表。
    仅保留工作目录在持久化目录（~/files）下的用户进程，
    避免误备份 runner 临时脚本 / 系统命令 / 项目目录自身。
    """
    worker_pids = find_self_worker_pids()
    # 持久化目录（~/files）
    import config as _config
    files_dir = os.path.realpath(os.path.expanduser(_config.FILES_DIR))
    user_procs = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            info = _read_proc(pid)
            if not info:
                continue
            if is_system(info, worker_pids):
                continue
            # 关键：只保留 cwd 在持久化目录下的进程（用户项目）
            if not info.cwd:
                continue
            cwd_real = os.path.realpath(info.cwd)
            if not (cwd_real == files_dir or cwd_real.startswith(files_dir + os.sep)):
                continue
            user_procs.append(info)
    except Exception as e:
        log.setup_logger("process.scan").error(f"扫描失败: {e}")
    return user_procs