# -*- coding: utf-8 -*-
"""
通用工具模块：时间、命令执行、文件操作、网络、进程信息等
"""
import os
import re
import time
import json
import shutil
import socket
import subprocess
import threading
import datetime
import urllib.request
import urllib.error

import log

logger = log.setup_logger("utils")


# ==================== 时间 ====================
def utc_now():
    """当前 UTC 时间戳"""
    return time.time()


def utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ts_to_iso(ts):
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    except Exception:
        return ""


# ==================== 命令执行 ====================
def run_cmd(cmd, timeout=30, shell=True, capture=True, cwd=None, env=None):
    """
    执行命令，返回 (code, stdout, stderr)。
    带超时与异常兜底，绝不抛未捕获异常。
    """
    try:
        r = subprocess.run(
            cmd if shell else cmd.split(),
            shell=shell, capture_output=capture, text=True,
            timeout=timeout, cwd=cwd, env=env)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


def run_cmd_bg(cmd, logfile=None, cwd=None, env=None):
    """
    后台执行命令（脱离终端，start_new_session）。
    返回 Popen 对象；logfile 指定则重定向输出。
    """
    try:
        if logfile:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
            f = open(logfile, "ab")
            return subprocess.Popen(cmd, shell=True, stdout=f, stderr=subprocess.STDOUT,
                                    start_new_session=True, cwd=cwd, env=env)
        return subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                cwd=cwd, env=env)
    except Exception as e:
        logger.error(f"[utils] 后台启动失败: {e}")
        return None


def is_alive(pid):
    """判断 PID 是否存活"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ==================== 文件 ====================
def safe_makedirs(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        logger.warning(f"[utils] 创建目录失败 {path}: {e}")
        return False


def safe_remove(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def dir_size_mb(path):
    """计算目录大小(MB)，忽略错误"""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total / (1024 * 1024)


def copy_tree(src, dst, exclude=None):
    """复制目录树，支持排除子路径。返回复制文件数。"""
    exclude = set(exclude or [])
    count = 0
    try:
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            dirs[:] = [d for d in dirs if d not in exclude and rel not in exclude]
            for d in dirs:
                safe_makedirs(os.path.join(dst, rel, d))
            for f in files:
                if rel in exclude:
                    continue
                s = os.path.join(root, f)
                d = os.path.join(dst, rel, f)
                try:
                    safe_makedirs(os.path.dirname(d))
                    shutil.copy2(s, d)
                    count += 1
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[utils] 复制失败: {e}")
    return count


# ==================== 网络 ====================
def http_get(url, timeout=15, headers=None):
    """GET 请求，返回 (status, body_bytes)。异常捕获。"""
    h = {"User-Agent": "Mozilla/5.0 (ghbox)"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def http_json(method, url, token=None, data=None, timeout=30, headers=None):
    """HTTP JSON 请求，返回 (status, parsed_json_or_str)。"""
    h = {"User-Agent": "Mozilla/5.0 (ghbox)",
         "Content-Type": "application/json",
         "Accept": "application/vnd.github.v3+json"}
    if token:
        h["Authorization"] = f"token {token}"
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode() or "null")
            except Exception:
                return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode() or "null")
        except Exception:
            return e.code, raw.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def get_public_ip():
    """获取公网 IP（尽力而为）"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            status, body = http_get(url, timeout=8)
            if status == 200:
                ip = body.decode().strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    return ip
        except Exception:
            continue
    return ""


# ==================== 线程工具 ====================
def spawn_thread(target, args=(), kwargs=None, name=None, daemon=True):
    """安全启动后台线程"""
    t = threading.Thread(target=target, args=args, kwargs=kwargs or {},
                         name=name, daemon=daemon)
    t.start()
    return t


# ==================== 系统信息 ====================
def system_info():
    """收集系统基本信息（主机名/内核/CPU数/内存）"""
    info = {}
    try:
        info["hostname"] = socket.gethostname()
    except Exception:
        info["hostname"] = ""
    try:
        with open("/proc/uptime") as f:
            info["uptime"] = float(f.read().split()[0])
    except Exception:
        pass
    try:
        info["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except Exception:
        pass
    return info