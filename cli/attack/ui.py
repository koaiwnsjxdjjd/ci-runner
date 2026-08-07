# -*- coding: utf-8 -*-
"""
攻击客户端科技风界面

- 表格（机器/PPS/Mbps/连接数/状态）
- ASCII 带宽曲线
- 实时进度条
- Live 刷新
"""
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn

console = Console()


def format_num(n):
    """格式化数字（1,234,567）"""
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_display_table(stats):
    """构建科技风统计表格"""
    table = Table(show_header=True, header_style="bold cyan",
                  border_style="blue", box=None)
    table.add_column("机器", style="bold")
    table.add_column("PPS", justify="right", style="green")
    table.add_column("Mbps", justify="right", style="yellow")
    table.add_column("连接数", justify="right")
    table.add_column("状态", style="magenta")
    for inst_id, s in stats.items():
        status = "● 攻击中" if s.get("running") else "○ 停止"
        table.add_row(
            inst_id,
            format_num(s.get("pps", 0)),
            format_num(s.get("mbps", 0)),
            format_num(s.get("conns", 0)),
            status,
        )
    return table


def build_bar_chart(history, width=40):
    """ASCII 图表：带宽曲线"""
    if len(history) < 2:
        return "  等待数据..."
    data = history[-width:]
    max_v = max(data) if max(data) > 0 else 1
    levels = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    chart = ""
    for v in data:
        idx = int(v / max_v * (len(levels) - 1))
        chart += levels[idx]
    return chart


def build_layout(stats, history, elapsed, total_seconds):
    """构建完整 Live 布局"""
    total_pps = sum(s["pps"] for s in stats.values())
    total_mbps = sum(s["mbps"] for s in stats.values())
    title = Text("G H B O X   A T T A C K", style="bold red")
    info = Text(f"已运行 {elapsed}s / 共 {total_seconds}s | "
                f"总火力: {format_num(total_pps)} pps / "
                f"{format_num(total_mbps)} Mbps", style="bold cyan")
    table = build_display_table(stats)
    chart = build_bar_chart(history)
    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("[bold]{task.percentage:>3.0f}%"),
    )
    task = progress.add_task("攻击进度", total=total_seconds, completed=elapsed)
    layout = Layout()
    layout.split_column(
        Layout(Panel(title, border_style="red")),
        Layout(Panel(info, border_style="cyan")),
        Layout(Panel(table, border_style="blue")),
        Layout(Panel(progress, border_style="green")),
        Layout(Panel(Text(chart + f"  {format_num(total_mbps)} Mbps 曲线", style="green"),
                     border_style="yellow", title="带宽曲线")),
        Layout(Text("[red]Ctrl+C 停止攻击[/red]  [yellow]总火力实时汇总[/yellow]",
                    style="dim")),
    )
    return layout