# -*- coding: utf-8 -*-
"""
进程恢复/启动/停止/重启（单一职责）

- 从 processes/<name>/app 恢复项目文件
- 执行依赖安装（install）
- 启动/停止/重启进程（后台，脱离终端，独立日志）
- 崩溃自恢复
"""
import os
import time
import signal
import subprocess

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.restore")


def restore_files(cfg):
    """恢复进程项目文件到 cwd"""
    name = cfg.get("name", "proc")
    if not cfg.get("files_backed", True):
        return True
    src = os.path.join(pconfig.proc_dir(), name, "app")
    cwd = cfg.get("cwd") or ""
    if not src or not os.path.isdir(src) or not cwd:
        return True
    os.makedirs(cwd, exist_ok=True)
    utils.copy_tree(src, cwd, set())
    return True


def install_deps(cfg):
    """执行依赖安装命令，失败抛异常"""
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    for cmd in cfg.get("install") or []:
        logger.info(f"[restore] {cfg['name']} 执行安装: {cmd}")
        code, out, err = utils.run_cmd(cmd, timeout=600, cwd=cwd)
        if code != 0:
            raise RuntimeError(f"安装失败: {err[:200]}")


def start_process(name, cfg=None):
    """启动进程（后台，脱离终端）。返回 True/False"""
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        logger.error(f"[start] {name} 无配置")
        return False
    command = cfg.get("command") or ""
    if not command:
        logger.error(f"[start] {name} 无启动命令")
        return False
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    env = dict(os.environ)
    for k, v in (cfg.get("env") or {}).items():
        env[k] = v
    # 独立日志文件
    try:
        import log as _log
        _, logpath = _log.process_logger(name)
        logf = open(logpath, "ab")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            command, shell=True, stdout=logf, stderr=subprocess.STDOUT,
            cwd=cwd, env=env, start_new_session=True, executable="/bin/bash")
        logger.info(f"[start] 已启动进程 {name} (pid={proc.pid}, cmd={command})")
        return True, proc.pid
    except Exception as e:
        logger.error(f"[start] {name} 启动失败: {e}")
        return False, None


def stop_process(name, cfg=None, pid=None):
    """停止进程（SIGTERM 后 SIGKILL）。返回 (ok, msg)"""
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        return False, "无配置"
    if not pid:
        pid = cfg.get("source_pid")
    if not pid or not utils.is_alive(pid):
        return False, "进程未运行"
    # 精准杀单进程（不用 killpg，避免误杀同进程组的其他进程如 cloudflared）
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1.5)
        if utils.is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    logger.info(f"[stop] 已停止进程 {name} (pid={pid})")
    return True, "已停止"


def restart_process(name, cfg=None):
    """重启进程"""
    stop_process(name, cfg=cfg)
    time.sleep(1)
    return start_process(name, cfg)


def restore_one(name, cfg=None):
    """
    恢复并启动单个进程（含重试）。
    返回 (ok, pid)。
    """
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        logger.error(f"[restore] {name} 无配置")
        return False, None
    retries = config.PROC_MAX_RETRY
    attempt = 0
    while attempt <= retries:
        try:
            restore_files(cfg)
            install_deps(cfg)
            return start_process(name, cfg)
        except Exception as e:
            attempt += 1
            if attempt <= retries:
                delay = config.PROC_RETRY_DELAY[min(attempt - 1,
                                                     len(config.PROC_RETRY_DELAY) - 1)]
                logger.warning(f"[restore] {name} 第{attempt}次失败: {e}，{delay}s后重试")
                time.sleep(delay)
            else:
                logger.error(f"[restore] {name} 最终失败: {e}")
    return False, None


def restore_all():
    """
    恢复并启动所有持久化进程。
    返回 (restored, failed)。
    """
    procs = pconfig.load_manifest()
    if not procs:
        logger.info("[restore] 无进程清单，跳过恢复")
        return 0, 0
    restored, failed = 0, 0
    for name in procs:
        ok, _ = restore_one(name)
        if ok:
            restored += 1
        else:
            failed += 1
    logger.info(f"[restore] 恢复完成: {restored} 成功, {failed} 失败")
    return restored, failed