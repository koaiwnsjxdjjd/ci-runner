# -*- coding: utf-8 -*-
"""
ghbox worker/process 进程持久化子包

模块划分（单一职责）：
- scanner.py  进程扫描与识别（过滤系统/自身/隧道）
- config.py   进程配置与清单读写（ghvps.json / manifest.json）
- backup.py   快照与文件备份
- restore.py  恢复/启动/停止/重启
- manager.py  ProcessManager 门面（聚合以上，供 API 调用）
"""
from worker.process.manager import ProcessManager

__all__ = ["ProcessManager"]