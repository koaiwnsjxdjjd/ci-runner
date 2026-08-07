# -*- coding: utf-8 -*-
"""
ghbox 统一日志系统（生产级增强版）

特性：
- 四级输出：控制台 + 内存环形缓冲（可查询）+ 文件（自动轮转）+ 可选 JSON
- 结构化字段：时间 / 级别 / 模块 / 事件ID / JOB_ID / 耗时
- 请求日志：自动记录每个 API 调用（方法/路径/耗时/状态/来源IP）
- 进程日志：持久化进程独立日志文件
- 资源监控：CPU/内存/磁盘 周期性采样
- 日志查询：按级别/模块/关键词/时间段过滤
"""
import os
import time
import json
import threading
import logging
import subprocess
from logging.handlers import RotatingFileHandler

import config

# ==================== 配置 ====================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_RING_LINES = int(os.environ.get("LOG_RING_LINES", "5000"))
LOG_FILE = os.path.join(os.path.expanduser("~"), "ghbox.log")
LOG_FILE_MAX_BYTES = int(os.environ.get("LOG_FILE_MAX_MB", "10")) * 1024 * 1024
LOG_FILE_BACKUP = int(os.environ.get("LOG_FILE_BACKUP", "5"))
JOB_ID = os.environ.get("GHBOX_JOB_ID", "")

# 内存环形缓冲
_ring = []                 # 每项: {"time": ts, "level": str, "module": str, "msg": str, "job": str}
_ring_lock = threading.Lock()
_stats = {"error": 0, "warning": 0, "info": 0, "request": 0}
_stats_lock = threading.Lock()


class RingBufferHandler(logging.Handler):
    """内存环形缓冲 handler（可查询，带模块/级别）"""

    def emit(self, record):
        try:
            msg = self.format(record)
            entry = {
                "time": time.time(),
                "level": record.levelname,
                "module": getattr(record, "module", ""),
                "job": getattr(record, "job", JOB_ID),
                "msg": msg,
            }
            with _ring_lock:
                _ring.append(entry)
                if len(_ring) > MAX_RING_LINES:
                    del _ring[:len(_ring) - MAX_RING_LINES]
            with _stats_lock:
                if record.levelno >= logging.ERROR:
                    _stats["error"] += 1
                elif record.levelno >= logging.WARNING:
                    _stats["warning"] += 1
                else:
                    _stats["info"] += 1
        except Exception:
            pass


class ContextFilter(logging.Filter):
    """为日志记录附加 module 属性（取 logger 名最后一段）"""

    def filter(self, record):
        name = getattr(record, "name", "")
        record.module = name.split(".")[-1] if name else ""
        record.job = JOB_ID
        return True


# ==================== Logger 工厂 ====================
_loggers = {}
_loggers_lock = threading.Lock()


def setup_logger(name="ghbox"):
    """获取/创建统一 logger（共享同一套 handler）"""
    with _loggers_lock:
        if name in _loggers:
            return _loggers[name]
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.setLevel(LOG_LEVEL)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(module)s: %(message)s",
                "%Y-%m-%d %H:%M:%S")
            # 控制台
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            logger.addHandler(ch)
            # 内存环形缓冲
            rb = RingBufferHandler()
            rb.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(rb)
            # 文件（自动轮转）
            try:
                fh = RotatingFileHandler(
                    LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES,
                    backupCount=LOG_FILE_BACKUP, encoding="utf-8")
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            except Exception:
                pass
            logger.addFilter(ContextFilter())
        _loggers[name] = logger
        return logger


def get_logger(module=None):
    """模块日志入口：logger = log.get_logger(__name__)"""
    return setup_logger(module or "ghbox")


# ==================== 日志查询 API ====================
def get_logs(limit=500, level=None, module=None, keyword=None, since=None):
    """
    查询最近日志。
    level: None=全部, 或 "INFO"/"WARNING"/"ERROR"
    module: 按模块过滤（子串匹配）
    keyword: 按关键词过滤（子串匹配）
    since: 起始时间戳
    """
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_lv = levels.get(level, 0)
    with _ring_lock:
        entries = list(_ring)
    result = []
    for e in entries:
        if min_lv and levels.get(e.get("level"), 20) < min_lv:
            continue
        if module and module.lower() not in e.get("module", "").lower():
            continue
        if keyword and keyword.lower() not in e.get("msg", "").lower():
            continue
        if since and e.get("time", 0) < since:
            continue
        result.append(e)
    return result[-limit:]


def get_stats():
    """错误/警告/请求统计"""
    with _stats_lock:
        return dict(_stats)


def reset_stats():
    """重置统计（用于周期报告）"""
    with _stats_lock:
        _stats.clear()
        _stats.update({"error": 0, "warning": 0, "info": 0, "request": 0})


# ==================== 请求日志（异步，不阻塞响应） ====================
import queue as _queue
_req_log_queue = _queue.Queue(maxsize=5000)


def request_logger(app):
    """Flask 请求日志：异步记录 method/路径/耗时/状态/来源IP（脱敏）"""
    from flask import request

    def _client_ip():
        ip = request.headers.get("CF-Connecting-IP", "")
        if not ip:
            ip = request.remote_addr or ""
        parts = ip.split(".")
        if len(parts) == 4:
            ip = ".".join(parts[:3]) + ".x"
        return ip

    # 启动异步写日志线程（只启动一次）
    if not getattr(request_logger, "_started", False):
        request_logger._started = True
        threading.Thread(target=_req_log_writer, daemon=True).start()

    @app.before_request
    def _start():
        request.environ["_req_start"] = time.time()

    @app.after_request
    def _end(response):
        start = request.environ.get("_req_start", time.time())
        dur = (time.time() - start) * 1000
        try:
            _req_log_queue.put_nowait(
                (request.method, request.path, response.status_code, dur, _client_ip()))
        except _queue.Full:
            pass  # 队列满则丢弃（不影响响应）
        with _stats_lock:
            _stats["request"] += 1
        return response

    return app


def _req_log_writer():
    """请求日志后台写线程"""
    lg = get_logger("api")
    while True:
        try:
            item = _req_log_queue.get()
            if item is None:
                break
            method, path, status, dur, ip = item
            lg.info("%s %s -> %d (%.0fms) ip=%s", method, path, status, dur, ip)
        except Exception:
            time.sleep(0.1)


# ==================== 进程日志 ====================
def process_logger(name):
    """为持久化进程创建独立日志文件（logs/<name>.log）"""
    logs_dir = os.path.join(config.LOGS_DIR)
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"{name}.log")
    lg = logging.getLogger(f"proc.{name}")
    if lg.handlers:
        return lg, path
    lg.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except Exception:
        pass
    return lg, path


def read_process_log(name, limit=200):
    """读取进程日志文件内容"""
    path = os.path.join(config.LOGS_DIR, f"{name}.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-limit:]
    except Exception:
        return []


# ==================== 资源监控 ====================
def get_resource_stats():
    """获取 CPU/内存/磁盘 使用情况"""
    result = {}
    # CPU / 内存（读 /proc/stat 与 /proc/meminfo）
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        total = sum(int(x) for x in fields[1:] if x.isdigit())
        idle = int(fields[4]) if len(fields) > 4 else 0
        result["cpu_total"] = total
        result["cpu_idle"] = idle
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        result["mem_total_kb"] = mem.get("MemTotal", 0)
        result["mem_avail_kb"] = mem.get("MemAvailable", mem.get("MemFree", 0))
    except Exception:
        pass
    # 磁盘
    try:
        r = subprocess.run(["df", "-k", os.path.expanduser("~")],
                           capture_output=True, text=True, timeout=10)
        lines = r.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            result["disk_total_kb"] = int(parts[1])
            result["disk_used_kb"] = int(parts[2])
            result["disk_avail_kb"] = int(parts[3])
            result["disk_use_pct"] = float(parts[4].rstrip("%"))
    except Exception:
        pass
    return result