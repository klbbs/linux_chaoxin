#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
output_dir="$project_dir/dist"
if (( $# )); then
    echo '用法：scripts/build-deb.sh（结果写入项目 dist 目录）' >&2
    exit 2
fi
for required in src/chaoxing_linux/__main__.py assets/icon.svg scripts/install-host.sh scripts/install-arm-bridge.py scripts/install-phone-tools.py scripts/install-study-backend.py scripts/study-backend.lock packaging/chaoxing-linux.desktop; do
    [[ -f "$project_dir/$required" ]] || { echo "缺少文件：$required" >&2; exit 1; }
done
command -v dpkg-deb >/dev/null || { echo '需要 dpkg-deb。' >&2; exit 1; }
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
install -d "$stage/DEBIAN" "$stage/usr/lib/chaoxing-linux/chaoxing_linux" \
    "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps"

# Package only this launcher's Python code, never downloaded APK/DEX or analysis artifacts.
while IFS= read -r -d '' source_file; do
    relative=${source_file#"$project_dir/src/"}
    install -D -m 0644 "$source_file" "$stage/usr/lib/chaoxing-linux/$relative"
done < <(find "$project_dir/src/chaoxing_linux" -type f -name '*.py' -print0)
install -m 0755 "$project_dir/scripts/install-host.sh" "$stage/usr/lib/chaoxing-linux/install-host.sh"
install -m 0755 "$project_dir/scripts/install-arm-bridge.py" "$stage/usr/lib/chaoxing-linux/install-arm-bridge.py"
install -m 0755 "$project_dir/scripts/install-phone-tools.py" "$stage/usr/lib/chaoxing-linux/install-phone-tools.py"
install -m 0755 "$project_dir/scripts/install-study-backend.py" "$stage/usr/lib/chaoxing-linux/install-study-backend.py"
install -m 0644 "$project_dir/scripts/study-backend.lock" "$stage/usr/lib/chaoxing-linux/study-backend.lock"
install -m 0644 "$project_dir/assets/icon.svg" "$stage/usr/share/icons/hicolor/scalable/apps/chaoxing-linux.svg"
install -m 0644 "$project_dir/packaging/chaoxing-linux.desktop" "$stage/usr/share/applications/chaoxing-linux.desktop"
cat > "$stage/usr/bin/chaoxing-linux" <<'WRAPPER'
#!/bin/sh
export PYTHONPATH="/usr/lib/chaoxing-linux${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m chaoxing_linux "$@"
WRAPPER
chmod 0755 "$stage/usr/bin/chaoxing-linux"
cat > "$stage/DEBIAN/control" <<'CONTROL'
Package: chaoxing-linux
Version: 0.5.2
Section: education
Priority: optional
Architecture: all
Maintainer: Chaoxing Linux Local Build <noreply@localhost>
Depends: python3, python3-gi, gir1.2-gtk-3.0, python3-dbus, python3-cryptography
Recommends: libreoffice-writer, fonts-noto-cjk, poppler-utils, adb
Suggests: antiword, catdoc, bubblewrap, waydroid, weston
Description: Linux desktop workspace for Chaoxing courses and Android phones
 Provides an independent GTK workspace for course tasks, Word/PDF review
 guides, AI-assisted work, attendance and authorized USB phone controls.
 Study and phone runtimes are installed separately. Also retains an
 experimental Waydroid launcher.
CONTROL
chmod 0644 "$stage/DEBIAN/control"
mkdir -p -- "$output_dir"
dpkg-deb --root-owner-group --build "$stage" "$output_dir/chaoxing-linux_0.5.2_all.deb"
echo "已生成：$output_dir/chaoxing-linux_0.5.2_all.deb"
