#!/usr/bin/env python3
"""War Thunder 8111 投弹提醒器。

只读取游戏内置的 localhost HTTP 接口，不注入游戏进程。
运行后在 http://127.0.0.1:8112 打开本地仪表盘。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import mimetypes
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
WT_ORIGIN = "http://127.0.0.1:8111"
G = 9.80665
BOMB_CHART_SHEET_ID = "1oNwp_MXszU5J2dcaz5IoCtSAQ-infPdOWhwtJXqtrwU"
BOMB_CHART_SOURCE_URL = (
    f"https://docs.google.com/spreadsheets/d/{BOMB_CHART_SHEET_ID}/edit?gid=1447098598#gid=1447098598"
)
BASE_HP_TIERS = [
    {"min_br": 1.0, "max_br": 2.0, "hp": 4000},
    {"min_br": 2.3, "max_br": 3.3, "hp": 6000},
    {"min_br": 3.7, "max_br": 4.7, "hp": 10000},
    {"min_br": 5.0, "max_br": 6.3, "hp": 16000},
    {"min_br": 6.7, "max_br": 7.7, "hp": 22000},
    {"min_br": 8.0, "max_br": 13.7, "hp": 25900},
]
NATION_SHEETS = [
    "🇺🇸 USA",
    "🇩🇪 Germany",
    "🇷🇺 USSR",
    "🇬🇧 UK",
    "🇯🇵 Japan",
    "🇨🇳 China",
    "🇮🇹 Italy",
    "🇫🇷 France",
    "🇸🇪 Sweden",
    "🇮🇱 Israel",
]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(data.get(key))
        if value is not None:
            return value
    return None


def parse_mass_to_kg(value: str) -> float | None:
    """把表格中的 kg/lb 重量统一为 kg。"""
    match = re.search(r"([\d,.]+)\s*(kg|lb)", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    return number if match.group(2).lower() == "kg" else number * 0.45359237


def _cell_number(value: str) -> float | None:
    match = re.search(r"-?[\d,.]+", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_bomb_chart_csv(content: str) -> list[dict[str, Any]]:
    """解析 LEGION Bomb Chart；兼容 Google 合并单元格造成的空计数字段。"""
    bombs: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(content)):
        if len(row) < 16:
            row += [""] * (16 - len(row))
        damage = _cell_number(row[15])
        full_name = row[6].strip()
        if damage is None or not full_name:
            continue
        counts = [int(value) if value is not None else None for value in (_cell_number(cell) for cell in row[8:14])]
        bombs.append(
            {
                "chart_name": row[5].strip() or full_name,
                "full_name": full_name,
                "type": row[7].strip(),
                "actual_weight_kg": parse_mass_to_kg(row[3]),
                "tnt_equivalent_kg": parse_mass_to_kg(row[4]),
                "damage": damage,
                "counts": counts,
            }
        )

    # 变体弹药经常共用一个合并的计数单元格；相同伤害值可安全继承同组计数。
    counts_by_damage = {
        bomb["damage"]: bomb["counts"]
        for bomb in bombs
        if all(value is not None for value in bomb["counts"])
    }
    for bomb in bombs:
        inherited = counts_by_damage.get(bomb["damage"])
        if inherited and any(value is None for value in bomb["counts"]):
            bomb["counts"] = [own if own is not None else inherited[index] for index, own in enumerate(bomb["counts"])]
    return [bomb for bomb in bombs if any(value is not None for value in bomb["counts"])]


def parse_aircraft_sheet_csv(content: str, nation: str) -> list[dict[str, Any]]:
    aircraft: list[dict[str, Any]] = []
    by_key: dict[tuple[str, float], dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for row in csv.reader(io.StringIO(content)):
        if len(row) < 5:
            continue
        lines = [line.strip() for line in row[4].splitlines() if line.strip()]
        if len(lines) >= 2:
            br = _cell_number(lines[-1])
            name = " ".join(lines[:-1]).strip()
            if br is not None and name and 1.0 <= br <= 20.0:
                key = (name.casefold(), br)
                current = by_key.get(key)
                if current is None:
                    current = {"name": name, "br": br, "nation": nation, "bomb_names": []}
                    by_key[key] = current
                    aircraft.append(current)

        if current is None:
            continue
        known = set(current["bomb_names"])
        for cell in row[6:16:2]:
            for loadout_line in cell.splitlines():
                match = re.match(r"\s*(.+?)\s*[×x]\s*\d+\s*$", loadout_line)
                if match and match.group(1).strip() not in known:
                    name = match.group(1).strip()
                    current["bomb_names"].append(name)
                    known.add(name)
    return aircraft


def normalize_aircraft_name(value: str) -> str:
    value = value.casefold().replace("_", "-")
    # 8111 常带内部后缀（例如 f-104s_cb）；先保留主体型号，再去除标点。
    value = re.sub(r"-(?:cb|late|early|research|event|premium)$", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def match_aircraft_br(raw_name: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = normalize_aircraft_name(raw_name)
    if not needle:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in catalog:
        normalized = normalize_aircraft_name(str(item.get("name", "")))
        if normalized == needle:
            return item
        if len(normalized) >= 4 and (needle.startswith(normalized) or normalized.startswith(needle)):
            candidates.append((len(normalized), item))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


class BombChartService:
    """按需读取公开社区表格，并在内存中缓存，避免轮询 Google。"""

    def __init__(self, cache_seconds: float = 21600.0):
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._bombs: list[dict[str, Any]] = []
        self._aircraft: list[dict[str, Any]] = []

    @staticmethod
    def _download(sheet_name: str) -> str:
        url = (
            f"https://docs.google.com/spreadsheets/d/{BOMB_CHART_SHEET_ID}/gviz/tq"
            f"?tqx=out:csv&sheet={quote(sheet_name)}"
        )
        request = urllib.request.Request(url, headers={"User-Agent": "WT-Bomb-Alert/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8-sig")

    def _refresh(self) -> None:
        bombs = parse_bomb_chart_csv(self._download("💣 Bomb Chart"))
        aircraft: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._download, sheet): sheet for sheet in NATION_SHEETS}
            for future in as_completed(futures):
                aircraft.extend(parse_aircraft_sheet_csv(future.result(), futures[future]))
        if not bombs:
            raise ValueError("Bomb Chart 没有可用炸弹数据")
        self._bombs = bombs
        self._aircraft = aircraft
        self._loaded_at = time.time()

    def payload(self, aircraft_raw: str) -> dict[str, Any]:
        with self._lock:
            if not self._bombs or time.time() - self._loaded_at > self.cache_seconds:
                self._refresh()
            matched = match_aircraft_br(aircraft_raw, self._aircraft)
            return {
                "source": {"title": "LEGION's Loadouts · Bomb Chart", "url": BOMB_CHART_SOURCE_URL},
                "note": "社区表格数据；默认按四基地地图计算，游戏更新后请手动核对。",
                "tiers": BASE_HP_TIERS,
                "bombs": self._bombs,
                "aircraft_match": matched,
                "loaded_at": self._loaded_at,
            }


def normalize(x: float, y: float) -> tuple[float, float] | None:
    length = math.hypot(x, y)
    if length < 1e-9:
        return None
    return x / length, y / length


def find_player(objects: Any) -> dict[str, Any] | None:
    if not isinstance(objects, list):
        return None
    for item in objects:
        if not isinstance(item, dict):
            continue
        if str(item.get("icon", "")).lower() == "player":
            return item
    return None


ZONE_TYPES: dict[str, tuple[str, str]] = {
    "bombing_point": ("轰炸区", "bombing"),
    "airfield": ("机场", "airfield"),
    "capture_zone": ("占领区", "capture"),
    "defending_point": ("防守区", "capture"),
}


def classify_team(item: dict[str, Any]) -> str:
    rgb = item.get("color[]")
    if isinstance(rgb, list) and len(rgb) >= 3:
        red, green, blue = (_number(rgb[i]) or 0.0 for i in range(3))
    else:
        color = str(item.get("color", "")).lstrip("#")
        if len(color) not in (6, 8):
            return "neutral"
        try:
            red, green, blue = (int(color[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return "neutral"
    if red > blue * 1.25 and red > green * 1.25:
        return "hostile"
    if blue > red * 1.15 or green > red * 1.15:
        return "friendly"
    return "neutral"


def extract_safe_zones(objects: Any) -> list[dict[str, Any]]:
    """只提取游戏战术地图上公开的固定任务区域，明确排除作战单位。"""
    if not isinstance(objects, list):
        return []

    zones: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in objects:
        if not isinstance(item, dict):
            continue
        object_type = str(item.get("type", "")).lower()
        zone_definition = ZONE_TYPES.get(object_type)
        if zone_definition is None:
            continue

        x = _number(item.get("x"))
        y = _number(item.get("y"))
        if x is None or y is None:
            sx, sy = _number(item.get("sx")), _number(item.get("sy"))
            ex, ey = _number(item.get("ex")), _number(item.get("ey"))
            if None not in (sx, sy, ex, ey):
                x = (float(sx) + float(ex)) / 2.0
                y = (float(sy) + float(ey)) / 2.0
        if x is None or y is None or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue

        label_base, kind = zone_definition
        counts[kind] = counts.get(kind, 0) + 1
        number = counts[kind]

        team = classify_team(item)

        short_prefix = {"bombing": "B", "airfield": "A", "capture": "Z"}[kind]
        zones.append(
            {
                "id": f"{kind}-{round(float(x), 6)}-{round(float(y), 6)}",
                "kind": kind,
                "team": team,
                "label": f"{label_base} {number}",
                "short_label": f"{short_prefix}{number}",
                "x": float(x),
                "y": float(y),
            }
        )
    return zones


def extract_friendly_aircraft(
    objects: Any, bounds: tuple[float, float, float, float] | None
) -> list[dict[str, Any]]:
    """提取明确为友方颜色的飞机；红色和阵营不明对象一律不返回。"""
    if not isinstance(objects, list):
        return []
    width = (bounds[2] - bounds[0]) if bounds else 1.0
    height = (bounds[3] - bounds[1]) if bounds else 1.0
    allies: list[dict[str, Any]] = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() != "aircraft":
            continue
        if str(item.get("icon", "")).lower() == "player":
            continue
        if classify_team(item) != "friendly":
            continue
        x, y = _number(item.get("x")), _number(item.get("y"))
        dx, dy = _number(item.get("dx")), _number(item.get("dy"))
        if None in (x, y, dx, dy):
            continue
        heading = normalize(float(dx) * width, float(dy) * height)
        if heading is None:
            continue
        heading_deg = (
            math.degrees(math.atan2(heading[0], -heading[1])) + 360.0
        ) % 360.0
        number = len(allies) + 1
        allies.append(
            {
                "id": f"ally-{number}",
                "label": f"友机 {number}",
                "aircraft_type": str(item.get("icon", "Aircraft")),
                "x": float(x),
                "y": float(y),
                "heading_deg": heading_deg,
            }
        )
    return allies


def map_bounds(map_info: dict[str, Any]) -> tuple[float, float, float, float] | None:
    minimum = map_info.get("map_min")
    maximum = map_info.get("map_max")
    if not (
        isinstance(minimum, list)
        and isinstance(maximum, list)
        and len(minimum) >= 2
        and len(maximum) >= 2
    ):
        return None
    values = [_number(v) for v in (minimum[0], minimum[1], maximum[0], maximum[1])]
    if any(v is None for v in values):
        return None
    min_x, min_y, max_x, max_y = (float(v) for v in values)
    if max_x <= min_x or max_y <= min_y:
        return None
    return min_x, min_y, max_x, max_y


def normalized_to_world(
    x: float, y: float, bounds: tuple[float, float, float, float]
) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = bounds
    return min_x + x * (max_x - min_x), min_y + y * (max_y - min_y)


def fall_time(height_m: float, vertical_speed_mps: float) -> float | None:
    """无空气阻力的落地时间；向上为正。"""
    if height_m <= 0:
        return None
    discriminant = vertical_speed_mps**2 + 2 * G * height_m
    return (vertical_speed_mps + math.sqrt(discriminant)) / G


@dataclass(frozen=True)
class SolverSettings:
    target_elevation_m: float = 0.0
    horizontal_retention: float = 0.92
    calibration_seconds: float = 0.0
    approach_warning_seconds: float = 20.0
    release_warning_seconds: float = 5.0
    release_window_seconds: float = 0.8
    max_cross_track_m: float = 800.0


def solve_release(
    state: dict[str, Any],
    indicators: dict[str, Any],
    map_info: dict[str, Any],
    map_objects: Any,
    target: tuple[float, float] | None,
    settings: SolverSettings,
) -> dict[str, Any]:
    """根据己机遥测和手动目标点计算简化的自由落体投弹解。"""
    valid_state = state.get("valid") is not False
    valid_indicators = indicators.get("valid") is not False
    player = find_player(map_objects)
    bounds = map_bounds(map_info)

    result: dict[str, Any] = {
        "status": "waiting",
        "message": "等待游戏遥测",
        "player": None,
        "target": list(target) if target else None,
    }

    if not valid_state or not valid_indicators or player is None or bounds is None:
        return result

    px = _number(player.get("x"))
    py = _number(player.get("y"))
    dx = _number(player.get("dx"))
    dy = _number(player.get("dy"))
    if None in (px, py, dx, dy):
        result["message"] = "己机地图坐标暂不可用"
        return result

    px, py, dx, dy = float(px), float(py), float(dx), float(dy)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    heading = normalize(dx * width, dy * height)
    if heading is None:
        result["message"] = "己机航向暂不可用"
        return result

    heading_x, heading_y = heading
    heading_deg = (math.degrees(math.atan2(heading_x, -heading_y)) + 360.0) % 360.0
    result["player"] = {"x": px, "y": py, "heading_deg": heading_deg}

    altitude_m = first_number(state, "H, m")
    tas_kmh = first_number(state, "TAS, km/h")
    ias_kmh = first_number(state, "IAS, km/h")
    vertical_speed = first_number(state, "Vy, m/s")
    roll_deg = first_number(indicators, "aviahorizon_roll", "bank")
    pitch_deg = first_number(indicators, "aviahorizon_pitch")

    if altitude_m is None or tas_kmh is None:
        result["message"] = "当前载具未提供高度或真空速"
        return result

    vertical_speed = vertical_speed or 0.0
    tas_mps = max(0.0, tas_kmh / 3.6)
    horizontal_speed = math.sqrt(max(0.0, tas_mps**2 - vertical_speed**2))
    height_agl = altitude_m - settings.target_elevation_m
    time_fall = fall_time(height_agl, vertical_speed)

    result["telemetry"] = {
        "altitude_m": altitude_m,
        "height_over_target_m": height_agl,
        "tas_kmh": tas_kmh,
        "ias_kmh": ias_kmh,
        "vertical_speed_mps": vertical_speed,
        "roll_deg": roll_deg,
        "pitch_deg": pitch_deg,
    }

    if target is None:
        result["status"] = "select_target"
        result["message"] = "请在地图上点击投弹目标"
        return result
    if time_fall is None:
        result["status"] = "invalid_altitude"
        result["message"] = "飞机高度必须高于目标标高"
        return result

    tx, ty = target
    player_world = normalized_to_world(px, py, bounds)
    target_world = normalized_to_world(tx, ty, bounds)
    rel_x = target_world[0] - player_world[0]
    rel_y = target_world[1] - player_world[1]
    distance = math.hypot(rel_x, rel_y)
    along_track = rel_x * heading_x + rel_y * heading_y
    cross_track = abs(rel_x * heading_y - rel_y * heading_x)
    closing_speed = horizontal_speed * (along_track / distance) if distance > 1 else 0.0

    retention = min(1.2, max(0.3, settings.horizontal_retention))
    release_distance = horizontal_speed * time_fall * retention
    seconds_to_release = None
    if closing_speed > 1.0:
        seconds_to_release = (
            (along_track - release_distance) / closing_speed
            + settings.calibration_seconds
        )

    bearing_to_target = (
        math.degrees(math.atan2(rel_x, -rel_y)) + 360.0
    ) % 360.0
    alignment_deg = abs((bearing_to_target - heading_deg + 180.0) % 360.0 - 180.0)

    result.update(
        {
            "distance_m": distance,
            "along_track_m": along_track,
            "cross_track_m": cross_track,
            "bearing_to_target_deg": bearing_to_target,
            "alignment_error_deg": alignment_deg,
            "fall_time_s": time_fall,
            "release_distance_m": release_distance,
            "seconds_to_release": seconds_to_release,
        }
    )

    if along_track <= 0 or closing_speed <= 1.0:
        result["status"] = "not_closing"
        result["message"] = "目标不在当前航向前方"
    elif cross_track > settings.max_cross_track_m:
        result["status"] = "misaligned"
        result["message"] = f"修正航向：偏离投弹航线 {cross_track:.0f} m"
    elif seconds_to_release is None:
        result["status"] = "waiting"
        result["message"] = "无法计算接近速度"
    elif seconds_to_release < -settings.release_window_seconds:
        result["status"] = "passed"
        result["message"] = "已越过计算投弹点"
    elif seconds_to_release <= settings.release_window_seconds:
        result["status"] = "release"
        result["message"] = "立即投弹"
    elif seconds_to_release <= settings.release_warning_seconds:
        result["status"] = "countdown"
        result["message"] = f"准备投弹：{seconds_to_release:.1f} 秒"
    elif seconds_to_release <= settings.approach_warning_seconds:
        result["status"] = "approaching"
        result["message"] = f"进入投弹航线：{seconds_to_release:.1f} 秒"
    else:
        result["status"] = "enroute"
        result["message"] = f"距投弹点 {seconds_to_release:.1f} 秒"
    return result


class WTClient:
    def __init__(self, origin: str = WT_ORIGIN, timeout: float = 0.45):
        self.origin = origin.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        request = urllib.request.Request(
            f"{self.origin}{path}", headers={"User-Agent": "WT-Bomb-Alert/1.0"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def json(self, path: str) -> Any:
        return json.loads(self._get(path).decode("utf-8"))

    def bytes(self, path: str) -> bytes:
        return self._get(path)


class DemoClient(WTClient):
    def __init__(self) -> None:
        super().__init__("demo://local")
        self.started = time.monotonic()

    def json(self, path: str) -> Any:
        elapsed = time.monotonic() - self.started
        x = 0.20 + min(0.48, elapsed * 0.004)
        if path == "/state":
            return {
                "valid": True,
                "H, m": 3150.0,
                "TAS, km/h": 610.0,
                "IAS, km/h": 575.0,
                "Vy, m/s": -1.8,
            }
        if path == "/indicators":
            return {
                "valid": True,
                "type": "Demo bomber",
                "aviahorizon_roll": 1.4,
                "aviahorizon_pitch": -0.6,
            }
        if path == "/map_info.json":
            return {
                "map_min": [-32768.0, -32768.0],
                "map_max": [32768.0, 32768.0],
                "grid_steps": [8192.0, 8192.0],
            }
        if path == "/map_obj.json":
            return [
                {
                    "type": "aircraft",
                    "icon": "Player",
                    "x": x,
                    "y": 0.52,
                    "dx": 1.0,
                    "dy": 0.0,
                },
                {
                    "type": "bombing_point",
                    "icon": "bombing_point",
                    "color[]": [250, 12, 0],
                    "x": 0.68,
                    "y": 0.52,
                },
                {
                    "type": "bombing_point",
                    "icon": "bombing_point",
                    "color[]": [250, 12, 0],
                    "x": 0.79,
                    "y": 0.43,
                },
                {
                    "type": "airfield",
                    "icon": "none",
                    "color[]": [23, 77, 255],
                    "sx": 0.84,
                    "sy": 0.63,
                    "ex": 0.76,
                    "ey": 0.64,
                },
                {
                    "type": "aircraft",
                    "icon": "Fighter",
                    "color[]": [23, 77, 255],
                    "x": max(0.0, x - 0.06),
                    "y": 0.46,
                    "dx": 0.96,
                    "dy": 0.12,
                },
                {
                    "type": "aircraft",
                    "icon": "Fighter",
                    "color[]": [250, 12, 0],
                    "x": 0.72,
                    "y": 0.38,
                    "dx": -0.8,
                    "dy": 0.2,
                },
            ]
        raise urllib.error.URLError(f"unknown demo endpoint {path}")

    def bytes(self, path: str) -> bytes:
        if path == "/map.img":
            return (WEB_ROOT / "demo-map.svg").read_bytes()
        raise urllib.error.URLError(f"unknown demo endpoint {path}")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], client: WTClient):
        super().__init__(address, DashboardHandler)
        self.wt_client = client
        self.demo = isinstance(client, DemoClient)
        self.bomb_chart = BombChartService()

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class DashboardHandler(SimpleHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        # 只记录错误，避免 10 Hz 轮询刷屏。
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(format, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            self._snapshot(parse_qs(parsed.query))
            return
        if parsed.path == "/api/bomb-chart":
            self._bomb_chart(parse_qs(parsed.query))
            return
        if parsed.path == "/api/map":
            self._map()
            return
        if parsed.path == "/api/health":
            self._json_response({"ok": True, "demo": self.server.demo})
            return
        self._static(parsed.path)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    @staticmethod
    def _query_float(query: dict[str, list[str]], key: str, default: float) -> float:
        try:
            return float(query.get(key, [str(default)])[0])
        except (TypeError, ValueError):
            return default

    def _snapshot(self, query: dict[str, list[str]]) -> None:
        target = None
        if "tx" in query and "ty" in query:
            try:
                target = (
                    min(1.0, max(0.0, float(query["tx"][0]))),
                    min(1.0, max(0.0, float(query["ty"][0]))),
                )
            except (TypeError, ValueError, IndexError):
                target = None

        settings = SolverSettings(
            target_elevation_m=self._query_float(query, "elevation", 0.0),
            horizontal_retention=self._query_float(query, "retention", 0.92),
            calibration_seconds=self._query_float(query, "calibration", 0.0),
            approach_warning_seconds=self._query_float(query, "approach", 20.0),
            release_warning_seconds=self._query_float(query, "ready", 5.0),
            max_cross_track_m=self._query_float(query, "corridor", 800.0),
        )

        try:
            state = self.server.wt_client.json("/state")
            indicators = self.server.wt_client.json("/indicators")
            map_info = self.server.wt_client.json("/map_info.json")
            map_objects = self.server.wt_client.json("/map_obj.json")
            solution = solve_release(
                state if isinstance(state, dict) else {},
                indicators if isinstance(indicators, dict) else {},
                map_info if isinstance(map_info, dict) else {},
                map_objects,
                target,
                settings,
            )
            zones = extract_safe_zones(map_objects)
            allies = extract_friendly_aircraft(map_objects, map_bounds(map_info))
            self._json_response(
                {
                    "connected": True,
                    "demo": self.server.demo,
                    "aircraft": indicators.get("type") if isinstance(indicators, dict) else None,
                    "solution": solution,
                    "zones": zones,
                    "allies": allies,
                    "map_generation": map_info.get("map_generation") if isinstance(map_info, dict) else None,
                    "timestamp": time.time(),
                }
            )
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            self._json_response(
                {
                    "connected": False,
                    "demo": self.server.demo,
                    "error": "未连接到战争雷霆 8111；请进入试飞或战斗",
                    "detail": str(exc),
                    "timestamp": time.time(),
                }
            )

    def _bomb_chart(self, query: dict[str, list[str]]) -> None:
        aircraft = query.get("aircraft", [""])[0][:120]
        try:
            self._json_response({"available": True, **self.server.bomb_chart.payload(aircraft)})
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            self._json_response(
                {
                    "available": False,
                    "error": "无法读取公开 Bomb Chart；请检查网络后重试",
                    "detail": str(exc),
                    "source": {"title": "LEGION's Loadouts · Bomb Chart", "url": BOMB_CHART_SOURCE_URL},
                },
                503,
            )

    def _map(self) -> None:
        try:
            content = self.server.wt_client.bytes("/map.img")
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type = "image/png"
            elif content.startswith((b"GIF87a", b"GIF89a")):
                content_type = "image/gif"
            elif content.lstrip().startswith(b"<svg"):
                content_type = "image/svg+xml"
            else:
                content_type = "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (OSError, TimeoutError, urllib.error.URLError):
            self.send_error(503, "War Thunder map unavailable")

    def _json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="War Thunder 8111 投弹提醒器")
    parser.add_argument("--host", default="127.0.0.1", help="仪表盘监听地址")
    parser.add_argument("--port", default=8112, type=int, help="仪表盘监听端口")
    parser.add_argument("--wt-origin", default=WT_ORIGIN, help="War Thunder 8111 地址")
    parser.add_argument("--demo", action="store_true", help="使用模拟遥测预览界面")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client: WTClient = DemoClient() if args.demo else WTClient(args.wt_origin)
    server = DashboardServer((args.host, args.port), client)
    print(f"WT 投弹提醒器已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止。" + (" 当前为演示模式。" if args.demo else " 请先进入试飞或战斗。"))
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
