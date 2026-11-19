#!/usr/bin/env bash
# Official reference: https://docs.waydro.id/usage/install-on-desktops
# Repository/key format: https://repo.waydro.id/ (reviewed 2026-10-31).
set -euo pipefail

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    echo '用法：install-host.sh（在终端输入管理员密码，安装 Waydroid 和 Weston）'
    echo '支持 Ubuntu 24.04 noble amd64；保留已有 Android 配置、镜像和应用。'
    exit 0
fi
if (( $# )); then
    echo '不支持此参数；使用 --help 查看用法。' >&2
    exit 2
fi
if (( EUID != 0 )); then
    exec sudo -- /bin/bash "$(readlink -f -- "$0")"
fi

die() { echo "安装未完成：$*" >&2; exit 1; }
trap 'echo "安装已停止。已有 Android 数据不会被重置；解决上面的错误后可重新运行。" >&2' ERR

# This installer is deliberately scoped to the tested host platform.
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_CODENAME:-} == noble ]] || die '目前仅支持 Ubuntu 24.04（noble）。'
[[ $(dpkg --print-architecture) == amd64 ]] || die '目前仅支持 amd64 主机。'
[[ -d /run/systemd/system ]] || die '需要运行 systemd 的正常桌面系统。'

echo '安装官方 Android 兼容环境；首次下载镜像可能需要较长时间。'
apt-get update
apt-get install -y ca-certificates curl gnupg kmod

work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
curl --fail --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    https://repo.waydro.id/waydroid.gpg --output "$work_dir/waydroid.gpg"
# Upstream serves a binary .gpg key, not ASCII armor; do not dearmor it again.
expected_fingerprint=7CE0331F71E0A238BB1002D70E406D181DCEE19C
actual_fingerprint=$(gpg --batch --no-options --homedir "$work_dir" --show-keys --with-colons \
    "$work_dir/waydroid.gpg" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ $actual_fingerprint == "$expected_fingerprint" ]] || die 'Waydroid 仓库签名密钥与已核对的指纹不符，请先核对官方更新。'
install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
install -m 0644 "$work_dir/waydroid.gpg" /usr/share/keyrings/waydroid.gpg
source_line='deb [signed-by=/usr/share/keyrings/waydroid.gpg] https://repo.waydro.id/ noble main'
source_path=/etc/apt/sources.list.d/waydroid.list
if [[ -e $source_path ]]; then
    # Keep user-managed repository settings intact, including comments/options.
    grep -Fqx "$source_line" "$source_path" || die "已有 $source_path 使用不同配置，请先人工核对。"
else
    printf '%s\n' "$source_line" > "$source_path"
    chmod 0644 "$source_path"
fi
apt-get update
apt-get install -y waydroid weston

if ! modprobe binder_linux; then
    [[ -d /sys/module/binder_linux ]] || die '当前内核无法加载 binder_linux，请使用支持 Android binder 的内核并重新启动。'
fi

# Match Waydroid's own initialized check. An interrupted first download leaves
# a config without rootfs; ordinary (non-force) init resumes that installation.
if [[ -f /var/lib/waydroid/waydroid.cfg && -d /var/lib/waydroid/rootfs ]]; then
    echo '检测到已初始化的 Android 环境，保留现状。'
else
    echo '完成 Android 首次初始化；中断的下载将由官方工具继续处理。'
    waydroid init -s VANILLA
fi
systemctl enable --now waydroid-container
echo '主机环境已安装。请以普通用户打开“学习通 Linux”，导入官方 APK 后启动。'
