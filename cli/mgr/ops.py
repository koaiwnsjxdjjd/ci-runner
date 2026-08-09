# -*- coding: utf-8 -*-
"""管理客户端操作（实例/账号/任务/日志/Turso/终端连接）"""
from cli.mgr import ui
from cli.common import api


def list_instances():
    """列出实例"""
    d = api.get("/api/instances")
    insts = d.get("instances", [])
    if not insts:
        print("  暂无实例")
        return []
    print(f"  {'ID':<8}{'域名':<30}{'状态':<10}{'账号':<10}")
    print("  " + "-" * 60)
    for i in insts:
        print(f"  {i['id']:<8}{i.get('hostname',''):<30}{i.get('status',''):<10}{i.get('account','')}")
    return insts


def create_instance():
    """创建实例（支持指定账号）"""
    d = api.get("/api/accounts")
    accounts = d.get("accounts", [])
    if not accounts:
        print("  没有可用账号，请先添加账号")
        return
    print("  可用账号:")
    for idx, a in enumerate(accounts):
        print(f"    [{idx}] {a['name']} ({a.get('repo','')})")
    print(f"    [auto] 自动选择最优账号")
    sel = ui._input("  选择账号（序号/auto，留空取消）: ")
    if sel is None:
        print("  已取消")
        return
    sel = sel.lower()
    payload = {}
    if sel != "auto":
        try:
            idx = int(sel)
            payload = {"account": accounts[idx]["name"]}
        except Exception:
            print("  无效选择，使用自动选择")
    print("  正在创建新实例（自动配置隧道+启动）...")
    d = api.post("/api/instances", payload)
    if d.get("ok"):
        inst = d.get("instance", {})
        print(f"  创建成功: {inst.get('id')} -> https://{inst.get('hostname')}")
        if inst.get("mcp_url"):
            print(f"     MCP: {inst.get('mcp_url')}")
    else:
        print(f"  失败: {d.get('error')}")


def close_instance():
    """关闭实例"""
    insts = list_instances()
    if not insts:
        return
    inst_id = ui._input("  输入要关闭的实例 ID（留空取消）: ")
    if inst_id is None:
        print("  已取消")
        return
    d = api.delete(f"/api/instances/{inst_id}")
    if d.get("ok"):
        print(f"  {d.get('msg','')}")
    else:
        print(f"  失败: {d.get('error','')}")


def add_account():
    """添加账号（全自动）"""
    print("  （全自动：验证token->fork->secrets->报备，任务化执行）")
    name = ui._input("  账号名称（留空取消）: ")
    if name is None:
        print("  已取消")
        return
    token = ui._input("  GitHub Token（留空取消）: ")
    if token is None:
        print("  已取消")
        return
    d = api.post("/api/accounts", {"name": name, "token": token})
    if d.get("ok"):
        print(f"  {d.get('msg')}")
        print("  任务已入队，可查看 [7] 任务队列")
    else:
        print(f"  失败: {d.get('error')}")


def list_accounts():
    """列出账号"""
    d = api.get("/api/accounts")
    accounts = d.get("accounts", [])
    if not accounts:
        print("  暂无账号")
        return
    print(f"  {'名称':<10}{'仓库':<35}{'并发':<6}")
    print("  " + "-" * 55)
    for a in accounts:
        print(f"  {a['name']:<10}{a.get('repo',''):<35}{a.get('max_concurrency','')}")


def list_tasks():
    """查看任务队列"""
    d = api.get("/api/tasks")
    tasks = d.get("tasks", [])
    if not tasks:
        print("  暂无任务")
        return
    print(f"  {'ID':<22}{'类型':<15}{'状态':<10}{'重试':<4}{'错误':<30}")
    print("  " + "-" * 80)
    for t in tasks[-20:]:
        print(f"  {t.get('id',''):<22}{t.get('type',''):<15}{t.get('status',''):<10}"
              f"{t.get('retries',0):<4}{t.get('error','')[:28]}")


def view_logs():
    """查看服务器日志"""
    limit_str = ui._input("  查看最近多少行日志（默认300，留空取消）: ")
    if limit_str is None:
        print("  已取消")
        return
    try:
        limit = int(limit_str) if limit_str else 300
    except ValueError:
        limit = 300
    limit = max(10, min(limit, 2000))
    d = api.get(f"/api/logs?limit={limit}")
    if not d.get("ok"):
        print(f"  {d.get('error')}")
        return
    stats = d.get("stats", {})
    print(f"  统计: 错误 {stats.get('error',0)} | 警告 {stats.get('warning',0)}")
    print("  " + "-" * 80)
    for entry in d.get("logs", []):
        if isinstance(entry, dict):
            print(f"  [{entry.get('level','')}] {entry.get('msg','')}")
        else:
            print(f"  {entry}")


def pick_and_connect():
    """选择实例并连接终端"""
    d = api.get("/api/instances")
    insts = [i for i in d.get("instances", []) if i.get("status") in ("running", "starting")]
    if not insts:
        print("  没有可连接的实例")
        return
    for idx, i in enumerate(insts):
        print(f"  [{idx}] {i['id']} -> https://{i.get('hostname')} ({i.get('status')})")
    sel = ui._input("  选择实例序号（留空取消）: ")
    if sel is None:
        print("  已取消")
        return
    try:
        inst = insts[int(sel)]
    except (ValueError, IndexError):
        print("  无效选择")
        return
    print(f"  连接 {inst['id']} ... (Ctrl+4 退出)")
    from cli.mgr import terminal
    terminal.connect_terminal(f"https://{inst.get('hostname')}")


# ==================== Turso 管理 ====================
def _import_turso():
    """导入 turso 模块"""
    import sys, os
    ghbox_path = os.path.expanduser("~/ghbox")
    if ghbox_path not in sys.path:
        sys.path.insert(0, ghbox_path)
    from core import turso
    return turso


def list_turso_accounts():
    """列出 Turso 账号"""
    try:
        turso = _import_turso()
        accounts = turso.list_accounts()
        if not accounts:
            print("  暂无 Turso 账号")
            return
        print(f"  {'名称':<15}{'URL':<50}{'Token':<15}")
        print("  " + "-" * 80)
        for a in accounts:
            print(f"  {a['name']:<15}{a.get('url',''):<50}{a.get('token_masked','')}")
    except Exception as e:
        print(f"  错误: {e}")


def add_turso_account():
    """添加 Turso 账号"""
    name = ui._input("  账号名称（留空取消）: ")
    if name is None:
        print("  已取消")
        return
    url = ui._input("  Turso URL（libsql://...，留空取消）: ")
    if url is None:
        print("  已取消")
        return
    token = ui._input("  Turso Token（留空取消）: ")
    if token is None:
        print("  已取消")
        return
    try:
        turso = _import_turso()
        ok = turso.add_account(name, url, token)
        if ok:
            print(f"  Turso 账号 {name} 添加成功（热更新，无需重启）")
            # 测试连接
            print("  正在测试连接...")
            test_ok, detail = turso.test_account(name)
            if test_ok:
                print(f"  连接正常: {detail}")
            else:
                print(f"  连接失败: {detail}")
        else:
            print(f"  添加失败")
    except Exception as e:
        print(f"  错误: {e}")


def remove_turso_account():
    """删除 Turso 账号"""
    try:
        turso = _import_turso()
        accounts = turso.list_accounts()
        if not accounts:
            print("  暂无 Turso 账号")
            return
        print("  Turso 账号列表:")
        for idx, a in enumerate(accounts):
            print(f"    [{idx}] {a['name']} -> {a.get('url','')}")
        sel = ui._input("  选择要删除的序号（留空取消）: ")
        if sel is None:
            print("  已取消")
            return
        try:
            name = accounts[int(sel)]["name"]
        except (ValueError, IndexError):
            print("  无效选择")
            return
        ok = turso.remove_account(name)
        if ok:
            print(f"  Turso 账号 {name} 已删除（热更新，无需重启）")
        else:
            print(f"  账号 {name} 不存在")
    except Exception as e:
        print(f"  错误: {e}")


def test_turso():
    """测试 Turso 连接"""
    try:
        turso = _import_turso()
        accounts = turso.list_accounts()
        if not accounts:
            print("  暂无 Turso 账号")
            return
        print("  正在测试所有 Turso 账号...")
        for a in accounts:
            ok, detail = turso.test_account(a["name"])
            status = "正常" if ok else "失败"
            print(f"  {a['name']}: {status} ({detail})")
    except Exception as e:
        print(f"  错误: {e}")
