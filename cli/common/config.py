# -*- coding: utf-8 -*-
"""CLI 公共配置：token / manager 地址 / 会话持久化"""
import os
import uuid

MANAGER = os.environ.get("GHBOX_MANAGER", "https://ghvps2.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")

SESSION_FILE = os.path.expanduser("~/.ghbox_session")


def load_session():
    """读取或创建持久化 session_key（断线重连保持会话）"""
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    k = uuid.uuid4().hex
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(k)
    except Exception:
        pass
    return k


def set_manager(url):
    global MANAGER
    MANAGER = url.rstrip("/")


def set_token(token):
    global TOKEN
    TOKEN = token


def mgr(path):
    """拼接 manager API 路径"""
    return MANAGER.rstrip("/") + path