"""Waydroid lifecycle management. No account or Android security modifications."""
from __future__ import annotations

import json
import configparser
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

PACKAGE = "com.chaoxing.mobile"
SOCKET_NAME = "chaoxing-linux-wayland"


class RuntimeFailure(RuntimeError):
    pass


def run(args, *, env=None, timeout=20, check=True):
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True,
                                timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeFailure(f"无法完成 {args[0]}：{exc}") from exc
    if check and result.returncode:
        # Commands never contain credentials. Keep error output bounded.
        detail = (result.stderr or result.stdout).strip()[-1800:]
        raise RuntimeFailure(detail or f"{args[0]} 返回 {result.returncode}")
    return result


def parse_status(text):
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    values = {}
    for line in clean.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower().replace(" ", "_")] = value.strip()
    return values


def status_uid(status):
    value = status.get("session_user", "")
    match = re.search(r"(?:\(|^)(\d+)\)?$", value)
    return int(match.group(1)) if match else None


def proc_identity(pid):
    """Bind an owned process to its start time, so recycled PIDs are not killed."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        fields = data.rsplit(")", 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def initialized():
    cfg = configparser.ConfigParser()
    try:
        cfg.read('/var/lib/waydroid/waydroid.cfg')
        images = Path(cfg.get('waydroid', 'images_path', fallback='/var/lib/waydroid/images'))
        return (Path('/var/lib/waydroid/waydroid.cfg').is_file()
                and Path('/var/lib/waydroid/rootfs').is_dir()
                and (images / 'system.img').is_file() and (images / 'vendor.img').is_file())
    except (OSError, ValueError, configparser.Error):
        return False


def app_installed(listing):
    return bool(re.search(r'^(?:packageName|Package):\s*' + re.escape(PACKAGE) + r'\s*$', listing, re.M))


def current_session():
    """Read the container's current session identity, including its actual PID."""
    try:
        import dbus
        proxy = dbus.SystemBus().get_object('id.waydro.Container', '/ContainerManager')
        value = dbus.Interface(proxy, 'id.waydro.ContainerManager').GetSession(timeout=5)
        return {str(k): str(v) for k, v in value.items()}
    except Exception:
        return {}


def doctor():
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        mem[key] = int(value.strip().split()[0])
    wd = shutil.which("waydroid")
    status = {}
    status_error = None
    if wd:
        try:
            status = parse_status(run([wd, "status"], timeout=8).stdout)
        except RuntimeFailure as exc:
            status_error = str(exc)
    home = Path.home()
    return {
        "uid": os.getuid(), "session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "display": os.environ.get("DISPLAY"), "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        "waydroid": wd, "weston": shutil.which("weston"),
        "initialized": initialized(),
        "binder_loaded": Path("/sys/module/binder_linux").exists(),
        "memory_available_mib": mem.get("MemAvailable", 0) // 1024,
        "disk_available_gib": round(shutil.disk_usage(home).free / 1024**3, 1),
        "status": status, "status_error": status_error,
        "apk_cached": (home / ".cache/chaoxing-linux/chaoxing-official.apk").is_file(),
        "notes": [
            "这份诊断不证明学习通已启动、登录或全部功能可用。",
            "ARM-only APK 需要运行时实际报告兼容的 ARM ABI。",
        ],
    }


class Runtime:
    def __init__(self, notify=lambda text: None, *, runner=run):
        self.notify = notify
        self.runner = runner
        self.env = dict(os.environ)
        self.waydroid = shutil.which("waydroid") or "waydroid"
        self.runtime_dir = None
        self.state_file = None
        self._children = []

    def status(self):
        return parse_status(self.runner([self.waydroid, "status"], env=self.env,
                                        timeout=10).stdout)

    def _state(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self, values):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(values))
        tmp.chmod(0o600)
        tmp.replace(self.state_file)

    def _spawn(self, args, log_name):
        with (self.runtime_dir / log_name).open("a") as log:
            child = subprocess.Popen(args, env=self.env, stdout=log, stderr=log,
                                     start_new_session=True)
        self._children.append(child)
        return child

    def prepare_display(self):
        if os.getuid() == 0:
            raise RuntimeFailure("请以普通桌面用户启动学习通，不要使用 sudo。")
        base = Path(self.env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        if not base.is_dir() or base.stat().st_uid != os.getuid():
            raise RuntimeFailure("当前桌面用户的 XDG_RUNTIME_DIR 不可用。")
        if not self.env.get("DBUS_SESSION_BUS_ADDRESS"):
            raise RuntimeFailure("没有桌面 D-Bus 会话，请从应用菜单启动。")
        self.runtime_dir = base / "chaoxing-linux"
        self.runtime_dir.mkdir(mode=0o700, exist_ok=True)
        self.runtime_dir.chmod(0o700)
        self.state_file = self.runtime_dir / "owned-runtime.json"
        state = self.status()
        if state.get("session") == "RUNNING":
            if status_uid(state) != os.getuid():
                raise RuntimeFailure("Android 会话属于其他用户，已保留该会话。")
            display = state.get("wayland_display")
            if not display:
                raise RuntimeFailure("无法确认已有 Android 会话的显示器，未修改该会话。")
            candidate = Path(display) if display.startswith("/") else base / display
            if not candidate.is_socket():
                raise RuntimeFailure("已有 Android 会话的 Wayland socket 不可用，请先检查该会话。")
            self.env["WAYLAND_DISPLAY"] = display
            self.notify("复用当前用户已有的 Android 会话。")
            return
        native = self.env.get("WAYLAND_DISPLAY")
        if native:
            candidate = Path(native) if native.startswith("/") else base / native
            if candidate.is_socket():
                return
        if not self.env.get("DISPLAY") or not shutil.which("weston"):
            raise RuntimeFailure("需要 Wayland 桌面，或在 X11 下安装 Weston。请先安装兼容环境。")
        display_socket = base / SOCKET_NAME
        record = self._state()
        if display_socket.exists():
            pid = record.get("weston_pid")
            if not pid or proc_identity(pid) != record.get("weston_start"):
                raise RuntimeFailure("兼容显示器名称已被其他进程使用，未替换或结束它。")
        else:
            self.notify("正在为 X11 创建独立的 Android 显示窗口…")
            child = self._spawn([shutil.which("weston"), "--no-config", "--backend=x11",
                                  f"--socket={SOCKET_NAME}", "--width=540", "--height=960",
                                  "--idle-time=0"], "weston.log")
            for _ in range(100):
                if display_socket.is_socket():
                    break
                if child.poll() is not None:
                    raise RuntimeFailure(f"Weston 启动失败，请查看 {self.runtime_dir / 'weston.log'}")
                time.sleep(0.1)
            if not display_socket.is_socket():
                raise RuntimeFailure("等待兼容显示窗口超时。")
            self._save({"weston_pid": child.pid, "weston_start": proc_identity(child.pid),
                        "owned_session": False, "display": SOCKET_NAME})
        self.env["WAYLAND_DISPLAY"] = SOCKET_NAME

    def ensure_ready(self, timeout=180):
        if not shutil.which("waydroid"):
            raise RuntimeFailure("尚未安装 Android 兼容环境，请先点击“安装运行环境”。")
        if not initialized():
            raise RuntimeFailure("Android 镜像尚未初始化，请先完成运行环境安装。")
        self.prepare_display()
        state = self.status()
        if state.get("session") != "RUNNING":
            self.notify("正在启动 Android；首次启动可能需要几分钟…")
            child = self._spawn([self.waydroid, "session", "start"], "android-session.log")
            record = self._state()
            record.update(owned_session=True, display=self.env.get("WAYLAND_DISPLAY"),
                          session_pid=child.pid, session_start=proc_identity(child.pid))
            self._save(record)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()
            if state.get("session") == "RUNNING":
                try:
                    result = self.runner([self.waydroid, "app", "list"], env=self.env,
                                         timeout=15, check=False)
                    boot = self.runner([self.waydroid, "prop", "get", "sys.boot_completed"],
                                       env=self.env, timeout=10, check=False)
                    entries = re.search(r'^packageName:\s*\S+', result.stdout, re.M)
                    if result.returncode == 0 and entries and boot.stdout.strip() == '1':
                        self.notify("Android 应用服务已就绪。")
                        return
                except RuntimeFailure:
                    # A session bus can exist before Android's package service is ready.
                    pass
            time.sleep(2)
        raise RuntimeFailure(f"Android 未在限定时间内就绪，请检查 {self.runtime_dir} 下的日志。")

    def install(self, path, info):
        from .apk import compatibility
        self.ensure_ready()
        before = self.runner([self.waydroid, "app", "list"], env=self.env).stdout
        if app_installed(before):
            raise RuntimeFailure("Android 中已有学习通。请在原应用中检查版本并更新；启动器不会把旧包存在误报为更新成功。")
        raw = self.runner([self.waydroid, "prop", "get", "ro.product.cpu.abilist"],
                          env=self.env).stdout.strip()
        abis = [x.strip() for x in raw.split(",") if x.strip()]
        if not abis:
            raise RuntimeFailure("Android 未报告 CPU ABI，无法确认 APK 兼容性。")
        if not compatibility(info, abis):
            raise RuntimeFailure(f"APK 架构 {', '.join(info['abis'])} 与当前 Android {raw} 不兼容。"
                                 "需要经过验证的 ARM 兼容层；本软件不会自动安装不明来源的转换组件。")
        self.notify("正在安装学习通官方 APK…")
        self.runner([self.waydroid, "app", "install", str(Path(path).resolve())],
                    env=self.env, timeout=180)
        # IPlatform.installApp may return before PackageManager publishes the
        # newly installed package. Wait for actual registration, with a bound.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            listing = self.runner([self.waydroid, "app", "list"], env=self.env).stdout
            if app_installed(listing):
                self.notify("学习通已出现在 Android 应用列表中。")
                return
            time.sleep(1)
        raise RuntimeFailure("安装命令已结束，但等待后仍未在应用列表中发现学习通。")

    def launch(self):
        self.ensure_ready()
        listing = self.runner([self.waydroid, "app", "list"], env=self.env).stdout
        if not app_installed(listing):
            raise RuntimeFailure("Android 中尚未安装学习通，请先导入官方 APK。")
        self.notify("正在打开学习通…")
        self.runner([self.waydroid, "app", "launch", PACKAGE], env=self.env, timeout=30)
        self.notify("启动请求已发出。请在 Android 窗口确认界面；登录与各项功能仍需实际验证。")

    def stop_owned(self):
        # Only stop a session and compositor started by this launcher.
        base = Path(self.env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        self.runtime_dir = base / "chaoxing-linux"
        self.state_file = self.runtime_dir / "owned-runtime.json"
        record = self._state()
        state = self.status()
        if state.get("session") == "RUNNING":
            session_pid = record.get("session_pid")
            same_process = session_pid and proc_identity(session_pid) == record.get("session_start")
            active = current_session()
            same_session = (active.get('pid') == str(session_pid)
                            and active.get('user_id') == str(os.getuid())
                            and active.get('wayland_display') == record.get('display'))
            if not record.get("owned_session") or not same_process or not same_session or status_uid(state) != os.getuid() or state.get("wayland_display") != record.get("display"):
                raise RuntimeFailure("当前 Android 会话不是由本启动器创建，已保留该会话。")
            self.runner([self.waydroid, "session", "stop"], env=self.env, timeout=30)
        pid = record.get("weston_pid")
        if pid and proc_identity(pid) == record.get("weston_start"):
            os.kill(pid, signal.SIGTERM)
        self.state_file.unlink(missing_ok=True)
        self.notify("已停止本启动器创建的 Android 会话和兼容窗口。系统服务保留。")
