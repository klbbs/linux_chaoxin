"""Native GTK3 interactive map. No attendance APIs, phone controls or browser JS."""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .location import (LocationError, MapViewport, PlaceSearch, USER_AGENT,
                       gcj02_to_wgs84, location_record, wgs84_to_gcj02)

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CACHE_AGE = 7*86400


class _DaemonPool:
    """Small cancellable worker pool; in-flight HTTP cannot hold desktop exit."""
    def __init__(self, max_workers=2, thread_name_prefix="map"):
        self.tasks, self.stopped = queue.Queue(), threading.Event()
        self.workers = [threading.Thread(target=self._work, name=f"{thread_name_prefix}-{i}", daemon=True)
                        for i in range(max_workers)]
        for worker in self.workers:
            worker.start()

    def _work(self):
        while True:
            task = self.tasks.get()
            try:
                if task is None:
                    return
                if not self.stopped.is_set():
                    function, args = task
                    function(*args)
            finally:
                self.tasks.task_done()

    def submit(self, function, *args):
        if not self.stopped.is_set():
            self.tasks.put((function, args))

    def shutdown(self, wait=False, cancel_futures=True):
        if self.stopped.is_set():
            return
        self.stopped.set()
        if cancel_futures:
            while True:
                try:
                    self.tasks.get_nowait()
                    self.tasks.task_done()
                except queue.Empty:
                    break
        for _ in self.workers:
            self.tasks.put(None)
        if wait:
            for worker in self.workers:
                worker.join()


class TileCache:
    """Only loads explicitly requested visible tiles; >= seven-day persistent cache."""
    def __init__(self, cache_dir=None, template=None, timeout=8, opener=None):
        self.template = template or os.environ.get("CHAOXING_TILE_URL") or TILE_URL
        if urllib.parse.urlparse(self.template).scheme != "https" or not all("{"+k+"}" in self.template for k in ("z", "x", "y")):
            raise LocationError("地图瓦片地址必须为含 {z}/{x}/{y} 的 HTTPS 模板")
        provider = hashlib.sha256(self.template.encode()).hexdigest()[:16]
        base = Path(cache_dir or Path.home()/".cache/chaoxing-linux/maps/tiles")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        base.chmod(0o700)
        self.cache_dir = base/provider
        self.timeout, self.opener = timeout, opener or urllib.request.urlopen

    def get(self, zoom, x, y):
        if not 2 <= zoom <= 19 or not 0 <= x < 2**zoom or not 0 <= y < 2**zoom:
            raise LocationError("无效地图瓦片")
        path = self.cache_dir/str(zoom)/str(x)/(str(y)+".png")
        metadata = path.with_suffix(".json")
        old = None
        try:
            old = path.read_bytes()
            if time.time()-path.stat().st_mtime < CACHE_AGE:
                return old
        except OSError:
            pass
        headers = {"User-Agent": USER_AGENT, "Accept": "image/png"}
        try:
            meta = json.loads(metadata.read_text())
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("modified"):
                headers["If-Modified-Since"] = meta["modified"]
        except (OSError, ValueError):
            pass
        try:
            request = urllib.request.Request(self.template.format(z=zoom, x=x, y=y), headers=headers)
            with self.opener(request, timeout=self.timeout) as response:
                data = response.read(2_000_001)
                meta = {"etag": response.headers.get("ETag"), "modified": response.headers.get("Last-Modified")}
            if len(data) > 2_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise LocationError("地图服务未返回有效 PNG")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = path.with_suffix(f".{threading.get_ident()}.tmp")
            temp.write_bytes(data)
            temp.chmod(0o600)
            temp.replace(path)
            metadata.write_text(json.dumps(meta))
            metadata.chmod(0o600)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and old:
                path.touch()
                return old
            if old:
                return old
            raise LocationError(f"地图请求失败：HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            if old:
                return old
            raise LocationError(f"地图暂不可用：{exc}") from exc


def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
    return Gtk, Gdk, GdkPixbuf, GLib


class MapPicker:
    def __init__(self, parent=None, initial=None):
        Gtk, Gdk, GdkPixbuf, GLib = _gtk()
        self.Gtk, self.Gdk, self.GdkPixbuf, self.GLib = Gtk, Gdk, GdkPixbuf, GLib
        self.dialog = Gtk.Dialog(title="选择位置", transient_for=parent, modal=True)
        self.dialog.set_default_size(1000, 720)
        self.dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        self.confirm = self.dialog.add_button("确认此位置", Gtk.ResponseType.OK)
        self.confirm.get_style_context().add_class("suggested-action")
        self.confirm.set_sensitive(False)
        self.closed, self.selected = False, None
        self._display_system, self._coordinates_dirty, self._writing_coordinates = "gcj02", False, False
        self.viewport = MapViewport()
        self.tiles, self.searcher = TileCache(), PlaceSearch()
        self.pool = _DaemonPool(max_workers=2, thread_name_prefix="map-tile")
        self.search_pool = _DaemonPool(max_workers=1, thread_name_prefix="map-search")
        self.images, self.pending, self.failed, self.visible = {}, set(), {}, set()
        self.press, self.dragged, self.search_generation = None, False, 0
        content = self.dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        bar = Gtk.Box(spacing=8)
        self.query = Gtk.Entry(placeholder_text="搜索学校、教学楼或地点（点击搜索才发送）")
        self.query.connect("activate", lambda *_: self._search())
        bar.pack_start(self.query, True, True, 0)
        self.search_button = Gtk.Button(label="搜索地点")
        self.search_button.connect("clicked", lambda *_: self._search())
        bar.pack_start(self.search_button, False, False, 0)
        content.pack_start(bar, False, False, 0)
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        pane.set_position(650)
        map_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        controls = Gtk.Box(spacing=5)
        for text, callback in (("＋", lambda *_: self._zoom(1)), ("−", lambda *_: self._zoom(-1)),
                               ("回到选中点", lambda *_: self._recenter()), ("查询此点地址", lambda *_: self._reverse())):
            button = Gtk.Button(label=text)
            button.connect("clicked", callback)
            controls.pack_start(button, False, False, 0)
        map_box.pack_start(controls, False, False, 0)
        self.area = Gtk.DrawingArea()
        self.area.set_size_request(440, 350)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.area.connect("draw", self._draw)
        self.area.connect("button-press-event", self._press)
        self.area.connect("button-release-event", self._release)
        self.area.connect("motion-notify-event", self._motion)
        self.area.connect("scroll-event", self._scroll)
        map_box.pack_start(self.area, True, True, 0)
        attribution = Gtk.Label(xalign=0)
        attribution.set_markup('© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> · <a href="https://www.openstreetmap.org/fixthemap">反馈地图问题</a>')
        map_box.pack_start(attribution, False, False, 0)
        pane.pack1(map_box, True, False)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label="候选地点 / 搜索结果", xalign=0)
        side.pack_start(heading, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.connect("row-selected", self._row_selected)
        scroll.add(self.results)
        side.pack_start(scroll, True, True, 0)
        note = Gtk.Label(label="拖动地图浏览，滚轮缩放，点击地图选点。\n查询公开地点将向 Photon 发送关键词或坐标；地图瓦片由 OpenStreetMap 提供。\n选点只填写位置，不会签到，也不读取手机定位。", xalign=0)
        note.set_line_wrap(True)
        side.pack_start(note, False, False, 0)
        pane.pack2(side, False, False)
        content.pack_start(pane, True, True, 0)
        self.status = Gtk.Label(label="请选择搜索结果或点击地图，也可以手动输入坐标。", xalign=0)
        self.status.set_line_wrap(True)
        content.pack_start(self.status, False, False, 0)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.name = Gtk.Entry(placeholder_text="常用位置名称，例如：一号教学楼")
        self.address = Gtk.Entry(placeholder_text="提交地址（可编辑）")
        grid.attach(Gtk.Label(label="名称", xalign=0), 0, 0, 1, 1)
        grid.attach(self.name, 1, 0, 5, 1)
        grid.attach(Gtk.Label(label="地址", xalign=0), 0, 1, 1, 1)
        grid.attach(self.address, 1, 1, 5, 1)
        self.latitude, self.longitude = Gtk.Entry(), Gtk.Entry()
        self.latitude.set_placeholder_text("纬度")
        self.longitude.set_placeholder_text("经度")
        self.system = Gtk.ComboBoxText()
        self.system.append("gcj02", "GCJ02 · 国内应用")
        self.system.append("wgs84", "WGS84 · 地图原坐标")
        self.system.set_active_id("gcj02")
        self.system.connect("changed", self._system_changed)
        for entry in (self.latitude, self.longitude):
            entry.connect("changed", self._coordinates_changed)
        grid.attach(Gtk.Label(label="坐标", xalign=0), 0, 2, 1, 1)
        grid.attach(self.latitude, 1, 2, 1, 1)
        grid.attach(self.longitude, 2, 2, 1, 1)
        grid.attach(self.system, 3, 2, 1, 1)
        apply = Gtk.Button(label="应用手动坐标")
        apply.connect("clicked", lambda *_: self._apply_manual())
        grid.attach(apply, 4, 2, 1, 1)
        for entry in (self.name, self.address):
            entry.set_hexpand(True)
        content.pack_start(grid, False, False, 0)
        self.dialog.connect("destroy", lambda *_: self._close())
        if initial and initial.get("latitude") is not None and initial.get("longitude") is not None:
            try:
                record = location_record(initial["latitude"], initial["longitude"],
                    coordinate_system=initial.get("coordinate_system", "gcj02"),
                    name=initial.get("name", ""), address=initial.get("address", ""), source=initial.get("source", "saved"))
                self.system.set_active_id(record["coordinate_system"])
                self._select(record)
            except LocationError as exc:
                self.status.set_text(str(exc))

    def _close(self):
        if not self.closed:
            self.closed = True
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.search_pool.shutdown(wait=False, cancel_futures=True)

    def _ui(self, callback, *args):
        def invoke():
            if not self.closed:
                callback(*args)
            return False
        self.GLib.idle_add(invoke)

    def _size(self):
        return self.area.get_allocated_width(), self.area.get_allocated_height()

    def _zoom(self, delta):
        width, height = self._size()
        self.viewport.zoom_at(delta, width/2, height/2, width, height)
        self.area.queue_draw()

    def _draw(self, area, cr):
        width, height = self._size()
        cr.set_source_rgb(.91, .94, .96)
        cr.paint()
        tiles = list(self.viewport.tiles(width, height))
        self.visible = {(z, x, y) for z, x, y, _, _ in tiles}
        for z, x, y, left, top in tiles:
            key = z, x, y
            pixbuf = self.images.get(key)
            if pixbuf:
                self.Gdk.cairo_set_source_pixbuf(cr, pixbuf, left, top)
                cr.rectangle(left, top, 256, 256)
                cr.fill()
            else:
                cr.set_source_rgba(.65, .7, .75, .3)
                cr.rectangle(left, top, 256, 256)
                cr.stroke()
                if key not in self.pending and time.monotonic()-self.failed.get(key, -100) > 60:
                    self.pending.add(key)
                    self.pool.submit(self._load_tile, key)
        cr.set_source_rgba(.1, .2, .35, .8)
        cr.set_font_size(12)
        cr.move_to(10, height-12)
        cr.show_text(f"zoom {self.viewport.zoom}")
        if self.selected:
            px, py = self.viewport.pixel_at(self.selected["wgs84_latitude"], self.selected["wgs84_longitude"], width, height)
            cr.new_path()
            cr.set_source_rgb(.1, .65, .4)
            cr.arc(px, py, 10, 0, 2*math.pi)
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(3)
            cr.stroke()
        return False

    def _load_tile(self, key):
        if self.closed or key not in self.visible:
            self._ui(self.pending.discard, key)
            return
        try:
            data = self.tiles.get(*key)
            self._ui(self._tile_ready, key, data, None)
        except Exception as exc:
            self._ui(self._tile_ready, key, None, str(exc))

    def _tile_ready(self, key, data, error):
        self.pending.discard(key)
        if error:
            self.failed[key] = time.monotonic()
            self.status.set_text(error+"；仍可搜索地点或手动填写坐标。")
        else:
            try:
                loader = self.GdkPixbuf.PixbufLoader.new_with_type("png")
                loader.write(data)
                loader.close()
                self.images[key] = loader.get_pixbuf()
                if len(self.images) > 160:
                    for old in list(self.images):
                        if old not in self.visible:
                            del self.images[old]
                            if len(self.images) <= 120:
                                break
            except Exception:
                self.failed[key] = time.monotonic()
        self.area.queue_draw()

    def _press(self, area, event):
        if event.button == 1:
            self.press = (event.x, event.y, event.x, event.y)
            self.dragged = False
        return True

    def _motion(self, area, event):
        if self.press:
            startx, starty, lastx, lasty = self.press
            if self.dragged or math.hypot(event.x-startx, event.y-starty) > 4:
                self.dragged = True
                self.viewport.pan(event.x-lastx, event.y-lasty)
                self.area.queue_draw()
            self.press = startx, starty, event.x, event.y
        return True

    def _release(self, area, event):
        if event.button == 1 and self.press:
            if not self.dragged:
                lat, lon = self.viewport.coordinates_at(event.x, event.y, *self._size())
                self._select(location_record(lat, lon, coordinate_system="wgs84", name="地图选点", source="map"), center=False)
                self.status.set_text("已选中地图位置。可点击“查询此点地址”，或手动填写名称和地址。")
            self.press = None
        return True

    def _scroll(self, area, event):
        delta = 1 if event.direction == self.Gdk.ScrollDirection.UP else -1
        if event.direction == self.Gdk.ScrollDirection.SMOOTH:
            _, _, dy = event.get_scroll_deltas()
            if abs(dy) < .05:
                return True
            delta = -1 if dy > 0 else 1
        self.viewport.zoom_at(delta, event.x, event.y, *self._size())
        self.area.queue_draw()
        return True

    def _select(self, record, center=True):
        self.selected = record
        self.name.set_text(record.get("name", ""))
        self.address.set_text(record.get("address", ""))
        self._write_coordinates()
        if center:
            self.viewport.latitude, self.viewport.longitude = record["wgs84_latitude"], record["wgs84_longitude"]
            self.viewport.zoom = 16
        self.confirm.set_sensitive(True)
        self.area.queue_draw()

    def _coordinates_changed(self, *_):
        if not self._writing_coordinates:
            self._coordinates_dirty = True

    def _write_coordinates(self):
        if self.selected:
            lat, lon = self.selected["wgs84_latitude"], self.selected["wgs84_longitude"]
            if self.system.get_active_id() == "gcj02":
                lat, lon = wgs84_to_gcj02(lat, lon)
            self._writing_coordinates = True
            try:
                self.latitude.set_text(f"{lat:.7f}")
                self.longitude.set_text(f"{lon:.7f}")
            finally:
                self._writing_coordinates = False
            self._coordinates_dirty = False
        self._display_system = self.system.get_active_id()

    def _system_changed(self, *_):
        new_system = self.system.get_active_id()
        if self.selected and new_system != self._display_system:
            if self._coordinates_dirty:
                try:
                    self.selected = location_record(self.latitude.get_text(), self.longitude.get_text(),
                        coordinate_system=self._display_system, name=self.name.get_text(), address=self.address.get_text(), source="manual")
                except LocationError as exc:
                    self.system.set_active_id(self._display_system)
                    self.status.set_text(str(exc)+"；请先修正手动坐标，再切换坐标系。")
                    return
            self._write_coordinates()
            self.area.queue_draw()
        else:
            self._display_system = new_system

    def _apply_manual(self):
        try:
            record = location_record(self.latitude.get_text(), self.longitude.get_text(),
                coordinate_system=self.system.get_active_id(), name=self.name.get_text(), address=self.address.get_text())
            self._select(record)
            self.status.set_text("已应用手动坐标。地图显示 WGS84，返回坐标系见下方选项。")
            return True
        except LocationError as exc:
            self.status.set_text(str(exc))
            return False

    def _recenter(self):
        if self.selected:
            self.viewport.latitude, self.viewport.longitude = self.selected["wgs84_latitude"], self.selected["wgs84_longitude"]
            self.area.queue_draw()

    def _search(self):
        query = self.query.get_text().strip()
        if len(query) < 2:
            self.status.set_text("请输入至少两个字的地点名称。")
            return
        self._search_task(lambda: self.searcher.search(query))

    def _reverse(self):
        if not self.selected:
            self.status.set_text("请先点击地图选择一个位置。")
            return
        lat, lon = self.selected["wgs84_latitude"], self.selected["wgs84_longitude"]
        self._search_task(lambda: self.searcher.reverse(lat, lon))

    def _search_task(self, task):
        self.search_generation += 1
        generation = self.search_generation
        self.search_button.set_sensitive(False)
        self.status.set_text("正在查询公开地点服务…")
        def work():
            try:
                records, error = task(), None
            except Exception as exc:
                records, error = [], str(exc)
            self._ui(self._search_ready, generation, records, error)
        self.search_pool.submit(work)

    def _search_ready(self, generation, records, error):
        if generation != self.search_generation:
            return
        self.search_button.set_sensitive(True)
        for row in self.results.get_children():
            self.results.remove(row)
        for record in records:
            row = self.Gtk.ListBoxRow()
            row.location = record
            text = self.Gtk.Label(label=record["name"]+"\n"+record["address"], xalign=0)
            text.set_line_wrap(True)
            text.set_max_width_chars(30)
            text.set_margin_top(9)
            text.set_margin_bottom(9)
            row.add(text)
            self.results.add(row)
        self.results.show_all()
        self.status.set_text(error or (f"找到 {len(records)} 个地点，点击右侧结果后确认。" if records else "未找到地点；请尝试学校全称或城市名，或在地图选点。"))

    def _row_selected(self, box, row):
        if row is not None:
            self._select(row.location)

    def run_location(self):
        self.dialog.show_all()
        result = None
        try:
            while self.dialog.run() == self.Gtk.ResponseType.OK:
                if not self.selected:
                    continue
                try:
                    result = location_record(self.latitude.get_text(), self.longitude.get_text(),
                        coordinate_system=self.system.get_active_id(), name=self.name.get_text(), address=self.address.get_text(),
                        source=self.selected.get("source", "manual"))
                    if max(abs(result[k]-self.selected[k]) for k in ("wgs84_latitude", "wgs84_longitude")) > 1e-6:
                        result["source"] = "manual"
                    break
                except LocationError as exc:
                    self.status.set_text(str(exc))
        finally:
            self.dialog.destroy()
            self._close()
        return result


def choose_location(parent=None, initial=None):
    """Return an explicitly confirmed location, or None on cancel. No sign-in is sent."""
    return MapPicker(parent, initial).run_location()
