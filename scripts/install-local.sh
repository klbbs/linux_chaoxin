#!/usr/bin/env bash
set -euo pipefail

if (( EUID == 0 )); then
    echo '请以普通用户运行，无需 sudo。' >&2
    exit 1
fi
if (( $# )); then
    echo '用法：scripts/install-local.sh（安装到当前用户目录）' >&2
    exit 2
fi
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}
[[ $data_dir == /* && $HOME == /* ]] || { echo '安装路径必须是绝对路径。' >&2; exit 1; }
app_dir="$data_dir/chaoxing-linux"
for required in src/chaoxing_linux/__main__.py assets/icon.svg scripts/install-host.sh scripts/install-arm-bridge.py scripts/install-phone-tools.py scripts/install-study-backend.py scripts/study-backend.lock packaging/chaoxing-linux.desktop; do
    [[ -f "$project_dir/$required" ]] || { echo "缺少文件：$required" >&2; exit 1; }
done
install -d "$app_dir/chaoxing_linux" "$HOME/.local/bin" "$data_dir/applications" \
    "$data_dir/icons/hicolor/scalable/apps"
while IFS= read -r -d '' source_file; do
    relative=${source_file#"$project_dir/src/"}
    install -D -m 0644 "$source_file" "$app_dir/$relative"
done < <(find "$project_dir/src/chaoxing_linux" -type f -name '*.py' -print0)
install -m 0755 "$project_dir/scripts/install-host.sh" "$app_dir/install-host.sh"
install -m 0755 "$project_dir/scripts/install-arm-bridge.py" "$app_dir/install-arm-bridge.py"
install -m 0755 "$project_dir/scripts/install-phone-tools.py" "$app_dir/install-phone-tools.py"
install -m 0755 "$project_dir/scripts/install-study-backend.py" "$app_dir/install-study-backend.py"
install -m 0644 "$project_dir/scripts/study-backend.lock" "$app_dir/study-backend.lock"
install -m 0644 "$project_dir/assets/icon.svg" "$data_dir/icons/hicolor/scalable/apps/chaoxing-linux.svg"

# shlex.quote safely handles spaces and apostrophes in the user's home/data paths.
/usr/bin/python3 - "$app_dir" "$HOME/.local/bin/chaoxing-linux" \
    "$project_dir/packaging/chaoxing-linux.desktop" "$data_dir/applications/chaoxing-linux.desktop" <<'PY'
import pathlib
import shlex
import sys

app_dir, wrapper, desktop_source, desktop_target = sys.argv[1:]
wrapper_path = pathlib.Path(wrapper)
wrapper_path.write_text(
    '#!/bin/sh\n'
    f'APP_DIR={shlex.quote(app_dir)}\n'
    'export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"\n'
    'exec /usr/bin/python3 -m chaoxing_linux "$@"\n', encoding='utf-8')
wrapper_path.chmod(0o755)
# Desktop entry Exec uses its own escaping rules, not shell quoting.
escaped = wrapper.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
desktop = pathlib.Path(desktop_source).read_text(encoding='utf-8')
desktop = desktop.replace('Exec=chaoxing-linux\n', f'Exec="{escaped}"\n')
pathlib.Path(desktop_target).write_text(desktop, encoding='utf-8')
PY
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$data_dir/applications" || true
fi
echo "已安装：$HOME/.local/bin/chaoxing-linux"
echo '可在应用菜单打开“学习通 Linux”工作台，同步学习通登录状态并配置 AI，或连接已授权的 USB 安卓手机。'
