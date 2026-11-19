# 工作台架构

## 运行链路

```text
GTK工作台 workspace.py
  └─ AppService：私有设置、后台线程、历史记录、去重、取消
      ├─ AIClient → 搜索服务 + 用户配置的模型API
      ├─ BackendClient → Python3.13 worker → 固定开源后端 → 学习通网站接口
      └─ PhoneController → ADB/独立scrcpy窗口 → 已授权手机官方学习通
```

GTK主进程使用系统Python，后端依赖在独立Python3.13环境。后端源码可使用原工作区 `chaoxing/`，或安装到用户目录的 `study-backend/source`，其解释器在同级 `.venv/`。上游源码不导入GTK进程。

## 模块职责

| 文件 | 职责 |
|---|---|
| `__main__.py` | 默认工作台、只读CLI、手机CLI及旧启动器入口 |
| `workspace.py` | 七页界面、后台调用、可编辑答案、手机坐标映射、任务和关闭流程 |
| `service.py` | 设置、私有存储、后台调度、日志脱敏、重复写操作拦截、AI作业串联 |
| `assistant_ai.py` | 兼容Chat API、联网检索、题型和来源校验、重试/取消 |
| `chatgpt_auth.py` / `chatgpt_ai.py` | ChatGPT 官方浏览器授权、凭据更新、账号模型列表和 Responses 流式请求 |
| `review_ui.py` / `review.py` | 课程复习入口、摘要与自测组织、后台导出任务协调 |
| `review_fetch.py` | 独立 worker 内读取课程章节、已提交作业、资料目录及授权文件文本 |
| `review_documents.py` | 构造 Word 和 PDF，成对保存到新目录 |
| `question_images.py` | 题图来源限制、限量下载、私有缓存、格式与SHA256校验；AI构建多模态请求 |
| `study_backend.py` | JSON管道、进程组超时/取消、源码与解释器发现 |
| `study_worker.py` | 私有工作目录、上游适配、课程计划/执行、作业解析/提交/回读、签到参数 |
| `session_sync.py` | 按Firefox配置隔离发现本站会话；service临时目录验证成功后原子复制 |
| `location.py` / `map_picker.py` | WGS84/GCJ02、公开地图搜索及原生GTK选点；不发送签到 |
| `phone_control.py` | USB选择、学习通前台检查、预览及输入命令 |
| `phone.py` | 独立scrcpy窗口与进程身份管理 |
| `gui.py` / `runtime.py` / `apk.py` | 原实验Waydroid界面、运行环境和APK检查 |

## 状态与进程

设置保存到 `$XDG_CONFIG_HOME/chaoxing-linux/settings.json`（默认 `~/.config`），任务及后端Cookie在 `~/.local/share/chaoxing-linux/study-data/`，文件权限0600、目录0700。密钥仅通过配置及stdin管道传给所选服务，不进入进程参数和任务日志。

常用位置为 `study-data/locations.json`，签到历史为 `study-data/sign-history.json`（最近80条），均使用原子写入与0600权限。位置标明坐标系，明确WGS84数据在签到参数交给后端前转换一次；GCJ02不重复转换。小程序只提供交互参考，学习通接口来源与验证范围见 [ATTENDANCE-API](ATTENDANCE-API.md)。

后台任务记录状态、时间、进度和逐项结果。退出时取消全部任务；BackendClient终止独立进程组，worker守护线程检查父PID与启动身份。重新启动只将未完成历史标为中断，不自动重放写请求。

同一作业、课程或签到活动的活跃写任务互斥，短时间相同已结束请求也会拦截。取消不能撤销服务器已经接受的操作。

## 协议边界

作业链接和重定向限制学习通HTTPS域，提交地址从识别出的真实表单/已核对缺省接口得到。再次读取题目并比对ID、题型、文本、选项和题图hash/位置后构造原生字段。题图未读取完整、AI图片发送记录不完整、未知答案、未识别多空、验证码和登录失效不随机回退。题图base64仅用于模型请求，持久记录保留附件描述；共享缓存检查拒绝越界、符号链接及错误权限，详见 [题图说明](IMAGE-QUESTIONS.md)。

课程计划阶段抑制上游空章节处理副作用；执行阶段逐项记录 `reported` 和回读核对。章节草稿不计完成。AI网络重定向被拒绝，来源仅取真实检索结果，模型未使用搜索如实记not_used；实际搜索失败或引用不能核验时暂停自动正式提交。

手机预览和输入仅在选定USB手机的官方学习通前台执行，不读取其他应用私有文件或消息。普通ADB无法取得正式版应用私有Cookie，账号登录与手机授权分开管理。

## 安装与构建

用户安装复制本项目所有Python源码到 `~/.local/share/chaoxing-linux/chaoxing_linux/`；deb复制到 `/usr/lib/chaoxing-linux/`。安装器和锁定清单随包提供，第三方后端由单独安装器固定归档SHA256取得，保留上游资源和LICENSE。

包内不放账号、密钥、APK、DEX、研究证据和字节码。手机连接工具仍为单独的官方x86_64归档；Android兼容环境仍为可选实验路径。

在项目根目录验证和构建：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
bash scripts/build-deb.sh
```

真实在线功能验证与离线测试分开记录在 [FEATURES](FEATURES.md)。任何“完成”结论需说明服务器接受还是回读核对，不能从HTTP200推断课程分。
