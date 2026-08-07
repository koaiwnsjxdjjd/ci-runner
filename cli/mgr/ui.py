# -*- coding: utf-8 -*-
"""管理客户端主菜单 UI"""
from cli.common import config
from cli.mgr import ops


def main_menu():
    print("\n" + "=" * 54)
    print("  ghbox GitHub Actions 云端实例管理")
    print(f"  Manager: {config.MANAGER}")
    print("=" * 54)
    print("  [1] 查看所有实例")
    print("  [2] 新建实例")
    print("  [3] 连接实例终端")
    print("  [4] 关闭实例")
    print("  [5] 查看账号")
    print("  [6] 添加账号")
    print("  [7] 任务队列")
    print("  [8] 查看服务器日志")
    print("  [0] 退出")
    return input("\n  请选择: ").strip()


def run_menu():
    """主菜单循环"""
    while True:
        try:
            choice = main_menu()
            if choice == "1":
                ops.list_instances()
            elif choice == "2":
                ops.create_instance()
            elif choice == "3":
                ops.pick_and_connect()
            elif choice == "4":
                ops.close_instance()
            elif choice == "5":
                ops.list_accounts()
            elif choice == "6":
                ops.add_account()
            elif choice == "7":
                ops.list_tasks()
            elif choice == "8":
                ops.view_logs()
            elif choice == "0":
                print("  再见！")
                break
            else:
                print("  无效选择")
        except KeyboardInterrupt:
            print("\n  返回菜单")
        except Exception as e:
            print(f"  错误: {e}")