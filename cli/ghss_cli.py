# -*- coding: utf-8 -*-
"""
ghbox 管理客户端入口

用法：
  python3 -m cli.ghss_cli <EXEC_TOKEN> [MANAGER_URL] [INSTANCE_URL]
  python3 -m cli.ghss_cli --json <op> [args]   # 脚本模式

JSON 模式操作：instances / create / close <id> / accounts /
               add-account <name> <token> / tasks / logs [limit]
"""
import sys

from cli.common import config
from cli.mgr import ui, terminal


def _json_mode(args):
    """JSON 脚本模式：单次操作输出 JSON"""
    op = args[0] if args else "instances"
    from cli.common import api
    if op == "instances":
        print(json_dumps(api.get("/api/instances")))
    elif op == "create":
        print(json_dumps(api.post("/api/instances")))
    elif op == "close" and len(args) > 1:
        print(json_dumps(api.delete(f"/api/instances/{args[1]}")))
    elif op == "accounts":
        print(json_dumps(api.get("/api/accounts")))
    elif op == "add-account" and len(args) > 2:
        print(json_dumps(api.post("/api/accounts", {"name": args[1], "token": args[2]})))
    elif op == "tasks":
        print(json_dumps(api.get("/api/tasks")))
    elif op == "logs":
        limit = int(args[1]) if len(args) > 1 else 300
        print(json_dumps(api.get(f"/api/logs?limit={limit}")))
    else:
        print(json_dumps({"ok": False, "error": f"未知操作: {op}"}))


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def main():
    # JSON 模式（token 从环境变量取）
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        if not config.TOKEN:
            config.TOKEN = __import__("os").environ.get("EXEC_TOKEN", "")
        _json_mode(sys.argv[2:])
        return

    # 交互模式：argv[1] 为 token，argv[2] 可选 manager URL，argv[3] 可选实例 URL
    if len(sys.argv) > 1 and not sys.argv[1].startswith(("http", "--")):
        config.set_token(sys.argv[1])
    if not config.TOKEN:
        config.TOKEN = __import__("os").environ.get("EXEC_TOKEN", "")
    if not config.TOKEN:
        print("用法: ghss <EXEC_TOKEN> [MANAGER_URL] [INSTANCE_URL]")
        sys.exit(1)
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        if len(sys.argv) > 3 and sys.argv[3].startswith("http"):
            # manager + 实例
            config.set_manager(sys.argv[2])
            terminal.connect_terminal(sys.argv[3])
            return
        # 单个 URL：直接当实例连接终端
        terminal.connect_terminal(sys.argv[2])
        return
    ui.run_menu()


if __name__ == "__main__":
    main()