# 地图选点与坐标

地图弹窗用于选择和确认地点，并把位置返回签到表单。打开地图、搜索地点、拖动浏览或确认位置，都不会发送签到。

## 操作

1. 在签到的位置区域打开「地图选点」。如已有常用位置，地图以该位置为起点；没有位置时显示北京区域，确认按钮保持禁用。
2. 输入学校、校区、楼宇或地点的完整名称，点击「搜索地点」。右侧显示候选结果，点击一个结果跳转并标记。
3. 拖动地图浏览，使用滚轮或「＋ / −」缩放；点击地图选点。绿色标记表示已选中的位置。「回到选中点」恢复视野。
4. 「查询此点地址」仅在点击后请求公开服务，显示附近地址候选。不同地区的 OpenStreetMap 数据完整度不同，无法查到时可直接编辑名称、地址或坐标。
5. 检查名称、提交地址、纬度、经度及坐标系。手动修改坐标后点击「应用手动坐标」。完成后点击「确认此位置」返回表单，或点击「取消」放弃。
6. 返回表单后由工作台保存常用位置；实际签到仍需单独操作。选出的地点只是填写的坐标，不代表手机实际位置，也不保证活动接受该位置。

切换坐标系会基于当前地理位置换算显示值；已有选点时，尚未应用的有效手动输入也会先按原坐标系解析，再转换，不会被旧选点覆盖。无效输入会阻止切换并提示修正。

## 坐标系

- 地图与 Photon 搜索结果使用 **WGS84**，也保留为 `wgs84_latitude`、`wgs84_longitude`。
- 弹窗默认向表单返回 **GCJ02**（国内应用通常使用的坐标）。用户可以明确改为 WGS84。
- `latitude` 为纬度，`longitude` 为经度，不可颠倒。输入校验会拒绝 NaN、无穷大及超范围数字。
- 坐标换算通过常用公开数学近似实现，WGS84 → GCJ02 在中国区域内施加偏移，逆向采用迭代求解；它不具有测绘精度保证。中国边界附近及港澳地区需依据实际地图/服务坐标系人工核对。
- 签到接口需要 GCJ02 时，只转换明确标记为 WGS84 的输入。GCJ02 输入不能再次偏移。

## 公开服务与网络

当前地图使用 [OpenStreetMap 标准瓦片](https://tile.openstreetmap.org)，地点查询使用 [Photon 公共 API](https://photon.komoot.io/)（搜索 `/api/`、附近地址 `/reverse`）。不需要 API Key，服务没有可用性保证，中文地点覆盖和排序也可能不完整。

实际连通检查：2026-11-10，`Tsinghua University` 查询返回清华大学（WGS84 纬度 `40.0022905`、经度 `116.320963`）以及其他校区候选。查到多个校区时必须检查候选地址和地图，不能只按同名选点。

隐私：地图服务可看到当前视野对应的瓦片请求及网络地址；手动点击搜索会把关键词发送给 Photon，点击「查询此点地址」会发送选中的 WGS84 坐标。本模块不向这些服务发送账号、Cookie、课程或签到任务参数，也不读取手机定位。

为遵守 [OSM 瓦片使用政策](https://operations.osmfoundation.org/policies/tiles/)，程序使用明确应用 User-Agent，只请求当前显示的视口，不提供批量预取/离线下载，持续显示 OpenStreetMap 署名及地图问题反馈链接。磁盘瓦片缓存至少保留七天，过期时使用服务返回的 ETag/Last-Modified 条件请求。地点查询按点击触发、串行限速并缓存七天，不随输入自动搜索，不使用公共 Nominatim。请遵守 [Photon 公平使用说明](https://photon.komoot.io/)，大量部署应使用自建或获准服务。

可在启动工作台前切换兼容服务：

```bash
export CHAOXING_TILE_URL='https://your-provider.example/{z}/{x}/{y}.png'
export CHAOXING_PHOTON_URL='https://your-photon.example'
chaoxing-linux
```

自定义瓦片需为 HTTPS PNG 瓦片；地点服务需兼容 Photon GeoJSON 接口。更换瓦片服务必须遵守新服务条款；界面当前显示 OSM 署名，因此仅适用于 OSM 数据源。缓存目录为 `~/.cache/chaoxing-linux/maps/`，服务不同会使用不同缓存。搜索缓存及瓦片缓存入口目录权限为 `0700`，新缓存文件为 `0600`，避免其他本机用户读取地点关键词或视野坐标。

## 开发接口

```python
from chaoxing_linux.map_picker import choose_location

selected = choose_location(parent=window, initial=None)
# None 表示取消；以下仅为确认后的返回示例
# {
#   'latitude': 40.0036, 'longitude': 116.3271,
#   'coordinate_system': 'gcj02',
#   'wgs84_latitude': 40.00229, 'wgs84_longitude': 116.320963,
#   'name': '清华大学', 'address': '中国 · 北京市 · 清华大学',
#   'source': 'manual'  # 或 photon / map / saved
# }
```

`MapPicker(parent=None, initial=None).run_location()` 提供相同返回契约。GUI 使用系统已有的 GTK3 / GdkPixbuf，无新增 JavaScript 库或 WebKit 运行时。网络请求在后台 daemon 工作线程进行；关闭窗口会取消排队请求，已发出的请求自行等待网络超时，但不会阻止整个工作台进程退出。

`location.py` 纯函数提供 `validate_coordinates`、`location_record`、`wgs84_to_gcj02`、`gcj02_to_wgs84`、`project`、`unproject` 和 `MapViewport`。`PlaceSearch` 提供手动搜索与逆向查询，`TileCache` 负责视口瓦片磁盘缓存。测试覆盖坐标已知位移和往返、鼠标点到坐标的映射、缩放锚点、视口瓦片范围、请求缓存、超时及取消。
