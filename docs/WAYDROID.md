# Waydroid 实验兼容环境

## 当前状态

2026-11-06 本机安装了 Waydroid 1.6.3、Weston 13.0.0、官方 Android 13 system/vendor 镜像和固定 Android 13 NDK ARM 转换组件。Android 实际完成启动，运行时可见 ARM64 ABI；但学习通 7.0.4、7.0.6 首次启动原生崩溃，内测 7.0.6.1 也未显示可用界面。

因此，此环境仅用于后续兼容性研究。正常使用请采用 [USB 手机模式](../README.md)。保留的安装器不意味着学习通兼容问题已经解决。

## 环境安装与边界

以下命令在项目目录执行，适用 Ubuntu 24.04 noble / amd64。安装器使用固定指纹核对官方 Waydroid 仓库密钥，安装 Waydroid 与 Weston；已有完成的 Android 环境不会强制重新初始化。

```bash
bash scripts/install-host.sh --help
sudo bash scripts/install-host.sh
```

Android 镜像较大，下载和展开还需要额外磁盘及内存空间。本机已完成安装，无需再次初始化。X11 使用启动器管理的 Weston，Wayland 环境直接使用桌面显示。

## ARM 转换

已检查的官方学习通 7.0.6 仅包含 ARM64 原生库，x86_64 Android 需要额外转换组件。本项目固定 Android 13 NDK 归档，SHA256：

```text
a142d1586c9eafb5edf62277110f2128bc03066179a6783bcaa33ee322e1cbd0
```

安装器只处理审核过的 177 个文件及相关配置，提供备份、失败恢复和防覆盖回滚，没有执行整套社区综合脚本。

```bash
python3 scripts/install-arm-bridge.py --plan
sudo python3 scripts/install-arm-bridge.py --apply
sudo python3 scripts/install-arm-bridge.py --rollback
```

安装和回滚前必须停止 Android 会话。备份位于 `/var/lib/waydroid/chaoxing-linux-arm-bridge/`。若文件在安装后被别的程序修改，回滚会报告冲突并保留现状。可使用 `--archive /absolute/path/archive.zip` 复用哈希一致的归档；默认安装路径会自行取得固定归档，不依赖已删除的参考仓库。

## APK 检查与运行

从 [官方学习通页面](https://app.chaoxing.com/) 获取原始 APK。启动器解析包名、版本和 ABI；这不替代发布者签名验证。

```bash
chaoxing-linux --inspect-apk "$HOME/.cache/chaoxing-linux/chaoxing-official.apk"
chaoxing-linux --install-apk "$HOME/.cache/chaoxing-linux/chaoxing-official.apk"
chaoxing-linux --launch
chaoxing-linux --stop
```

`--stop` 只管理本启动器创建且进程身份仍匹配的会话/Weston；不会自动结束原来存在的其他会话。关闭启动器窗口不会停止后台 Android。

不要把 Android 成功启动或 APK 成功安装解释为学习通功能已通过验证。

## 镜像下载与首次初始化恢复

保留两个开发辅助工具，普通使用不需要执行：

```bash
python3 scripts/fetch-images.py
python3 scripts/complete-runtime.py --manifest "$HOME/.cache/chaoxing-linux/images/manifest.json"
```

前者读取官方 OTA 清单，将镜像并行下载到当前用户缓存并核对 SHA256；后者默认生成只读计划，同时核对缓存清单与当前官方 OTA。后者仅支持尚无镜像、尚未完成的默认首次初始化，不覆盖已经完成的环境。实际应用需要管理员身份及 `--apply`，使用前先阅读脚本条件。

曾经为本机中断安装编写的 `finalize-this-host.sh` 含个人绝对路径，任务完成后已经删除。通用下载、恢复及 ARM 安装工具继续保留。

## 诊断资料

运行日志在 `$XDG_RUNTIME_DIR/chaoxing-linux/`。历史下载进度日志已清理；兼容性研究的原始记录保存在本地。原始 APK 和镜像缓存位于用户目录，未随工作区清理而删除。
