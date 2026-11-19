"""GTK desktop front end. Android content runs in its original application."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import threading
import webbrowser

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from .apk import inspect_apk
from .runtime import Runtime, RuntimeFailure, doctor

CSS = b"""
window { background: #f5f7fb; color: #15243b; }
.sidebar { background: #15243b; padding: 28px 22px; }
.brand { color: #ffffff; font-size: 25px; font-weight: 700; }
.side-label { color: #9dacc0; font-size: 13px; }
.side-section { color: #eef5ff; font-weight: 600; padding-top: 22px; }
.content { padding: 32px; }
.eyebrow { color: #3168d5; font-weight: 700; font-size: 12px; }
.title { font-size: 30px; font-weight: 700; }
.subtitle { color: #63728a; font-size: 14px; }
.card { background: white; border: 1px solid #e2e8f2; border-radius: 12px; padding: 18px; }
.card-title { color: #6a7890; font-size: 12px; }
.card-value { font-size: 18px; font-weight: 600; padding-top: 9px; }
.section-title { font-size: 17px; font-weight: 600; }
.step { background: #edf3ff; color: #3168d5; border-radius: 18px; padding: 9px; font-weight: 700; }
button { background: white; color: #263b5a; border: 1px solid #d6deeb; border-radius: 7px; padding: 9px 16px; box-shadow: none; }
button:hover { background: #edf3ff; }
button.primary { background: #3168d5; color: white; border-color: #3168d5; }
button.primary:hover { background: #2558b9; }
.notice { background: #fff5dc; color: #72551d; padding: 12px; border-radius: 8px; }
textview, textview text { background: #17263b; color: #e0e9f7; font-family: monospace; font-size: 12px; }
"""


def label(text, css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def installer_path(name="install-host.sh"):
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / name,
                      here.parents[2] / "scripts" / name):
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到运行环境安装脚本，请重新安装启动器。")


class Window(Gtk.Window):
    def __init__(self, smoke_path=None):
        super().__init__(title="学习通 Linux")
        self.set_default_size(1120, 790)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", Gtk.main_quit)
        self.busy = False
        self.buttons = []
        self.smoke_path = smoke_path
        self.apk_path = Path.home() / ".cache/chaoxing-linux/chaoxing-official.apk"
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(shell)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        side.set_size_request(212, -1)
        side.get_style_context().add_class("sidebar")
        side.pack_start(label("学习通", "brand"), False, False, 0)
        side.pack_start(label("LINUX COMPANION", "side-label"), False, False, 0)
        side.pack_start(label("手机端兼容运行", "side-section"), False, False, 0)
        for text in ("课程与视频", "作业与考试", "消息与通知", "资料与个人中心"):
            side.pack_start(label(text, "side-label"), False, False, 1)
        side.pack_end(label("v0.1.0 · 本地构建\n由官方 Android 应用提供内容", "side-label"), False, False, 0)
        shell.pack_start(side, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        shell.pack_start(scroll, True, True, 0)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        main.get_style_context().add_class("content")
        scroll.add(main)
        main.pack_start(label("ANDROID ON LINUX", "eyebrow"), False, False, 0)
        main.pack_start(label("让手机端，回到你的桌面。", "title"), False, False, 0)
        main.pack_start(label("连接安卓手机，或尝试电脑兼容模式。账号和课程数据由官方学习通提供。", "subtitle", True), False, False, 0)
        phone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        phone_box.get_style_context().add_class("card")
        phone_box.pack_start(label("连接安卓手机", "section-title"), False, False, 0)
        phone_box.pack_start(label("用数据线连接已授权的手机，在电脑窗口操作手机中的学习通；相机和定位继续使用手机。", "subtitle", True), False, False, 0)
        phone_row = Gtk.Box(spacing=8)
        self.phone_select = Gtk.ComboBoxText()
        phone_row.pack_start(self.phone_select, True, True, 0)
        for text, callback in (("刷新手机", self.refresh_phone), ("打开手机学习通", self.launch_phone),
                               ("关闭手机窗口", self.stop_phone)):
            button = Gtk.Button(label=text)
            button.connect("clicked", callback)
            if text == "打开手机学习通":
                button.get_style_context().add_class("primary")
            self.buttons.append(button)
            phone_row.pack_start(button, False, False, 0)
        phone_box.pack_start(phone_row, False, False, 0)
        tools_button = Gtk.Button(label="安装手机连接工具")
        tools_button.connect("clicked", self.install_phone_tools)
        self.buttons.append(tools_button)
        phone_box.pack_start(tools_button, False, False, 0)
        main.pack_start(phone_box, False, False, 0)
        main.pack_start(label("电脑兼容模式 · 实验功能", "section-title"), False, False, 0)
        cards = Gtk.Box(spacing=12, homogeneous=True)
        self.cards = {}
        for key, title in (("runtime", "运行环境"), ("display", "桌面显示"), ("apk", "学习通 APK")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            card.get_style_context().add_class("card")
            card.pack_start(label(title, "card-title"), False, False, 0)
            value = label("正在检查…", "card-value")
            self.cards[key] = value
            card.pack_start(value, False, False, 0)
            cards.pack_start(card, True, True, 0)
        main.pack_start(cards, False, False, 0)
        notice = label("相机、定位、蓝牙、人脸验证等功能取决于电脑硬件和兼容环境；尚未验证完整手机端功能。", "notice", True)
        main.pack_start(notice, False, False, 0)
        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        steps.get_style_context().add_class("card")
        self.add_step(steps, "1", "准备 Android 运行环境", "首次安装需要管理员密码；保留已有 Android 数据。", "安装运行环境", self.install_host)
        self.add_step(steps, "2", "导入官方学习通 APK", "检查包名、版本和 CPU 架构后，再安装到 Android。", "选择 APK", self.choose_apk)
        self.add_step(steps, "3", "打开手机端学习通", "在独立窗口登录并使用原应用。", "启动学习通", self.launch, primary=True)
        main.pack_start(steps, False, False, 0)
        row = Gtk.Box(spacing=10)
        for text, callback in (("下载官方 APK", self.open_download), ("安装所选 APK", self.install_apk),
                               ("刷新状态", self.refresh), ("停止本次环境", self.stop)):
            button = Gtk.Button(label=text)
            button.connect("clicked", callback)
            row.pack_start(button, False, False, 0)
            self.buttons.append(button)
        main.pack_start(row, False, False, 0)
        arm_row = Gtk.Box(spacing=12)
        arm_button = Gtk.Button(label="安装 ARM64 兼容组件")
        arm_button.connect("clicked", self.install_arm)
        self.buttons.append(arm_button)
        arm_row.pack_start(arm_button, False, False, 0)
        arm_row.pack_start(label("官方 APK 仅支持 ARM64；需先完成运行环境安装。", "subtitle", True), True, True, 0)
        main.pack_start(arm_row, False, False, 0)
        self.apk_label = label("尚未检查 APK", "subtitle", True)
        main.pack_start(self.apk_label, False, False, 0)
        main.pack_start(label("运行记录", "section-title"), False, False, 0)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_min_content_height(135)
        log_scroll.add(self.log_view)
        main.pack_start(log_scroll, True, True, 0)
        self.runtime = Runtime(lambda text: GLib.idle_add(self.log, text))
        from .phone import PhoneRuntime
        self.phone = PhoneRuntime(lambda text: GLib.idle_add(self.log, text))
        self.log("启动器已打开。请选择手机连接或电脑兼容模式。")
        self.show_all()
        self.refresh()
        if self.smoke_path:
            GLib.timeout_add(2000, self.smoke_capture)

    def add_step(self, box, number, title, detail, action, callback, primary=False):
        row = Gtk.Box(spacing=16)
        row.pack_start(label(number, "step"), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        text.pack_start(label(title, "section-title"), False, False, 0)
        text.pack_start(label(detail, "subtitle", True), False, False, 0)
        row.pack_start(text, True, True, 0)
        button = Gtk.Button(label=action)
        if primary:
            button.get_style_context().add_class("primary")
        button.connect("clicked", callback)
        self.buttons.append(button)
        row.pack_end(button, False, False, 0)
        box.pack_start(row, False, False, 0)

    def log(self, text):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text + "\n")
        return False

    def background(self, operation):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.set_sensitive(False)

        def execute():
            try:
                operation()
            except Exception as exc:
                GLib.idle_add(self.log, "未完成：" + str(exc))
            finally:
                GLib.idle_add(done)

        def done():
            self.busy = False
            for button in self.buttons:
                button.set_sensitive(True)
            return False

        threading.Thread(target=execute, daemon=True).start()

    def refresh(self, *_):
        def check():
            info = doctor()
            GLib.idle_add(self.show_report, info)
            try:
                GLib.idle_add(self.show_phones, self.phone.devices())
            except RuntimeFailure as exc:
                GLib.idle_add(self.log, str(exc))
            if self.apk_path.is_file():
                try:
                    apk = inspect_apk(self.apk_path)
                    GLib.idle_add(self.show_apk, apk)
                except ValueError as exc:
                    GLib.idle_add(self.log, str(exc))
        self.background(check)

    def show_report(self, info):
        state = info["status"].get("session", "")
        self.cards["runtime"].set_text("Android 运行中" if state == "RUNNING" else
                                       "已安装，待启动" if info["initialized"] else "尚未安装")
        self.cards["display"].set_text("Wayland" if info["wayland_display"] else "X11 · 需 Weston")
        self.cards["apk"].set_text("已下载，待检查" if info["apk_cached"] else "等待导入")
        self.log(f"可用内存 {info['memory_available_mib']} MiB；剩余磁盘 {info['disk_available_gib']} GiB。")
        if info["memory_available_mib"] < 2500:
            self.log("当前可用内存较少。启动 Android 前建议关闭不需要的应用。")
        return False

    def show_apk(self, apk):
        self.cards["apk"].set_text(apk["version_name"] or "已检查包名")
        abis = ", ".join(apk["abis"]) or "无 lib/ 原生库（通用）"
        self.apk_label.set_text(f"已选择：{self.apk_path.name}\n版本 {apk['version_name']} · {abis} · 未验证数字签名")
        return False

    def choose_apk(self, *_):
        dialog = Gtk.FileChooserDialog(title="选择学习通官方 APK", transient_for=self,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Android APK")
        file_filter.add_pattern("*.apk")
        dialog.add_filter(file_filter)
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and filename:
            self.apk_path = Path(filename)
            self.background(lambda: GLib.idle_add(self.show_apk, inspect_apk(self.apk_path)))

    def install_host(self, *_):
        try:
            script = str(installer_path())
            terminal = shutil.which("x-terminal-emulator")
            if not terminal:
                raise RuntimeError(f"请在终端执行：bash {script}")
            subprocess.Popen([terminal, "-e", "/bin/bash", "-c",
                              'bash "$1"; result=$?; printf "\\n按回车关闭窗口…"; read -r; exit "$result"',
                              "chaoxing-host-setup", script], start_new_session=True)
            self.log("已打开环境安装终端，请在该窗口输入管理员密码。完成后点击“刷新状态”。")
        except Exception as exc:
            self.log(str(exc))

    def install_apk(self, *_):
        self.background(lambda: self.runtime.install(self.apk_path, inspect_apk(self.apk_path)))

    def install_arm(self, *_):
        try:
            script = str(installer_path("install-arm-bridge.py"))
            terminal = shutil.which("x-terminal-emulator")
            if not terminal:
                raise RuntimeError(f"请在终端执行：sudo python3 {script} --apply")
            subprocess.Popen([terminal, "-e", "/bin/bash", "-c",
                              'sudo /usr/bin/python3 "$1" --apply; result=$?; printf "\\n按回车关闭窗口…"; read -r; exit "$result"',
                              "chaoxing-arm-setup", script], start_new_session=True)
            self.log("已打开 ARM64 组件安装终端。使用固定 Android 13 NDK 转换组件，保留改动清单和备份。")
        except Exception as exc:
            self.log(str(exc))

    def launch(self, *_):
        self.background(self.runtime.launch)

    def show_phones(self, devices):
        previous = self.phone_select.get_active_id()
        self.phone_select.remove_all()
        for item in devices:
            state = "已授权" if item['state'] == 'device' else "等待手机授权"
            self.phone_select.append(item['serial'], f"{item['model']} · {state}")
        if previous and any(item['serial'] == previous for item in devices):
            self.phone_select.set_active_id(previous)
        elif len(devices) == 1:
            self.phone_select.set_active(0)
        if not devices:
            self.phone_select.append('', '未检测到 USB 手机')
            self.phone_select.set_active(0)
        return False

    def refresh_phone(self, *_):
        self.background(lambda: GLib.idle_add(self.show_phones, self.phone.devices()))

    def launch_phone(self, *_):
        serial = self.phone_select.get_active_id() or None
        self.background(lambda: self.phone.launch(serial))

    def stop_phone(self, *_):
        self.background(self.phone.stop)

    def install_phone_tools(self, *_):
        def install():
            from .runtime import run
            result = run(['/usr/bin/python3', str(installer_path('install-phone-tools.py')), '--apply'], timeout=240)
            GLib.idle_add(self.log, result.stdout[-2000:])
        self.background(install)

    def stop(self, *_):
        self.background(self.runtime.stop_owned)

    def open_download(self, *_):
        webbrowser.open("https://app.chaoxing.com/")

    def smoke_capture(self):
        # A self-test snapshot of this application's own window; no desktop inputs.
        window = self.get_window()
        width, height = self.get_size()
        pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
        target = Path(self.smoke_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        pixbuf.savev(str(target), "png", [], [])
        print(json.dumps({"gui_smoke": "passed", "screenshot": str(target)}, ensure_ascii=False), flush=True)
        self.destroy()
        return False


def main(smoke_path=None):
    Window(smoke_path=smoke_path)
    Gtk.main()
