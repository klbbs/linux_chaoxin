"""Coordinates and opt-in public place search. This module never submits attendance."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

USER_AGENT = "ChaoxingLinux/0.5.2 (Linux desktop interactive map picker)"
PHOTON_URL = "https://photon.komoot.io"
_A, _EE = 6378245.0, 0.00669342162296594323


class LocationError(ValueError):
    pass


def validate_coordinates(latitude, longitude):
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise LocationError("经纬度必须是数字") from exc
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise LocationError("纬度应为 -90 至 90，经度应为 -180 至 180")
    return lat, lon


def _outside_china(lat, lon):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def wgs84_to_gcj02(latitude, longitude):
    lat, lon = validate_coordinates(latitude, longitude)
    if _outside_china(lat, lon):
        return lat, lon
    x, y = lon - 105, lat - 35
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    dlat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi))*2/3
    dlat += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    dlon += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlon += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi))*2/3
    dlon += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi))*2/3
    rad = lat/180*math.pi
    magic = 1 - _EE*math.sin(rad)**2
    dlat = dlat*180/((_A*(1-_EE))/(magic*math.sqrt(magic))*math.pi)
    dlon = dlon*180/(_A/math.sqrt(magic)*math.cos(rad)*math.pi)
    return lat+dlat, lon+dlon


def gcj02_to_wgs84(latitude, longitude):
    lat, lon = validate_coordinates(latitude, longitude)
    if _outside_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(12):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        dlat, dlon = glat-lat, glon-lon
        wlat, wlon = wlat-dlat, wlon-dlon
        if max(abs(dlat), abs(dlon)) < 1e-9:
            break
    return wlat, wlon


def location_record(latitude, longitude, *, coordinate_system="gcj02", name="", address="", source="manual"):
    lat, lon = validate_coordinates(latitude, longitude)
    if coordinate_system not in ("wgs84", "gcj02"):
        raise LocationError("仅支持 WGS84 和 GCJ02 坐标")
    wlat, wlon = (lat, lon) if coordinate_system == "wgs84" else gcj02_to_wgs84(lat, lon)
    return {"latitude": lat, "longitude": lon, "coordinate_system": coordinate_system,
            "wgs84_latitude": wlat, "wgs84_longitude": wlon,
            "name": str(name).strip(), "address": str(address).strip(), "source": source}


def format_coordinates(latitude, longitude):
    lat, lon = validate_coordinates(latitude, longitude)
    return f"{lat:.6f}, {lon:.6f}"


def project(latitude, longitude, zoom):
    lat, lon = validate_coordinates(latitude, longitude)
    lat = max(-85.05112878, min(85.05112878, lat))
    size = 256 * 2**zoom
    sin = math.sin(math.radians(lat))
    return (lon+180)/360*size, (.5-math.log((1+sin)/(1-sin))/(4*math.pi))*size


def unproject(x, y, zoom):
    size = 256 * 2**zoom
    lon = (x/size*360) % 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi*(1-2*max(0, min(size, y))/size))))
    return lat, lon


class MapViewport:
    def __init__(self, latitude=39.9042, longitude=116.4074, zoom=12):
        self.latitude, self.longitude = validate_coordinates(latitude, longitude)
        self.zoom = max(2, min(19, int(zoom)))

    def coordinates_at(self, x, y, width, height):
        cx, cy = project(self.latitude, self.longitude, self.zoom)
        return unproject(cx+x-width/2, cy+y-height/2, self.zoom)

    def pixel_at(self, latitude, longitude, width, height):
        cx, cy = project(self.latitude, self.longitude, self.zoom)
        px, py = project(latitude, longitude, self.zoom)
        world = 256*2**self.zoom
        dx = (px-cx+world/2) % world-world/2
        return width/2+dx, height/2+py-cy

    def pan(self, dx, dy):
        cx, cy = project(self.latitude, self.longitude, self.zoom)
        self.latitude, self.longitude = unproject(cx-dx, cy-dy, self.zoom)

    def zoom_at(self, delta, x, y, width, height):
        anchor = self.coordinates_at(x, y, width, height)
        self.zoom = max(2, min(19, self.zoom+delta))
        px, py = project(*anchor, self.zoom)
        self.latitude, self.longitude = unproject(px-x+width/2, py-y+height/2, self.zoom)

    def tiles(self, width, height):
        cx, cy = project(self.latitude, self.longitude, self.zoom)
        left, top = cx-width/2, cy-height/2
        count = 2**self.zoom
        for x in range(math.floor(left/256), math.ceil((left+width)/256)):
            for y in range(max(0, math.floor(top/256)), min(count, math.ceil((top+height)/256))):
                yield self.zoom, x % count, y, x*256-left, y*256-top


class PlaceSearch:
    """Photon requests only when explicitly invoked, serialized, cached and throttled."""
    def __init__(self, base_url=None, cache_dir=None, timeout=12, opener=None):
        self.base_url = (base_url or os.environ.get("CHAOXING_PHOTON_URL") or PHOTON_URL).rstrip("/")
        if urllib.parse.urlparse(self.base_url).scheme != "https":
            raise LocationError("地点搜索服务必须使用 HTTPS")
        self.cache_dir = Path(cache_dir or Path.home()/".cache/chaoxing-linux/maps/search")
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.chmod(0o700)
        self.timeout, self.opener = timeout, opener or urllib.request.urlopen
        self._lock, self._last = threading.Lock(), 0.0

    def _request(self, endpoint, params):
        url = self.base_url+endpoint+"?"+urllib.parse.urlencode(params)
        path = self.cache_dir/(hashlib.sha256(url.encode()).hexdigest()+".json")
        with self._lock:
            try:
                if time.time()-path.stat().st_mtime < 7*86400:
                    return json.loads(path.read_text())
            except (OSError, ValueError):
                pass
            time.sleep(max(0, 1-(time.monotonic()-self._last)))
            self._last = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with self.opener(req, timeout=self.timeout) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("服务响应过大")
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                    raise ValueError("服务返回格式错误")
            except (OSError, ValueError) as exc:
                raise LocationError(f"地点服务暂不可用：{exc}") from exc
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False))
                temporary.chmod(0o600)
                temporary.replace(path)
            except OSError:
                pass
            return data

    @staticmethod
    def _places(data):
        places = []
        for feature in data["features"]:
            try:
                lon, lat = feature["geometry"]["coordinates"][:2]
                props = feature.get("properties", {})
                address = " · ".join(dict.fromkeys(str(props[k]) for k in
                    ("country", "state", "city", "district", "street", "housenumber", "name") if props.get(k)))
                places.append(location_record(lat, lon, coordinate_system="wgs84", name=props.get("name", "地点"), address=address, source="photon"))
            except (KeyError, TypeError, ValueError):
                continue
        return places

    def search(self, query):
        query = str(query).strip()
        if len(query) < 2:
            raise LocationError("请输入至少两个字的地点名称")
        return self._places(self._request("/api/", {"q": query[:200], "limit": 8}))

    def reverse(self, latitude, longitude):
        lat, lon = validate_coordinates(latitude, longitude)
        return self._places(self._request("/reverse", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "limit": 1}))
