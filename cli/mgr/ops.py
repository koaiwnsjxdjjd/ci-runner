# -*- coding: utf-8 -*-
"""管理客户端操作（实例/账号/任务/日志/终端连接）"""
from cli.common import api
from cli.mgr import terminal


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
    """创建实例"""
    print("  正在创建新实例（自动配置隧道+启动）...")
    d = api.post("/api/instances")
    if d.get("ok"):
        inst = d.get("instance", {})
        print(f"  ✅ 创建成功: {inst.get('id')} → https://{inst.get('hostname')}")
    else:
        print(f"  ❌ 失败: {d.get('error')}")


def close_instance():
    """关闭实例"""
    insts = list_instances()
    if not insts:
        return
    inst_id = input("  输入要关闭的实例 ID: ").strip()
    if not inst_id:
        print("  已取消")
        return
    d = api.delete(f"/api/instances/{inst_id}")
    print(f"  {'✅ ' + d.get('msg','') if d.get('ok') else '❌ ' + d.get('error','')}")


def add_account():
    """添加账号（全自动）"""
    print("  （全自动：验证token→fork→secrets→报备，任务化执行）")
    name = input("  账号名称: ").strip()
    token = input("  GitHub Token: ").strip()
    if not name or not token:
        print("  名称和 token 必填")
        return
    d = api.post("/api/accounts", {"name": name, "token": token})
    if d.get("ok"):
        print(f"  ✅ {d.get('msg')}")
        print("  ⏳ 任务已入队，可查看 [7] 任务队列")
    else:
        print(f"  ❌ {d.get('error')}")


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
    try:
        limit = int(input("  查看最近多少行日志（默认300）: ").strip() or "300")
    except Exception:
        limit = 300
    limit = max(10, min(limit, 2000))
    d = api.get(f"/api/logs?limit={limit}")
    if not d.get("ok"):
        print(f"  ❌ {d.get('error')}")
        return
    stats = d.get("stats", {})
    print(f"  📊 统计: 错误 {stats.get('error',0)} | 警告 {stats.get('warning',0)}")
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
        print(f"  [{idx}] {i['id']} → https://{i.get('hostname')} ({i.get('status')})")
    try:
        sel = int(input("  选择实例序号: ").strip())
        inst = insts[sel]
    except Exception:
        print("  无效选择")
        return
    print(f"  连接 {inst['id']} ... (Ctrl+4 退出)")
    terminal.connect_terminal(f"https://{inst.get('hostname')}")