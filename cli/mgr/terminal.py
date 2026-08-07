# -*- coding: utf-8 -*-
"""
WSS 交互式终端客户端（精简静默版）

特性：
- 完全静默重连：意外断线自动重连（最多 3 次），用户无感知
- 重连成功后自动恢复窗口尺寸 + 拉取干净屏幕（无缝衔接）
- 重连彻底失败才提示退出
- 无任何多余提示（不显示快捷键说明等）

快捷键：
- Ctrl+C  传给远程终端（中断命令，不退出）
- Ctrl+4  强制退出终端（返回菜单）
- Ctrl+O  复制干净屏幕
"""
import os
import sys
import time
import tty
import struct
import fcntl
import termios
import threading
import urllib.request
import urllib.error

import socketio

from cli.common import config

# 最大重连次数
MAX_RECONNECT = 3

# 快捷键
KEY_CTRL_4 = b"\x1c"
KEY_CTRL_O = b"\x0f"


def _get_term_size():
    """获取终端行列数"""
    try:
        return struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0"))[:2]
    except Exception:
        return 35, 120


def _get_clean_screen(url, session):
    """拉取干净屏幕（重连后恢复显示 / Ctrl+O 复制）"""
    try:
        req = urllib.request.Request(
            url.rstrip("/") + f"/api/term/screen?session={session}",
            headers={"User-Agent": "Mozilla/5.0 (ghbox-cli)",
                     "Authorization": f"Bearer {config.TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json_loads(r.read().decode())
            if d.get("ok"):
                return d.get("screen", "")
    except Exception:
        pass
    return ""


def json_loads(s):
    import json
    return json.loads(s)


def connect_terminal(host):
    """
    连接实例 WSS 终端（静默重连，无啰嗦提示）。
    host: 实例域名或完整 URL。
    """
    url = f"https://{host}" if not host.startswith("http") else host
    session = config.load_session()
    state = {"force_exit": False}

    sio = socketio.Client(reconnection=False)

    # ==================== 事件（全部静默） ====================
    @sio.on("output")
    def on_output(data):
        try:
            if isinstance(data, bytes):
                sys.stdout.buffer.write(data)
            else:
                sys.stdout.write(data)
            sys.stdout.flush()
        except Exception:
            pass

    @sio.on("exit")
    def on_exit(data):
        try:
            sio.disconnect()
        except Exception:
            pass

    @sio.event
    def connect():
        # 完全静默重连：只恢复窗口尺寸，不输出干净屏幕
        # （避免在正在输入的行上插入换行/重复提示符，造成突兀）
        try:
            rows, cols = _get_term_size()
            sio.emit("resize", {"rows": rows, "cols": cols})
        except Exception:
            pass

    @sio.event
    def disconnect():
        pass  # 完全静默

    # ==================== 发送循环 ====================
    def send_loop():
        try:
            while True:
                ch = os.read(0, 1)
                if not ch:
                    break
                if ch == KEY_CTRL_4:  # 主动退出
                    state["force_exit"] = True
                    try:
                        sio.disconnect()
                    except Exception:
                        pass
                    break
                if ch == KEY_CTRL_O:  # 复制干净屏幕
                    screen = _get_clean_screen(url, session)
                    if screen:
                        sys.stdout.write("\r\n" + screen + "\r\n")
                        sys.stdout.flush()
                    continue
                # 其余字节传给远程
                try:
                    sio.emit("input", ch)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass

    def _connect():
        # 先检查实例是否可用
        try:
            req = urllib.request.Request(url.rstrip("/") + "/api/health",
                headers={"User-Agent": "Mozilla/5.0 (ghbox-cli)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status != 200:
                    raise ConnectionError(f"实例返回 {r.status}")
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"实例返回 {e.code}")
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接实例: {e.reason}")
        sio.connect(url, auth={"token": config.TOKEN, "session": session},
                    transports=["websocket"], wait_timeout=25)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # 首次连接
        try:
            _connect()
        except Exception as e:
            sys.stderr.write(f"\r\n[连接失败] {e}\r\n")
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            return
        threading.Thread(target=send_loop, daemon=True).start()

        # 主循环：轮询连接状态，断开则静默重连
        while not state["force_exit"]:
            # 等待连接断开（sio.connected 为 False）
            while sio.connected and not state["force_exit"]:
                time.sleep(0.5)
            if state["force_exit"]:
                break
            # 断开：静默重连（最多 3 次，递增退避）
            reconnected = False
            for attempt in range(1, MAX_RECONNECT + 1):
                if state["force_exit"]:
                    break
                time.sleep(attempt * 2)  # 2s/4s/6s
                try:
                    _connect()
                    reconnected = True
                    break
                except Exception:
                    continue
            if not reconnected and not state["force_exit"]:
                state["force_exit"] = True
                sys.stderr.write(
                    f"\r\n[连接失败] 已重试 {MAX_RECONNECT} 次仍无法连接，退出终端\r\n")
                sys.stderr.flush()
    except KeyboardInterrupt:
        state["force_exit"] = True
    except Exception as e:
        state["force_exit"] = True
        sys.stderr.write(f"\r\n[终端错误] {e}\r\n")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if state["force_exit"]:
            try:
                sio.disconnect()
            except Exception:
                pass