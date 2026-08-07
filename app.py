# -*- coding: utf-8 -*-
"""
ghbox 统一入口：按 INSTANCE_ROLE 启动 manager / worker / survival

用法：python3 app.py
"""
import config


def main():
    if config.ROLE == "manager":
        import manager.app
        manager.app.run()
    elif config.ROLE == "survival":
        import survival
        survival.run()
    else:
        import worker.app
        worker.app.run()


if __name__ == "__main__":
    main()