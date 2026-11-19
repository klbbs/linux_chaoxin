import argparse
import json

from . import __version__


def main():
    parser = argparse.ArgumentParser(description="学习通 Linux：课程、作业 AI、签到和 USB 手机工作台")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--doctor", action="store_true", help="只读检查当前环境")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--inspect-apk", metavar="PATH", help="只读检查 APK 包名、版本及 CPU 架构")
    parser.add_argument("--install-apk", metavar="PATH", help="安装已检查架构兼容的学习通 APK")
    parser.add_argument("--launch", action="store_true", help="打开已安装的学习通")
    parser.add_argument("--stop", action="store_true", help="停止本启动器创建的 Android 会话")
    parser.add_argument("--phone", action="store_true", help="在 Linux 窗口中打开 USB 手机的学习通")
    parser.add_argument("--phone-devices", action="store_true", help="列出连接的 USB 手机")
    parser.add_argument("--phone-serial", help="选择 USB 手机（连接多台时使用）")
    parser.add_argument("--stop-phone", action="store_true", help="关闭本启动器的手机窗口")
    parser.add_argument("--legacy-launcher", action="store_true", help="打开实验 Android 兼容环境启动器")
    parser.add_argument("--study-status", action="store_true", help="只读检查课程服务组件，不访问线上账号")
    parser.add_argument("--smoke-test", metavar="PNG", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.study_status:
        from .service import AppService
        service = AppService()
        try:
            print(json.dumps(service.status(),indent=2,ensure_ascii=False))
        finally:
            service.shutdown()
    elif args.phone or args.phone_devices or args.stop_phone:
        from .phone import PhoneRuntime
        from .runtime import RuntimeFailure
        phone = PhoneRuntime(print)
        try:
            if args.phone_devices:
                print(json.dumps(phone.devices(), indent=2, ensure_ascii=False))
            elif args.stop_phone:
                phone.stop()
            else:
                phone.launch(args.phone_serial)
        except RuntimeFailure as exc:
            parser.exit(1, f"未完成：{exc}\n")
    elif args.doctor:
        from .runtime import doctor
        print(json.dumps(doctor(), indent=2, ensure_ascii=False))
    elif args.inspect_apk:
        from .apk import inspect_apk
        print(json.dumps(inspect_apk(args.inspect_apk), indent=2, ensure_ascii=False))
    elif args.install_apk or args.launch or args.stop:
        from .runtime import Runtime, RuntimeFailure
        runtime = Runtime(print)
        try:
            if args.install_apk:
                from .apk import inspect_apk
                runtime.install(args.install_apk, inspect_apk(args.install_apk))
            elif args.launch:
                runtime.launch()
            else:
                runtime.stop_owned()
        except (RuntimeFailure, ValueError) as exc:
            parser.exit(1, f"未完成：{exc}\n")
    else:
        if args.legacy_launcher:
            from .gui import main as gui_main
        else:
            from .workspace import main as gui_main
        gui_main(args.smoke_test)


if __name__ == "__main__":
    main()
