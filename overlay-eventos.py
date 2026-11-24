"""Draw a Windows overlay from rAthena, a local JSON snapshot or a simulation.

An external JSON producer replaces the snapshot at least every 750 ms.
"""

import argparse
import sys
import ctypes
from ctypes import wintypes
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from subprocess import list2cmdline
import tempfile
import threading
import time
import tkinter as tk
import collections
from dataclasses import dataclass
from threading import Event, Lock, Thread

import psutil
from scapy.all import conf

import win32_input
from win32_input import (
    INPUT, INPUT_KEYBOARD, INPUT_MOUSE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEINPUT,
    POINTER_TOUCH_INFO, PT_TOUCH, ULONG_PTR, ensure_input_desktop, ensure_touch_injection,
    kernel32, u32,
)

from rathena_feed import KAFRA_JOBS, DEFAULT_GRF, DEFAULT_LENGTHS, World, capture, list_interfaces, packet_lengths
from rathena_mock import Loopback
from rathena_terrain import (
    CELL_SIZE, Camera, Gat, RagnarokProjector3D, Terrain, get_map_camera_profile, MapCameraProfile,
    CITY_NAMES, CITY_KAFRA, MAP_PORTALS, find_map_route, get_city_travel_plan,
    get_mob_catalog, load_item_catalog, get_item_tier, search_mob_catalog,
    KNOWN_CITIES, load_heatmap,
    matches_city, save_learned_warp, load_learned_warps
)


# ── Parser Universal de /navi ──
_NAVI_RE = re.compile(
    r"""(?:/navi\s+)?
    (?:(?P<map>[a-zA-Z0-9_]+)\s+)?
    (?P<x>\d+)[/,\s]+(?P<y>\d+)""",
    re.VERBOSE | re.IGNORECASE,
)


def parse_navi_command(text: str, current_map: str = "") -> dict | None:
    """Parseia '/navi prontera 150/180', '150,180', 'mapa x y', etc.
    Retorna {'map': str, 'x': int, 'y': int} ou None se não reconhecer.
    """
    t = text.strip().lstrip("/")
    # Remove prefixo 'navi' se presente
    if t.lower().startswith("navi "):
        t = t[5:].strip()
    m = _NAVI_RE.fullmatch(t.strip()) or _NAVI_RE.search(t.strip())
    if not m:
        return None
    map_name = (m.group("map") or "").strip().lower()
    if not map_name:
        map_name = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw")
    x = int(m.group("x"))
    y = int(m.group("y"))
    return {"map": map_name, "x": x, "y": y}


def normalize_dialog_option(text: str) -> str:
    """Normaliza texto de opção de menu: minúsculas, sem acentos, sem «», sem preços (ex: 120z) e sem códigos de cores."""
    if not text:
        return ""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    t = t.replace("«", "").replace("»", "")
    t = re.sub(r"\d+[\d.,]*\s*z\b", "", t)
    t = re.sub(r"\^[0-9a-f]{6}", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def npc_colors(job, name):
    normalized = normalize_dialog_option(name)
    if job in KAFRA_JOBS or any(term in normalized for term in ("kafra", "funcionaria")):
        return "#f59e0b", "#3b82f6"
    if any(term in normalized for term in ("placa", "soldado")):
        return "#6b7280", "#6b7280"
    if any(term in normalized for term in ("eden", "quadro", "boya", "instrutor")) or "ur" in normalized.split():
        return "#10b981", "#10b981"
    if any(term in normalized for term in ("curandeira", "teleportadora", "utilidades", "estilista")):
        return "#8b5cf6", "#22d3ee"
    return "#6b7280", "#6b7280"


HUD_BINDINGS = {
    "play": ("Play/Pause", 0x20),
    "farm": ("Farm", 0x27),
    "loot": ("Loot", 0x25),
    "loot_alt": ("Loot (alternativo)", 0x2D),
    "patrol": ("Patrol", 0x75),
    "drawer": ("Gaveta / restaurar HUD", 0x77),
}
HUD_KEY_NAMES = {
    0x20: "Espaço", 0x21: "Page Up", 0x22: "Page Down", 0x23: "End", 0x24: "Home",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓", 0x2D: "Insert", 0x2E: "Delete",
    **{key: chr(key) for key in range(0x30, 0x3A)},
    **{key: chr(key) for key in range(0x41, 0x5B)},
    **{0x70 + idx: f"F{idx + 1}" for idx in range(12)},
}

SURVIVAL_NUMBERS = (
    ("survival_hp_pct", "HP Fugir %", 30, 1, 100),
    ("survival_resume_pct", "HP Voltar %", 100, 1, 100),
    ("survival_danger_radius", "Raio Perigo Mobs", 8, 1, 30),
)
SURVIVAL_RETREAT = "SURVIVAL_RETREAT"
SURVIVAL_RETURN = "SURVIVAL_RETURN"


def load_filters_config() -> dict:
    cfg_path = Path(__file__).with_name("filters_config.json")
    default_cfg = {
        "tier1_items": [
            "carta", "card", "velha caixa azul", "velha caixa roxa",
            "album de cartas", "álbum de cartas", "oridecon", "elunium"
        ],
        "tier3_items": [
            "jellopy", "felpa", "pluma", "trevo", "casca", "garrafa vazia"
        ],
        "tier3_max_weight_pct": 55,
        "hud_pause_on_drawer": True,
        "hud_pause_on_click": True,
        "survival_enabled": True,
        "survival_seek_portal": True,
        "survival_auto_respawn": False,
        "auto_store_kafra": False,
        "kafra_trigger_weight_pct": 75,
        "kafra_trigger_slots": 88,
        "kafra_pin": "",
        "keep_in_bag": ["Asa de Mosca", "Asa de Borboleta"],
        "restock_items": {},
        **{key: default for key, _, default, _, _ in SURVIVAL_NUMBERS},
    }
    if not cfg_path.exists():
        save_filters_config(default_cfg)
        return default_cfg
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = {**default_cfg, **cfg}
        for key in ("kafra_trigger_weight_pct", "kafra_trigger_slots"):
            value = merged[key]
            if type(value) is not int or not 1 <= value <= 100:
                merged[key] = default_cfg[key]
        if not isinstance(merged["keep_in_bag"], list):
            merged["keep_in_bag"] = default_cfg["keep_in_bag"]
        merged["keep_in_bag"] = [name for name in merged["keep_in_bag"] if isinstance(name, str) and name.strip()]
        if not isinstance(merged["restock_items"], dict):
            merged["restock_items"] = {}
        if not re.fullmatch(r"[0-9]*", str(merged["kafra_pin"])):
            merged["kafra_pin"] = ""
        for key, _, default, minimum, maximum in SURVIVAL_NUMBERS:
            value = merged[key]
            if type(value) is not int or not minimum <= value <= maximum:
                merged[key] = default
        merged["survival_resume_pct"] = max(merged["survival_hp_pct"], merged["survival_resume_pct"])
        if merged != cfg:
            save_filters_config(merged)
        return merged
    except Exception:
        return default_cfg


def save_filters_config(cfg: dict):
    cfg_path = Path(__file__).with_name("filters_config.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cfg_path.parent,
                                         prefix=".filters_config-", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        temporary.replace(cfg_path)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[Config] Falha ao salvar {cfg_path.name}: {type(exc).__name__}.", flush=True)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_world_routes() -> dict:
    routes_path = Path(__file__).with_name("world_routes.json")
    if routes_path.exists():
        try:
            with open(routes_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cities": {}, "portals": [], "aliases": {}, "world_map_grid": []}


def calculate_travel_route(start_map: str, dest_map: str) -> list[dict]:
    """Calcula o menor caminho combinando Kafra e portais via BFS/Dijkstra."""
    import heapq
    s_map = str(start_map).lower().removesuffix(".gat").removesuffix(".rsw")
    d_map = str(dest_map).lower().removesuffix(".gat").removesuffix(".rsw")
    if s_map == d_map:
        return []

    routes_data = load_world_routes()
    combined_portals = dict(MAP_PORTALS)
    for p in routes_data.get("portals", []):
        combined_portals[(p["from_map"].lower(), p["to_map"].lower())] = (int(p["wx"]), int(p["wy"]))
    for k, v in load_learned_warps().items():
        if "->" in k and isinstance(v, (list, tuple)) and len(v) == 2:
            mf, mt = k.split("->", 1)
            combined_portals[(mf.lower(), mt.lower())] = (int(v[0]), int(v[1]))

    adj: dict[str, list[tuple[str, dict, float]]] = {}
    for (m_from, m_to), (px, py) in combined_portals.items():
        step = {"type": "portal", "from_map": m_from, "to_map": m_to, "wx": px, "wy": py}
        adj.setdefault(m_from, []).append((m_to, step, 1.0))

    kafra_db = dict(CITY_KAFRA)
    for c_id, c_data in routes_data.get("cities", {}).items():
        kafra_db[c_id.lower()] = c_data

    for c_id, c_info in kafra_db.items():
        c_map = c_info.get("map", c_id).lower()
        k_pos = tuple(c_info.get("kafra_pos", (150, 150)))
        for t_city, _cost in c_info.get("teleports", {}).items():
            t_city_low = t_city.lower()
            t_map = kafra_db.get(t_city_low, {}).get("map", t_city_low)
            step = {"type": "kafra", "from_city": c_map, "to_city": t_map, "kafra_pos": k_pos}
            adj.setdefault(c_map, []).append((t_map, step, 1.5))

    heap = [(0.0, s_map, [])]
    best_dist = {s_map: 0.0}

    while heap:
        cur_cost, cur_node, path = heapq.heappop(heap)
        if cur_node == d_map:
            return path
        if cur_cost > best_dist.get(cur_node, float("inf")):
            continue
        for nxt_node, step, edge_cost in adj.get(cur_node, []):
            new_cost = cur_cost + edge_cost
            if new_cost < best_dist.get(nxt_node, float("inf")):
                best_dist[nxt_node] = new_cost
                heapq.heappush(heap, (new_cost, nxt_node, path + [step]))

    plan = get_city_travel_plan(s_map, d_map)
    if plan and plan.get("mode") == "walk":
        return [{"type": "portal", "from_map": f, "to_map": t, "wx": x, "wy": y} for (f, t, x, y) in plan.get("route", [])]
    return []


BG_KEY = "#010101"
DEFAULT_CLICK_METHOD = "touch_postmsg"
MAX_SNAPSHOT_BYTES = 256_000
MAX_OBJECTS = 200
SNAPSHOT_TTL = .75
DEFAULT_CATALOG = Path("D:/Jogos/Ragnarok/System/itemInfo.lua")
MOB_CATALOG = Path(__file__).with_name("monstros-pt-br.json")


@dataclass(frozen=True)
class ScreenObject:
    key: str
    kind: str
    catalog_id: int
    u: float  # Ponto de clique / centro do corpo do sprite
    v: float
    amount: int = 1
    ownership: str = "FREE"
    anchor_u: float = None  # Âncora no chão (sprite_z = 0.0) para base geométrica e A*
    anchor_v: float = None
    public_at: float = 0.0
    name: str = None

    def get_anchor(self):
        au = self.anchor_u if self.anchor_u is not None else self.u
        av = self.anchor_v if self.anchor_v is not None else self.v
        return au, av


@dataclass(frozen=True)
class Snapshot:
    viewport: tuple[int, int, int, int]
    objects: tuple[ScreenObject, ...]
    player: object = None
    weight_ratio: float = 0.0
    base_level: int = 1
    map: str = None


class KafraVision:
    def __init__(self):
        import numpy as np
        from PIL import Image, ImageGrab

        try:
            import cv2
        except ImportError:
            cv2 = None
        self.cv2, self.np, self.Image, self.grab = cv2, np, Image, ImageGrab.grab
        assets = Path(__file__).with_name("assets")
        with Image.open(assets / "kafra_pin_ref.png") as image:
            self.pin = np.array(image.convert("L"))
        with Image.open(assets / "inv_header.png") as image:
            self.inventory = np.array(image.convert("L"))
        self.digit_centers = ((22, 35), (52, 35), (82, 35), (22, 65), (52, 65),
                              (82, 65), (22, 95), (52, 95), (82, 95), (22, 125))
        self.digits = dict(zip("5497638201", self._digit_masks(self.pin)))
        try:
            import pytesseract
            self.ocr = pytesseract
        except ImportError:
            self.ocr = None

    def capture(self, viewport):
        left, top, width, height = viewport
        return self.np.array(self.grab((left, top, left + width, top + height), all_screens=True).convert("L"))

    def _match(self, image, pattern, confidence=0.91):
        if any(a < b for a, b in zip(image.shape, pattern.shape)):
            return None
        if self.cv2 is not None:
            result = self.cv2.matchTemplate(image, pattern, self.cv2.TM_CCOEFF_NORMED)
            _, score, _, point = self.cv2.minMaxLoc(result)
            return point if score >= confidence else None
        np = self.np
        h, w = pattern.shape
        rows, columns = image.shape[0] - h + 1, image.shape[1] - w + 1
        template = pattern.astype("float32") - pattern.mean()
        spread = np.linalg.norm(template)
        if not spread:
            return None
        votes = np.zeros((rows, columns), dtype="uint8")
        for row in np.array_split(np.arange(h), 3):
            for col in np.array_split(np.arange(w), 4):
                sample = np.abs(template[np.ix_(row, col)])
                sy, sx = np.unravel_index(sample.argmax(), sample.shape)
                y, x = row[sy], col[sx]
                value = int(pattern[y, x])
                pixels = image[y:y + rows, x:x + columns]
                votes += (pixels >= max(0, value - 18)) & (pixels <= min(255, value + 18))
        candidates = np.argwhere(votes >= max(9, int(votes.max()) - 1))
        if len(candidates) > 500:
            return None
        best = None
        for y, x in candidates:
            patch = image[y:y + h, x:x + w]
            centered = patch.astype("float32") - patch.mean()
            norm = spread * np.linalg.norm(centered)
            if norm and (score := float(np.sum(centered * template) / norm)) >= confidence:
                best, confidence = (int(x), int(y)), score
        return best

    def _digit_masks(self, panel):
        return [(panel[y - 6:y + 6, x - 6:x + 6] >= 220) for x, y in self.digit_centers]

    def pin_points(self, screen, pin):
        title = self._match(screen, self.pin[2:15, 3:109])
        if title is None:
            return None
        x, y = title[0] - 3, title[1] - 2
        height, width = self.pin.shape
        panel = screen[y:y + height, x:x + width]
        if x < 0 or y < 0 or panel.shape != self.pin.shape:
            return None
        if (self._match(panel[112:139, 38:94], self.pin[117:133, 47:85]) is None
                or self._match(panel[143:167, 130:170], self.pin[146:164, 136:164]) is None):
            return None
        buttons = {}
        for mask, (bx, by) in zip(self._digit_masks(panel), self.digit_centers):
            scores = sorted((self.np.count_nonzero(mask != template), digit)
                            for digit, template in self.digits.items())
            if not mask.any() or scores[0][0] > 24 or scores[1][0] - scores[0][0] < 4:
                return None
            buttons[scores[0][1]] = (x + bx, y + by)
        if len(buttons) != 10:
            return None
        return [*(buttons[digit] for digit in pin), (x + 150, y + 155)]

    def inventory_grid(self, screen):
        title = self._match(screen, self.inventory[7:21, 7:86])
        if title is None:
            return None
        x, y = title[0] - 7, title[1] - 7
        if x < 0 or y < 0:
            return None
        return self._window_grid(screen, x, y)

    def _window_grid(self, screen, x, y):
        close = self._match(screen[max(0, y + 3):y + 25, x + 154:],
                            self.inventory[8:20, 256:283])
        if close is None:
            return None
        y = max(0, y + 3) + close[1] - 8
        right = x + 154 + close[0] + 30
        footer = self._match(screen[y + 72:, right - 19:right + 3],
                             self.inventory[194:208, 271:285])
        if footer is None:
            return None
        bottom = y + 72 + footer[1] + 14
        columns = min(10, (right - x - 92) // 32 + 1)
        rows = (bottom - y - 75) // 32 + 1
        if y < 0 or columns < 1 or rows < 1:
            return None
        return x, y, right, bottom, columns, rows

    def _ocr_words(self, screen):
        if self.ocr is None:
            return ()
        try:
            words = self.ocr.image_to_data(self.Image.fromarray(screen), config="--psm 11", timeout=1.2,
                                           output_type=self.ocr.Output.DICT)
        except (OSError, RuntimeError):
            self.ocr = None
            return ()
        return [(normalize_dialog_option(word), x, y, width, height)
                for word, x, y, width, height in zip(words["text"], words["left"], words["top"],
                                                     words["width"], words["height"]) if word.strip()]

    def storage_grid(self, screen):
        for title, x, y, _, _ in self._ocr_words(screen):
            if title in ("armazem", "armazen", "storage") and x >= 23 and y >= 9:
                if grid := self._window_grid(screen, x - 23, y - 9):
                    return grid
        return None

    def quantity_box(self, screen):
        for title, x, y, _, _ in self._ocr_words(screen):
            if title not in ("quantidade", "quantity"):
                continue
            left, top = max(0, x - 30), max(0, y - 10)
            area = screen[top:y + 116, left:x + 240]
            ok = self._match(area, self.pin[146:164, 136:164], 0.84)
            if ok:
                value_area = area[y - top + 8:ok[1], :]
                return (left + ok[0] + 14, top + ok[1] + 9), value_area
        return None

    def item_at(self, screen, point):
        x, y = point
        patch = screen[y - 10:y + 11, x - 11:x + 12]
        return patch.size > 0 and self.np.count_nonzero(patch < 200) >= 12

    def tooltip(self, screen, point):
        if self.ocr is None:
            return ""
        x, y = point
        area = screen[max(0, y - 60):max(0, y - 7), max(0, x - 120):x + 240]
        if area.size == 0:
            return ""
        # Isola linhas com texto branco sobre o fundo escuro do tooltip.
        dark_rows = self.np.count_nonzero(area < 85, axis=1) >= 35
        bright = self.np.where((area > 200) & dark_rows[:, None], 0, 255).astype("uint8")
        try:
            image = self.Image.fromarray(bright)
            return self.ocr.image_to_string(image.resize((image.width * 2, image.height * 2)),
                                            config="--psm 6", timeout=0.8)
        except (OSError, RuntimeError):
            self.ocr = None
            return ""


def decode_snapshot(text):
    payload = json.loads(text)
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Versão de snapshot inválida.")
    viewport = payload.get("viewport")
    if not isinstance(viewport, dict):
        raise ValueError("Viewport ausente.")
    bounds = tuple(viewport.get(key) for key in ("left", "top", "width", "height"))
    if any(type(value) is not int for value in bounds):
        raise ValueError("O viewport requer coordenadas inteiras.")
    left, top, width, height = bounds
    if not (-32000 <= left <= 32000 and -32000 <= top <= 32000 and 1 <= width <= 16384 and 1 <= height <= 16384):
        raise ValueError("Viewport fora dos limites.")

    objects = payload.get("objects")
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise ValueError("Lista de objetos inválida.")
    seen = set()
    entities = []
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("Objeto inválido.")
        key, kind = item.get("key"), item.get("kind")
        cid, u, v = (item.get(field) for field in ("id", "u", "v"))
        amount = item.get("amount", 1)
        ownership = item.get("ownership", "FREE")
        if not isinstance(key, str) or not 0 < len(key) <= 80 or key in seen:
            raise ValueError("Identificador de objeto inválido ou duplicado.")
        if kind not in ("drop", "mob", "npc", "warp", "player") or type(cid) is not int or not 0 <= cid <= 0xFFFFFFFF:
            raise ValueError("Tipo ou ID de catálogo inválido.")
        if any(type(n) not in (int, float) or not 0 <= n <= 1 for n in (u, v)):
            raise ValueError("Posição fora do viewport.")
        if type(amount) is not int or not 1 <= amount <= 65535:
            raise ValueError("Quantidade inválida.")
        seen.add(key)
        anchor_u = item.get("anchor_u", u)
        anchor_v = item.get("anchor_v", v)
        entities.append(ScreenObject(key, kind, cid, u, v, amount, ownership, anchor_u, anchor_v))
    return Snapshot(bounds, tuple(entities))


def clean_lua_text(texto):
    if not texto:
        return ""
    # Converte escapes decimais de Lua (\123)
    texto = re.sub(r'\\(\d{1,3})', lambda m: chr(int(m.group(1))), texto)
    # Remove tags de cor (^RRGGBB)
    texto = re.sub(r'\^[0-9A-Fa-f]{6}', '', texto)
    # Substitui quebras de linha e caracteres de controle por espaço simples
    texto = re.sub(r'[\r\n\t\x00-\x1f\x7f-\x9f]+', ' ', texto)
    return " ".join(texto.split())


def read_catalogs(catalog_path):
    names = {"drop": {}, "mob": {}, "npc": {}, "warp": {}}
    item_cat = load_item_catalog(lua_path=catalog_path)
    names["drop"].update(item_cat.get("names", {}))
    mob_cat = get_mob_catalog()
    for jid, entry in mob_cat.get("by_id", {}).items():
        names["mob"][jid] = entry["name"]
    return names


class SnapshotFeed:
    def __init__(self, path):
        self.path = path
        self.last_change = None
        self.value = None

    def current(self):
        try:
            stat = self.path.stat()
            if not 0 < stat.st_size <= MAX_SNAPSHOT_BYTES or time.time() - stat.st_mtime > SNAPSHOT_TTL:
                return None
            signature = stat.st_mtime_ns, stat.st_size
            if signature != self.last_change:
                content = self.path.read_bytes()
                if len(content) > MAX_SNAPSHOT_BYTES:
                    return None
                self.value = decode_snapshot(content.decode("utf-8-sig"))
                self.last_change = signature
        except (OSError, ValueError, UnicodeError):
            return None
        return self.value


class LiveFeed:
    def __init__(self, world, terrain, camera, viewport):
        self.world = world
        self.terrain = terrain
        self.camera = camera
        self.viewport = viewport
        self._cam = None  # (cam_x, cam_y, cam_z) suavizado exponencialmente
        self._last_cam_time = None

    def stats(self):
        return self.world.stats()

    def current(self):
        name, player, entities = self.world.snapshot()
        viewport = self.viewport()
        if not viewport:
            return None
        objects = []
        if player:
            height = flat_height
            gat = None
            if self.terrain and name:
                try:
                    gat = self.terrain.for_map(name)
                    if gat is not None:
                        height = gat.get_elevation
                except (OSError, ValueError) as exc:
                    print(f"Terreno de {name}: {exc}", flush=True)

            now = time.perf_counter()
            raw_px, raw_py = player.cell(now)
            raw_pz = height(raw_px, raw_py)

            # Suavização exponencial contínua sem snap brusco ao parar:
            # tau = 0.11s em (x, y) e tau = 0.14s em z tanto na caminhada quanto na desaceleração final
            if self._cam is None or self._last_cam_time is None:
                self._cam = (raw_px, raw_py, raw_pz)
                self._last_cam_time = now
            else:
                dt = max(1e-4, now - self._last_cam_time)
                self._last_cam_time = now
                tau_xy = 0.11
                tau_z = 0.14
                alpha_xy = 1.0 - math.exp(-dt / tau_xy)
                alpha_z = 1.0 - math.exp(-dt / tau_z)
                cx, cy, cz = self._cam
                ncx = cx + alpha_xy * (raw_px - cx)
                ncy = cy + alpha_xy * (raw_py - cy)
                ncz = cz + alpha_z * (raw_pz - cz)
                # Zona morta microscópica: snap apenas quando |cam - target| < 0.002 células
                if math.hypot(ncx - raw_px, ncy - raw_py) < 0.002:
                    ncx, ncy, ncz = raw_px, raw_py, raw_pz
                self._cam = (ncx, ncy, ncz)

            if hasattr(self.camera, "_current_cam_pos"):
                self.camera._current_cam_pos = self._cam

            project = self.camera.projector(viewport, self._cam, height)
            for (kind, aid), entity in entities:
                render_now = now - (0.12 if kind == "mob" else 0.0)
                fx, fy = entity.track.cell(render_now)
                # 1. Âncora no chão (sprite_z = 0.0)
                anchor_u, anchor_v = project(fx, fy, 0.0)
                # 2. Centro visual do sprite: sprite_z = 5.5 para mob/npc, 1.2 para drop/warp
                sprite_z = 5.5 if kind in ("mob", "npc") else 1.2
                click_u, click_v = project(fx, fy, sprite_z)
                if (-0.2 <= click_u <= 1.2 and -0.2 <= click_v <= 1.2) or (-0.2 <= anchor_u <= 1.2 and -0.2 <= anchor_v <= 1.2):
                    ownership = getattr(entity, "ownership", "FREE")
                    public_at = getattr(entity, "public_at", 0.0)
                    objects.append(ScreenObject(
                        f"{kind}:{aid}", kind, entity.catalog_id,
                        click_u, click_v, entity.amount, ownership,
                        anchor_u, anchor_v, public_at,
                        self.world.npc_name(aid, entity.catalog_id, entity.track.target, name) if kind == "npc" else None,
                    ))

            # Renderização de Players próximos (self.players): sprite_z = 5.5
            if hasattr(self.world, "players"):
                with self.world.lock:
                    p_items = list(self.world.players.items())
                    my_pid = getattr(self.world, "player_id", None)
                now_m = time.monotonic()
                for pid, (p_cell, p_t) in p_items:
                    if my_pid is not None and pid == my_pid:
                        continue
                    if now_m - p_t > 15.0:
                        continue
                    p_fx, p_fy = p_cell
                    p_anc_u, p_anc_v = project(p_fx, p_fy, 0.0)
                    p_clk_u, p_clk_v = project(p_fx, p_fy, 5.5)
                    if (-0.2 <= p_clk_u <= 1.2 and -0.2 <= p_clk_v <= 1.2) or (-0.2 <= p_anc_u <= 1.2 and -0.2 <= p_anc_v <= 1.2):
                        objects.append(ScreenObject(
                            f"player:{pid}", "player", pid,
                            p_clk_u, p_clk_v, 1, "FREE",
                            p_anc_u, p_anc_v
                        ))
        return Snapshot(
            viewport, tuple(objects),
            player=player,
            weight_ratio=getattr(player, "weight_ratio", 0.0) if player else 0.0,
            base_level=getattr(player, "base_level", 1) if player else 1,
            map=name
        )


def flat_height(x, y):
    return 0.0


# ============================================================================
# Win32 API Isolada (u32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
u32.GetClientRect.restype = wintypes.BOOL

u32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
u32.ClientToScreen.restype = wintypes.BOOL

u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
u32.GetWindowTextW.restype = ctypes.c_int

u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
u32.GetWindowTextLengthW.restype = ctypes.c_int

u32.IsWindowVisible.argtypes = (wintypes.HWND,)
u32.IsWindowVisible.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
u32.EnumWindows.restype = wintypes.BOOL
u32.EnumChildWindows.argtypes = (wintypes.HWND, WNDENUMPROC, wintypes.LPARAM)
u32.EnumChildWindows.restype = wintypes.BOOL

u32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
u32.GetAncestor.restype = wintypes.HWND

u32.GetTopWindow.argtypes = (wintypes.HWND,)
u32.GetTopWindow.restype = wintypes.HWND
u32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
u32.GetWindow.restype = wintypes.HWND

u32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
u32.GetWindowLongW.restype = ctypes.c_long

u32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
u32.SetWindowLongW.restype = ctypes.c_long

u32.SetLayeredWindowAttributes.argtypes = (
    wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD
)
u32.SetLayeredWindowAttributes.restype = wintypes.BOOL

u32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT
)
u32.SetWindowPos.restype = wintypes.BOOL

u32.GetKeyState.argtypes = (ctypes.c_int,)
u32.GetKeyState.restype = ctypes.c_short
u32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
u32.GetKeyboardLayout.restype = wintypes.HANDLE
u32.ToUnicodeEx.argtypes = (
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
    wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HANDLE,
)
u32.ToUnicodeEx.restype = ctypes.c_int



LRESULT = ctypes.c_ssize_t

HUD_SUBCLASSPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ctypes.c_size_t, ctypes.c_size_t,
)
comctl32 = ctypes.WinDLL("comctl32")
comctl32.SetWindowSubclass.argtypes = (wintypes.HWND, HUD_SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t)
comctl32.SetWindowSubclass.restype = wintypes.BOOL
comctl32.RemoveWindowSubclass.argtypes = (wintypes.HWND, HUD_SUBCLASSPROC, ctypes.c_size_t)
comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
comctl32.DefSubclassProc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
comctl32.DefSubclassProc.restype = LRESULT

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

try:
    u32.SetWindowsHookExW.argtypes = (ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD)
    u32.SetWindowsHookExW.restype = wintypes.HHOOK
    u32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
    u32.UnhookWindowsHookEx.restype = wintypes.BOOL
    u32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    u32.CallNextHookEx.restype = LRESULT
except Exception:
    pass

def log_click_file(msg: str):
    """Grava log de diagnóstico imediato em click_debug.log e no terminal."""
    try:
        with open("click_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    print(f"[ClickDebug] {msg}", flush=True)


class GameViewport:
    """Read the foreground window's client rectangle through the window manager."""

    def __init__(self, title, handle=None):
        self.title = title.casefold() if title else ""
        self.handle = handle
        self.user = u32
        self._cached_viewport = None
        self._last_viewport_time = 0.0

    def __call__(self, force=False):
        now = time.monotonic()
        # Atualiza game_viewport no ciclo de 120ms (desacoplado do draw 60 FPS)
        if not force and self._cached_viewport is not None and (now - self._last_viewport_time < 0.120):
            return self._cached_viewport

        # Se o handle foi fechado, tenta redetectar a janela do jogo
        if self.handle and not self.user.IsWindow(self.handle):
            self.handle = None
            new_h, _, _ = find_ragnarok_window()
            if new_h:
                self.handle = new_h

        target = self.handle
        foreground = self.user.GetForegroundWindow()
        if foreground:
            caption = ctypes.create_unicode_buffer(512)
            if self.user.GetWindowTextW(foreground, caption, len(caption)):
                if self.handle and int(foreground) == int(self.handle):
                    target = foreground
                elif self.title and self.title in caption.value.casefold():
                    target = foreground

        if not target or not self.user.IsWindow(target) or self.user.IsIconic(target):
            self._cached_viewport = None
            self._last_viewport_time = now
            return None

        bounds = wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        if (self.user.GetClientRect(target, ctypes.byref(bounds))
                and self.user.ClientToScreen(target, ctypes.byref(origin))
                and bounds.right > 0 and bounds.bottom > 0):
            res = (origin.x, origin.y, bounds.right, bounds.bottom)
            self._cached_viewport = res
            self._last_viewport_time = now
            return res
        self._cached_viewport = None
        self._last_viewport_time = now
        return None


class CameraTracker:
    """Rastreamento de Câmera com Perfil Nativo (.grf) e Controles Win32 Calibrados.
    - Óptica real: FOV=15.0°, CELL_SIZE=5.0, distance=399.0 por padrão.
    - Leitura direta de ViewpointTable.txt e indoorrswtable.txt do .grf.
    - Rotação: sensibilidade 0.28, clamp em [yaw_min, yaw_max] se amplitude < 360°, duplo-clique restaura yaw_default.
    - Shift: inclinação pitch em [pitch_min, pitch_max].
    - Ctrl / Scroll: zoom distance em [distance_min, distance_max].
    """

    def __init__(self, target_hwnd=None, pid=None, world=None, terrain=None,
                 yaw_deg=None, pitch_deg=None, distance=None):
        self.target_hwnd = target_hwnd
        self.pid = pid
        self.world = world
        self.terrain = terrain
        self.current_gat = None
        self.current_map = ""

        # Perfil nativo inicial (padrão outdoor)
        self.profile = get_map_camera_profile("rachel")
        self.yaw_deg = float(yaw_deg) if yaw_deg is not None else self.profile.yaw_default
        self.pitch_deg = float(pitch_deg) if pitch_deg is not None else self.profile.pitch_default
        self.distance = float(distance) if distance is not None else self.profile.distance_default

        self.yaw_sensitivity = 0.28
        self.pitch_sensitivity = 0.15
        self.distance_sensitivity = 0.80

        self.projector_3d = RagnarokProjector3D(self.yaw_deg, self.pitch_deg, self.distance, fov_deg=15.0)
        self._current_cam_pos = None

        self._prev_rmb = False
        self._prev_lmb = False
        self._rmb_down_time = 0.0
        self._rmb_start_pos = (0, 0)
        self._last_rmb_pos = None
        self._has_dragged = False
        self._last_rmb_click_time = 0.0
        self._last_camera_input_time = 0.0

        self._wheel_hook = None
        self._wheel_hook_proc = None
        self.on_lbutton_down_callback = None
        self.on_physical_click = None

        # Camada 3: Leitura Opcional de Memória (Silenciosa)
        self._h_process = None
        self._init_memory_reader()

        if self.world is not None:
            self.world.on_player_move_callback = self.on_player_move
            self.world.on_map_change_callback = self.on_map_change
            if getattr(self.world, "map", None):
                self.on_map_change(self.world.map, getattr(self.world, "current_map", None))

    def _init_memory_reader(self):
        if not self.pid:
            return
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x0010, False, int(self.pid))  # PROCESS_VM_READ
            if not h:
                return
            self._h_process = h
        except Exception:
            pass

    def on_map_change(self, map_base, map_gat=None):
        """Ao receber pacotes de troca/entrada de mapa, aplica imediatamente os valores nativos do perfil."""
        self.current_map = map_gat or (f"{map_base}.gat" if map_base else "")
        self.profile = get_map_camera_profile(map_base or map_gat)
        self.yaw_deg = self.profile.yaw_default
        self.pitch_deg = self.profile.pitch_default
        self.distance = self.profile.distance_default

        if self.terrain and self.current_map:
            try:
                self.current_gat = self.terrain.for_map(self.current_map)
            except Exception:
                self.current_gat = None

        self.sync_projector()

    def sync_projector(self):
        self.projector_3d.yaw_deg = self.yaw_deg
        self.projector_3d.pitch_deg = self.pitch_deg
        self.projector_3d.distance = self.distance

    def projector(self, viewport, cam_pos, height_fn=None):
        self._current_cam_pos = cam_pos
        self.sync_projector()
        return self.projector_3d.projector(viewport, cam_pos, height_fn)

    def record_click(self, sx, sy, viewport=None):
        """Compatibilidade: calibração de clique desativada (parâmetros vêm do .grf)."""
        pass

    def on_player_move(self, dst_x, dst_y):
        """Compatibilidade: calibração de clique desativada."""
        pass

    def poll_gestures(self, viewport):
        """Rastreamento de entrada a 60Hz enquanto o Ragnarok estiver em foco."""
        if not viewport:
            return
        left, top, width, height = viewport

        fg = u32.GetForegroundWindow()
        is_focused = True
        if self.target_hwnd and fg != self.target_hwnd:
            is_focused = False

        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        in_viewport = (left <= pt.x < left + width and top <= pt.y < top + height)

        rmb = bool(u32.GetAsyncKeyState(0x02) & 0x8000)
        lmb = (is_focused and not win32_input.is_bot_clicking(0.85)
               and bool(u32.GetAsyncKeyState(0x01) & 0x8000))
        shift = bool(u32.GetAsyncKeyState(0x10) & 0x8000)
        ctrl = bool(u32.GetAsyncKeyState(0x11) & 0x8000)
        now = time.perf_counter()

        if rmb or shift or ctrl:
            self._last_camera_input_time = now

        if is_focused and in_viewport:
            # Botão direito (Arrasto e Duplo-clique)
            if rmb and not self._prev_rmb:
                self._rmb_down_time = now
                self._rmb_start_pos = (pt.x, pt.y)
                self._last_rmb_pos = (pt.x, pt.y)
                self._has_dragged = False
            elif rmb and self._last_rmb_pos is not None:
                dx = pt.x - self._last_rmb_pos[0]
                dy = pt.y - self._last_rmb_pos[1]
                dist_from_start = math.hypot(pt.x - self._rmb_start_pos[0], pt.y - self._rmb_start_pos[1])
                if dist_from_start >= 4.0:
                    self._has_dragged = True

                if shift:
                    # Com VK_SHIFT: congela yaw_deg e ajusta apenas pitch_deg dentro de [pitch_min, pitch_max]
                    self.pitch_deg = max(self.profile.pitch_min, min(self.profile.pitch_max, self.pitch_deg + dy * self.pitch_sensitivity))
                elif ctrl:
                    # Com VK_CONTROL: congela yaw_deg e pitch_deg, ajusta apenas distance dentro de [distance_min, distance_max]
                    self.distance = max(self.profile.distance_min, min(self.profile.distance_max, self.distance + dy * self.distance_sensitivity))
                else:
                    # Sem Shift/Ctrl: atualiza yaw_deg com sensibilidade calibrada (dx * 0.28)
                    # e clamp estrito em [yaw_min, yaw_max] sem wrap quando yaw_max - yaw_min < 360
                    if dx != 0:
                        yaw_span = self.profile.yaw_max - self.profile.yaw_min
                        if yaw_span < 360.0:
                            self.yaw_deg = max(self.profile.yaw_min, min(self.profile.yaw_max, self.yaw_deg + dx * self.yaw_sensitivity))
                        else:
                            new_yaw = self.yaw_deg + dx * self.yaw_sensitivity
                            self.yaw_deg = (new_yaw + 180.0) % 360.0 - 180.0

                self._last_rmb_pos = (pt.x, pt.y)
            elif not rmb and self._prev_rmb:
                duration = now - self._rmb_down_time
                dist_from_start = math.hypot(pt.x - self._rmb_start_pos[0], pt.y - self._rmb_start_pos[1])
                # Duplo clique direito (< 300ms e deslocamento < 4px sem modificadores): restaura yaw_default
                if not shift and not ctrl and not self._has_dragged and dist_from_start < 4.0 and duration < 0.300:
                    if now - self._last_rmb_click_time < 0.300:
                        self.yaw_deg = self.profile.yaw_default
                        self._last_rmb_click_time = 0.0
                    else:
                        self._last_rmb_click_time = now
                self._last_rmb_pos = None

        self._prev_rmb = rmb
        self._prev_lmb = lmb
        self.sync_projector()

    def install_wheel_hook(self):
        """Instala listener passivo de scroll do mouse (WH_MOUSE_LL lendo WM_MOUSEWHEEL sem bloquear)."""
        if self._wheel_hook is not None:
            return
        try:
            WH_MOUSE_LL = 14
            WM_MOUSEWHEEL = 0x020A
            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t),
                ]

            def _wheel_proc(nCode, wParam, lParam):
                if nCode >= 0 and lParam:
                    try:
                        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        if (self.on_physical_click and wParam in (0x0201, 0x0204, 0x0207, 0x020B)
                                and not ms.flags & 0x01):  # LLMHF_INJECTED
                            self.on_physical_click(ms.pt.x, ms.pt.y, wParam == 0x0201)  # VK_LBUTTON
                        fg = u32.GetForegroundWindow()
                        is_game = True
                        if self.target_hwnd and fg != self.target_hwnd:
                            is_game = False
                        if is_game:
                            if wParam == WM_MOUSEWHEEL:
                                delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                                # Scroll para frente aproxima câmera (diminui distance)
                                step = (delta / 120.0) * 25.0
                                self.distance = max(self.profile.distance_min, min(self.profile.distance_max, self.distance - step))
                                self._last_camera_input_time = time.perf_counter()
                                self.sync_projector()
                            elif wParam == 0x0201:  # WM_LBUTTONDOWN
                                cb = getattr(self, "on_lbutton_down_callback", None)
                                if cb is not None:
                                    cb(ms.pt.x, ms.pt.y)
                    except Exception:
                        pass
                return u32.CallNextHookEx(self._wheel_hook, nCode, wParam, lParam)

            self._wheel_hook_proc = HOOKPROC(_wheel_proc)
            h_mod = kernel32.GetModuleHandleW(None)
            self._wheel_hook = u32.SetWindowsHookExW(WH_MOUSE_LL, self._wheel_hook_proc, h_mod, 0)
        except Exception:
            self._wheel_hook = None

    def uninstall_wheel_hook(self):
        if self._wheel_hook:
            try:
                u32.UnhookWindowsHookEx(self._wheel_hook)
            except Exception:
                pass
            self._wheel_hook = None
            self._wheel_hook_proc = None

    def start_tracking(self, root, viewport_provider):
        """Inicia loop de rastreamento a 60Hz (~16ms) e instala hook de mousewheel."""
        self.install_wheel_hook()
        if hasattr(root, "bind"):
            root.bind("<Destroy>", lambda _: self.uninstall_wheel_hook(), add="+")

        def _loop():
            vp = viewport_provider()
            if vp:
                self.poll_gestures(vp)
            root.after(16, _loop)
        _loop()


# ============================================================================
# Interception Driver (interception.dll) - Kernel Filter Bypass
# ============================================================================
class InterceptionMouseStroke(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class InterceptionDriver:
    """Wrapper dinâmico para interception.dll (Oblita Interception).
    Gera cliques no nível de driver de filtro do kernel do mouse, bypassando
    hooks de user-mode (WH_MOUSE_LL), UIPI e rotinas de detecção Win32 do GameGuard/Gepard.
    """
    def __init__(self, dll_path=None):
        self.dll = None
        self.context = None
        self.mouse_dev = None
        self.is_available = False

        paths_to_try = []
        if dll_path:
            paths_to_try.append(Path(dll_path))
        base_dir = Path(__file__).resolve().parent
        paths_to_try.extend([
            base_dir / "interception.dll",
            base_dir / "bin" / "interception.dll",
            Path("interception.dll"),
        ])

        target = None
        for p in paths_to_try:
            if p.is_file():
                target = str(p)
                break

        if not target:
            try:
                self.dll = ctypes.CDLL("interception.dll")
                target = "interception.dll"
            except Exception:
                return

        if not self.dll:
            try:
                self.dll = ctypes.CDLL(target)
            except Exception as exc:
                print(f"[HID] Falha ao carregar {target}: {exc}", flush=True)
                return

        try:
            self.dll.interception_create_context.restype = ctypes.c_void_p
            self.dll.interception_destroy_context.argtypes = (ctypes.c_void_p,)
            self.dll.interception_is_mouse.argtypes = (ctypes.c_int,)
            self.dll.interception_is_mouse.restype = ctypes.c_int
            self.dll.interception_send.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
            self.dll.interception_send.restype = ctypes.c_int

            self.context = self.dll.interception_create_context()
            if not self.context:
                print("[HID] Contexto do Interception retornou NULL (driver instalado no Windows?).", flush=True)
                return

            for dev in range(1, 21):
                if self.dll.interception_is_mouse(dev):
                    self.mouse_dev = dev
                    break

            if self.mouse_dev:
                self.is_available = True
                print(f"[HID] Driver interception.dll carregado com sucesso (MouseDevice={self.mouse_dev}).", flush=True)
            else:
                print("[HID] Interception carregado, mas nenhum dispositivo de mouse detectado.", flush=True)
        except Exception as exc:
            print(f"[HID] Erro inicializando Interception: {exc}", flush=True)

    def send_click(self, sx: int, sy: int, screen_w: int = 1920, screen_h: int = 1080) -> bool:
        if not self.is_available or not self.context or not self.mouse_dev:
            return False
        try:
            norm_x = int((sx * 65535) / max(1, screen_w))
            norm_y = int((sy * 65535) / max(1, screen_h))

            # Mover (INTERCEPTION_MOUSE_MOVE_ABSOLUTE = 0x001)
            stroke_move = InterceptionMouseStroke(
                state=0, flags=0x001, rolling=0, x=norm_x, y=norm_y, information=0
            )
            self.dll.interception_send(self.context, self.mouse_dev, ctypes.byref(stroke_move), 1)
            time.sleep(0.015)

            # Left Down (INTERCEPTION_MOUSE_LEFT_BUTTON_DOWN = 0x001)
            stroke_down = InterceptionMouseStroke(
                state=0x0001, flags=0, rolling=0, x=0, y=0, information=0
            )
            self.dll.interception_send(self.context, self.mouse_dev, ctypes.byref(stroke_down), 1)
            time.sleep(0.025)

            # Left Up (INTERCEPTION_MOUSE_LEFT_BUTTON_UP = 0x002)
            stroke_up = InterceptionMouseStroke(
                state=0x0002, flags=0, rolling=0, x=0, y=0, information=0
            )
            self.dll.interception_send(self.context, self.mouse_dev, ctypes.byref(stroke_up), 1)
            return True
        except Exception as exc:
            print(f"[HID] Erro ao enviar clique via Interception: {exc}", flush=True)
            return False

    def close(self):
        if self.context and self.dll:
            try:
                self.dll.interception_destroy_context(self.context)
            except Exception:
                pass
            self.context = None
            self.is_available = False


# ============================================================================
# Serial USB HID Microcontroller Support (Arduino Leonardo / Pico / Teensy)
# ============================================================================
class SerialHID:
    """Suporte a microcontrolador USB HID via porta Serial (COM).
    Transmite comandos no formato (x,y,click) para Arduino Leonardo / Raspberry Pi Pico
    ou Teensy operando como mouse físico USB. Imune a qualquer anti-cheat ou UIPI.
    """
    def __init__(self, port=None, baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self._serial_obj = None
        self._handle = None
        if self.port:
            self.connect(self.port, self.baudrate)

    @property
    def is_connected(self) -> bool:
        if self._serial_obj is not None:
            return getattr(self._serial_obj, "is_open", True)
        return self._handle is not None and self._handle not in (-1, 0)

    def connect(self, port: str, baudrate: int = 115200) -> bool:
        self.port = port
        self.baudrate = baudrate
        # 1. Tenta pyserial se disponível
        try:
            import serial
            self._serial_obj = serial.Serial(self.port, self.baudrate, timeout=0.1)
            print(f"[SerialHID] Conectado via pyserial em {self.port} ({self.baudrate} baud).", flush=True)
            return True
        except ImportError:
            pass
        except Exception as exc:
            print(f"[SerialHID] pyserial falhou em {self.port}: {exc}. Tentando Win32 nativo...", flush=True)

        # 2. Fallback Win32 CreateFileW nativo (sem dependência externa)
        try:
            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            port_name = f"\\\\.\\{self.port}" if not self.port.startswith("\\\\.\\") else self.port
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
            )
            k32.CreateFileW.restype = wintypes.HANDLE
            handle = k32.CreateFileW(
                port_name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
            )
            if handle and handle != wintypes.HANDLE(-1).value and handle != -1:
                self._handle = handle
                print(f"[SerialHID] Conectado nativamente (Win32) em {self.port}.", flush=True)
                return True
            else:
                err = ctypes.get_last_error()
                print(f"[SerialHID] Falha ao abrir {self.port} nativamente: Win32 LastError={err}", flush=True)
        except Exception as exc:
            print(f"[SerialHID] Erro conectando à porta {self.port}: {exc}", flush=True)
        return False

    def send_click(self, sx: int, sy: int) -> bool:
        """Envia clique no formato (x,y,click): 1 para down, aguarda 25ms, 0 para up."""
        if not self.is_connected:
            return False
        # Down: (x,y,1)
        msg_down = f"({sx},{sy},1)\n".encode("ascii")
        if not self._write(msg_down):
            return False
        time.sleep(0.025)
        # Up: (x,y,0)
        msg_up = f"({sx},{sy},0)\n".encode("ascii")
        self._write(msg_up)
        return True

    def _write(self, data: bytes) -> bool:
        if self._serial_obj:
            try:
                self._serial_obj.write(data)
                self._serial_obj.flush()
                return True
            except Exception:
                return False
        if self._handle:
            try:
                written = wintypes.DWORD()
                k32 = ctypes.windll.kernel32
                k32.WriteFile.argtypes = (
                    wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
                )
                k32.WriteFile.restype = wintypes.BOOL
                res = k32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)
                return bool(res and written.value == len(data))
            except Exception:
                return False
        return False

    def close(self):
        if self._serial_obj:
            try:
                self._serial_obj.close()
            except Exception:
                pass
            self._serial_obj = None
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def screen_api():
    return u32


class CanvasPool:
    """Pool reutilizável de itens do Tkinter Canvas para 60 FPS estáveis sem canvas.delete('all')."""
    def __init__(self, canvas):
        self.canvas = canvas
        self.rects = []
        self.ovals = []
        self.texts = []
        self.lines = []
        self._rect_idx = 0
        self._oval_idx = 0
        self._text_idx = 0
        self._line_idx = 0
        self.pos_cache = {}

    def begin_frame(self):
        self._rect_idx = 0
        self._oval_idx = 0
        self._text_idx = 0
        self._line_idx = 0

    def end_frame(self):
        # Oculta itens remanescentes do pool que não foram reutilizados neste frame
        for i in range(self._rect_idx, len(self.rects)):
            self.canvas.itemconfigure(self.rects[i], state="hidden")
        for i in range(self._oval_idx, len(self.ovals)):
            self.canvas.itemconfigure(self.ovals[i], state="hidden")
        for i in range(self._text_idx, len(self.texts)):
            self.canvas.itemconfigure(self.texts[i], state="hidden")
        for i in range(self._line_idx, len(self.lines)):
            self.canvas.itemconfigure(self.lines[i], state="hidden")

    def hide_all(self):
        self.begin_frame()
        self.end_frame()
        self.pos_cache.clear()

    def get_rect(self, x1, y1, x2, y2, **kwargs):
        if self._rect_idx < len(self.rects):
            item = self.rects[self._rect_idx]
            self.canvas.coords(item, x1, y1, x2, y2)
            self.canvas.itemconfigure(item, state="normal", **kwargs)
        else:
            item = self.canvas.create_rectangle(x1, y1, x2, y2, state="normal", **kwargs)
            self.rects.append(item)
        self._rect_idx += 1
        return item

    def get_oval(self, x1, y1, x2, y2, **kwargs):
        if self._oval_idx < len(self.ovals):
            item = self.ovals[self._oval_idx]
            self.canvas.coords(item, x1, y1, x2, y2)
            self.canvas.itemconfigure(item, state="normal", **kwargs)
        else:
            item = self.canvas.create_oval(x1, y1, x2, y2, state="normal", **kwargs)
            self.ovals.append(item)
        self._oval_idx += 1
        return item

    def get_text(self, x, y, **kwargs):
        if self._text_idx < len(self.texts):
            item = self.texts[self._text_idx]
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, state="normal", **kwargs)
        else:
            item = self.canvas.create_text(x, y, state="normal", **kwargs)
            self.texts.append(item)
        self._text_idx += 1
        return item

    def get_line(self, x1, y1, x2, y2, **kwargs):
        if self._line_idx < len(self.lines):
            item = self.lines[self._line_idx]
            self.canvas.coords(item, x1, y1, x2, y2)
            self.canvas.itemconfigure(item, state="normal", **kwargs)
        else:
            item = self.canvas.create_line(x1, y1, x2, y2, state="normal", **kwargs)
            self.lines.append(item)
        self._line_idx += 1
        return item

    def apply_hysteresis(self, key, x, y, threshold=0.65, snap_px=None):
        """Histerese estável. Se snap_px fornecido, quantiza para múltiplos desse valor (antitremer durante caminhada)."""
        prev = self.pos_cache.get(key)
        if prev is not None:
            prev_x, prev_y = prev
            if math.hypot(x - prev_x, y - prev_y) < threshold:
                return prev_x, prev_y
        if snap_px and snap_px > 1:
            stable = (float(round(x / snap_px) * snap_px), float(round(y / snap_px) * snap_px))
        else:
            stable = (float(round(x)), float(round(y)))
        self.pos_cache[key] = stable
        return stable


class Overlay:
    def __init__(self, root, feed, names):
        self.root = root
        self.feed = feed
        self.names = names
        self.window = tk.Toplevel(root, bg=BG_KEY, highlightthickness=0, bd=0)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-transparentcolor", BG_KEY, "-topmost", True)
        self.canvas = tk.Canvas(self.window, bg=BG_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas_pool = CanvasPool(self.canvas)
        self.window.state("normal")
        self.window.update_idletasks()
        self.user32 = screen_api()
        overlay_hwnd = u32.GetAncestor(self.window.winfo_id(), 2)  # GA_ROOT
        if not overlay_hwnd:
            raise RuntimeError("A janela do overlay não foi criada.")
        self.handle = overlay_hwnd
        # Overlay 100% Zero-Interferência: WS_DISABLED (0x08000000) em GWL_STYLE (-16) e WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE (0x08080028) em GWL_EXSTYLE (-20)
        style = u32.GetWindowLongW(overlay_hwnd, -16)
        u32.SetWindowLongW(overlay_hwnd, -16, style | 0x08000000)
        ex_style = u32.GetWindowLongW(overlay_hwnd, -20)
        ctypes.set_last_error(0)
        previous_style = u32.SetWindowLongW(
            overlay_hwnd, -20, ex_style | 0x08080028
        )
        if not previous_style and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if not u32.SetLayeredWindowAttributes(overlay_hwnd, 0x00010101, 0, 0x00000001):
            raise ctypes.WinError(ctypes.get_last_error())
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED
        if not u32.SetWindowPos(overlay_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0033):
            raise ctypes.WinError(ctypes.get_last_error())
        self._render_layout = (0, 0, 0, 0, True)
        self._hud_render_foreground = False
        self._hud_render_anchor = None
        self._hud_restack_render = False
        self._hud_focus_lost_since = None
        self._hud_snapshot_missing_since = None
        self.last = None
        self.last_stats = None
        self.active_nav_path = None
        self.active_nav_color = "#38BDF8"
        self.auto_loot = False
        self.auto_farm = False
        self.auto_patrol = False
        self._reset_survival()
        self._respawn_started = 0.0
        self._respawn_attempts = 0
        self._handled_death_ts = 0.0
        self._ignore_tier3_over_60 = tk.BooleanVar(value=False)
        self._last_decision_ts = 0.0
        self._last_viewport_pos = None
        self._last_fg_hwnd = None
        self._last_fg_result = False
        self.last_loot_click = 0.0
        self.last_farm_click = 0.0
        self.last_patrol_click_time = 0.0
        self.last_patrol_click_player_pos = None
        self.locked_mob_gid = None
        self.locked_combat_mob_gid = None
        self.locked_drop_gid = None
        self.current_patrol_path = []
        self.patrol_path = []
        self.patrol_waypoints = []
        self.current_waypoint = None
        self.patrol_waypoint = None
        self.visited_cells = []
        self.temp_blocked_cells = {}
        self.stuck_count = 0
        self.patrol_target_route = None  # (map_name, x, y, target_npc_or_warp)
        self.nav_goal = None  # (map_name, x, y) — rota direcionada INDEPENDENTE do auto_patrol
        self.manual_nav_goal = None
        self.travel_plan = None
        self._travel_allowed_portal = None
        self._world_map_open = False
        self._world_map_popup = None
        self.filters_config = load_filters_config()
        self.tier1_items = self.filters_config.get("tier1_items", [])
        self.tier3_items = self.filters_config.get("tier3_items", [])
        self.tier3_max_weight_pct = self.filters_config.get("tier3_max_weight_pct", 55)
        self.world_routes_data = load_world_routes()
        self.nav_path = None
        self.farm_map = None
        self._kafra_cycle = None
        self._kafra_manual = False
        self._kafra_capacity_latched = False
        self._kafra_block = None
        self.center_popup_until = 0.0
        self._last_handled_invite_ts = 0.0
        self._engagement = None
        self._engagement_zone = None
        self._last_kill_event = 0.0
        self.post_kill_wait_until = 0.0
        self.last_kill_pos = None
        # Suporte a hardware HID (driver Interception e/ou Serial USB HID para bypass de anti-cheat)
        import os as _os
        self.interception = InterceptionDriver()
        serial_env = _os.environ.get("RAG_SERIAL_PORT") or _os.environ.get("SERIAL_PORT")
        self.serial_hid = SerialHID(port=serial_env)
        self._anti_cheat_warning_shown = False
        # Admin check para feedback no HUD
        try:
            self.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            self.is_admin = False
        self._INPUT_MOUSE = INPUT_MOUSE
        self._INPUT = INPUT
        self._MOUSEINPUT = MOUSEINPUT
        # PID do processo Python para verificação de foco
        self._own_pid = _os.getpid()
        # Throttle de log de diagnóstico [ClickDebug]: no máximo 1 linha por segundo
        self._last_debug_log = 0.0
        # Dimensões da tela virtual (para validação de coordenadas)
        SM_CXSCREEN = 0
        SM_CYSCREEN = 1
        self._screen_w = self.user32.GetSystemMetrics(SM_CXSCREEN) or 1920
        self._screen_h = self.user32.GetSystemMetrics(SM_CYSCREEN) or 1080
        self._click_method = DEFAULT_CLICK_METHOD
        self._ragexe_pid = None
        self._game_window = (None, None, 0)
        self._overlay_visible = self.filters_config.get("hud_overlay_visible", True)
        self._show_routes = tk.BooleanVar(value=self.filters_config.get("hud_show_routes", True))
        self._automation_running = False
        self._pending_hud_interaction = None
        self._hud_npc_line_target = None
        self._hud_manual_clicks = collections.deque(maxlen=16)
        self._hud_click_grace = (0.0, 0.0)
        self._hud_pressed_keys = None
        self._hud_binding_action = None
        self._hud_edit = None
        self._hud_edit_hwnd = None
        self._hud_edit_keys = collections.deque()
        self._hud_dead_key = ""
        self._hud_choice = None
        self._hud_choice_button = None
        self._hud_hotkeys = self._load_hud_hotkeys()
        self._hud_panel = None
        self._hud_drawer = None
        self._hud_subclass_proc = None
        self._hud_subclass_windows = set()
        self._hud_panel_visible = True
        self._hud_active_drawer = None
        self._hud_drawer_paused = False
        self._hud_drawer_walk_since = None
        self._hud_drawer_frames = {}
        self._hud_drawer_buttons = {}
        self._hud_tray = None
        self._hud_tray_restore = Event()
        self._hud_update_after = None
        self.current_viewport = None
        self._monster_filter = ""
        self._hud_btns = {}
        self._patrol_npc_blocked_coords = {}
        self._walk_watchdog = None
        self._show_mobs = tk.BooleanVar(value=True)
        self._show_drops = tk.BooleanVar(value=True)
        self._show_npcs = tk.BooleanVar(value=True)
        self._show_warps = tk.BooleanVar(value=True)
        self._show_players = tk.BooleanVar(value=True)
        self._mob_filter_mode = "Todos"
        self._drop_filter_mode = "Todos"
        self.mob_whitelist = set()
        self.mob_whitelist_names = set()
        self.mob_blacklist = set()
        self.mob_blacklist_names = set()
        self.drop_whitelist = set()
        self.drop_whitelist_names = set()
        self.drop_blacklist = set()
        self.drop_blacklist_names = set()
        self._city_travel_state = None
        self.last_kafra_click_ts = 0.0
        self.handled_dialog_seq = -1
        self.last_dialog_action_ts = 0.0
        self._navi_buffer = ""  # buffer do hook de teclado para /navi
        self._navi_hook_handle = None  # handle do WH_KEYBOARD_LL
        self._navi_hook_proc = None   # mantém referência ao CFUNCTYPE vivo
        self._click_in_progress = False
        self._action_click_used = None
        self._last_patrol_click_target = None
        self._patrol_stuck_attempts = 0
        self._npc_click_state = {}
        self._session_kills = collections.Counter()
        self._session_items = collections.Counter()
        self._session_item_ids = collections.defaultdict(set)
        self._session_items_lock = Lock()
        self._session_kill_count = 0
        self._kill_history_last_check = time.monotonic()
        self._session_active_seconds = 0.0
        self._session_active_since = None
        world = getattr(self.feed, "world", None)
        if world:
            world.on_pickup_ack_callback = self._on_pickup_ack
        self._create_hud_panel()
        self._install_navi_hook()
        if hasattr(self.feed, "camera"):
            self.feed.camera.on_lbutton_down_callback = self._on_game_lbutton_down
            self.feed.camera.on_physical_click = self._record_hud_click

    def _create_hud_panel(self):
        bg, surface, fg = "#12141d", "#222638", "#eef0f8"
        blue, purple = "#3b82f6", "#8b5cf6"
        font = ("Segoe UI", 9)
        button_style = dict(bg=surface, fg=fg, activebackground=blue,
                            activeforeground="#ffffff", relief="flat", bd=0,
                            font=font, padx=5, pady=2, cursor="hand2")

        def window(title):
            hud = tk.Toplevel(self.root, bg=bg)
            hud.withdraw()
            hud.title(title)
            hud.overrideredirect(True)
            hud.resizable(False, False)
            hud.attributes("-alpha", 0.92, "-topmost", True, "-toolwindow", True)
            hud.bind("<Button-1>", self._on_hud_control_click, add="+")
            hud.bind("<ButtonRelease-1>", self._on_hud_control_release, add="+")
            return hud

        def row(parent):
            frame = tk.Frame(parent, bg=bg)
            frame.pack(fill="x", pady=2)
            return frame

        def label(parent, text, color=fg):
            return tk.Label(parent, text=text, bg=bg, fg=color, font=font, anchor="w")

        def button(parent, text, command, side="left"):
            widget = tk.Button(parent, text=text, command=command, **button_style)
            widget.pack(side=side, padx=1)
            return widget

        def entry(parent, width=None):
            options = {"width": width} if width else {}
            widget = tk.Entry(parent, bg=surface, fg=fg, insertbackground=fg,
                              relief="flat", bd=3, font=font, **options)
            widget.pack(side="left", fill="x", expand=width is None, padx=2)
            self._bind_hud_entry(widget, surface)
            return widget

        def menu(parent, var, values, command=None):
            caption = tk.StringVar()
            def update_caption(*_):
                caption.set(f"{var.get()} ▾")
            var.trace_add("write", update_caption)
            update_caption()

            def open_choices():
                already_open = self._hud_choice_button is widget
                self._dismiss_hud_choice()
                if already_open:
                    return
                choices = tk.Frame(drawer, bg=surface, highlightthickness=1, highlightbackground=purple)
                listing = listbox(choices, min(7, len(values)), expand=True)
                listing.insert(tk.END, *values)
                listing.see(values.index(var.get()))

                def choose(event):
                    value = values[listing.nearest(event.y)]
                    self._dismiss_hud_choice()
                    var.set(value)
                    if command:
                        command(value)
                    return "break"

                listing.bind("<Button-1>", lambda _event: "break")
                listing.bind("<ButtonRelease-1>", choose)
                x = widget.winfo_rootx() - drawer.winfo_rootx()
                y = widget.winfo_rooty() - drawer.winfo_rooty() + widget.winfo_height()
                choices.update_idletasks()
                y = min(y, drawer.winfo_height() - choices.winfo_reqheight())
                choices.place(x=x, y=max(0, y), width=150)
                choices.lift()
                self._hud_choice = choices
                self._hud_choice_button = widget

            widget = tk.Button(parent, textvariable=caption, command=open_choices,
                               takefocus=False, **button_style)
            widget.pack(side="left", padx=2)

        def listbox(parent, height, expand=False):
            frame = tk.Frame(parent, bg=bg)
            frame.pack(fill="both", expand=expand, pady=2)
            scroll = tk.Scrollbar(frame)
            widget = tk.Listbox(frame, height=height, width=1, bg=surface, fg=fg,
                                selectbackground=blue, exportselection=False, relief="flat",
                                highlightthickness=0, font=font, takefocus=False, yscrollcommand=scroll.set)
            def select(event):
                self._on_hud_control_click(event)
                if widget.size():
                    index = widget.nearest(event.y)
                    widget.selection_clear(0, tk.END)
                    widget.selection_set(index)
                    widget.activate(index)
                return "break"

            widget.bind("<Button-1>", select)
            widget.pack(side="left", fill="both", expand=True)
            scroll.configure(command=widget.yview)
            scroll.pack(side="right", fill="y")
            return widget

        def selection(widget):
            indices = widget.curselection()
            if indices:
                return getattr(widget, "_hud_rows", ())[indices[0]]

        def split(parent):
            frame = row(parent)
            frame.columnconfigure((0, 1), weight=1, uniform="filters")
            columns = [tk.Frame(frame, bg=bg) for _ in range(2)]
            for idx, column in enumerate(columns):
                column.grid(row=0, column=idx, sticky="nsew", padx=2)
            return columns

        filter_views = []

        def filter_list(parent, title, ids, names, catalog):
            label(parent, title, purple).pack(fill="x")
            widget = listbox(parent, 3)
            controls = row(parent)
            field = entry(controls)

            def add(*_):
                value = field.get().strip()
                numeric = value.lstrip("#")
                if numeric.isdecimal():
                    ids.add(int(numeric))
                elif value:
                    names.add(value.lower())
                field.delete(0, tk.END)
                update_filters()

            def remove():
                selected = selection(widget)
                if selected:
                    value = selected[0]
                    if isinstance(value, int):
                        ids.discard(value)
                    else:
                        names.discard(value)
                    update_filters()

            field._hud_submit = add
            button(controls, "+", add)
            button(controls, "Remover", remove)
            filter_views.append((widget, ids, names, catalog))

        def update_filters():
            for widget, ids, names, catalog in filter_views:
                rows = [(key, f"{catalog.get(key, 'ID')} (#{key})") for key in ids]
                rows.extend((name, name) for name in names)
                self._set_hud_list(widget, sorted(rows, key=lambda item: item[1].casefold()))

        def quick_filters(parent, widget, whitelist, blacklist, on_add=None):
            def add(mode, ids):
                selected = selection(widget)
                if selected:
                    key = selected[0]
                    if isinstance(key, int) and key:
                        ids.add(key)
                    elif isinstance(key, str):
                        with self._session_items_lock:
                            ids.update(self._session_item_ids[key])
                    if on_add:
                        on_add(mode)
                    update_filters()

            button(parent, "+ Whitelist", lambda: add("Whitelist", whitelist))
            button(parent, "+ Blacklist", lambda: add("Blacklist", blacklist))

        dock = self._hud_panel = window("Ragnalok Top Dock")
        drawer = self._hud_drawer = window("Ragnalok Gavetas")
        drawer.geometry("420x340")
        strip = tk.Frame(dock, bg=bg, padx=4)
        strip.pack(fill="both", expand=True)
        self._hud_status = tk.StringVar(value="Mapa —  |  Peso —  |  Slots: —/100  |  00:00:00")
        tk.Label(strip, textvariable=self._hud_status, bg=bg, fg=blue, width=52,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        self._hud_play_button = button(strip, "▶ Play", self._toggle_hud_run)
        self._set_hud_running(False)
        for title, attr, action in (
            ("Farm", "auto_farm", self._toggle_auto_farm),
            ("Loot", "auto_loot", self._toggle_auto_loot),
            ("Patrol", "auto_patrol", self._toggle_auto_patrol),
        ):
            self._hud_btns[attr] = button(strip, title, action)
        button(strip, "Guardar na Kafra", self._request_kafra_storage)
        button(strip, "Parar", self._stop_hud_actions)

        def add_drawer(title):
            frame = tk.Frame(drawer, bg=bg, padx=10, pady=7)
            self._hud_drawer_frames[title] = frame
            self._hud_drawer_buttons[title] = button(strip, title, lambda: self._toggle_hud_drawer(title))
            return frame

        maps = add_drawer("Mapas")
        city_row = row(maps)
        label(city_row, "Cidade").pack(side="left")
        cities = ("Prontera", "Izlude", "Geffen", "Payon", "Morocc", "Alberta", "Aldebaran",
                  "Rachel", "Veins", "Juno", "Comodo", "Einbroch", "Lighthalzen", "Hugel")
        city = tk.StringVar(value="Izlude")
        menu(city_row, city, cities)
        button(city_row, "Viajar",
               lambda: self._hud_travel("yuno" if city.get() == "Juno" else city.get(), "travel"), "right")

        def search(parent, maps_only=False):
            label(parent, "Monstro ou mapa" if maps_only else "Buscar Monstro / Carta / Item",
                  purple).pack(fill="x")
            controls = row(parent)
            field = entry(controls)
            results = listbox(parent, 4 if maps_only else 2)

            def find(*_):
                query = field.get().strip()
                rows = []
                if query:
                    world = getattr(self.feed, "world", None)
                    level = getattr(getattr(world, "player", None), "base_level", 1) or 1
                    if maps_only:
                        known_maps = {name for pair in MAP_PORTALS for name in pair}
                        for portal in self.world_routes_data.get("portals", ()):
                            known_maps.update((portal["from_map"], portal["to_map"]))
                        known_maps.update(self.world_routes_data.get("aliases", {}).values())
                        rows.extend((name, name) for name in sorted(known_maps)
                                    if query.casefold() in name.casefold())
                    for mob in search_mob_catalog(query, base_level=level):
                        matched = mob.get("matched_drop")
                        suffix = f" · {matched['item_name']} ({matched['rate']:.2f}%)" if matched else ""
                        rows.append((mob["job_id"], f"{mob['name']} · Nv. {mob['level']}{suffix}", mob))
                self._set_hud_list(results, rows[:40])
                if results.size():
                    results.selection_clear(0, tk.END)
                    results.selection_set(0)
                    results.see(0)

            def travel(mode):
                selected = selection(results)
                if selected and len(selected) == 3:
                    mob = selected[2]
                    spawns = mob.get("spawns") or ()
                    aliases = self.world_routes_data.get("aliases", {})
                    target = aliases.get(mob["name"].lower()) or (spawns[0] if spawns else "")
                    if not target:
                        self._click_debug(f"BUSCA: Nenhum mapa encontrado para {mob['name']}")
                elif selected:
                    target = selected[0]
                else:
                    target = field.get().strip() if maps_only else ""
                if target:
                    self._hud_travel(target, mode)

            def focus():
                selected = selection(results)
                if selected:
                    self.mob_whitelist.add(selected[0])
                    self._mob_filter_mode = "Whitelist"
                    mob_mode.set("Whitelist")
                    update_filters()

            field._hud_submit = find
            field._hud_changed = lambda: results.selection_clear(0, tk.END)
            button(controls, "Buscar", find)
            actions = row(parent)
            if maps_only:
                button(actions, "Viajar & Farmar", lambda: travel("farm"))
                results.bind("<Double-Button-1>", lambda _event: travel("farm"))
            else:
                button(actions, "Focar Monstro", focus)
                button(actions, "Ir para Mapa", lambda: travel("travel"))

        search(maps, maps_only=True)
        coord = row(maps)
        label(coord, "X").pack(side="left")
        x = entry(coord, 5)
        label(coord, "Y").pack(side="left")
        y = entry(coord, 5)
        button(coord, "Ir Andando", lambda: self._hud_walk_to(x.get(), y.get()))
        navi = row(maps)
        label(navi, "/navi").pack(side="left")
        command = entry(navi)
        command._hud_submit = lambda: self._hud_navi(command.get())
        button(navi, "Ir (/navi)", lambda: self._hud_navi(command.get()))

        world_map = tk.Frame(drawer, bg=bg, padx=10, pady=7)
        button(row(maps), "Mapa Mundi", lambda: self._show_hud_world_map(maps, world_map))
        map_controls = row(world_map)
        button(map_controls, "Voltar", lambda: self._show_hud_world_map(world_map, maps))
        map_target = tk.StringVar(value="")
        canvas = tk.Canvas(world_map, bg=surface, highlightthickness=0, width=396, height=245)
        canvas.pack(fill="both", expand=True)
        tk.Label(world_map, textvariable=map_target, bg=bg, fg=fg, font=font).pack(anchor="w")

        def select_map(event):
            selected = self._get_world_map_entry(event.x / canvas.winfo_width(),
                                                 event.y / canvas.winfo_height())
            if selected:
                map_target.set(selected["map"])

        def draw_world_map(event):
            canvas.delete("all")
            if not self.world_routes_data.get("world_map_grid"):
                canvas.create_text(event.width // 2, event.height // 2,
                                   text="Mapa Mundi indisponível", fill=fg, font=font)
            for region in self.world_routes_data.get("world_map_grid", ()):
                x1, x2 = (region[key] * event.width for key in ("u_min", "u_max"))
                y1, y2 = (region[key] * event.height for key in ("v_min", "v_max"))
                canvas.create_rectangle(x1, y1, x2, y2, fill="#2b2543", outline=purple)
                canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, fill=fg,
                                   text=region.get("name") or region["map"], font=("Segoe UI", 7))

        canvas.bind("<Configure>", draw_world_map)
        canvas.bind("<Button-1>", select_map)
        button(map_controls, "Viajar", lambda: self._hud_travel(map_target.get(), "travel"))
        button(map_controls, "Viajar & Farmar", lambda: self._hud_travel(map_target.get(), "farm"))

        mobs = add_drawer("Mobs W/B")
        mob_mode = tk.StringVar(value=self._mob_filter_mode)
        mode = row(mobs)
        label(mode, "Modo").pack(side="left")
        menu(mode, mob_mode, ("Todos", "Whitelist", "Blacklist"),
             lambda value: setattr(self, "_mob_filter_mode", value))
        label(mobs, "Monstros Vivos Agora", purple).pack(fill="x")
        self._mobs_listbox = listbox(mobs, 6)
        quick_filters(row(mobs), self._mobs_listbox, self.mob_whitelist, self.mob_blacklist)
        white, black = split(mobs)
        filter_list(white, "Whitelist de Monstros", self.mob_whitelist, self.mob_whitelist_names,
                    self.names.get("mob", {}))
        filter_list(black, "Blacklist de Monstros", self.mob_blacklist, self.mob_blacklist_names,
                    self.names.get("mob", {}))

        items = add_drawer("Itens W/B")
        drop_mode = tk.StringVar(value=self._drop_filter_mode)
        mode = row(items)
        label(mode, "Modo").pack(side="left")
        def set_drop_mode(value):
            drop_mode.set(value)
            self._drop_filter_mode = value

        menu(mode, drop_mode, ("Todos", "Whitelist", "Blacklist"), set_drop_mode)
        white, black = split(items)
        filter_list(white, "Whitelist de Itens", self.drop_whitelist, self.drop_whitelist_names,
                    self.names.get("drop", {}))
        filter_list(black, "Blacklist de Itens", self.drop_blacklist, self.drop_blacklist_names,
                    self.names.get("drop", {}))
        tier1, tier3 = split(items)
        label(tier1, "Tier 1 · Prioridade máxima", purple).pack(fill="x")
        label(tier3, f"Tier 3 · Ignorar com peso ≥ {self.tier3_max_weight_pct}%", purple).pack(fill="x")
        for column, values in ((tier1, self.tier1_items), (tier3, self.tier3_items)):
            listbox(column, 2).insert(tk.END, *values)
        search(items)

        npcs = add_drawer("NPCs")
        label(npcs, "NPCs e Portais Próximos", purple).pack(fill="x")
        self._npc_listbox = listbox(npcs, 8, expand=True)
        self._npc_listbox.bind("<Double-Button-1>", lambda _event: self._hud_interact())
        button(row(npcs), "Ir / Interagir", self._hud_interact)

        summary = add_drawer("Resumo")
        self._hud_summary = label(summary, "", blue)
        self._hud_summary.pack(fill="x")
        label(summary, "Abates Agrupados", purple).pack(fill="x")
        self._hud_kills_list = listbox(summary, 5, expand=True)
        label(summary, "Itens Coletados Agrupados", purple).pack(fill="x")
        self._hud_items_list = listbox(summary, 5, expand=True)
        quick_filters(row(summary), self._hud_items_list, self.drop_whitelist, self.drop_blacklist,
                      on_add=set_drop_mode)

        settings = add_drawer("⚙")
        tk.Label(settings, text="Configurações", bg=bg, fg=fg,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", pady=(0, 4))
        tabs = row(settings)
        content = tk.Frame(settings, bg=bg)
        content.pack(fill="both", expand=True, pady=(5, 0))
        pages = {}
        tab_buttons = {}

        def show_category(title):
            self._cancel_hud_binding()
            for name, page in pages.items():
                page.pack_forget()
                tab_buttons[name].configure(bg=purple if title == name else surface)
            pages[title].pack(fill="both", expand=True)

        for title in ("Execução", "Sobrevivência", "Visuais", "Atalhos", "Kafra"):
            pages[title] = tk.Frame(content, bg=bg)
            tab_buttons[title] = button(tabs, title, lambda name=title: show_category(name))

        check_style = dict(bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                           selectcolor=surface, font=font, bd=0, highlightthickness=0,
                           anchor="w", takefocus=False)

        def check(parent, text, variable, action=None, help_text=None):
            tk.Checkbutton(parent, text=text, variable=variable, command=action,
                           **check_style).pack(anchor="w")
            if help_text:
                tk.Label(parent, text=help_text, bg=bg, fg="#aab1c6", justify="left",
                         wraplength=378, anchor="w", font=("Segoe UI", 8)).pack(fill="x", padx=6, pady=(0, 5))

        execution = pages["Execução"]
        label(execution, "Selecione os modos no Top Dock e use Play para iniciar.", purple).pack(fill="x", pady=(2, 6))
        self._hud_pause_var = tk.BooleanVar(value=self.filters_config["hud_pause_on_drawer"])
        self._hud_mouse_pause_var = tk.BooleanVar(value=self.filters_config["hud_pause_on_click"])
        check(execution, "Pausar ao abrir uma gaveta", self._hud_pause_var, self._save_hud_pause,
              "Retoma ao fechar se a execução estava em Play.")
        check(execution, "Pausar ao clicar manualmente no jogo", self._hud_mouse_pause_var,
              lambda: self._save_hud_setting("hud_pause_on_click", self._hud_mouse_pause_var.get()),
              "Depois do clique, use Play para retomar.")
        check(execution, "Ignorar lixo com peso acima de 60%", self._ignore_tier3_over_60)
        button(row(execution), "Minimizar para bandeja", self._minimize_hud)
        self._hud_tray_status = label(execution, "", purple)
        self._hud_tray_status.pack(fill="x")

        kafra = pages["Kafra"]
        auto_store = tk.BooleanVar(value=self.filters_config["auto_store_kafra"])

        def toggle_kafra():
            self._kafra_capacity_latched = False
            self._kafra_block = None
            self._save_hud_setting("auto_store_kafra", auto_store.get())

        check(kafra, "Auto-Guardar na Kafra por peso ou slots", auto_store, toggle_kafra)
        for key, title, mask in (
            ("kafra_trigger_weight_pct", "Peso %", ""),
            ("kafra_trigger_slots", "Limite de slots", ""),
            ("kafra_pin", "Senha Kafra (PIN)", "•"),
            ("keep_in_bag", "Manter na Mochila", ""),
        ):
            line = row(kafra)
            label(line, title).pack(side="left")
            value = self.filters_config[key]
            field = tk.Entry(line, width=26, show=mask, bg=surface, fg=fg,
                             insertbackground=fg, relief="flat", font=font)
            field.insert(0, ", ".join(value) if isinstance(value, list) else str(value))
            field.pack(side="right", fill="x", expand=True, padx=(8, 0))
            self._bind_hud_entry(field, surface)
            field._hud_finish = lambda name=key, entry=field: self._save_kafra_field(name, entry)
        button(row(kafra), "Guardar na Kafra Agora", self._request_kafra_storage)

        survival = pages["Sobrevivência"]
        for key, title in (
            ("survival_enabled", "Auto-Fugir e Sentar"),
            ("survival_seek_portal", "Buscar Portal Seguro ao Fugir"),
            ("survival_auto_respawn", "Auto-Ressuscitar e Voltar ao Farm"),
        ):
            enabled = tk.BooleanVar(value=self.filters_config[key])
            check(survival, title, enabled,
                  lambda name=key, value=enabled: self._save_hud_setting(name, value.get()))

        fields = {}

        def save_survival_numbers(_event=None):
            for key, _, _, minimum, maximum in SURVIVAL_NUMBERS:
                field = fields[key]
                try:
                    value = int(field.get())
                except ValueError:
                    value = self.filters_config[key]
                self.filters_config[key] = min(maximum, max(minimum, value))
            self.filters_config["survival_resume_pct"] = max(
                self.filters_config["survival_hp_pct"], self.filters_config["survival_resume_pct"])
            for key, field in fields.items():
                value = str(self.filters_config[key])
                if field.get() != value:
                    field.set(value)
            save_filters_config(self.filters_config)

        for key, title, _, minimum, maximum in SURVIVAL_NUMBERS:
            options = row(survival)
            label(options, title).pack(side="left")
            fields[key] = tk.StringVar(value=str(self.filters_config[key]))
            spin = tk.Spinbox(options, from_=minimum, to=maximum, width=4,
                              textvariable=fields[key], command=save_survival_numbers,
                              bg=surface, fg=fg, insertbackground=fg, relief="flat", font=font)
            spin.pack(side="right")
            self._bind_hud_entry(spin, surface)
            spin._hud_finish = save_survival_numbers
        test_row = row(survival)
        button(test_row, "Testar Sentar (↓)", self._test_sit_key)
        self._hud_sit_test_status = label(test_row, "", purple)
        self._hud_sit_test_status.pack(side="left", padx=5)

        visuals = pages["Visuais"]
        overlay = tk.BooleanVar(value=self._overlay_visible)
        check(visuals, "Mostrar marcações no jogo", overlay,
              lambda: self._set_hud_overlay(overlay.get()),
              "Controla as marcações e rotas sobre o mapa. O Top Dock continua visível.")
        check(visuals, "Rotas e waypoints", self._show_routes,
              lambda: self._save_hud_setting("hud_show_routes", self._show_routes.get()),
              "Mostra as rotas calculadas e o alvo direto do Farm.")
        for title, variable in (("Mobs", self._show_mobs), ("Drops", self._show_drops),
                                ("NPCs", self._show_npcs), ("Portais", self._show_warps),
                                ("Players", self._show_players)):
            check(visuals, title, variable)

        shortcuts = pages["Atalhos"]
        tk.Label(shortcuts, text="Clique no atalho que quer trocar e pressione uma tecla. "
                                "O jogo também recebe a tecla.",
                 bg=bg, fg="#aab1c6", justify="left", wraplength=380,
                 anchor="w", font=font).pack(fill="x", pady=(0, 4))
        keys = tk.Frame(shortcuts, bg=bg)
        keys.pack(fill="x")
        keys.columnconfigure(0, weight=1)
        self._hud_binding_labels = {}
        for idx, (action, (title, _)) in enumerate(HUD_BINDINGS.items()):
            label(keys, title).grid(row=idx, column=0, sticky="w", pady=1)
            value = tk.StringVar(value=HUD_KEY_NAMES[self._hud_hotkeys[action]])
            self._hud_binding_labels[action] = value
            tk.Button(keys, textvariable=value, **button_style, width=14, takefocus=False,
                      command=lambda name=action: self._begin_hud_binding(name)).grid(
                          row=idx, column=1, sticky="e", pady=1)
        self._hud_binding_status = label(shortcuts, "", purple)
        self._hud_binding_status.pack(fill="x", pady=(3, 0))
        show_category("Execução")

        update_filters()
        dock.update_idletasks()
        self._hud_dock_width = strip.winfo_reqwidth()
        dock.geometry(f"{self._hud_dock_width}x30")
        self._update_hud()

    @staticmethod
    def _set_hud_list(widget, rows):
        previous = getattr(widget, "_hud_rows", ())
        rows = tuple(rows)
        if previous == rows:
            return
        indices = widget.curselection()
        selected = previous[indices[0]][0] if indices else None
        scroll = widget.yview()[0]
        widget.delete(0, tk.END)
        if rows:
            widget.insert(tk.END, *(item[1] for item in rows))
        for idx, item in enumerate(rows):
            if item[0] == selected:
                widget.selection_set(idx)
                break
        widget.yview_moveto(scroll)
        widget._hud_rows = rows

    def _toggle_hud_drawer(self, title):
        active = self._hud_active_drawer
        self._close_hud_drawer(restore_focus=title == active or title == "⚙", hide_window=title == active)
        if title == active:
            return
        self._hud_active_drawer = title
        self._hud_drawer_walk_since = None
        self._hud_drawer_frames[title].pack(fill="both", expand=True)
        self._hud_drawer_paused = self._hud_pause_var.get()
        self._sync_hud_windows(self.current_viewport, True)
        self._refresh_hud_drawer(time.monotonic())

    def _close_hud_drawer(self, restore_focus=True, *, hide_window=True):
        if self._hud_active_drawer is None:
            return
        had_focus = restore_focus and self._is_hud_foreground()
        self._finish_hud_edit()
        self._dismiss_hud_choice()
        for child in self._hud_drawer.pack_slaves():
            child.pack_forget()
        if hide_window:
            self._hide_hud_window(self._hud_drawer)
        self._cancel_hud_binding()
        self._hud_active_drawer = None
        self._hud_drawer_paused = False
        self._hud_drawer_walk_since = None
        if had_focus:
            self._focus_hud_game()

    @staticmethod
    def _show_hud_world_map(current, target):
        current.pack_forget()
        target.pack(fill="both", expand=True)

    def _dismiss_hud_choice(self):
        if self._hud_choice is not None:
            self._hud_choice.destroy()
            self._hud_choice = None
            self._hud_choice_button = None

    def _on_hud_control_click(self, event):
        widget = event.widget
        if isinstance(widget, (tk.Button, tk.Checkbutton, tk.Radiobutton)):
            self._mark_hud_control_click()
        self._finish_hud_edit()
        if self._hud_choice is None or widget is self._hud_choice_button:
            return
        while widget is not None and widget is not self._hud_choice:
            widget = getattr(widget, "master", None)
        if widget is None:
            self._dismiss_hud_choice()

    def _on_hud_control_release(self, event):
        if isinstance(event.widget, (tk.Button, tk.Checkbutton, tk.Radiobutton)):
            self._mark_hud_control_click()
            self._focus_hud_game()

    def _mark_hud_control_click(self):
        now = time.monotonic()
        start, end = self._hud_click_grace
        self._hud_click_grace = (start if now <= end else now, now + 0.300)

    def _bind_hud_entry(self, field, color):
        field.configure(highlightthickness=1, highlightbackground=color, takefocus=False)
        field._hud_border = color

        def edit(event):
            if isinstance(field, tk.Spinbox):
                element = field.identify(event.x, event.y)
                if element in ("buttonup", "buttondown"):
                    self._mark_hud_control_click()
                    self._finish_hud_edit()
                    field.invoke(element)
                    self._focus_hud_game()
                    return "break"
            if field is not self._hud_edit:
                self._finish_hud_edit()
                self._dismiss_hud_choice()
                self._hud_edit = field
                self._hud_edit_hwnd = u32.GetForegroundWindow()
                field.configure(highlightbackground="#8b5cf6")
            field.tk.call(field._w, "selection", "clear")
            field.icursor(field.index(f"@{event.x}"))
            field.tk.call(field._w, "selection", "from", tk.INSERT)
            return "break"

        def select(event):
            field.tk.call(field._w, "selection", "to", field.index(f"@{event.x}"))
            return "break"

        def select_all(event):
            edit(event)
            if field is self._hud_edit:
                field.tk.call(field._w, "selection", "range", 0, tk.END)
            return "break"

        field.bind("<Button-1>", edit)
        field.bind("<Double-Button-1>", select_all)
        field.bind("<B1-Motion>", select)

    def _drain_hud_edit_keys(self, *, finishing=False):
        while self._hud_edit_keys:
            field, key, ctrl, text = self._hud_edit_keys.popleft()
            if finishing and key in (0x0D, 0x1B):
                break
            self._apply_hud_key(field, key, ctrl, text)

    def _finish_hud_edit(self, *, apply_queued_keys=True):
        field = self._hud_edit
        if field is not None:
            if apply_queued_keys:
                self._drain_hud_edit_keys(finishing=True)
            self._hud_edit = None
            self._hud_edit_hwnd = None
            self._hud_edit_keys.clear()
            self._hud_dead_key = ""
            self._hud_pressed_keys = None
            field.configure(highlightbackground=field._hud_border)
            if finish := getattr(field, "_hud_finish", None):
                finish()

    def _apply_hud_key(self, field, key, ctrl, text):
        if field is not self._hud_edit:
            return
        if key in (0x0D, 0x1B):
            self._finish_hud_edit(apply_queued_keys=False)
            if key == 0x1B:
                self._close_hud_drawer()
            elif submit := getattr(field, "_hud_submit", None):
                submit()
            return
        cursor = field.index(tk.INSERT)
        if key in (0x23, 0x24, 0x25, 0x27):
            field.tk.call(field._w, "selection", "clear")
            field.icursor({0x23: tk.END, 0x24: 0, 0x25: max(0, cursor - 1), 0x27: cursor + 1}[key])
            return
        if ctrl:
            if key == ord("A"):
                field.tk.call(field._w, "selection", "range", 0, tk.END)
                return
            if key != ord("V"):
                return
            try:
                text = field.clipboard_get().splitlines()[0]
            except (tk.TclError, IndexError):
                return
        if not text and key not in (0x08, 0x2E):
            return
        if field.tk.getboolean(field.tk.call(field._w, "selection", "present")):
            field.delete(tk.SEL_FIRST, tk.SEL_LAST)
        elif key == 0x08 and cursor:
            field.delete(cursor - 1)
        elif key == 0x2E:
            field.delete(cursor)
        if text:
            field.insert(tk.INSERT, text)
        if changed := getattr(field, "_hud_changed", None):
            changed()

    def _save_hud_setting(self, key, value):
        self.filters_config[key] = value
        save_filters_config(self.filters_config)

    def _set_hud_overlay(self, visible):
        self._overlay_visible = visible
        self._save_hud_setting("hud_overlay_visible", visible)

    def _hud_can_run(self):
        return (self._automation_running and not self._hud_drawer_paused
                and self._hud_binding_action is None and self._hud_edit is None)

    def _set_hud_running(self, running):
        self._automation_running = running
        self._hud_play_button.configure(
            text="Ⅱ Pause" if running else "▶ Play",
            bg="#8b5cf6" if running else "#3b82f6",
        )

    def _toggle_hud_run(self):
        running = not self._automation_running
        if running:
            self._close_hud_drawer()
        self._set_hud_running(running)

    def _load_hud_hotkeys(self):
        defaults = {action: key for action, (_, key) in HUD_BINDINGS.items()}
        saved = self.filters_config.get("hud_hotkeys")
        if not isinstance(saved, dict):
            return defaults
        bindings = {action: saved.get(action, key) for action, key in defaults.items()}
        if any(type(key) is not int or key not in HUD_KEY_NAMES for key in bindings.values()):
            return defaults
        return bindings if len(set(bindings.values())) == len(bindings) else defaults

    def _begin_hud_binding(self, action):
        self._hud_binding_action = action
        self._hud_pressed_keys = None
        self._hud_binding_status.configure(text="Pressione a nova tecla. Esc cancela.")

    def _cancel_hud_binding(self):
        self._hud_binding_action = None
        self._hud_binding_status.configure(text="")

    def _finish_hud_binding(self, action, key):
        conflict = next((name for name, code in self._hud_hotkeys.items() if code == key and name != action), None)
        if conflict is not None:
            self._hud_binding_status.configure(text=f"{HUD_KEY_NAMES[key]} já está em uso. Escolha outra tecla.")
            return
        self._hud_hotkeys[action] = key
        self._hud_binding_labels[action].set(HUD_KEY_NAMES[key])
        self._cancel_hud_binding()
        self._save_hud_setting("hud_hotkeys", self._hud_hotkeys)

    def _manual_mouse_button_pressed(self):
        if win32_input.is_bot_clicking(0.85) or self._is_hud_foreground():
            return False
        pt = wintypes.POINT()
        if u32.GetCursorPos(ctypes.byref(pt)) and self._is_point_in_hud(pt.x, pt.y, margin=0):
            return False
        return any(u32.GetAsyncKeyState(key) & 0x8000 for key in (0x01, 0x02, 0x04))

    def _record_hud_click(self, sx, sy, left_button):
        if (not self._hud_render_foreground or self._click_in_progress
                or not (self._automation_running or self._hud_edit is not None or self._hud_choice is not None)):
            return
        fg = u32.GetForegroundWindow() or 0
        if self._is_hud_foreground(fg) or not self._is_game_focused(fg):
            return
        clicked_at = time.monotonic()
        if win32_input.is_bot_clicking(0.85):
            return
        start, end = self._hud_click_grace
        if left_button and start <= clicked_at <= end:
            return
        # O hook do mouse lê os retângulos publicados pelo HUD sem chamar Tk.
        for hud in (self._hud_panel, self._hud_drawer):
            if layout := getattr(hud, "_hud_layout", None):
                left, top, width, height, visible = layout
                if visible and left <= sx < left + width and top <= sy < top + height:
                    return
        self._hud_manual_clicks.append((sx, sy, left_button, clicked_at))

    def _poll_hud_input(self):
        viewport = self.current_viewport
        while self._hud_manual_clicks:
            sx, sy, left_button, clicked_at = self._hud_manual_clicks.popleft()
            if self._click_in_progress or win32_input.is_bot_clicking(0.85):
                continue
            start, end = self._hud_click_grace
            if left_button and start <= clicked_at <= end:
                continue
            if viewport and not self._is_hud_foreground():
                left, top, width, height = viewport
                if (left <= sx < left + width and top <= sy < top + height
                        and not self._is_point_in_hud(sx, sy, margin=0)):
                    self._finish_hud_edit()
                    self._dismiss_hud_choice()
                    if self._automation_running and self._hud_mouse_pause_var.get():
                        self._set_hud_running(False)
                        print("[Automação] Pausada por clique manual. Use Play para retomar.", flush=True)
                    self._hud_manual_clicks.clear()
                    break

        if self._hud_edit is not None:
            return
        action = self._hud_binding_action
        keys = HUD_KEY_NAMES if action else self._hud_hotkeys.values()
        pressed = {key for key in (*keys, 0x1B)
                   if self.user32.GetAsyncKeyState(key) & 0x8000}
        new_keys = pressed - self._hud_pressed_keys if self._hud_pressed_keys is not None else set()
        self._hud_pressed_keys = pressed
        if action:
            if 0x1B in new_keys:
                self._cancel_hud_binding()
            elif candidate := new_keys.intersection(HUD_KEY_NAMES):
                self._finish_hud_binding(action, min(candidate))
            return

        settings = self._hud_hotkeys
        if 0x1B in new_keys or settings["drawer"] in new_keys:
            if self._hud_active_drawer:
                self._close_hud_drawer()
            elif settings["drawer"] in new_keys and not self._hud_panel_visible:
                self._restore_hud()

        if self._is_hud_foreground():
            return
        if settings["play"] in new_keys:
            self._toggle_hud_run()
        if settings["loot"] in new_keys or settings["loot_alt"] in new_keys:
            self._toggle_auto_loot()
        if settings["farm"] in new_keys:
            self._toggle_auto_farm()
        if settings["patrol"] in new_keys:
            self._toggle_auto_patrol()

    def _save_hud_pause(self):
        enabled = self._hud_pause_var.get()
        self.filters_config["hud_pause_on_drawer"] = enabled
        self._hud_drawer_paused = bool(self._hud_active_drawer and enabled)
        save_filters_config(self.filters_config)

    def _focus_hud_game(self):
        hwnd = self.get_game_hwnd(None)
        if hwnd:
            _, root_hwnd, _ = self._remember_game_window(hwnd)
            if u32.GetForegroundWindow() != root_hwnd:
                u32.SetForegroundWindow(root_hwnd)
        return hwnd

    def _guard_hud_activation(self, hwnd):
        if hwnd in self._hud_subclass_windows:
            return
        if self._hud_subclass_proc is None:
            @HUD_SUBCLASSPROC
            def window_proc(handle, message, wparam, lparam, _subclass_id, _data):
                if message == 0x0021:
                    return 3  # WM_MOUSEACTIVATE: MA_NOACTIVATE, preserva o clique
                if message == 0x0082:  # WM_NCDESTROY
                    self._hud_subclass_windows.discard(handle)
                    comctl32.RemoveWindowSubclass(handle, self._hud_subclass_proc, 1)
                return comctl32.DefSubclassProc(handle, message, wparam, lparam)

            self._hud_subclass_proc = window_proc
        if comctl32.SetWindowSubclass(hwnd, self._hud_subclass_proc, 1, 0):
            self._hud_subclass_windows.add(hwnd)

    def _show_hud_window(self, window, x, y, width=0, height=0, *, restack=True, move=True):
        opening = not window.winfo_ismapped()
        if opening:
            window.state("normal")
            window.update_idletasks()
        hwnd = u32.GetAncestor(window.winfo_id(), 2)
        if not hwnd:
            return False
        self._guard_hud_activation(hwnd)
        ws_ex_toolwindow, ws_ex_appwindow = 0x80, 0x40000
        previous_style = u32.GetWindowLongW(hwnd, -20)
        style = (previous_style | ws_ex_toolwindow | 0x08000000) & ~ws_ex_appwindow
        flags = 0x0210  # SWP_NOOWNERZORDER | SWP_NOACTIVATE
        if opening:
            flags |= 0x0040  # SWP_SHOWWINDOW
        if not (width and height):
            flags |= 0x0001  # SWP_NOSIZE
        if not move:
            flags |= 0x0002  # SWP_NOMOVE
        if not restack:
            flags |= 0x0004  # SWP_NOZORDER
        if style != previous_style:
            u32.SetWindowLongW(hwnd, -20, style)
            flags |= 0x0020  # SWP_FRAMECHANGED
        if not u32.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, flags):
            return False
        window._hud_hwnd = hwnd
        return True

    @staticmethod
    def _hide_hud_window(window):
        previous = getattr(window, "_hud_layout", None)
        if previous is not None and not previous[4]:
            return
        if window.winfo_ismapped():
            window.withdraw()
        if previous is not None:
            window._hud_layout = (*previous[:4], False)

    def _sync_hud_windows(self, viewport, foreground, game_hwnd=None):
        if not self._hud_panel:
            return
        windows = (
            (self._hud_panel, self._hud_panel_visible, self._hud_dock_width, 30, 0),
            (self._hud_drawer, self._hud_panel_visible and self._hud_active_drawer, 420, 340, 30),
        )
        anchor = game_hwnd or self._hud_render_anchor
        for window, visible, width, height, offset_y in windows:
            if not (foreground and viewport and visible):
                self._hide_hud_window(window)
                continue
            left, top, vw, _ = viewport
            x = left + (vw - width) // 2
            y = top + offset_y
            layout = (x, y, width, height, True)
            previous = getattr(window, "_hud_layout", None)
            hwnd = getattr(window, "_hud_hwnd", None)
            restack = (previous is None or not previous[4] or getattr(window, "_hud_anchor", None) != anchor
                       or getattr(window, "_hud_restack", False)
                       or (hwnd and not u32.GetWindowLongW(hwnd, -20) & 0x0008))  # WS_EX_TOPMOST
            if layout == previous and not restack:
                continue
            resized = previous is None or previous[2:4] != layout[2:4]
            size = (width, height) if resized else (0, 0)
            moved = previous is None or previous[:2] != layout[:2]
            if self._show_hud_window(window, x, y, *size, restack=restack, move=moved):
                window._hud_layout = layout
                window._hud_anchor = anchor
                window._hud_restack = False

    def _minimize_hud(self):
        if self._hud_tray is None:
            try:
                import pystray
                from PIL import Image, ImageDraw

                image = Image.new("RGB", (64, 64), "#12141d")
                draw = ImageDraw.Draw(image)
                draw.rectangle((10, 15, 54, 24), fill="#3b82f6")
                draw.rectangle((17, 29, 47, 51), fill="#8b5cf6")
                action = pystray.MenuItem("Restaurar HUD", lambda *_: self._hud_tray_restore.set(),
                                          default=True)
                icon = pystray.Icon("Ragnalok", image, "Ragnalok HUD", pystray.Menu(action))

                def setup(tray):
                    tray.visible = True

                icon.run_detached(setup=setup)
                self._hud_tray = icon
            except Exception:
                self._hud_tray_status.configure(text="Bandeja do sistema indisponível.")
                return
        self._close_hud_drawer()
        self._hud_panel_visible = False
        self._hide_hud_window(self._hud_panel)

    def _restore_hud(self):
        self._hud_panel_visible = True
        self._focus_hud_game()
        self._stop_hud_tray()

    def _stop_hud_tray(self):
        if self._hud_tray:
            try:
                self._hud_tray.stop()
            except Exception:
                pass
            self._hud_tray = None

    def _toggle_auto_loot(self):
        self.auto_loot = not self.auto_loot
        if not self.auto_loot and not self.auto_farm:
            self.locked_drop_gid = None
        print(f"[Auto-Loot] {'ON' if self.auto_loot else 'OFF'}", flush=True)

    def _stop_hud_actions(self):
        self._set_hud_running(False)
        self._pending_hud_interaction = None
        self._hud_npc_line_target = None
        self._kafra_manual = False
        self._kafra_cycle = None
        if self.auto_farm:
            self._toggle_auto_farm()
        if self.auto_loot:
            self._toggle_auto_loot()
        if self.auto_patrol:
            self._toggle_auto_patrol()
        self._clear_recovery_navigation()
        self.travel_plan = None
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._reset_survival()
        self._respawn_started = 0.0
        self._close_hud_drawer()
        self._focus_hud_game()

    def _hud_travel(self, target, mode):
        if target:
            self._close_hud_drawer()
            self.start_travel_plan(target, mode=mode)

    def _hud_walk_to(self, x, y):
        try:
            rx, ry = int(x), int(y)
        except ValueError:
            return
        current_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
        if not current_map:
            return
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(current_map) if terrain else None
        if gat and not gat.is_walkable(rx, ry):
            nearby = ((rx + dx, ry + dy) for dx in range(-4, 5) for dy in range(-4, 5))
            walkable = (cell for cell in nearby if gat.is_walkable(*cell))
            best = min(walkable, key=lambda cell: math.hypot(cell[0] - rx, cell[1] - ry), default=None)
            if best is None:
                return
            rx, ry = best
        self.auto_patrol = False
        self._clear_patrol_route()
        self.manual_nav_goal = (current_map, rx, ry)
        self.nav_goal = self.manual_nav_goal
        self._close_hud_drawer()
        self._focus_hud_game()

    def _hud_navi(self, text):
        current_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
        target = parse_navi_command(text.strip(), current_map)
        if target:
            self.manual_nav_goal = None
            self.nav_goal = (target["map"], target["x"], target["y"])
            self._close_hud_drawer()
            self._focus_hud_game()
        else:
            self._click_debug(f"[Navi] Formato inválido: {text!r}")

    def _update_hud(self):
        world = getattr(self.feed, "world", None)
        now = time.monotonic()
        if world:
            with world.lock:
                deaths = [dead for dead in world.recent_dead_mobs
                          if dead.get("time", 0.0) > self._kill_history_last_check]
            for dead in deaths:
                cid = dead.get("catalog_id", 0)
                if cid:
                    name = self.names.get("mob", {}).get(cid, f"Mob #{cid}")
                    self._session_kills[name] += 1
                    self._session_kill_count += 1
            if deaths:
                self._kill_history_last_check = max(dead["time"] for dead in deaths)

        stats = self.last_stats or {}
        snapshot = self.last
        map_name = stats.get("current_map") or stats.get("map") or getattr(snapshot, "map", None) or "—"
        player = getattr(snapshot, "player", None)
        pos = stats.get("pos") or (player.cell(now) if player else None)
        coords = f"({int(pos[0])}, {int(pos[1])})" if pos and None not in pos else "(—, —)"
        weight = stats.get("weight_ratio", getattr(snapshot, "weight_ratio", 0.0))
        slots = stats.get("estimated_slots_used", 0)
        self._hud_status.set(f"{map_name} {coords}  |  Peso {weight:.0%}  |  Slots: {slots}/100  |  {self._hud_elapsed()}")
        for attr, button in self._hud_btns.items():
            button.configure(bg="#3b82f6" if getattr(self, attr) else "#222638")
        for title, button in self._hud_drawer_buttons.items():
            button.configure(bg="#8b5cf6" if title == self._hud_active_drawer else "#222638")
        self._refresh_hud_drawer(now)
        self._hud_update_after = self.root.after(200, self._update_hud)

    def _hud_elapsed(self):
        minutes, seconds = divmod(int(self._session_active_seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _refresh_hud_drawer(self, now):
        active = self._hud_active_drawer
        if active == "Resumo":
            self._hud_summary.configure(text=f"Tempo ativo {self._hud_elapsed()}  |  Abates {self._session_kill_count}")
            with self._session_items_lock:
                items = self._session_items.copy()
            for widget, counts in ((self._hud_kills_list, self._session_kills),
                                   (self._hud_items_list, items)):
                ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
                self._set_hud_list(widget, ((name, f"{name} x{count}") for name, count in ordered))
        elif active in ("Mobs W/B", "NPCs"):
            self._refresh_hud_entities(now, active)

    def _refresh_hud_entities(self, now, active):
        world = getattr(self.feed, "world", None)
        if not world:
            return
        with world.lock:
            pos = world.player.cell(now) if world.player else None
            if pos and None in pos:
                pos = None
            current_map = world.map
            entities = [(kind, aid, ent.catalog_id, ent.track.target,
                         world.npc_name(aid, ent.catalog_id, ent.track.target, current_map) if kind == "npc" else None)
                        for (kind, aid), ent in world.entities.items() if kind in ("mob", "npc", "warp")]
        mobs, nearby = [], []
        for kind, aid, cid, (x, y), name in entities:
            distance = math.hypot(x - pos[0], y - pos[1]) if pos else math.inf
            if kind == "mob":
                name = self.names.get("mob", {}).get(cid, f"Mob #{cid}")
                mobs.append((distance, (cid, f"{name} (#{cid})")))
            elif pos and max(abs(x - pos[0]), abs(y - pos[1])) <= 30:
                if kind == "npc" and cid in (111, 139, 32767):
                    continue
                color = npc_colors(cid, name)[0] if kind == "npc" else "#22d3ee"
                name = name or (f"NPC #{aid}" if kind == "npc" else f"Portal #{aid}")
                nearby.append((distance, ((kind, aid), f"{name} · {distance:.1f} células · ({x}, {y})",
                                          current_map, color)))
        if active == "Mobs W/B":
            ordered = sorted(mobs, key=lambda item: item[0])
            self._set_hud_list(self._mobs_listbox, dict.fromkeys(row for _, row in ordered))
        else:
            nearby.sort(key=lambda item: item[0])
            rows = tuple(entry for _, entry in nearby)
            if rows != getattr(self._npc_listbox, "_hud_rows", ()):
                self._set_hud_list(self._npc_listbox, rows)
                for index, entry in enumerate(rows):
                    self._npc_listbox.itemconfigure(index, fg=entry[3])

    def _hud_interact(self):
        indices = self._npc_listbox.curselection()
        if not indices:
            return
        self._pending_hud_interaction = self._npc_listbox._hud_rows[indices[0]]
        self._close_hud_drawer()

    def _run_hud_interaction(self, selection):
        (kind, aid), _, selected_map, _ = selection
        world = getattr(self.feed, "world", None)
        if not world:
            return
        now_i = time.monotonic()
        with world.lock:
            target = world.entities.get((kind, aid))
            if not target or world.map != selected_map or not world.player:
                return
            tx, ty = map(int, target.track.target)
            px_i, py_i = world.player.cell(now_i)
            dialog_open = world.npc_dialog.get("open")

        if px_i is None or py_i is None:
            return
        if dialog_open:
            self._npc_click_state.pop((aid, tx, ty), None)
            self.nav_goal = None
            return
        dist_i = math.hypot(px_i - tx, py_i - ty)
        _npc_key = (aid, tx, ty)
        _npc_st = self._npc_click_state.get(_npc_key, {"clicks": 0, "last_click_ts": 0.0})

        if dist_i > 3.8:
            self._clear_patrol_route()
            self.manual_nav_goal = None
            if kind == "warp":
                self.manual_nav_goal = (selected_map, tx, ty)
                self.nav_goal = self.manual_nav_goal
            self.patrol_target_route = (selected_map, tx, ty, aid)
            self.active_nav_color = "#A78BFA"
            return

        terrain = getattr(self.feed, "terrain", None)
        gat_i = terrain.for_map(selected_map) if terrain else None
        # Mantém o limite de tentativas e o intervalo mínimo da interação direta.
        if _npc_st["clicks"] >= 2:
            # 2 tentativas sem abrir diálogo → desiste
            self._click_debug(f"INTERAGIR: 2 tentativas sem diálogo para #{aid}. Encerrando.")
            self._npc_click_state.pop(_npc_key, None)
            self.nav_goal = None
            return

        if now_i - _npc_st["last_click_ts"] < 0.950:
            return  # aguarda intervalo mínimo

        # Projeta e clica no sprite
        snap_i = self.feed.current()
        if snap_i:
            vp_i = snap_i.viewport
            left_i, top_i, w_i, h_i = vp_i
            sprite_z_i = 5.5 if kind == "npc" else 1.2
            h_fn_i = getattr(gat_i, "get_elevation", None) if gat_i else None
            cam_i = getattr(self.feed, "_cam", None) or (px_i, py_i)
            proj_i = self.feed.camera.projector(vp_i, cam_i, h_fn_i)
            u_i, v_i = proj_i(tx, ty, sprite_z_i)
            sx_i = left_i + int(u_i * w_i)
            sy_i = top_i + int(v_i * h_i)
            g_hwnd_i = self.get_game_hwnd(None)
            if g_hwnd_i:
                u32.SetForegroundWindow(g_hwnd_i)
            if self._is_point_in_game_ui(sx_i, sy_i):
                self._side_step_to_reveal((tx, ty), px_i, py_i)
            else:
                self._click_debug(f"INTERAGIR: clique #{_npc_st['clicks']+1} a {dist_i:.1f} células em ({sx_i},{sy_i})")
                if not self.patrol_move_and_click(sx_i, sy_i, hwnd=g_hwnd_i):
                    return
                self._hud_npc_line_target = (f"{kind}:{aid}", selected_map, now_i + 0.950)
                _npc_st["clicks"] += 1
                _npc_st["last_click_ts"] = now_i
                self._npc_click_state[_npc_key] = _npc_st

    def _handle_city_travel(self, snapshot, px, py):
        """Gerencia viagem autônoma para outra cidade via portais a pé ou teleporte Kafra via teclado."""
        if not self._city_travel_state or px is None or py is None:
            return

        now = time.monotonic()
        curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
        curr_map = curr_map.lower().removesuffix(".gat").removesuffix(".rsw")
        mode = self._city_travel_state.get("mode")
        target_city = self._city_travel_state.get("target_city", "").lower()
        if target_city == "juno":
            target_city = "yuno"

        # Chegamos ao destino?
        if curr_map and matches_city(curr_map, target_city):
            self._click_debug(f"CIDADE: Viagem concluída! Chegamos a {target_city}.")
            self._city_travel_state = None
            self.nav_goal = None
            self.patrol_target_route = None
            return

        # ── Modo Caminhada por Portais (distância <= 2 mapas) ──
        if mode == "walk":
            route = self._city_travel_state.get("route", [])
            step_idx = self._city_travel_state.get("step_idx", 0)
            if step_idx < len(route):
                m_from, m_to, port_x, port_y = route[step_idx]
                if curr_map == m_to:
                    self._city_travel_state["step_idx"] = step_idx + 1
                    if step_idx + 1 >= len(route):
                        self._click_debug(f"CIDADE: Viagem a pé concluída! Chegamos a {target_city}.")
                        self._city_travel_state = None
                        self.patrol_target_route = None
                        return
                    m_from, m_to, port_x, port_y = route[step_idx + 1]

                self.nav_goal = (m_from, port_x, port_y)
                self.patrol_target_route = (m_from, port_x, port_y, "warp")

        # ── Modo Kafra (Teleporte adaptativo via hardware keys e máquina de estados) ──
        elif mode == "kafra":
            vp_obj = getattr(self.feed, "viewport", None)
            target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
            target_hwnd = self.get_game_hwnd(target_hwnd)

            dialog = getattr(getattr(self.feed, "world", None), "npc_dialog", {})
            is_dialog_open = bool(dialog.get("open"))
            dialog_seq = dialog.get("seq", 0)
            dialog_last_ts = dialog.get("last_ts", 0.0)
            dialog_type = dialog.get("type", "closed")
            options = dialog.get("options", [])

            # ── 1. Se o diálogo estiver fechado ──
            if not is_dialog_open:
                # Trava anti-reclick: menos de 1.4s desde o último clique na Kafra -> não anda nem clica
                if (now - self.last_kafra_click_ts) < 1.4:
                    return

                # Detecta Kafra na tela primeiro
                kafra_obj = None
                kafra_cell = None
                for item in snapshot.objects:
                    if item.kind == "npc":
                        if npc_colors(item.catalog_id, item.name)[0] == "#f59e0b":
                            kafra_obj = item
                            kafra_cell = self.feed.world.get_entity_cell("npc", item.key) if hasattr(self.feed, "world") else None
                            break

                if kafra_cell:
                    kx, ky = kafra_cell
                else:
                    kafra_pos = self._city_travel_state.get("kafra_pos", (150, 150))
                    kx, ky = kafra_pos

                dist_kafra = math.hypot(px - kx, py - ky)
                left, top, width, height = snapshot.viewport

                if dist_kafra <= 3.5:
                    self.nav_goal = None
                    if kafra_obj:
                        sx = left + int(kafra_obj.u * width)
                        sy = top + int(kafra_obj.v * height)
                    else:
                        gat = self.feed.terrain.for_map(curr_map) if getattr(self.feed, "terrain", None) else None
                        h_fn = getattr(gat, "get_elevation", None) if gat else None
                        proj = self.feed.camera.projector(snapshot.viewport, self.feed._cam or (px, py), h_fn)
                        u, v = proj(kx, ky, 5.5)
                        sx = left + int(u * width)
                        sy = top + int(v * height)

                    # Só volta a clicar se o diálogo estiver fechado há mais de 1.8s após o clique anterior
                    if (now - self.last_kafra_click_ts) >= 1.8:
                        self.last_kafra_click_ts = now
                        self._city_travel_state["last_action_time"] = now
                        self._click_debug(f"[ClickDebug] KAFRA: clicando Kafra em ({sx}, {sy}) (dist={dist_kafra:.1f})")
                        self.patrol_move_and_click(sx, sy, hwnd=target_hwnd)
                else:
                    # Longe (> 3.5 células): aproxima da Kafra via nav_goal sem ligar auto_patrol
                    self.nav_goal = (curr_map, int(kx), int(ky))
                    self.active_nav_color = "#A78BFA"

            # ── 2. Diálogo aberto: Máquina de estados adaptativa ──
            else:
                now_perf = time.perf_counter()
                elapsed_dialog = now_perf - dialog_last_ts
                is_new_seq = (dialog_seq != self.handled_dialog_seq)
                is_timeout = (now - self.last_dialog_action_ts >= 1.8)

                if elapsed_dialog >= 0.22 and (is_new_seq or is_timeout):
                    self.handled_dialog_seq = dialog_seq
                    self.last_dialog_action_ts = now

                    if dialog_type == "wait":
                        self._click_debug(f"[ClickDebug] KAFRA: diálogo 'wait' (seq={dialog_seq}), enviando VK_RETURN")
                        self._send_game_key(0x0D)
                    elif dialog_type in ("menu", "warplist"):
                        norm_options = [normalize_dialog_option(opt) for opt in options]

                        # Caso B: Menu de Destino de Teleporte (match com target_city)
                        dest_idx = None
                        for i, (orig_opt, norm_opt) in enumerate(zip(options, norm_options)):
                            if matches_city(orig_opt, target_city) or matches_city(norm_opt, target_city):
                                dest_idx = i
                                break

                        if dest_idx is not None:
                            self._click_debug(f"[ClickDebug] KAFRA: Destino direto encontrado idx={dest_idx} ({options[dest_idx]}) para {target_city}")
                            self._send_menu_selection(dest_idx)
                            return

                        # Caso A: Menu Principal da Kafra (procura "Teleporte")
                        teleport_idx = None
                        for i, norm_opt in enumerate(norm_options):
                            if any(w in norm_opt for w in ("teleporte", "teleport", "servico de teleporte", "warp")):
                                teleport_idx = i
                                break

                        if teleport_idx is not None:
                            self._click_debug(f"[ClickDebug] KAFRA: Menu Teleporte encontrado idx={teleport_idx} ({options[teleport_idx]})")
                            self._send_menu_selection(teleport_idx)
                            return

                        # Roteamento multi-hop: se target_city não estiver nas opções, procura cidade ponte
                        bridge_candidates = ["al de baran", "geffen", "izlude", "prontera", "yuno", "rachel", "morocc", "payon", "alberta"]
                        bridge_idx = None
                        for bridge_city in bridge_candidates:
                            for i, (orig_opt, norm_opt) in enumerate(zip(options, norm_options)):
                                if matches_city(orig_opt, bridge_city) or matches_city(norm_opt, bridge_city):
                                    bridge_idx = i
                                    self._click_debug(f"[ClickDebug] KAFRA: Multi-hop selecionando cidade ponte idx={i} ({orig_opt}) rumo a {target_city}")
                                    break
                            if bridge_idx is not None:
                                break

                        if bridge_idx is not None:
                            self._send_menu_selection(bridge_idx)
                            return

                        # Caso C: Diálogo acidental / menu desconhecido -> Cancelar ou Esc
                        cancel_idx = None
                        for i, norm_opt in enumerate(norm_options):
                            if any(w in norm_opt for w in ("cancelar", "cancel", "sair", "fechar")):
                                cancel_idx = i
                                break

                        if cancel_idx is not None:
                            self._click_debug(f"[ClickDebug] KAFRA: Opção de cancelamento idx={cancel_idx} ({options[cancel_idx]})")
                            self._send_menu_selection(cancel_idx)
                        else:
                            self._click_debug("[ClickDebug] KAFRA: Menu desconhecido sem opção reconhecida, enviando VK_ESCAPE")
                            self._send_game_key(0x1B)


    def _capture_hud_key(self, keyboard, released):
        field = self._hud_edit
        foreground = u32.GetForegroundWindow()
        key = keyboard.vkCode
        if (field is None or not foreground or foreground != self._hud_edit_hwnd
                or keyboard.flags & 0x10 or key in (0x09, 0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C)
                or 0x70 <= key <= 0x87 or 0xA0 <= key <= 0xA5):
            return False
        ctrl = bool(u32.GetAsyncKeyState(0x11) & 0x8000)
        alt = bool(keyboard.flags & 0x20)
        if alt and not ctrl:
            return False
        if released:
            return True
        text = ""
        if key not in (0x08, 0x0D, 0x1B, 0x23, 0x24, 0x25, 0x27, 0x2E) and (not ctrl or alt):
            state = (ctypes.c_ubyte * 256)()
            for modifier in (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
                if u32.GetAsyncKeyState(modifier) & 0x8000:
                    state[modifier] = 0x80
            state[0x14] = u32.GetKeyState(0x14) & 1
            state[0x90] = u32.GetKeyState(0x90) & 1
            state[key] |= 0x80
            layout = u32.GetKeyboardLayout(u32.GetWindowThreadProcessId(foreground, None))
            buffer = ctypes.create_unicode_buffer(8)
            count = u32.ToUnicodeEx(key, keyboard.scanCode, state, buffer, len(buffer), 4, layout)
            if count < 0:
                self._hud_dead_key = buffer[0]
                return True
            if count > 0:
                text = buffer[:count]
                if accent := self._hud_dead_key:
                    import unicodedata

                    mark = {"´": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
                            "¨": "\u0308", "¸": "\u0327"}.get(accent)
                    text = (unicodedata.normalize("NFC", text[0] + mark) + text[1:]
                            if mark and text[0].isalpha() else accent + text.lstrip(" "))
                    self._hud_dead_key = ""
        # O hook Win32 pode rodar durante uma chamada nativa do Tk.
        self._hud_edit_keys.append((field, key, ctrl and not alt, text))
        return True

    def _install_navi_hook(self):
        """Instala WH_KEYBOARD_LL passivo para capturar /navi digitado no jogo."""
        try:
            WH_KEYBOARD_LL = 13
            WM_KEYDOWN = 0x0100
            VK_RETURN = 0x0D
            VK_BACK = 0x08

            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            def _low_level_keyboard_proc(nCode, wParam, lParam):
                try:
                    if nCode >= 0 and self._hud_edit is not None and wParam in (0x100, 0x101, 0x104, 0x105):
                        kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        if self._capture_hud_key(kbd, wParam in (0x101, 0x105)):
                            return 1
                    if nCode >= 0 and wParam == WM_KEYDOWN:
                        kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = kbd.vkCode
                        # Só acumula se Ragexe.exe estiver em foco
                        fg = u32.GetForegroundWindow()
                        if fg:
                            pid_buf = wintypes.DWORD(0)
                            u32.GetWindowThreadProcessId(fg, ctypes.byref(pid_buf))
                            if pid_buf.value == getattr(self, "_ragexe_pid", 0) or True:
                                ctrl_down = bool(u32.GetAsyncKeyState(0x11) & 0x8000)
                                if ctrl_down and vk in (0xDE, 0xC0, ord('\'')):
                                    self._world_map_open = not getattr(self, "_world_map_open", False)
                                    if self._world_map_open:
                                        self._click_debug("MAPA MUNDI: Aberto no cliente (Ctrl + ')")
                                    else:
                                        self._click_debug("MAPA MUNDI: Fechado no cliente")
                                        self._close_world_map_popup()
                                elif vk == 0x1B:  # VK_ESCAPE
                                    if getattr(self, "_world_map_open", False):
                                        self._world_map_open = False
                                        self._close_world_map_popup()
                                elif vk == VK_RETURN:
                                    buf = getattr(self, "_navi_buffer", "")
                                    if buf.lower().startswith("/navi") or buf.lower().startswith("navi "):
                                        curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
                                        result = parse_navi_command(buf, curr_map)
                                        if result:
                                            self.nav_goal = (result["map"], result["x"], result["y"])
                                            self._click_debug(f"[Navi] Rota capturada do jogo: {result['map']} ({result['x']},{result['y']})")
                                    self._navi_buffer = ""
                                elif vk == VK_BACK:
                                    self._navi_buffer = getattr(self, "_navi_buffer", "")[:-1]
                                elif 0x20 <= vk <= 0x7E:
                                    char = chr(vk).lower()
                                    self._navi_buffer = getattr(self, "_navi_buffer", "") + char
                                    if len(self._navi_buffer) > 60:
                                        self._navi_buffer = self._navi_buffer[-60:]
                except Exception:
                    pass
                return u32.CallNextHookEx(getattr(self, "_navi_hook_handle", None), nCode, wParam, lParam)

            hook_proc = HOOKPROC(_low_level_keyboard_proc)
            self._navi_hook_proc = hook_proc  # mantém referência viva
            handle = u32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, kernel32.GetModuleHandleW(None), 0)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._navi_hook_handle = handle
        except Exception as exc:
            print(f"[Navi] Falha ao instalar hook de teclado: {exc}", flush=True)

    def close(self):
        """Libera conexões seriais e contextos de driver HID."""
        if self._hud_update_after is not None:
            try:
                self.root.after_cancel(self._hud_update_after)
            except tk.TclError:
                pass
            self._hud_update_after = None
        self._stop_hud_tray()
        self._close_world_map_popup()
        for hwnd in self._hud_subclass_windows:
            if u32.IsWindow(hwnd):
                comctl32.RemoveWindowSubclass(hwnd, self._hud_subclass_proc, 1)
        self._hud_subclass_windows.clear()
        if hasattr(self, "_navi_hook_handle") and self._navi_hook_handle:
            try:
                u32.UnhookWindowsHookEx(self._navi_hook_handle)
            except Exception:
                pass
            self._navi_hook_handle = None
        camera = getattr(self.feed, "camera", None)
        if camera:
            camera.on_physical_click = None
        if hasattr(self, "serial_hid") and self.serial_hid:
            self.serial_hid.close()
        if hasattr(self, "interception") and self.interception:
            self.interception.close()

    def _skip_hud_tier3(self, weight_pct):
        return weight_pct >= self.tier3_max_weight_pct or (
            self._ignore_tier3_over_60.get() and weight_pct > 60
        )

    def classify_item_tier(self, nameid: int, item_name: str = "") -> int:
        """Classifica cada drop dinamicamente em 3 Tiers (Tier 1 Crítico, Tier 2 Neutro, Tier 3 Lixo)."""
        norm_name = str(item_name or "").strip().lower()
        id_str = str(nameid)
        id_hash = f"#{nameid}"

        # 1. Match em tier1_items
        for t1 in getattr(self, "tier1_items", []):
            t1_low = str(t1).strip().lower()
            if t1_low and (t1_low == norm_name or t1_low in norm_name or t1_low == id_str or t1_low == id_hash):
                return 1

        # 2. Nome contendo "carta" / "card" (case-insensitive)
        if "carta" in norm_name or "card" in norm_name:
            return 1

        # 3. nameid na faixa de cartas (4001 <= nameid <= 4700 ou 27000 <= nameid <= 31999)
        if (4001 <= nameid <= 4700) or (27000 <= nameid <= 31999):
            return 1

        # 4. Equipamentos conhecidos
        cat = load_item_catalog()
        if (nameid in cat.get("equips", set())) or (1100 <= nameid <= 2999) or (5001 <= nameid <= 5999) or (13000 <= nameid <= 19999):
            return 1

        # 5. Tier 3: match em tier3_items
        for t3 in getattr(self, "tier3_items", []):
            t3_low = str(t3).strip().lower()
            if t3_low and (t3_low == norm_name or t3_low in norm_name or t3_low == id_str or t3_low == id_hash):
                return 3

        # Fallback de itens conhecidos do rathena_terrain
        base = get_item_tier(nameid, norm_name)
        if base == 3:
            return 3

        # Tier 2 (Neutro)
        return 2

    def _save_kafra_field(self, key, field):
        text = field.get().strip()
        if key == "keep_in_bag":
            value = list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
            text = ", ".join(value)
        elif key == "kafra_pin":
            value = text if re.fullmatch(r"[0-9]*", text) else str(self.filters_config[key])
            text = value
        else:
            try:
                value = min(100, max(1, int(text)))
            except ValueError:
                value = self.filters_config[key]
            text = str(value)
            self._kafra_capacity_latched = False
            self._kafra_block = None
        field.delete(0, tk.END)
        field.insert(0, text)
        self._save_hud_setting(key, value)

    def _request_kafra_storage(self):
        if self._kafra_cycle is None:
            self._kafra_manual = True
            self._close_hud_drawer()
            if hwnd := self.get_game_hwnd():
                win32_input.activate_game_window(hwnd)

    def _start_kafra_storage(self, world, player, current_map, now, manual):
        cfg = self.filters_config
        if not manual and not cfg["auto_store_kafra"]:
            return
        weight_pct = 100 * player.weight_ratio if player else 0
        with world.lock:
            slots_limit = world.estimated_slots_used >= cfg["kafra_trigger_slots"]
            pickup_blocked = world.inventory_pickup_blocked
            zeny = world.player_zeny if world.zeny_known else None
            busy = world.npc_dialog.get("open") or world.storage_capacity is not None
        over_limit = slots_limit or weight_pct >= cfg["kafra_trigger_weight_pct"] or pickup_blocked
        if not over_limit:
            self._kafra_capacity_latched = False
            self._kafra_block = None
        if not manual and (not over_limit or self._kafra_capacity_latched):
            return
        self._kafra_manual = False
        if not current_map or not player or self.travel_plan or self._city_travel_state:
            if manual:
                self._click_debug("KAFRA: aguarde a posição do jogador e o fim da viagem atual.")
            return
        if busy:
            if manual:
                self._click_debug("KAFRA: feche o diálogo ou armazém atual antes de iniciar.")
            return
        if zeny is not None and zeny < 50:
            blocker = "saldo insuficiente para abrir o armazém (mínimo de 50 Zeny)."
            if manual or self._kafra_block != blocker:
                print(f"[Kafra] {blocker}", flush=True)
            self._kafra_block = blocker
            return

        self._kafra_capacity_latched = over_limit
        self._kafra_block = None
        try:
            vision = KafraVision()
        except (ImportError, OSError, ValueError) as exc:
            print(f"[Kafra] Captura visual indisponível ({exc}).", flush=True)
            return
        cities = {entry.get("map", key): tuple(entry["kafra_pos"])
                  for key, entry in {**CITY_KAFRA, **self.world_routes_data.get("cities", {})}.items()
                  if entry.get("kafra_pos")}
        if current_map in cities:
            city = current_map
        else:
            routes = [(sum(1.5 if step["type"] == "kafra" else 1 for step in route), key)
                      for key in cities if (route := calculate_travel_route(current_map, key))]
            if not routes:
                print(f"[Kafra] Nenhuma cidade com armazém acessível de {current_map}.", flush=True)
                return
            _, city = min(routes)

        self._kafra_cycle = {
            "phase": "travel" if city != current_map else "approach",
            "city": city, "position": cities[city],
            "farm_map": (self.farm_map or current_map).lower().removesuffix(".gat").removesuffix(".rsw"),
            "modes": (self.auto_farm, self.auto_loot, self.auto_patrol),
            "play": self._automation_running, "deadline": now + 180, "next": now,
            "vision": vision, "tabs": [2, 0, 1], "tab": None, "hover": None,
            "pending": None, "opened_inventory": False, "pin_entered": False,
            "keep": tuple(filter(None, map(normalize_dialog_option, cfg["keep_in_bag"]))),
            "restock": {normalize_dialog_option(name): count for name, count in cfg["restock_items"].items()
                        if type(count) is int and count > 0 and normalize_dialog_option(name)},
        }
        self.auto_farm = self.auto_loot = self.auto_patrol = False
        self._pending_hud_interaction = None
        self._clear_recovery_navigation()
        self.nav_goal = self.patrol_target_route = None
        self._set_hud_running(True)
        if zeny is None:
            print("[Kafra] Saldo ainda não capturado; seguindo para o armazém...", flush=True)
        if city != current_map:
            self.start_travel_plan(city)
        print(f"[Kafra] Ciclo iniciado em direção a {city}.", flush=True)

    def _finish_kafra_storage(self, message, current_map):
        cycle = self._kafra_cycle
        self._kafra_cycle = None
        self.travel_plan = self.nav_goal = self.patrol_target_route = None
        self._clear_recovery_navigation()
        self.farm_map = cycle["farm_map"]
        if current_map == self.farm_map:
            self.auto_farm, self.auto_loot, self.auto_patrol = cycle["modes"]
        if not cycle["play"]:
            self._set_hud_running(False)
        print(f"[Kafra] {message}", flush=True)

    def _tick_kafra_storage(self, snapshot, world, current_map, px, py, now):
        cycle = self._kafra_cycle
        phase = cycle["phase"]
        if now >= cycle["deadline"]:
            if phase in ("dialog", "opening", "store", "closing"):
                self._send_game_key(0x1B)
            self._finish_kafra_storage(f"tempo limite excedido na etapa {phase}.", current_map)
            return
        if px is None or py is None or now < cycle["next"]:
            return
        destination = cycle["farm_map"] if phase == "return" else cycle["city"]
        if phase in ("travel", "return"):
            if self.travel_plan:
                self._handle_travel_plan(snapshot, px, py)
                if self.nav_goal or self.patrol_target_route:
                    self._try_auto_patrol(snapshot, px, py)
            if current_map == destination:
                if phase == "return":
                    self._finish_kafra_storage("ciclo concluído.", current_map)
                else:
                    cycle.update(phase="approach", deadline=now + 60)
            elif not self.travel_plan:
                self._finish_kafra_storage(f"rota interrompida antes de chegar a {destination}.", current_map)
            return
        if current_map != destination:
            self._finish_kafra_storage("mapa mudou durante a interação.", current_map)
            return
        hwnd = self.get_game_hwnd()
        if not hwnd or u32.GetForegroundWindow() != hwnd:
            return
        with world.lock:
            capacity = world.storage_capacity
            dialog = dict(world.npc_dialog)
            inventory = dict(world.inventory)
            storage = dict(world.storage_items)
            inventory_ready = world.inventory_ready
            zeny = world.player_zeny if world.zeny_known else None
            refused = False
            if (phase in ("dialog", "opening") and dialog.get("text_npc_gid") == cycle["npc"]
                    and dialog.get("text_ts", 0) >= cycle["clicked_at"]):
                dialog_text = " ".join(dialog.get("text", "").casefold().split())
                refused = "saldo suficiente" in dialog_text or "custa 50" in dialog_text
                if refused:
                    world._set_player_zeny(0)
        try:
            if refused:
                raise ValueError("saldo insuficiente indicado no diálogo da Kafra. Operação abortada.")
            if phase == "approach":
                self._approach_kafra_storage(snapshot, world, hwnd, px, py, now, dialog, zeny)
            elif phase in ("dialog", "opening"):
                self._open_kafra_storage(snapshot, hwnd, now, phase, dialog, capacity)
            elif phase == "store":
                if not inventory_ready:
                    raise ValueError("a lista TCP completa do inventário está indisponível. Reconecte com a captura ativa para transferir automaticamente.")
                if capacity is None:
                    raise ValueError("armazém fechado durante a transferência.")
                if capacity[0] >= capacity[1]:
                    raise ValueError(f"armazém cheio ({capacity[0]}/{capacity[1]}).")
                self._store_kafra_items(snapshot, hwnd, now, inventory, storage)
            elif phase == "closing":
                if capacity is not None:
                    return
                cycle.update(phase="return", deadline=now + 180)
                self.start_travel_plan(cycle["farm_map"], mode="farm")
        except (OSError, ValueError) as exc:
            if phase in ("dialog", "opening", "store"):
                self._send_game_key(0x1B)
            self._finish_kafra_storage(str(exc), current_map)

    def _approach_kafra_storage(self, snapshot, world, hwnd, px, py, now, dialog, zeny):
        cycle = self._kafra_cycle
        target = cycle["position"]
        candidates = []
        for npc in snapshot.objects:
            if npc.kind == "npc" and npc_colors(npc.catalog_id, npc.name)[0] == "#f59e0b":
                if cell := world.get_entity_cell("npc", npc.key):
                    candidates.append((math.dist((px, py), cell), npc, cell))
        nearest = min(candidates, default=None, key=lambda row: row[0])
        if nearest:
            distance, npc, target = nearest
            if distance <= 3.5:
                if zeny is not None and zeny < 50:
                    raise ValueError("saldo insuficiente ao chegar à Kafra.")
                if dialog.get("open"):
                    raise ValueError("já existe um diálogo aberto.")
                self.nav_goal = self.patrol_target_route = None
                self._clear_patrol_route()
                left, top, width, height = snapshot.viewport
                if not self.patrol_move_and_click(left + npc.u * width, top + npc.v * height, hwnd=hwnd):
                    return
                cycle.update(phase="dialog", npc=int(npc.key.split(":")[1]),
                             seq=dialog.get("seq"), clicked_at=now, deadline=now + 12, next=now + 0.25)
                return
        if math.dist((px, py), target) > 2:
            self.nav_goal = (snapshot.map, *map(int, target))
            self.active_nav_color = "#f59e0b"
            self._try_auto_patrol(snapshot, px, py)

    def _open_kafra_storage(self, snapshot, hwnd, now, phase, dialog, capacity):
        cycle = self._kafra_cycle
        if phase == "opening":
            if capacity is not None:
                cycle.update(phase="store", deadline=now + 180, next=now + 0.25)
                return
            pin = str(self.filters_config["kafra_pin"])
            if pin and not cycle["pin_entered"]:
                vision = cycle["vision"]
                if points := vision.pin_points(vision.capture(snapshot.viewport), pin):
                    for point in points:
                        self._kafra_ui_click(snapshot, hwnd, point)
                    cycle.update(pin_entered=True, next=now + 0.35)
                    return
            cycle["next"] = now + 0.18
        if not dialog.get("open") or dialog.get("seq") == cycle["seq"]:
            return
        if dialog.get("npc_gid") != cycle["npc"]:
            raise ValueError("diálogo recebido de outro NPC.")
        if time.perf_counter() - dialog.get("last_ts", 0) < 0.22:
            return
        cycle["seq"] = dialog.get("seq")
        if dialog.get("type") == "wait":
            self._send_game_key(0x0D)
        elif dialog.get("type") == "menu" and phase == "dialog":
            for index, option in enumerate(dialog.get("options", ())):
                if "usar o armazem" in normalize_dialog_option(option):
                    self._send_menu_selection(index)
                    cycle.update(phase="opening", deadline=now + 35, next=now + 0.5)
                    return
            raise ValueError("a Kafra não ofereceu a opção Usar o Armazém.")

    def _kafra_screen_point(self, snapshot, hwnd, point):
        left, top, width, height = snapshot.viewport
        x, y = point
        if (u32.GetForegroundWindow() != hwnd or not (0 <= x < width and 0 <= y < height)
                or self._is_point_in_hud(left + x, top + y, margin=2)):
            raise ValueError("controle da Kafra fora da área clicável do jogo.")
        return left + x, top + y

    def _kafra_ui_click(self, snapshot, hwnd, point):
        # O controle já foi identificado na janela do jogo, sobre a interface nativa.
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        ensure_input_desktop()
        self._move_and_click_sync(x, y, hwnd)

    def _kafra_hover(self, snapshot, hwnd, point):
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        ensure_input_desktop()
        self._smooth_move_to(x, y)
        cursor = wintypes.POINT()
        if not u32.GetCursorPos(ctypes.byref(cursor)) or max(abs(cursor.x - x), abs(cursor.y - y)) > 4:
            raise ValueError("cursor não alcançou o slot indicado.")

    def _kafra_alt_right(self, snapshot, hwnd, point):
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        cursor = wintypes.POINT()
        if not u32.GetCursorPos(ctypes.byref(cursor)) or max(abs(cursor.x - x), abs(cursor.y - y)) > 4:
            raise ValueError("cursor saiu do slot antes da transferência.")
        self._kafra_alt_input()

    def _kafra_alt_input(self, key=None):
        alt = u32.MapVirtualKeyW(0x12, 0)  # VK_MENU
        if not alt or any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in (1, 2, 0x10, 0x11, 0x12)):
            raise ValueError("tecla modificadora ou botão do mouse já pressionado.")
        events = (INPUT * 4)()
        events[0].type = events[3].type = INPUT_KEYBOARD
        events[0].ki.wScan = events[3].ki.wScan = alt
        events[0].ki.dwFlags, events[3].ki.dwFlags = 0x0008, 0x000A
        if key is None:
            events[1].type = events[2].type = INPUT_MOUSE
            events[1].mi.dwFlags, events[2].mi.dwFlags = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        else:
            scan = u32.MapVirtualKeyW(key, 0)
            if not scan:
                raise ValueError("atalho do Inventário indisponível.")
            events[1].type = events[2].type = INPUT_KEYBOARD
            events[1].ki.wScan = events[2].ki.wScan = scan
            events[1].ki.dwFlags, events[2].ki.dwFlags = 0x0008, 0x000A
        size = ctypes.sizeof(INPUT)
        release = (INPUT * 2)(events[2], events[3])
        sent = u32.SendInput(2, events, size)
        released = 0
        try:
            if sent:
                time.sleep(0.040)
        finally:
            if sent:
                released = u32.SendInput(2, release, size)
                if released < 2:
                    released += sum(u32.SendInput(1, ctypes.byref(event), size) for event in release[released:])
        if sent != 2 or released != 2:
            raise ValueError("falha ao enviar o comando com Alt.")

    def _store_kafra_items(self, snapshot, hwnd, now, inventory, storage):
        cycle = self._kafra_cycle
        vision = cycle["vision"]
        if pending := cycle["pending"]:
            index, item, stored_before, expires = pending
            stored = sum(value[1] for value in storage.values() if value[0] == item[0])
            if index in inventory or stored < stored_before + item[1]:
                if now >= expires:
                    raise ValueError(f"o armazém não confirmou a transferência do item #{item[0]}.")
                return
            cycle["pending"] = None
            cycle["deadline"] = now + 180
        screen = vision.capture(snapshot.viewport)
        grid = vision.inventory_grid(screen)
        if grid is None:
            if cycle["opened_inventory"]:
                raise ValueError("cabeçalho ou bordas do Inventário não localizados.")
            self._kafra_alt_input(ord("E"))
            cycle.update(opened_inventory=True, next=now + 0.6)
            return
        x, y, right, bottom, columns, rows = grid
        if cycle["tab"] is None:
            if not cycle["tabs"]:
                if not self._restock_kafra_items(snapshot, hwnd, now, screen, grid, inventory, storage):
                    self._send_game_key(0x1B)
                    cycle.update(phase="closing", next=now + 0.8, deadline=now + 6)
                return
            tab = cycle["tabs"].pop(0)
            self._kafra_ui_click(snapshot, hwnd, (x + 17, y + 41 + 32 * tab))
            cycle.update(tab=tab, next=now + 0.25, hover=None, scroll=0, skipped=set(),
                         reset_scroll=True)
            return
        if cycle.pop("reset_scroll", False):
            # Cada clique na seta sobe uma linha. A quantidade TCP limita a rolagem.
            count = sum(item[2] == cycle["tab"] and not item[3] for item in inventory.values())
            up_clicks = max(0, (count + columns - 1) // columns - rows)
            if up_clicks:
                self._kafra_ui_click(snapshot, hwnd, (right - 10, y + 30))
                cycle.update(scroll_reset_left=up_clicks - 1, next=now + 0.1)
                return
        if cycle.get("scroll_reset_left", 0):
            self._kafra_ui_click(snapshot, hwnd, (right - 10, y + 30))
            cycle["scroll_reset_left"] -= 1
            cycle["next"] = now + 0.1
            return
        tab = cycle["tab"]
        names = self.names.get("drop", {})
        items = [(index, item) for index, item in sorted(inventory.items()) if item[2] == tab and not item[3]]
        offset = cycle["scroll"] * columns
        keep = cycle["keep"] + tuple(cycle["restock"])
        if cycle["scroll"] and offset + columns * (rows - 1) >= len(items):
            self._kafra_ui_click(snapshot, hwnd, (right - 10, y + 30))
            cycle.update(scroll=cycle["scroll"] - 1, hover=None, next=now + 0.15)
            return
        if hovered := cycle["hover"]:
            index, item, point = hovered
            cycle["hover"] = None
            if item != inventory.get(index):
                return
            ordinal = next((position for position, (key, _) in enumerate(items) if key == index), -1) - offset
            if ordinal < 0 or point != (x + 60 + 32 * (ordinal % columns), y + 39 + 32 * (ordinal // columns)):
                return
            name = normalize_dialog_option(names.get(item[0], ""))
            tooltip = normalize_dialog_option(vision.tooltip(screen, point)) if tab != 2 or not name else ""
            if any(term in tooltip or term in name for term in keep):
                cycle["skipped"].add(index)
                return
            if not name and not tooltip:
                self._click_debug(f"KAFRA: item #{item[0]} mantido porque o nome não foi identificado.")
                cycle["skipped"].add(index)
                return
            if tooltip and name and name not in tooltip:
                self._click_debug(f"KAFRA: item #{item[0]} mantido por divergência entre TCP e tooltip.")
                cycle["skipped"].add(index)
                return
            stored = sum(value[1] for value in storage.values() if value[0] == item[0])
            self._kafra_alt_right(snapshot, hwnd, point)
            cycle.update(pending=(index, item, stored, time.monotonic() + 3), next=now + 0.18)
            return
        visible = items[offset:offset + columns * rows]
        for ordinal in range(len(visible) - 1, -1, -1):
            index, item = visible[ordinal]
            name = normalize_dialog_option(names.get(item[0], ""))
            if index in cycle["skipped"] or any(term in name for term in keep):
                continue
            point = x + 60 + 32 * (ordinal % columns), y + 39 + 32 * (ordinal // columns)
            if not vision.item_at(screen, point):
                raise ValueError("a grade visual do Inventário divergiu dos slots recebidos pelo TCP.")
            self._kafra_hover(snapshot, hwnd, point)
            cycle.update(hover=(index, item, point), next=time.monotonic() + 0.12)
            return
        if offset + len(visible) < len(items):
            self._kafra_ui_click(snapshot, hwnd, (right - 10, bottom - 27))
            cycle.update(scroll=cycle["scroll"] + 1, next=now + 0.15)
        else:
            cycle["tab"] = None

    def _restock_kafra_items(self, snapshot, hwnd, now, screen, inv_grid, inventory, storage):
        cycle = self._kafra_cycle
        targets = cycle["restock"]
        if not targets:
            return False
        vision = cycle["vision"]
        names = self.names.get("drop", {})

        def amount(items, name):
            return sum(item[1] for item in items.values() if normalize_dialog_option(names.get(item[0])) == name)

        if pending := cycle.get("restock_pending"):
            name, before, stored, delta, expires = pending
            in_bag, in_storage = amount(inventory, name), amount(storage, name)
            if in_bag == before + delta and in_storage == stored - delta:
                cycle.pop("restock_pending")
                cycle.pop("restock_grid", None)
            elif now >= expires:
                raise ValueError(f"reposição de {name} sem confirmação da quantidade esperada.")
            else:
                return True
        if quantity := cycle.get("restock_quantity"):
            name, before, stored, delta, expires = quantity
            box = vision.quantity_box(screen)
            if box is None:
                if now >= expires:
                    raise ValueError("janela de quantidade da reposição não identificada.")
                return True
            button, value_area = box
            if not cycle.get("restock_typed"):
                keys = (0x23, *(0x08 for _ in range(len(str(stored)) + 1)), *map(ord, str(delta)))
                if not all(self._send_game_key(key) for key in keys):
                    raise ValueError("falha ao preencher a quantidade da reposição.")
                cycle.update(restock_typed=True, next=time.monotonic() + 0.2)
                return True
            values = [word for word, *_ in vision._ocr_words(value_area) if word.isascii() and word.isdigit()]
            if values != [str(delta)]:
                raise ValueError("a quantidade digitada para a reposição não foi confirmada por OCR.")
            self._kafra_ui_click(snapshot, hwnd, button)
            cycle.pop("restock_quantity")
            cycle.pop("restock_typed")
            cycle.update(restock_pending=(name, before, stored, delta, now + 4), next=now + 0.25)
            return True

        target = next(((name, desired - amount(inventory, name)) for name, desired in targets.items()
                       if desired > amount(inventory, name) and amount(storage, name) > 0), None)
        if target is None:
            for name, desired in targets.items():
                if amount(inventory, name) < desired:
                    self._click_debug(f"KAFRA: saldo no armazém insuficiente para repor {name} até {desired}.")
            return False
        name, need = target
        grid = cycle.get("restock_grid") or vision.storage_grid(screen)
        if grid is None:
            self._click_debug("KAFRA: reposição ignorada. O cabeçalho do armazém não pôde ser identificado por OCR.")
            return False
        x, y, right, bottom, columns, rows = grid
        ix, iy, iright, ibottom, _, _ = inv_grid
        if not (right <= ix or iright <= x or bottom <= iy or ibottom <= y):
            self._click_debug("KAFRA: reposição ignorada. As janelas de inventário e armazém se sobrepõem.")
            return False
        if hover := cycle.pop("restock_hover", None):
            point, stack = hover
            tooltip = normalize_dialog_option(vision.tooltip(screen, point))
            if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", tooltip):
                self._click_debug(f"KAFRA: reposição ignorada. Tooltip de {name} não confirmado.")
                return False
            before, stored = amount(inventory, name), amount(storage, name)
            delta = min(stack, need)
            if delta == stack:
                self._kafra_alt_right(snapshot, hwnd, point)
                cycle.update(restock_pending=(name, before, stored, delta, now + 4), next=now + 0.25)
            else:
                self._kafra_drag_item(snapshot, hwnd, point, (ix + 60, iy + 39))
                cycle.update(restock_quantity=(name, before, stored, delta, now + 5), next=now + 0.35)
            return True
        tab = next(item[2] for item in storage.values()
                   if normalize_dialog_option(names.get(item[0])) == name)
        if tab > 2:
            self._click_debug(f"KAFRA: reposição ignorada. Aba de {name} não identificada.")
            return False
        if cycle.get("restock_grid") is None or cycle.get("restock_tab") != tab:
            self._kafra_ui_click(snapshot, hwnd, (x + 17, y + 41 + 32 * tab))
            self._kafra_ui_click(snapshot, hwnd, (ix + 17, iy + 41 + 32 * tab))
            cycle.update(restock_grid=grid, restock_tab=tab, restock_scroll=0, next=now + 0.25)
            return True
        entries = [item for _, item in sorted(storage.items()) if item[2] == tab]
        ordinal, item = next((index, item) for index, item in enumerate(entries)
                             if normalize_dialog_option(names.get(item[0])) == name)
        offset = columns * cycle["restock_scroll"]
        if not offset <= ordinal < offset + columns * rows:
            down = ordinal >= offset
            self._kafra_ui_click(snapshot, hwnd, (right - 10, bottom - 27 if down else y + 30))
            cycle["restock_scroll"] += 1 if down else -1
            cycle["next"] = now + 0.18
            return True
        position = ordinal - offset
        point = x + 60 + 32 * (position % columns), y + 39 + 32 * (position // columns)
        self._kafra_hover(snapshot, hwnd, point)
        cycle.update(restock_hover=(point, item[1]), next=time.monotonic() + 0.12)
        return True

    def _kafra_drag_item(self, snapshot, hwnd, source, target):
        self._kafra_hover(snapshot, hwnd, source)
        self._kafra_screen_point(snapshot, hwnd, target)
        if any(u32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 0x10, 0x11, 0x12)):
            raise ValueError("tecla modificadora ou botão do mouse já pressionado.")
        press, release = INPUT(type=INPUT_MOUSE), INPUT(type=INPUT_MOUSE)
        press.mi.dwFlags, release.mi.dwFlags = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        size = ctypes.sizeof(INPUT)
        if not u32.SendInput(1, ctypes.byref(press), size):
            raise ValueError("não foi possível iniciar a reposição por arraste.")
        try:
            self._kafra_hover(snapshot, hwnd, target)
        finally:
            u32.SendInput(1, ctypes.byref(release), size)

    def start_travel_plan(self, target_input, mode="travel"):
        """Inicia plano de viagem autônomo (Kafra + Portais) com modo 'travel' ou 'farm'."""
        curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
        curr_map = curr_map.lower().removesuffix(".gat").removesuffix(".rsw")
        if not curr_map:
            self._click_debug("TRAVEL: Mapa atual desconhecido.")
            return

        t_str = str(target_input).strip().lower().removesuffix(".gat").removesuffix(".rsw")
        routes_data = getattr(self, "world_routes_data", {})
        aliases = routes_data.get("aliases", {})
        target_map = aliases.get(t_str, t_str)
        cities = routes_data.get("cities", {})
        if target_map in cities and "map" in cities[target_map]:
            target_map = cities[target_map]["map"].lower()

        if curr_map == target_map:
            self._click_debug(f"TRAVEL: Já estamos no mapa de destino {target_map}!")
            if mode == "farm":
                self.farm_map = target_map
                self.auto_farm = True
                self.auto_loot = True
                self.auto_patrol = True
            return

        steps = calculate_travel_route(curr_map, target_map)
        if not steps:
            self._click_debug(f"TRAVEL: Nenhuma rota encontrada de {curr_map} para {target_map}.")
            return

        self._click_debug(f"TRAVEL: Iniciando viagem para {target_map} ({len(steps)} etapas, modo={mode})")
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self.travel_plan = {
            "target_map": target_map,
            "mode": mode,
            "steps": steps,
            "step_idx": 0,
            "last_action_ts": 0.0,
        }
        self.manual_nav_goal = None
        self.nav_goal = None
        self.patrol_target_route = None
        self._clear_patrol_route()
        game_hwnd = self.get_game_hwnd(None)
        if game_hwnd:
            u32.SetForegroundWindow(game_hwnd)

    def _handle_travel_plan(self, snapshot, px, py):
        """Executa máquina de estados do plano de viagem (portais A* e Kafra)."""
        if not self.travel_plan or px is None or py is None:
            return

        curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
        curr_map = curr_map.lower().removesuffix(".gat").removesuffix(".rsw")
        target_map = self.travel_plan.get("target_map")
        mode = self.travel_plan.get("mode", "travel")

        if curr_map and curr_map == target_map:
            self.travel_plan = None
            self._city_travel_state = None
            self.nav_goal = None
            self.patrol_target_route = None
            self._travel_allowed_portal = None
            self._clear_patrol_route()
            if mode == "farm":
                self.farm_map = target_map
                self.auto_farm = True
                self.auto_loot = True
                self.auto_patrol = True
                self._click_debug(f"TRAVEL_PLAN: Chegamos em {target_map}! Auto-Farm, Auto-Loot e Auto-Patrol ATIVADOS.")
            else:
                self._click_debug(f"TRAVEL_PLAN: Viagem para {target_map} concluída.")
            return

        steps = self.travel_plan.get("steps", [])
        idx = self.travel_plan.get("step_idx", 0)
        if idx >= len(steps):
            new_steps = calculate_travel_route(curr_map, target_map)
            if new_steps:
                self.travel_plan["steps"] = new_steps
                self.travel_plan["step_idx"] = 0
                return
            else:
                self._click_debug(f"TRAVEL_PLAN: Etapas esgotadas sem chegar a {target_map}.")
                self.travel_plan = None
                return

        step = steps[idx]
        stype = step.get("type")

        if stype == "portal":
            from_m = step["from_map"]
            to_m = step["to_map"]
            wx, wy = step["wx"], step["wy"]

            if curr_map == to_m:
                self.travel_plan["step_idx"] = idx + 1
                self.travel_plan["last_action_ts"] = 0.0
                self.nav_goal = None
                self.patrol_target_route = None
                self._travel_allowed_portal = None
                return

            if curr_map != from_m:
                new_steps = calculate_travel_route(curr_map, target_map)
                if new_steps:
                    self.travel_plan["steps"] = new_steps
                    self.travel_plan["step_idx"] = 0
                else:
                    self.travel_plan = None
                return

            if self.travel_plan.get("recovery"):
                now = time.monotonic()
                if now - self.travel_plan["last_action_ts"] >= 0.25:
                    self.travel_plan["last_action_ts"] = now
                    terrain = getattr(self.feed, "terrain", None)
                    gat = terrain.for_map(curr_map) if terrain else None
                    if gat:
                        start = (round(px), round(py))
                        blocked = self._get_portal_blocked_cells(radius=0, allow_current_goal=False)
                        goal = self._visible_portal_cell((wx, wy), blocked)
                        if gat.is_walkable(*goal):
                            path = gat.find_path(start, goal, blocked_cells=blocked - {goal})
                            if path and path[-1] == goal:
                                self._move_on_retreat_path(snapshot, gat, (px, py), path)
                return

            self._travel_allowed_portal = (wx, wy)
            self.nav_goal = (from_m, wx, wy)
            self.patrol_target_route = (from_m, wx, wy, "warp")
            self.active_nav_color = "#22D3EE"

        elif stype == "kafra":
            from_c = step["from_city"]
            to_c = step["to_city"]
            k_pos = step.get("kafra_pos", (150, 150))

            if curr_map == to_c:
                self.travel_plan["step_idx"] = idx + 1
                self._city_travel_state = None
                self.nav_goal = None
                return

            if not self._city_travel_state or self._city_travel_state.get("target_city") != to_c:
                self._city_travel_state = {
                    "mode": "kafra",
                    "target_city": to_c,
                    "kafra_pos": k_pos,
                    "state": "walking_to_kafra",
                    "last_action_time": 0.0,
                }
            self._handle_city_travel(snapshot, px, py)

    def _on_game_lbutton_down(self, screen_x, screen_y):
        """Detecta clique no Mapa Mundi e abre popup de viagem/farm."""
        if not getattr(self, "_world_map_open", False):
            return
        snap = self.feed.current()
        if not snap:
            return
        left, top, width, height = snap.viewport
        wm_x = left + int(width * 0.12)
        wm_y = top + int(height * 0.10)
        wm_w = int(width * 0.76)
        wm_h = int(height * 0.78)
        if wm_x <= screen_x <= (wm_x + wm_w) and wm_y <= screen_y <= (wm_y + wm_h):
            u = (screen_x - wm_x) / float(wm_w)
            v = (screen_y - wm_y) / float(wm_h)
            entry = self._get_world_map_entry(u, v)
            if entry:
                self.root.after(0, lambda: self._show_world_map_popup(entry, screen_x, screen_y))

    def _get_world_map_entry(self, u, v):
        routes_data = getattr(self, "world_routes_data", {})
        grid = routes_data.get("world_map_grid", [])
        if not grid:
            return None
        for entry in grid:
            if entry["u_min"] <= u <= entry["u_max"] and entry["v_min"] <= v <= entry["v_max"]:
                return entry
        best_e = None
        best_d = 999.0
        for entry in grid:
            cu = (entry["u_min"] + entry["u_max"]) / 2.0
            cv = (entry["v_min"] + entry["v_max"]) / 2.0
            d = math.hypot(u - cu, v - cv)
            if d < best_d:
                best_d = d
                best_e = entry
        return best_e

    def _show_world_map_popup(self, entry, sx, sy):
        self._close_world_map_popup()
        popup = tk.Toplevel(self.root)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True, "-toolwindow", True)
        popup.configure(bg="#0F172A", highlightthickness=1, highlightbackground="#3B82F6")

        px = max(10, min(sx + 12, self.root.winfo_screenwidth() - 240))
        py = max(10, min(sy + 12, self.root.winfo_screenheight() - 130))
        popup.geometry(f"+{px}+{py}")

        pad_f = tk.Frame(popup, bg="#0F172A", padx=8, pady=6)
        pad_f.pack()

        head_f = tk.Frame(pad_f, bg="#0F172A")
        head_f.pack(fill="x", pady=(0, 2))
        tk.Label(head_f, text=entry.get("name", entry.get("map", "")),
                 bg="#0F172A", fg="#F8FAFC", font=("Segoe UI", 8, "bold")).pack(side="left")
        btn_close = tk.Label(head_f, text="✕", bg="#0F172A", fg="#94A3B8", font=("Segoe UI", 8), cursor="hand2")
        btn_close.pack(side="right")
        btn_close.bind("<Button-1>", lambda _e: self._close_world_map_popup())

        mobs_str = entry.get("mobs", "")
        if mobs_str:
            tk.Label(pad_f, text=f"Monstros: {mobs_str}", bg="#0F172A", fg="#94A3B8",
                     font=("Segoe UI", 7), wraplength=210, justify="left").pack(anchor="w", pady=(0, 4))

        btns_f = tk.Frame(pad_f, bg="#0F172A")
        btns_f.pack(fill="x", pady=2)

        def _on_travel():
            self._close_world_map_popup()
            self._world_map_open = False
            self.start_travel_plan(entry["map"], mode="travel")

        def _on_farm():
            self._close_world_map_popup()
            self._world_map_open = False
            self.start_travel_plan(entry["map"], mode="farm")

        tk.Button(btns_f, text="Viajar", command=_on_travel,
                  bg="#2563EB", fg="#F8FAFC", font=("Segoe UI", 7, "bold"),
                  relief="flat", cursor="hand2").pack(side="left", padx=(0, 4))

        tk.Button(btns_f, text="Viajar & Farmar", command=_on_farm,
                  bg="#059669", fg="#F8FAFC", font=("Segoe UI", 7, "bold"),
                  relief="flat", cursor="hand2").pack(side="left")

        self._world_map_popup = popup
        self._show_hud_window(popup, px, py)

    def _close_world_map_popup(self):
        if getattr(self, "_world_map_popup", None):
            try:
                self._world_map_popup.destroy()
            except Exception:
                pass
            self._world_map_popup = None

    def refresh(self):
        try:
            self._refresh_frame()
        finally:
            self.window.after(16 if self._hud_render_foreground else 40, self.refresh)

    def _refresh_frame(self):
        self._drain_hud_edit_keys()
        if self._hud_tray_restore.is_set():
            self._hud_tray_restore.clear()
            self._restore_hud()

        viewport_provider = getattr(self.feed, "viewport", None)
        game_hwnd = getattr(viewport_provider, "handle", None) or self._game_window[0]
        game_window = self._remember_game_window(game_hwnd)
        fg = u32.GetForegroundWindow() or 0
        foreground = self._is_game_focused(fg, game_window=game_window)
        hud_focused = foreground or self._is_hud_foreground(fg)
        anchor = game_window[1] if hud_focused else None
        previous_anchor = self._hud_render_anchor
        now = time.monotonic()
        if self._session_active_since is not None:
            self._session_active_seconds += now - self._session_active_since
        self._session_active_since = None
        if (not hud_focused and self._hud_render_foreground and game_hwnd
                and u32.IsWindow(game_hwnd) and not u32.IsIconic(game_hwnd)):
            if self._hud_focus_lost_since is None:
                self._hud_focus_lost_since = now
            if now - self._hud_focus_lost_since < 0.12:
                self._hud_drawer_walk_since = None
                return
        self._hud_focus_lost_since = None
        self._hud_render_foreground, self._hud_render_anchor = hud_focused, anchor
        if anchor != previous_anchor:
            self._hud_restack_render = True
            if self._hud_edit_hwnd != anchor:
                self._finish_hud_edit()

        if not self._hud_render_foreground:
            self._finish_hud_edit()
            self._dismiss_hud_choice()
            self._close_world_map_popup()
            self._hud_pressed_keys = None
            self._hud_manual_clicks.clear()
            self._position_render_window(None)
            self._hud_snapshot_missing_since = None
            self._hud_drawer_walk_since = None
            self.current_viewport = None
            self.last = None
            self._sync_hud_windows(None, False)
            return

        snapshot = self.feed.current()
        stats = self.feed.stats() if hasattr(self.feed, "stats") else None
        hud_viewport = snapshot.viewport if snapshot else None
        if hud_viewport is None and callable(viewport_provider):
            hud_viewport = viewport_provider()
        if hud_viewport is None and self._hud_render_anchor:
            hwnd = self._hud_render_anchor
            bounds = wintypes.RECT()
            origin = wintypes.POINT()
            if (u32.GetClientRect(hwnd, ctypes.byref(bounds)) and bounds.right > 0 and bounds.bottom > 0
                    and u32.ClientToScreen(hwnd, ctypes.byref(origin))):
                hud_viewport = (origin.x, origin.y, bounds.right, bounds.bottom)
        if hud_viewport is None and anchor == previous_anchor:
            hud_viewport = self.current_viewport
        self.current_viewport = hud_viewport
        try:
            if snapshot is None:
                if self._hud_snapshot_missing_since is None:
                    self._hud_snapshot_missing_since = now
                if not self._overlay_visible or now - self._hud_snapshot_missing_since >= 0.12:
                    self._position_render_window(None)
                    self.last = None
            else:
                self._hud_snapshot_missing_since = None
                if self.draw(snapshot, stats):
                    self.last = snapshot
                    self.last_stats = stats
        finally:
            self._sync_hud_windows(hud_viewport, True, self._hud_render_anchor)
        self._poll_hud_input()

        world = getattr(self.feed, "world", None)
        if (self._hud_active_drawer and not self._hud_drawer_paused and self._hud_binding_action is None
                and self._hud_edit is None and snapshot is not None
                and getattr(world, "_is_player_walking", lambda: False)()):
            if self._hud_drawer_walk_since is None:
                self._hud_drawer_walk_since = now
            elif now - self._hud_drawer_walk_since >= 0.25:
                self._close_hud_drawer()
        else:
            self._hud_drawer_walk_since = None

        if snapshot is not None and foreground:
            self._action_click_used = False
            try:
                self._handle_actions(snapshot)
            finally:
                self._action_click_used = None
            if self._hud_can_run() and (self.auto_farm or self.auto_patrol or self._has_hud_travel()):
                self._session_active_since = now

    def _has_hud_travel(self):
        return bool(self.travel_plan or self._city_travel_state or self.nav_goal or self.patrol_target_route)

    @staticmethod
    def _is_mob_hostile(world, mob):
        if mob.catalog_id in world.learned_hostile_mobs or mob.is_aggro:
            return True
        entry = get_mob_catalog().get("by_id", {}).get(mob.catalog_id, {})
        return entry.get("hostile", False)

    @staticmethod
    def _is_active_threat(world, gid, mob, position, now):
        return (
            (gid == world.last_player_attacker_gid and now - world.last_player_aggro_ts < 2.0)
            or (mob.is_aggro and now - mob.last_aggro_ts < 2.0)
            or (mob.track.is_moving and math.dist(mob.track.target, position) <= 2.5)
        )

    def _reset_survival(self):
        self._survival_state = None
        self._survival_portal_to = None
        self._survival_portal_committed = False
        self._survival_safe_map = None
        self._survival_next_action = 0.0
        self._survival_posture_request = None
        self._survival_portal_warp_ts = 0.0

    def _clear_recovery_navigation(self):
        self._clear_patrol_route()
        self.manual_nav_goal = None
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._last_patrol_click_target = None
        self.locked_mob_gid = None
        self.locked_combat_mob_gid = None
        self.locked_drop_gid = None
        self._engagement = None
        self.post_kill_wait_until = 0.0
        self.last_kill_pos = None

    def _handle_respawn(self, world, current_map, now):
        with world.lock:
            dead = world.player_is_dead
            death_ts = world.last_player_death_ts
            warp_ts = world.last_map_warp_ts
            warped = bool(world.last_map_warp == current_map and warp_ts > death_ts)
            hp = world.player_hp

        if not self.filters_config["survival_auto_respawn"]:
            self._respawn_started = 0.0
            return dead

        if death_ts > self._handled_death_ts and (dead or warped):
            self._handled_death_ts = death_ts
            self._respawn_started = death_ts
            self._respawn_attempts = 0
            self._respawn_next_attempt = death_ts + 1.2
            self._reset_survival()
            self.travel_plan = None
            self._pending_hud_interaction = None
            self._clear_recovery_navigation()
            self._click_debug("RESSUSCITAR: morte detectada.")

        if not self._respawn_started:
            return dead
        if dead:
            if now >= self._respawn_next_attempt and self._respawn_attempts < 2:
                self._send_game_key(0x0D)
                self._click_debug("RESSUSCITAR: Enter enviado à janela de morte.")
                self._respawn_attempts += 1
                self._respawn_next_attempt = now + 1.2
            return True

        if not warped or hp <= 0 or now < warp_ts + 1.5:
            return True

        if self.farm_map:
            if now < self._respawn_next_attempt:
                return True
            self._respawn_next_attempt = now + 5.0
            self.visited_cells.clear()
            self.temp_blocked_cells.clear()
            self.start_travel_plan(self.farm_map, mode="farm")
            if self.travel_plan:
                self.travel_plan["recovery"] = True
            elif current_map != self.farm_map:
                return True
        self._respawn_started = 0.0
        return False

    def _safe_retreat_portals(self, current_map):
        cities = set(KNOWN_CITIES) | set(CITY_NAMES)
        cities.update(city.get("map", key).lower()
                      for key, city in self.world_routes_data.get("cities", {}).items())
        portals = {
            (source, destination): {
                "wx": wx, "wy": wy,
                "safe_retreat": destination in cities or (source, destination) == ("orcsdun01", "gef_fild10"),
            }
            for (source, destination), (wx, wy) in MAP_PORTALS.items()
        }
        for portal in self.world_routes_data.get("portals", ()):
            link = (portal["from_map"].lower(), portal["to_map"].lower())
            portals[link] = {
                "safe_retreat": portals.get(link, {}).get("safe_retreat", link[1] in cities),
                **portal,
            }
        for link, cell in load_learned_warps().items():
            source, _, destination = link.lower().partition("->")
            if source == current_map and destination and isinstance(cell, (list, tuple)) and len(cell) == 2:
                portal = portals.setdefault((source, destination), {"safe_retreat": destination in cities})
                portal.update(wx=cell[0], wy=cell[1])
        return [(destination, (int(portal["wx"]), int(portal["wy"])))
                for (source, destination), portal in portals.items()
                if source == current_map and portal["safe_retreat"]]

    def _request_survival_posture(self, sitting, now):
        pending = self._survival_posture_request
        if pending and pending[0] == sitting and now < pending[1]:
            return
        self._survival_posture_request = (sitting, now + 0.55)
        if self._send_game_key(0x28):
            self._click_debug("SOBREVIVÊNCIA: ↓ enviada para " + ("sentar." if sitting else "levantar."))

    @staticmethod
    def _visible_portal_cell(goal, portal_cells):
        nearby = [(math.dist(goal, cell), cell) for cell in portal_cells if math.dist(goal, cell) <= 2.5]
        return min(nearby)[1] if nearby else goal

    def _move_on_retreat_path(self, snapshot, gat, player_cell, path):
        self.active_nav_path = path
        self.active_nav_color = "#F87171"
        if not path or self._click_in_progress or self._manual_mouse_button_pressed():
            return
        hwnd = self.get_game_hwnd()
        if not hwnd:
            return
        left, top, width, height = snapshot.viewport
        camera_cell = getattr(self.feed, "_cam", None) or player_cell
        height_fn = getattr(gat, "get_elevation", gat.height_at)
        project = self.feed.camera.projector(snapshot.viewport, camera_cell, height_fn)
        # Restringe cada clique a um trecho reto para o cliente não cortar o desvio do A*.
        step = path[1] if len(path) > 1 else path[0]
        dx, dy = step[0] - path[0][0], step[1] - path[0][1]
        segment = [step]
        for index, cell in enumerate(path[2:5], start=2):
            if cell != (path[0][0] + dx * index, path[0][1] + dy * index):
                break
            segment.append(cell)
        for cell in reversed(segment):
            u, v = project(*cell)
            sx, sy = left + int(u * width), top + int(v * height)
            if (0.05 <= u <= 0.95 and 0.05 <= v <= 0.95
                    and not self._is_point_in_game_ui(sx, sy)
                    and not self._is_point_in_hud(sx, sy, margin=10)):
                self._send_click(hwnd, sx, sy)
                return

    def _handle_survival(self, snapshot, world, player, current_map, now, hp_pct, entities):
        cfg = self.filters_config
        low_hp = cfg["survival_enabled"] and hp_pct < cfg["survival_hp_pct"]
        if self._survival_state == SURVIVAL_RETURN and low_hp:
            if not (self.travel_plan and self.travel_plan.get("mode") == "farm"):
                self.travel_plan = None
            self._reset_survival()
        if self._survival_state is None and low_hp:
            self._reset_survival()
            self._survival_state = SURVIVAL_RETREAT
            self._survival_map = current_map
            self._clear_recovery_navigation()
            self._click_debug(f"SOBREVIVÊNCIA: {SURVIVAL_RETREAT} ({hp_pct:.0f}% HP).")
        if self._survival_state is None:
            return False

        if (self._survival_portal_to and current_map == self._survival_portal_to == world.last_map_warp
                and world.last_map_warp_ts > self._survival_portal_warp_ts):
            self._survival_safe_map = current_map
            self._survival_portal_to = None
            self._click_debug(f"SOBREVIVÊNCIA: portal confirmado em {current_map}.")
        if self._survival_state == SURVIVAL_RETREAT and current_map != self._survival_map:
            self._survival_map = current_map
            if current_map != self._survival_portal_to:
                self._survival_portal_to = None
            self._survival_portal_committed = False
            self._survival_posture_request = None
            self._survival_next_action = 0.0
            self.visited_cells.clear()
            self.temp_blocked_cells.clear()

        if self._survival_state == SURVIVAL_RETURN:
            if not self.travel_plan and current_map == self.farm_map:
                self._reset_survival()
                return False
            if not self.travel_plan and now >= self._survival_next_action:
                self._survival_next_action = now + 5.0
                self.start_travel_plan(self.farm_map, mode="travel")
                if self.travel_plan:
                    self.travel_plan["recovery"] = True
            return self.travel_plan is None

        sitting = world.player_is_sitting
        pending = self._survival_posture_request
        if pending and sitting == pending[0]:
            self._survival_posture_request = None
        if not cfg["survival_enabled"] or hp_pct >= cfg["survival_resume_pct"]:
            if sitting:
                self._request_survival_posture(False, now)
                return True
            self.active_nav_path = None
            if cfg["survival_enabled"] and self.farm_map and current_map != self.farm_map:
                self._survival_state = SURVIVAL_RETURN
                self._survival_next_action = now
                if not (self.travel_plan and self.travel_plan.get("recovery")):
                    self.travel_plan = None
                return True
            self._reset_survival()
            return False

        if not player or not current_map:
            return True
        player_cell = player.cell()
        start = tuple(round(coord) for coord in player_cell)
        radius = cfg["survival_danger_radius"]
        mobs = [(gid, ent, ent.track.cell()) for (kind, gid), ent in entities if kind == "mob"]
        threats = [(gid, ent, cell) for gid, ent, cell in mobs
                   if self._is_mob_hostile(world, ent) or self._is_active_threat(world, gid, ent, player_cell, now)]
        nearest = min((math.dist(cell, player_cell) for _, _, cell in threats), default=math.inf)
        close_hostile = nearest <= radius
        recent_damage = now - world.last_player_damage_ts < 1.5
        recent_aggro = now - world.last_player_aggro_ts < 1.5
        if sitting and (close_hostile or recent_damage):
            self._survival_portal_committed = True
            self._request_survival_posture(False, now)
            return True

        if hp_pct > 15 and (self.auto_loot or self.auto_farm):
            drops = [entry for entry in self._eligible_drops(snapshot, player_cell, entities, now)
                     if entry[0] == 1 and entry[1] <= 6]
            if self._try_auto_loot(snapshot, player_cell, drops, stand_first=sitting):
                if sitting:
                    self._request_survival_posture(False, now)
                return True
        if sitting or now < self._survival_next_action:
            return True
        self._survival_next_action = now + 0.25

        portal_options = (self._safe_retreat_portals(current_map)
                          if cfg["survival_seek_portal"] and current_map != self._survival_safe_map else [])
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(current_map) if terrain else None
        portal_cells = self._get_portal_blocked_cells(radius=0, allow_current_goal=False)
        quiet = not (recent_damage or recent_aggro)
        walking = world._is_player_walking()
        if not portal_options and not close_hostile and quiet and not walking and start not in portal_cells:
            if gat is None or gat.is_walkable(*start):
                self.active_nav_path = None
                self._request_survival_posture(True, now)
                return True
        if gat is None:
            return True

        costs = {}
        group_cells = {}
        mob_cells = {tuple(round(coord) for coord in cell) for _, _, cell in mobs}
        for x, y in mob_cells:
            for dx in range(-6, 7):
                for dy in range(-6, 7):
                    cell = (x + dx, y + dy)
                    distance = max(abs(dx), abs(dy))
                    costs[cell] = costs.get(cell, 0) + 35 * (7 - distance)
                    if distance <= 2:
                        group_cells[cell] = group_cells.get(cell, 0) + 1
        group_limit = max(1, group_cells.get(start, 0))
        danger_blocks = mob_cells | {cell for cell, count in group_cells.items() if count > group_limit}
        # Bloqueia o núcleo de grupos. Se já estiver dentro de um, permite uma saída pelo menor custo.
        portal_paths = []
        for destination, goal in portal_options:
            goal = self._visible_portal_cell(goal, portal_cells)
            if max(abs(start[0] - goal[0]), abs(start[1] - goal[1])) > 55:
                continue
            if not gat.is_walkable(*goal) or goal in danger_blocks:
                continue
            blocked = danger_blocks | (portal_cells - {goal})
            path = gat.find_path(start, goal, blocked_cells=blocked, cell_costs=costs)
            if path and path[-1] == goal and len(path) - 1 <= 55:
                cost = self._path_cost(path) + sum(costs.get(cell, 0) for cell in path[1:])
                portal_paths.append((cost, destination, goal, path))

        if portal_paths:
            _, destination, goal, path = min(portal_paths)
            if destination != self._survival_portal_to:
                self._survival_portal_to = destination
                self._survival_portal_warp_ts = world.last_map_warp_ts
            distance = math.dist(goal, player_cell)
            if distance <= 14 or close_hostile or recent_damage or recent_aggro:
                self._survival_portal_committed = True
            if (distance > 16 and not self._survival_portal_committed and quiet
                    and not walking and nearest > radius and start not in portal_cells and gat.is_walkable(*start)):
                self.active_nav_path = path
                self._request_survival_posture(True, now)
            else:
                self._move_on_retreat_path(snapshot, gat, player_cell, path)
            return True

        best_cell = start
        best_clearance = min((math.dist(start, cell) for _, _, cell in threats), default=math.inf)

        def safe_cell(cell):
            nonlocal best_cell, best_clearance
            if cell in portal_cells:
                return False
            clearance = min((math.dist(cell, mob_cell) for _, _, mob_cell in threats), default=math.inf)
            if clearance > best_clearance or best_cell in portal_cells:
                best_cell, best_clearance = cell, clearance
            moved = max(abs(cell[0] - start[0]), abs(cell[1] - start[1]))
            return clearance > radius and (quiet or moved >= 3)

        blocked = danger_blocks | portal_cells
        path = gat.find_path(start, None, blocked_cells=blocked, cell_costs=costs, goal_test=safe_cell)
        if not path and best_cell != start:
            path = gat.find_path(start, best_cell, blocked_cells=blocked, cell_costs=costs)
        if len(path) > 1:
            self._move_on_retreat_path(snapshot, gat, player_cell, path)
        elif path and quiet and not walking:
            self.active_nav_path = None
            self._request_survival_posture(True, now)
        return True

    def _handle_actions(self, snapshot):
        manual_kafra = self._kafra_manual and not self._hud_drawer_paused and self._hud_edit is None
        if not (self._hud_can_run() or manual_kafra):
            return
        world = getattr(self.feed, "world", None)
        if world is None:
            return
        now = time.monotonic()
        curr_m, player, entities = world.snapshot()
        if snapshot.map != curr_m:
            return
        px, py = player.cell() if player else (None, None)
        curr_m = curr_m or getattr(self, "current_map", "")
        if self._kafra_cycle and world.player_is_dead:
            self._finish_kafra_storage("ciclo interrompido pela morte do personagem.", curr_m)
        if self._handle_respawn(world, curr_m, now):
            return
        if (self.auto_farm or self.auto_patrol) and self.farm_map is None and curr_m:
            self.farm_map = curr_m
        hp_pct = 100.0 * world.player_hp / world.player_max_hp if world.player_max_hp > 0 else 100.0
        if self._handle_survival(snapshot, world, player, curr_m, now, hp_pct, entities):
            if self._kafra_cycle:
                phase = self._kafra_cycle["phase"]
                if phase in ("dialog", "opening", "store", "closing"):
                    self._send_game_key(0x1B)
                self._finish_kafra_storage("ciclo interrompido pela rotina de sobrevivência.", curr_m)
            return
        if self._kafra_cycle is None:
            self._start_kafra_storage(world, player, curr_m, now, manual_kafra)
        if self._kafra_cycle:
            self._tick_kafra_storage(snapshot, world, curr_m, px, py, now)
            return
        if not self._hud_can_run():
            return
        if self.travel_plan and self.travel_plan.get("recovery"):
            self._handle_travel_plan(snapshot, px, py)
            if px is not None and self.travel_plan and (self.nav_goal or self.patrol_target_route):
                self._try_auto_patrol(snapshot, px, py)
            return
        if self._pending_hud_interaction is not None:
            selection = self._pending_hud_interaction
            self._pending_hud_interaction = None
            self._run_hud_interaction(selection)
            return
        if not (self.auto_loot or self.auto_farm or self.auto_patrol or self._has_hud_travel()):
            return

        # ── SEGURANÇA DE MAPA: desativa farm/patrol se o mapa mudar (0x0091) ──
        if curr_m and self.manual_nav_goal and curr_m != self.manual_nav_goal[0]:
            self.manual_nav_goal = None
            self._clear_patrol_route()
        if curr_m and self.farm_map and curr_m != self.farm_map:
            self._handle_map_change(curr_m)
            return

        # ── AUTO-RECUSA DE CONVITES MODAIS (Party / Troca / Amizade) ──
        invite_ts = getattr(getattr(self.feed, "world", None), "last_invite_popup_ts", 0.0)
        if invite_ts > getattr(self, "_last_handled_invite_ts", 0.0):
            self._last_handled_invite_ts = invite_ts
            if self.auto_farm or self.auto_patrol:
                self.center_popup_until = now + 3.0
                self._click_debug("CONVITE MODAL DETECTADO (Party/Troca/Amizade): agendando ESC em 220ms para auto-recusa!")
                self._schedule_escape_key(220)


        # Auto-fechar diálogo acidental de NPC durante Auto-Patrol (exceto em rota direcionada ou Kafra)
        if self.auto_patrol and hasattr(self.feed, "world"):
            dialog = self.feed.world.npc_dialog
            if dialog.get("open") and dialog.get("type") in ("say", "menu", "wait", "warplist"):
                is_targeted = (
                    (self.patrol_target_route is not None and dialog.get("npc_id") is not None)
                    or getattr(self, "_city_travel_state", None) is not None
                )
                if not is_targeted:
                    inp_esc = INPUT(type=INPUT_KEYBOARD)
                    inp_esc.ki.wVk = 0x1B
                    inp_esc.ki.dwFlags = 0
                    u32.SendInput(1, ctypes.byref(inp_esc), ctypes.sizeof(INPUT))
                    inp_esc_up = INPUT(type=INPUT_KEYBOARD)
                    inp_esc_up.ki.wVk = 0x1B
                    inp_esc_up.ki.dwFlags = 0x0002  # KEYEVENTF_KEYUP
                    u32.SendInput(1, ctypes.byref(inp_esc_up), ctypes.sizeof(INPUT))
                    self.feed.world.npc_dialog["open"] = False
                    if hasattr(self.feed, "_cam") and self.feed._cam:
                        bx = int(round(self.feed._cam[0]))
                        by = int(round(self.feed._cam[1]))
                        blocked = getattr(self, "_patrol_npc_blocked_coords", {})
                        blocked[(bx, by)] = now + 30.0
                        self._patrol_npc_blocked_coords = blocked
                    self._click_debug("PATROL: Diálogo de NPC fechado automaticamente (Esc)")

        if px is not None and (self.auto_farm or self.auto_loot or self.auto_patrol):
            position = (px, py)
            self._update_post_kill(world, curr_m)
            drops = self._eligible_drops(snapshot, position, entities, now) if self.auto_loot or self.auto_farm else []
            if self.locked_drop_gid is not None and all(entry[2] != self.locked_drop_gid for entry in drops):
                self.locked_drop_gid = None
            if now < self.post_kill_wait_until:
                started = self.post_kill_wait_until - 0.35
                if any(drop.created_at >= started for _, _, _, drop in drops):
                    self.post_kill_wait_until = 0.0
            waiting = now < self.post_kill_wait_until
            tier1 = [entry for entry in drops if entry[0] == 1 and (not waiting or entry[1] <= 2.2)]
            if tier1 and self._try_auto_loot(snapshot, position, tier1, direct_only=waiting):
                self._reset_patrol(px, py)
                return

            if self.auto_farm or self.auto_loot:
                if self._try_auto_farm(snapshot, position, entities, defense=True, direct_only=waiting):
                    self._reset_patrol(px, py)
                    return
                if not waiting and self._try_auto_loot(snapshot, position, drops):
                    self._reset_patrol(px, py)
                    return

            if waiting:
                return

        if self._has_hud_travel():
            if self.travel_plan:
                self._handle_travel_plan(snapshot, px, py)
            elif self._city_travel_state:
                self._handle_city_travel(snapshot, px, py)
            if px is not None and (self.nav_goal or self.patrol_target_route):
                self._try_auto_patrol(snapshot, px, py)
            return

        if px is not None and self.auto_farm:
            if self._try_auto_farm(snapshot, (px, py), entities):
                self._reset_patrol(px, py)
                return

        self._engagement = None
        self.locked_drop_gid = None
        if self.auto_patrol and px is not None:
            self._try_auto_patrol(snapshot, px, py)

    def _update_post_kill(self, world, current_map):
        with world.lock:
            deaths = tuple(world.recent_dead_mobs)
            zone = (current_map, world.last_map_warp_ts)
        if self._engagement_zone != zone:
            self._engagement_zone = zone
            self._engagement = None
            self.post_kill_wait_until = 0.0
            self.last_kill_pos = None
        for dead in deaths:
            if dead["time"] <= self._last_kill_event:
                continue
            self._last_kill_event = dead["time"]
            if (dead["near_player"] or dead["was_target"]
                    or self.locked_mob_gid in (dead["gid"], dead["aid"])):
                self.post_kill_wait_until = dead["time"] + 0.35
                self.last_kill_pos = dead["cell"]

    @staticmethod
    def _has_walkable_line(p0, p1, gat):
        """Verifica se há linha de caminhada direta sem colisão de paredes entre dois pontos."""
        if not gat:
            return True
        x0, y0 = int(round(p0[0])), int(round(p0[1]))
        x1, y1 = int(round(p1[0])), int(round(p1[1]))
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        steps = max(dx, dy)
        if steps <= 1:
            return gat.is_walkable(x1, y1)
        for s in range(1, steps + 1):
            tx = int(round(x0 + s * (x1 - x0) / steps))
            ty = int(round(y0 + s * (y1 - y0) / steps))
            if not gat.is_walkable(tx, ty):
                return False
        return True

    def _try_auto_patrol(self, snapshot, px, py):
        """Auto-Patrol: patrulha mapa por A*, calculando sub-passos a 8-11 células e clicando via movimento relativo."""
        if not snapshot or not hasattr(self.feed, "world"):
            return

        if self._click_in_progress:
            return
        if self._manual_mouse_button_pressed():
            self._click_debug("PATROL PAUSADO: botão físico do mouse pressionado")
            return

        vp_obj = getattr(self.feed, "viewport", None)
        target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        target_hwnd = self.get_game_hwnd(target_hwnd)
        if not self._game_is_foreground():
            return

        now = time.monotonic()
        cur_cell = (int(round(px)), int(round(py)))

        # Registra células visitadas recentemente
        if not self.visited_cells or math.hypot(cur_cell[0] - self.visited_cells[-1][0], cur_cell[1] - self.visited_cells[-1][1]) >= 1.0:
            self.visited_cells.append(cur_cell)
            if len(self.visited_cells) > 80:
                self.visited_cells.pop(0)

        # Limpa células bloqueadas temporariamente por travamento
        self.temp_blocked_cells = {
            cell: exp for cell, exp in self.temp_blocked_cells.items() if now < exp
        }

        # Obtém terreno / gat atual
        curr_map = getattr(self.feed.world, "map", None)
        if not curr_map or not hasattr(self.feed, "terrain") or not self.feed.terrain:
            # Sem mapa carregado: se nav_goal estiver ativo, faz passo simples sem gat
            nav = getattr(self, "nav_goal", None)
            if nav and px is not None:
                ng_map, ng_x, ng_y = nav
                dist_ng = math.hypot(px - ng_x, py - ng_y)
                if dist_ng <= 2.0:
                    self._click_debug(f"NAV_GOAL: Destino alcançado ({ng_x},{ng_y}) — encerrando.")
                    self.nav_goal = None
                    return
                viewport = snapshot.viewport
                left, top, width, height = viewport
                cam_pos = getattr(self.feed, "_cam", None) or (px, py)
                project_flat = self.feed.camera.projector(viewport, cam_pos, None)
                u_ng, v_ng = project_flat(ng_x, ng_y, 0.0)
                sx_ng = left + int(u_ng * width)
                sy_ng = top + int(v_ng * height)
                if not self._is_point_in_hud(sx_ng, sy_ng, margin=10):
                    self.patrol_move_and_click(sx_ng, sy_ng, hwnd=target_hwnd)
            return

        try:
            gat = self.feed.terrain.for_map(curr_map)
        except Exception as exc:
            self._click_debug(f"PATROL erro terreno: {exc}")
            return

        if gat is None:
            return

        # ── Helper de projeção (definido antes de qualquer uso) ──
        viewport = snapshot.viewport
        left, top, width, height = viewport
        height_fn = getattr(gat, "get_elevation", gat.height_at)
        cam_pos = getattr(self.feed, "_cam", None) or (px, py)
        project = self.feed.camera.projector(viewport, cam_pos, height_fn)

        # ── Suporte a nav_goal: rota direcionada 100% independente do auto_patrol ──
        nav = getattr(self, "nav_goal", None)
        if nav is not None:
            ng_map, ng_x, ng_y = nav
            norm_ng = ng_map.lower().removesuffix(".gat").removesuffix(".rsw")
            if curr_map == norm_ng:
                dist_ng = math.hypot(px - ng_x, py - ng_y)
                is_manual = (getattr(self, "manual_nav_goal", None) is not None)
                reach_thresh = 1.5 if is_manual else 2.0
                if dist_ng <= reach_thresh:
                    self._click_debug(f"NAV_GOAL: Destino alcançado ({ng_x},{ng_y}) — encerrando.")
                    self.manual_nav_goal = None
                    self.nav_goal = None
                    self._reset_patrol(px, py)
                    return
                # Usa apenas find_path, nunca pick_next_waypoint
                ng_path = gat.find_path((int(round(px)), int(round(py))), (ng_x, ng_y),
                                        blocked_cells=self._get_all_blocked_cells())
                if ng_path:
                    self.current_patrol_path = ng_path
                    self.current_waypoint = (ng_x, ng_y)
                    self.active_nav_path = list(ng_path)
                    self.active_nav_color = "#A78BFA"  # Roxo = nav_goal
                # Depois deixa a lógica de execução do caminho abaixo tomar conta
                # (não retorna aqui, cai no código de sub-step abaixo)
            else:
                # Nav_goal em outro mapa: roteia via find_map_route
                walk_route = find_map_route(curr_map, norm_ng)
                if walk_route:
                    m_from, m_to, port_x, port_y = walk_route[0]
                    self.patrol_target_route = (m_from, port_x, port_y, "warp")
                    self._click_debug(f"NAV_GOAL multi-mapa: Indo para portal {m_to} em ({port_x},{port_y})")
                else:
                    self._click_debug(f"NAV_GOAL: Sem rota de {curr_map} para {norm_ng}")
                    self.nav_goal = None
                    return

        # ── Suporte a Rotas de Destino: (map_name, x, y, target_npc_or_warp) ──
        if self.patrol_target_route:
            r_map, r_x, r_y, r_target = self.patrol_target_route
            norm_r_map = r_map.lower().removesuffix(".gat").removesuffix(".rsw")
            if curr_map == norm_r_map:
                dist_to_route = math.hypot(px - r_x, py - r_y)
                interact_dist = 3.8 if r_target else 2.5
                if dist_to_route <= interact_dist:
                    if r_target:
                        left, top, width, height = snapshot.viewport
                        for item in snapshot.objects:
                            if item.kind in ("npc", "warp"):
                                if str(r_target) in item.key or item.catalog_id == r_target:
                                    sprite_z = 5.5 if item.kind == "npc" else 1.2
                                    u_tgt, v_tgt = project(r_x, r_y, sprite_z)
                                    sx = left + int(u_tgt * width)
                                    sy = top + int(v_tgt * height)
                                    if self._is_point_in_game_ui(sx, sy):
                                        self._side_step_to_reveal((r_x, r_y), px, py)
                                        return
                                    self._click_debug(f"PATROL: Interagindo com alvo na rota {item.key} em ({sx},{sy})")
                                    if not self.patrol_move_and_click(sx, sy, hwnd=target_hwnd):
                                        return
                                    self._hud_npc_line_target = (item.key, curr_map, now + 0.950)
                                    self.patrol_target_route = None
                                    self._reset_patrol(px, py)
                                    return
                    self._click_debug(f"PATROL: Destino da rota alcançado ({r_x}, {r_y})")
                    self.patrol_target_route = None
                    self._reset_patrol(px, py)

        # ── Verificação de Movimento Estritamente por Pacotes TCP (0x0087 ZC_NOTIFY_PLAYERMOVE) ──
        if getattr(self, "_last_patrol_click_target", None) is not None:
            raw_tgt = self._last_patrol_click_target
            pc_time = raw_tgt[0]
            elapsed_pc = now - pc_time

            if elapsed_pc < 0.380:
                # Aguarda janela de 380ms para confirmação de pacote TCP pelo servidor
                return

            w = getattr(self.feed, "world", None)
            cur_tcp_cnt = getattr(w, "player_move_pkt_count", 0) if w else 0
            cur_tcp_time = getattr(w, "last_player_move_pkt_time", 0.0) if w else 0.0
            pc_px, pc_py, pc_gx, pc_gy = raw_tgt[1], raw_tgt[2], raw_tgt[3], raw_tgt[4]
            pc_sx = raw_tgt[5] if len(raw_tgt) > 5 else None
            pc_sy = raw_tgt[6] if len(raw_tgt) > 6 else None
            if self._touch_click_aborted((pc_sx, pc_sy), pc_time):
                self._last_patrol_click_target = None
                return
            pc_tcp_cnt = raw_tgt[7] if len(raw_tgt) > 7 else 0
            pc_tcp_time = raw_tgt[8] if len(raw_tgt) > 8 else 0.0
            retry_count = raw_tgt[9] if len(raw_tgt) > 9 else 0

            # Confirmação ESTRITA por pacotes TCP (0x0087 ZC_NOTIFY_PLAYERMOVE):
            tcp_confirmed = (cur_tcp_cnt > pc_tcp_cnt) or (cur_tcp_time > pc_time)

            if tcp_confirmed:
                # Pacote TCP 0x0087 recebido: o servidor confirmou que o personagem está se movendo!
                self._last_patrol_click_target = None
                self._patrol_stuck_attempts = 0
            else:
                # TCP 0x0087 NÃO recebido em 380ms: o personagem NÃO se moveu!
                # 1. Tenta clicar em quadrados vizinhos ao que estávamos clicando (retry_count < 2)
                neighbor_clicked = False
                if retry_count < 2:
                    cand_neighbors = []
                    for dx in (-1, 0, 1, -2, 2):
                        for dy in (-1, 0, 1, -2, 2):
                            if dx == 0 and dy == 0:
                                continue
                            n_gx = int(pc_gx) + dx
                            n_gy = int(pc_gy) + dy
                            if not (0 <= n_gx < gat.width and 0 <= n_gy < gat.height):
                                continue
                            if not gat.is_walkable(n_gx, n_gy):
                                continue
                            if (n_gx, n_gy) in self.temp_blocked_cells:
                                continue
                            if gat.count_walkable_neighbors(n_gx, n_gy) < 5:
                                continue
                            dist_to_p = math.hypot(n_gx - px, n_gy - py)
                            if dist_to_p < 2.0:
                                continue
                            nu, nv = project(n_gx, n_gy)
                            if not (0.16 <= nu <= 0.84 and 0.18 <= nv <= 0.82):
                                continue
                            nsx = left + int(nu * width)
                            nsy = top + int(nv * height)
                            if self._is_point_in_game_ui(nsx, nsy):
                                continue
                            wp_d = math.hypot(n_gx - self.current_waypoint[0], n_gy - self.current_waypoint[1]) if self.current_waypoint else 0.0
                            cand_neighbors.append((wp_d, n_gx, n_gy, nsx, nsy))

                    if cand_neighbors:
                        cand_neighbors.sort(key=lambda item: item[0])
                        _, chosen_gx, chosen_gy, chosen_sx, chosen_sy = cand_neighbors[0]
                        self._click_debug(f"PATROL [TCP sem 0x0087]: Não moveu! Tentando célula vizinha ({chosen_gx},{chosen_gy}) tela ({chosen_sx},{chosen_sy}) tentativa {retry_count + 1}")
                        self.temp_blocked_cells[(int(pc_gx), int(pc_gy))] = now + 25.0
                        _post_cnt = getattr(self.feed.world, "player_move_pkt_count", 0) if hasattr(self.feed, "world") else 0
                        _post_time = getattr(self.feed.world, "last_player_move_pkt_time", 0.0) if hasattr(self.feed, "world") else 0.0
                        self.patrol_move_and_click(chosen_sx, chosen_sy, hwnd=target_hwnd)
                        self.last_patrol_click_time = now
                        self.last_patrol_click_player_pos = (px, py)
                        self._last_patrol_click_target = (now, px, py, chosen_gx, chosen_gy, chosen_sx, chosen_sy, _post_cnt, _post_time, retry_count + 1)
                        neighbor_clicked = True
                        return

                # Se retry_count >= 2 ou nenhum vizinho viável:
                # O obstáculo é extenso (árvore/rocha/RSW). MUDAR DE ROTA IMEDIATAMENTE!
                self._click_debug(f"PATROL [TCP sem 0x0087]: Célula ({pc_gx},{pc_gy}) e vizinhos bloqueados. Mudando de rota!")
                for _bx in range(int(pc_gx) - 3, int(pc_gx) + 4):
                    for _by in range(int(pc_gy) - 3, int(pc_gy) + 4):
                        self.temp_blocked_cells[(_bx, _by)] = now + 35.0

                wp = self.current_waypoint
                if wp and math.hypot(wp[0] - pc_gx, wp[1] - pc_gy) <= 6.0:
                    self.temp_blocked_cells[(int(wp[0]), int(wp[1]))] = now + 35.0
                    self.current_waypoint = None

                cur_pt = (int(round(px)), int(round(py)))
                recalc_path = None
                if self.current_waypoint:
                    recalc_path = gat.find_path(cur_pt, self.current_waypoint, blocked_cells=self._get_all_blocked_cells())

                if recalc_path and len(recalc_path) > 1 and math.hypot(recalc_path[1][0] - pc_gx, recalc_path[1][1] - pc_gy) >= 2.0:
                    self._click_debug(f"PATROL: Nova rota A* para contornar obstáculo ({len(recalc_path)} passos).")
                    self.current_patrol_path = recalc_path
                    self.active_nav_path = list(recalc_path)
                    self._last_patrol_click_target = None
                else:
                    self._click_debug("PATROL: Rota obstruída. Selecionando novo waypoint e mudando direção!")
                    self.visited_cells.append((int(pc_gx), int(pc_gy)))
                    self.current_patrol_path = []
                    self.current_waypoint = None
                    self._last_patrol_click_target = None

                    hmap = self.feed.world.get_heatmap_sectors() if (hasattr(self.feed, "world") and hasattr(self.feed.world, "get_heatmap_sectors")) else None
                    new_wp = gat.pick_next_waypoint(
                        (px, py), visited_cells=self.visited_cells,
                        min_dist=14, max_dist=28, blocked_cells=self._get_all_blocked_cells(),
                        heatmap=hmap
                    )
                    if new_wp:
                        self.current_waypoint = new_wp
                        new_route = gat.find_path(cur_pt, new_wp, blocked_cells=self._get_all_blocked_cells())
                        if new_route:
                            self.current_patrol_path = new_route
                            self.active_nav_path = list(new_route)
                            self._click_debug(f"PATROL: Novo waypoint selecionado: {new_wp} ({len(new_route)} passos)")

                    # Passo imediato de fuga em direção oposta ao obstáculo
                    rescue_cell = None
                    portal_blocked = self._get_portal_blocked_cells()
                    _angles16 = [i * (360.0 / 16) for i in range(16)]
                    _blocked_dir_dx = pc_gx - px if px is not None else 0
                    _blocked_dir_dy = pc_gy - py if py is not None else 0
                    for _r in (3, 4, 5):
                        for _ang_deg in _angles16:
                            _ang_rad = math.radians(_ang_deg)
                            _cx = int(round(px + math.cos(_ang_rad) * _r))
                            _cy = int(round(py + math.sin(_ang_rad) * _r))
                            if not (0 <= _cx < gat.width and 0 <= _cy < gat.height):
                                continue
                            if not gat.is_walkable(_cx, _cy):
                                continue
                            if (_cx, _cy) in self.temp_blocked_cells or (_cx, _cy) in portal_blocked:
                                continue
                            if gat.count_walkable_neighbors(_cx, _cy) < 6:
                                continue
                            _cos_sim = ((_cx - px) * _blocked_dir_dx + (_cy - py) * _blocked_dir_dy)
                            _mag = math.hypot(_cx - px, _cy - py) * math.hypot(_blocked_dir_dx, _blocked_dir_dy)
                            if _mag > 0.01 and _cos_sim / _mag > 0.3:
                                continue
                            _u, _v = project(_cx, _cy)
                            if not (0.16 <= _u <= 0.84 and 0.18 <= _v <= 0.82):
                                continue
                            _rsx = left + int(_u * width)
                            _rsy = top + int(_v * height)
                            if self._is_point_in_game_ui(_rsx, _rsy):
                                continue
                            rescue_cell = (_cx, _cy, _rsx, _rsy)
                            break
                        if rescue_cell:
                            break

                    if rescue_cell:
                        _rc, _ry2, _rsx2, _rsy2 = rescue_cell
                        self._click_debug(f"PATROL: Passo de escape em área livre ({_rc},{_ry2}) tela ({_rsx2},{_rsy2})")
                        _post_cnt = getattr(self.feed.world, "player_move_pkt_count", 0) if hasattr(self.feed, "world") else 0
                        _post_time = getattr(self.feed.world, "last_player_move_pkt_time", 0.0) if hasattr(self.feed, "world") else 0.0
                        self.patrol_move_and_click(_rsx2, _rsy2, hwnd=target_hwnd)
                        self.last_patrol_click_time = now
                        self.last_patrol_click_player_pos = (px, py)
                        self._last_patrol_click_target = (now, px, py, _rc, _ry2, _rsx2, _rsy2, _post_cnt, _post_time, 0)
                    return

        # ── Detecção Complementar de Travamento por Tempo (> 1.8s sem mover) ──
        if self.last_patrol_click_time > 0 and (now - self.last_patrol_click_time > 1.8):
            if self.last_patrol_click_player_pos:
                dist_moved = math.hypot(px - self.last_patrol_click_player_pos[0], py - self.last_patrol_click_player_pos[1])
                if dist_moved < 0.6:
                    stuck_cell = next((cell for cell in self.current_patrol_path if cell != cur_cell), None)
                    if stuck_cell is not None:
                        self.temp_blocked_cells[stuck_cell] = now + 15.0
                    self._click_debug("PATROL: Travamento detectado (>1.8s sem mover). Recalculando rota.")
                    self.current_patrol_path = []
                    self.stuck_count += 1
                    if self.stuck_count >= 2:
                        self.current_waypoint = None
                        self.stuck_count = 0
                    self.last_patrol_click_time = now
                    return

        # ── Planejamento de Waypoint e Rota A* ──
        route_target = self.patrol_target_route and curr_map == self.patrol_target_route[0].lower().removesuffix(".gat")
        roaming = not route_target and getattr(self, "nav_goal", None) is None
        arrival_dist = 1.0 if roaming and len(self.current_patrol_path) <= 4 else 2.0
        if not self.current_waypoint or math.dist((px, py), self.current_waypoint) <= arrival_dist:
            next_path = None
            blocked = self._get_all_blocked_cells()
            if route_target:
                next_wp = (int(self.patrol_target_route[1]), int(self.patrol_target_route[2]))
            elif getattr(self, "nav_goal", None) is not None:
                # nav_goal ativo: nunca chama pick_next_waypoint, usa o destino direto
                ng_map, ng_x, ng_y = self.nav_goal
                next_wp = (ng_x, ng_y)
                self.active_nav_color = "#A78BFA"  # Roxo = nav_goal
            else:
                hmap = self.feed.world.get_heatmap_sectors() if (hasattr(self.feed, "world") and hasattr(self.feed.world, "get_heatmap_sectors")) else None
                next_wp = None
                for min_dist, max_dist in ((10, 22), (4, 9), (2, 3)):
                    candidate = gat.pick_next_waypoint(
                        (px, py), visited_cells=self.visited_cells,
                        min_dist=min_dist, max_dist=max_dist, blocked_cells=blocked, heatmap=hmap,
                    )
                    if candidate is None:
                        continue
                    path = gat.find_path((px, py), candidate, blocked_cells=blocked)
                    if path and len(path) > 1 and path[-1] == candidate:
                        next_wp, next_path = candidate, path
                        break
                self.active_nav_color = "#38BDF8"  # Ciano = Auto-Patrol
            if next_wp:
                self.current_waypoint = next_wp
                if next_path is None:
                    next_path = gat.find_path((px, py), next_wp, blocked_cells=blocked)
                self.current_patrol_path = next_path or []
                self.active_nav_path = list(self.current_patrol_path)
                # Preserva a cor já definida por nav_goal se aplicável
                if not hasattr(self, "active_nav_color") or self.active_nav_color != "#A78BFA":
                    self.active_nav_color = "#38BDF8"  # Ciano = Auto-Patrol
                self.stuck_count = 0
            else:
                self._click_debug("PATROL: Nenhum waypoint caminhável disponível.")
                return

        if not self.current_patrol_path:
            if self.current_waypoint:
                self.current_patrol_path = gat.find_path(
                    (px, py), self.current_waypoint, blocked_cells=self._get_all_blocked_cells()
                )
                if self.current_patrol_path:
                    self.active_nav_path = list(self.current_patrol_path)
                    self.active_nav_color = "#38BDF8"
            if not self.current_patrol_path:
                self.current_waypoint = None
                return

        # ── Detecção e Poda Precisa de Avanço na Rota ──
        if self.current_patrol_path:
            closest_idx = 0
            min_dist_to_path = float("inf")
            for i, (cx, cy) in enumerate(self.current_patrol_path):
                d = math.hypot(px - cx, py - cy)
                if d < min_dist_to_path:
                    min_dist_to_path = d
                    closest_idx = i

            # Se o jogador já avançou pela rota, descarta nós anteriores ao ponto mais próximo
            if closest_idx > 0 and min_dist_to_path <= 4.0:
                self.current_patrol_path = self.current_patrol_path[closest_idx:]

            # Remove os nós iniciais que o jogador já alcançou (distância <= 1.8 células)
            while self.current_patrol_path and math.hypot(self.current_patrol_path[0][0] - px, self.current_patrol_path[0][1] - py) <= 1.8:
                self.current_patrol_path.pop(0)

            # Se o personagem se desviou muito da rota (> 4.5 células), recalcula a partir de onde ele está
            if min_dist_to_path > 4.5 and self.current_waypoint:
                cur_cell_p = (int(round(px)), int(round(py)))
                recalc_p = gat.find_path(cur_cell_p, self.current_waypoint, blocked_cells=self._get_all_blocked_cells())
                if recalc_p:
                    self.current_patrol_path = recalc_p
                    self.active_nav_path = list(recalc_p)

        if not self.current_patrol_path:
            self.current_waypoint = None
            return

        # ── Projeta sub-passo INTELIGENTE AO LONGO DA ROTA (SEM PULAR MONTANHAS/DESPENHADEIROS) ──
        sub_step_cell = None
        sub_step_u = None
        sub_step_v = None

        p_elev = gat.get_elevation(px, py) if hasattr(gat, "get_elevation") else 0.0
        max_scan = min(len(self.current_patrol_path), 15)
        candidates_forward = []
        short_patrol = roaming and math.dist((px, py), self.current_waypoint) <= 3.0
        min_step_dist = 1.0 if short_patrol else 2.2
        min_neighbors = 2 if short_patrol else 5

        for idx in range(max_scan):
            cand = self.current_patrol_path[idx]
            cx, cy = cand
            dist_p = math.hypot(cx - px, cy - py)
            if dist_p < min_step_dist:
                continue

            # Proteção contra relevo/montanha: não clica do topo para o fundo de despenhadeiro
            if hasattr(gat, "get_elevation"):
                c_elev = gat.get_elevation(cx, cy)
                if abs(c_elev - p_elev) > 16.0:
                    # Desnível muito íngreme direto entre o player e este ponto;
                    # interrompe o scan adiante para forçar contorno suave da rota
                    break

            # Linha direta caminhável: não pode atravessar paredes ou abismos entre player e sub-passo
            if not self._has_walkable_line((px, py), (cx, cy), gat):
                break

            # Célula caminhável e segura
            if not gat.is_walkable(cx, cy):
                continue
            if (cx, cy) in self.temp_blocked_cells or (cx, cy) in self._get_portal_blocked_cells():
                continue
            if gat.count_walkable_neighbors(cx, cy) < min_neighbors:
                continue

            # Projeção segura na tela
            u, v = project(cx, cy)
            if not (0.16 <= u <= 0.84 and 0.18 <= v <= 0.82):
                continue

            cand_sx = left + int(u * width)
            cand_sy = top + int(v * height)
            if self._is_point_in_game_ui(cand_sx, cand_sy):
                continue

            # Zona de exclusão de entidades no mapa (NPC, warp, lojas, players)
            skip_ent = False
            if hasattr(self.feed, "world"):
                w = self.feed.world
                with w.lock:
                    for (ekind, _), ent in w.entities.items():
                        if ekind in ("npc", "warp") or getattr(ent, "catalog_id", None) == 45:
                            ec = ent.track.target
                            is_warp = (ekind == "warp" or getattr(ent, "catalog_id", None) == 45)
                            rad = 5.5 if is_warp else 2.2
                            tp_portal = getattr(self, "_travel_allowed_portal", None)
                            if is_warp and tp_portal is not None and math.hypot(ec[0] - tp_portal[0], ec[1] - tp_portal[1]) <= 2.5:
                                continue
                            if is_warp and getattr(self, "nav_goal", None) is not None and math.hypot(ec[0] - self.nav_goal[1], ec[1] - self.nav_goal[2]) <= 1.5:
                                continue
                            if math.hypot(ec[0] - cx, ec[1] - cy) <= rad:
                                skip_ent = True
                                break
                    if not skip_ent:
                        for _pid, (pc, _) in w.other_players.items():
                            if max(abs(pc[0] - cx), abs(pc[1] - cy)) <= 2.2:
                                skip_ent = True
                                break
                    if not skip_ent:
                        for vaid in w.vending_aids:
                            ent = w.entities.get(("npc", vaid))
                            if ent:
                                ec = ent.track.target
                                if max(abs(ec[0] - cx), abs(ec[1] - cy)) <= 2.2:
                                    skip_ent = True
                                    break
            if skip_ent:
                continue

            # Verifica proximidade visual de NPCs/Portais na tela (50px)
            near_screen_ent = False
            for item in snapshot.objects:
                if item.kind in ("npc", "warp"):
                    ix = left + int(item.u * width)
                    iy = top + int(item.v * height)
                    if math.hypot(cand_sx - ix, cand_sy - iy) < 50:
                        near_screen_ent = True
                        break
            if near_screen_ent:
                continue

            candidates_forward.append((dist_p, cand, u, v))

        # Seleciona nó preferencialmente no intervalo 10–14 células à frente
        if candidates_forward:
            in_range = [c for c in candidates_forward if 10.0 <= c[0] <= 14.5]
            if in_range:
                in_range.sort(key=lambda item: item[0], reverse=True)
                _, sub_step_cell, sub_step_u, sub_step_v = in_range[0]
            else:
                candidates_forward.sort(key=lambda item: item[0], reverse=True)
                _, sub_step_cell, sub_step_u, sub_step_v = candidates_forward[0]

        if sub_step_cell is None:
            self._click_debug("PATROL: Rota sem sub-passo clicável. Reiniciando patrol para nova rota!")
            self._reset_patrol(px, py)
            self.current_patrol_path = []
            self.current_waypoint = None
            return

        # ── Cadência Inteligente de Caminhada ──
        # Se o personagem já está ativamente caminhando rumo ao destino e ainda tem mais
        # de 2.2 células a percorrer no passo atual, não interrompe a animação com novos cliques
        w_player = getattr(getattr(self.feed, "world", None), "player", None)
        is_walking = getattr(self.feed.world, "_is_player_walking", lambda: False)()
        if is_walking and w_player:
            w_tgt = getattr(w_player, "target", None)
            if w_tgt:
                rem_walk = math.hypot(px - w_tgt[0], py - w_tgt[1])
                if rem_walk >= 2.2 and (now - self.last_patrol_click_time < 1.1):
                    return
            elif now - self.last_patrol_click_time < 0.85:
                return

        dist_player = math.hypot(sub_step_cell[0] - px, sub_step_cell[1] - py)
        if dist_player < min_step_dist:
            self._click_debug(f"PATROL: clique descartado por proximidade aos pés ({dist_player:.2f} < {min_step_dist:.1f})")
            return

        sx = left + int(sub_step_u * width)
        sy = top + int(sub_step_v * height)

        _w = getattr(self.feed, "world", None)
        _pre_tcp_cnt = getattr(_w, "player_move_pkt_count", 0) if _w else 0
        _pre_tcp_time = getattr(_w, "last_player_move_pkt_time", 0.0) if _w else 0.0

        self._click_debug(f"PATROL: Passo em ({sub_step_cell[0]}, {sub_step_cell[1]}) -> tela ({sx}, {sy})")
        self.patrol_move_and_click(sx, sy, hwnd=target_hwnd)
        self.last_patrol_click_time = now
        self.last_patrol_click_player_pos = (px, py)
        # Salva o alvo e dados para verificação estrita por pacotes TCP (0x0087)
        self._last_patrol_click_target = (now, px, py, sub_step_cell[0], sub_step_cell[1], sx, sy, _pre_tcp_cnt, _pre_tcp_time, 0)

    @staticmethod
    def _window_pid(hwnd):
        pid = wintypes.DWORD()
        if hwnd:
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _remember_game_window(self, hwnd):
        if not hwnd or not u32.IsWindow(hwnd):
            return None, None, 0
        root_hwnd = u32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
        if self._game_window[:2] != (hwnd, root_hwnd):
            self._game_window = (hwnd, root_hwnd, self._window_pid(hwnd))
        return self._game_window

    def _is_game_focused(self, fg=None, *, game_window=None):
        if fg is None:
            fg = u32.GetForegroundWindow()
        hwnd, root_hwnd, game_pid = game_window if game_window is not None else self._game_window
        return bool(fg and hwnd and (fg in (hwnd, root_hwnd) or (game_pid and self._window_pid(fg) == game_pid)))

    def _game_is_foreground(self):
        return self._is_game_focused()

    def _is_hud_foreground(self, fg=None):
        if fg is None:
            fg = u32.GetForegroundWindow()
        return self._window_pid(fg) == self._own_pid

    def _get_warp_positions(self):
        """Retorna lista de coordenadas (wx, wy) de todos os portais ativos (job == 45 ou kind == 'warp')."""
        warps = []
        if hasattr(self.feed, "world") and self.feed.world:
            with self.feed.world.lock:
                for (k, _aid), ent in self.feed.world.entities.items():
                    if k == "warp" or getattr(ent, "catalog_id", None) == 45:
                        pos = getattr(ent.track, "target", None) or ent.track.cell(time.monotonic())
                        if pos:
                            warps.append((pos[0], pos[1]))
        return warps

    def _get_portal_blocked_cells(self, radius=5.5, allow_current_goal=True):
        """Retorna conjunto de células a <= 5.5 de qualquer portal (job == 45 ou kind == 'warp'),
        bloqueando no A*, no Auto-Patrol e nos passos de fuga/desvio, exceto se for o nav_goal do usuário."""
        blocked = set()
        if not hasattr(self.feed, "world") or not self.feed.world:
            return blocked
        ng = self.nav_goal if allow_current_goal else None
        tp_portal = self._travel_allowed_portal if allow_current_goal else None
        limit = math.ceil(radius)
        with self.feed.world.lock:
            for (k, _aid), ent in self.feed.world.entities.items():
                if k == "warp" or getattr(ent, "catalog_id", None) == 45:
                    pos = getattr(ent.track, "target", None) or ent.track.cell(time.monotonic())
                    if not pos:
                        continue
                    wx, wy = pos[0], pos[1]
                    if tp_portal is not None:
                        if math.hypot(wx - tp_portal[0], wy - tp_portal[1]) <= 2.5:
                            continue  # Portal alvo da etapa atual liberado (todos os outros mantidos bloqueados a 5.5)
                    elif ng is not None:
                        _, ng_x, ng_y = ng
                        if math.hypot(wx - ng_x, wy - ng_y) <= 1.5:
                            continue  # Portal é o destino explícito do usuário
                    for dx in range(-limit, limit + 1):
                        for dy in range(-limit, limit + 1):
                            if math.hypot(dx, dy) <= radius:
                                blocked.add((int(round(wx)) + dx, int(round(wy)) + dy))
        return blocked

    def _get_all_blocked_cells(self):
        """Retorna combinação das células temporariamente bloqueadas + zona proibida ampla de portais."""
        portal_blocked = self._get_portal_blocked_cells()
        return set(getattr(self, "temp_blocked_cells", {}).keys()) | portal_blocked

    def _clear_patrol_route(self):
        """Limpa imediatamente todas as rotas e waypoints do Patrol e força redesenho do overlay."""
        self.patrol_path = []
        self.current_patrol_path = []
        self.patrol_waypoints = []
        self.current_waypoint = None
        self.active_nav_path = None
        self.nav_path = None
        self.nav_goal = None
        self.patrol_target_route = None
        if hasattr(self, "canvas_pool"):
            try:
                self.canvas_pool.begin_frame()
                self.canvas_pool.end_frame()
            except Exception:
                pass
        if hasattr(self, "draw") and getattr(self, "last", None):
            try:
                self.draw(self.last, getattr(self, "last_stats", None))
            except Exception:
                pass

    def _schedule_escape_key(self, delay_ms: int = 220):
        """Agenda um único toque rápido de VK_ESCAPE (0x1B) após delay_ms para fechar pop-ups modais."""
        def _fire():
            if not self._hud_can_run() or not self._hud_render_foreground:
                return
            if self._survival_state or self._respawn_started or self.feed.world.player_is_dead:
                return
            target_hwnd = self.get_game_hwnd(None)
            if target_hwnd and u32.IsWindow(target_hwnd):
                u32.SetForegroundWindow(target_hwnd)
            self._send_game_key(0x1B)
        self.root.after(delay_ms, _fire)

    def _handle_map_change(self, new_map):
        """Ao detectar troca de mapa (0x0091 / current_map != farm_map), atualiza rotas ou desativa farm."""
        norm_new = str(new_map or "").lower().removesuffix(".gat").removesuffix(".rsw")
        tp = getattr(self, "travel_plan", None)
        if tp:
            self._click_debug(f"MAPA ALTERADO (travel_plan ativo): {norm_new}")
            self.farm_map = new_map if self.auto_farm or self.auto_patrol else None
            self._city_travel_state = None
            steps = tp.get("steps", [])
            idx = tp.get("step_idx", 0)
            if idx < len(steps):
                step = steps[idx]
                if step.get("to_map", "").lower() == norm_new or step.get("to_city", "").lower() == norm_new:
                    tp["step_idx"] = idx + 1
            self.locked_mob_gid = None
            self.locked_combat_mob_gid = None
            self.locked_drop_gid = None
            self._clear_patrol_route()
            self.nav_goal = None
            self._travel_allowed_portal = None
            if norm_new == tp.get("target_map", "").lower():
                if tp.get("mode") == "farm":
                    self.farm_map = norm_new
                    self.auto_farm = True
                    self.auto_loot = True
                    self.auto_patrol = True
                    self._click_debug(f"TRAVEL_PLAN: Chegamos ao destino {norm_new}! Auto-Farm, Auto-Loot e Auto-Patrol ATIVADOS.")
                else:
                    self._click_debug(f"TRAVEL_PLAN: Viagem para {norm_new} concluída com sucesso.")
                self.travel_plan = None
            return

        self._click_debug(f"MAPA ALTERADO ({self.farm_map} -> {new_map}): Desativando Auto-Farm e Auto-Patrol!")
        self.auto_farm = False
        self.auto_patrol = False
        self.farm_map = None
        self.locked_mob_gid = None
        self.locked_combat_mob_gid = None
        if hasattr(getattr(self.feed, "world", None), "locked_combat_mob_gid"):
            self.feed.world.locked_combat_mob_gid = None
        self.locked_drop_gid = None
        self._clear_patrol_route()

    def _toggle_auto_farm(self):
        """Alterna estado do Auto-Farm e gerencia farm_map."""
        self.auto_farm = not self.auto_farm
        if self.auto_farm:
            self.farm_map = getattr(getattr(self.feed, "world", None), "map", None) or getattr(self, "current_map", "")
        else:
            self.locked_mob_gid = None
            self.locked_combat_mob_gid = None
            if hasattr(getattr(self.feed, "world", None), "locked_combat_mob_gid"):
                self.feed.world.locked_combat_mob_gid = None
            if not self.auto_patrol:
                self.farm_map = None
        print(f"[Auto-Farm] {'ON' if self.auto_farm else 'OFF'}", flush=True)

    def _toggle_auto_patrol(self):
        """Alterna estado do Auto-Patrol e gerencia rotas e farm_map."""
        self.auto_patrol = not self.auto_patrol
        if self.auto_patrol:
            self.farm_map = getattr(getattr(self.feed, "world", None), "map", None) or getattr(self, "current_map", "")
        else:
            self._clear_patrol_route()
            if not self.auto_farm:
                self.farm_map = None
        print(f"[Auto-Patrol / Roam] {'ON' if self.auto_patrol else 'OFF'}", flush=True)

    def _is_point_in_hud(self, sx, sy, margin=12):
        if margin:
            margin = max(12, margin)
        for hud in (self._hud_panel, self._hud_drawer):
            try:
                if hud and hud.winfo_viewable():
                    left, top = hud.winfo_rootx(), hud.winfo_rooty()
                    if (left - margin <= sx < left + hud.winfo_width() + margin
                            and top - margin <= sy < top + hud.winfo_height() + margin):
                        return True
            except tk.TclError:
                pass
        return False

    def _is_point_in_game_ui(self, sx, sy):
        """Verifica se (sx, sy) cai sobre a interface nativa do Ragnarok (chat, status, minimapa) ou sobre o HUD.
        Retorna True se o ponto estiver em zona proibida de clique.
        """
        if self._is_point_in_hud(sx, sy):
            return True

        # Regiões da interface nativa do Ragnarok.
        vp = getattr(self, "current_viewport", None)
        if vp is None:
            vp_obj = getattr(self.feed, "viewport", None)
            vp = vp_obj() if callable(vp_obj) else None
        if vp is not None:
            vx, vy, vw, vh = vp
            # Informações Básicas + ícones (topo esquerdo)
            if sx <= vx + 195 and sy <= vy + 425:
                return True
            # Barra de atalhos F1–F9 (topo)
            if sx <= vx + 555 and sy <= vy + 115:
                return True
            # Caixa de Chat (inferior esquerdo)
            if sx <= vx + 645 and sy >= vy + vh - 225:
                return True
            # Minimapa + Lista de Quests (topo direito)
            if sx >= vx + vw - 355 and sy <= vy + 445:
                return True
            # Pop-up central ativo: faixa central durante pop-ups modais
            now = time.monotonic()
            if now < getattr(self, "center_popup_until", 0.0):
                if (vx + vw * 0.32 <= sx <= vx + vw * 0.68) and (vy + vh * 0.32 <= sy <= vy + vh * 0.68):
                    return True
        return False

    def _side_step_to_reveal(self, target_cell, px, py):
        """Dá um passo lateral seguro de 2.2-3.2 células para deslocar a câmera quando o alvo está atrás do HUD."""
        if px is None or py is None or target_cell is None:
            return False
        curr_map = getattr(getattr(self.feed, "world", None), "map", None)
        gat = None
        if curr_map and hasattr(self.feed, "terrain") and self.feed.terrain:
            try:
                gat = self.feed.terrain.for_map(curr_map)
            except Exception:
                pass
        if not gat:
            return False

        vp_obj = getattr(self.feed, "viewport", None)
        target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        target_hwnd = self.get_game_hwnd(target_hwnd)
        snap = self.feed.current()
        if not snap:
            return False
        left, top, width, height = snap.viewport
        cam_pos = getattr(self.feed, "_cam", None) or (px, py)
        height_fn = getattr(gat, "get_elevation", gat.height_at)
        project = self.feed.camera.projector(snap.viewport, cam_pos, height_fn)

        tx, ty = target_cell
        dx = tx - px
        dy = ty - py
        dist = math.hypot(dx, dy)
        if dist < 0.1:
            perp_dx, perp_dy = 1.0, 0.0
        else:
            perp_dx, perp_dy = -dy / dist, dx / dist

        portal_blocked = self._get_portal_blocked_cells()
        candidates = []
        for sign in (1, -1):
            for step_r in (2.3, 2.8, 3.2):
                sx_cell = int(round(px + perp_dx * step_r * sign))
                sy_cell = int(round(py + perp_dy * step_r * sign))
                if not (0 <= sx_cell < gat.width and 0 <= sy_cell < gat.height):
                    continue
                if not gat.is_walkable(sx_cell, sy_cell):
                    continue
                if (sx_cell, sy_cell) in getattr(self, "temp_blocked_cells", {}) or (sx_cell, sy_cell) in portal_blocked:
                    continue
                u_cand, v_cand = project(sx_cell, sy_cell)
                if 0.15 <= u_cand <= 0.85 and 0.15 <= v_cand <= 0.85:
                    screen_x = left + int(u_cand * width)
                    screen_y = top + int(v_cand * height)
                    if not self._is_point_in_game_ui(screen_x, screen_y):
                        candidates.append((sx_cell, sy_cell, screen_x, screen_y))
                        break
            if candidates:
                break

        # Se não encontrou passo lateral, tenta célula caminhável segura próxima ao centro da tela na direção do alvo
        if not candidates and dist >= 0.1:
            dir_x = dx / dist
            dir_y = dy / dist
            for step_r in (2.0, 2.5, 3.0):
                cx_c = int(round(px + dir_x * step_r))
                cy_c = int(round(py + dir_y * step_r))
                if not (0 <= cx_c < gat.width and 0 <= cy_c < gat.height):
                    continue
                if not gat.is_walkable(cx_c, cy_c):
                    continue
                if (cx_c, cy_c) in getattr(self, "temp_blocked_cells", {}) or (cx_c, cy_c) in portal_blocked:
                    continue
                u_c, v_c = project(cx_c, cy_c)
                if 0.25 <= u_c <= 0.75 and 0.25 <= v_c <= 0.75:
                    sc_x = left + int(u_c * width)
                    sc_y = top + int(v_c * height)
                    if not self._is_point_in_game_ui(sc_x, sc_y):
                        candidates.append((cx_c, cy_c, sc_x, sc_y))
                        break

        if candidates:
            sx_c, sy_c, sc_x, sc_y = candidates[0]
            self._click_debug(f"PASSO DESLOCAMENTO CÂMERA: movendo char para ({sx_c},{sy_c}) para tirar alvo de trás da interface")
            return self.patrol_move_and_click(sc_x, sc_y, hwnd=target_hwnd)
        return False

    def _project_ground_cell(self, snapshot, gx, gy, sprite_z=0.0):
        """Helper para projetar qualquer coordenada do mapa na tela do jogo."""
        if not snapshot or not hasattr(self.feed, "camera"):
            return None, None, None, None
        viewport = snapshot.viewport
        left, top, width, height = viewport
        gat = None
        curr_map = getattr(getattr(self.feed, "world", None), "map", None)
        if curr_map and hasattr(self.feed, "terrain") and self.feed.terrain:
            try:
                gat = self.feed.terrain.for_map(curr_map)
            except Exception:
                pass
        height_fn = getattr(gat, "get_elevation", flat_height) if gat else flat_height
        cam_pos = getattr(self.feed, "_cam", None)
        if cam_pos is None and hasattr(self.feed, "world") and self.feed.world.player:
            cam_pos = self.feed.world.player.cell(time.perf_counter())
        if cam_pos is None:
            cam_pos = (gx, gy)
        proj = self.feed.camera.projector(viewport, cam_pos, height_fn)
        u, v = proj(gx, gy, sprite_z)
        sx = left + int(u * width)
        sy = top + int(v * height)
        return u, v, sx, sy

    def _reset_patrol(self, px, py):
        """Limpa patrulha e registra célula atual em visited_cells para escolher novo waypoint à frente."""
        self.current_patrol_path = []
        self.patrol_path = []
        self.current_waypoint = None
        self.patrol_waypoint = None
        self._last_patrol_click_target = None
        if px is not None and py is not None:
            cur = (int(round(px)), int(round(py)))
            if not self.visited_cells or self.visited_cells[-1] != cur:
                self.visited_cells.append(cur)
                if len(self.visited_cells) > 80:
                    self.visited_cells.pop(0)

    def _on_pickup_ack(self, item_id, cnt, fail):
        if fail == 0 and item_id and cnt > 0:
            item_name = self.names.get("drop", {}).get(item_id, f"Item #{item_id}")
            with self._session_items_lock:
                self._session_items[item_name] += cnt
                self._session_item_ids[item_name].add(item_id)

    def _click_debug(self, msg):
        """Imprime log de diagnóstico no terminal, limitado a 1 linha por segundo."""
        now = time.monotonic()
        if now - self._last_debug_log >= 1.0:
            print(f"[ClickDebug] {msg}", flush=True)
            self._last_debug_log = now

    def _passes_target_filter(self, kind, catalog_id, name, *, bypass_whitelist=False):
        mode = getattr(self, f"_{kind}_filter_mode")
        if mode == "Todos" or (mode == "Whitelist" and bypass_whitelist):
            return True
        filter_key = "whitelist" if mode == "Whitelist" else "blacklist"
        ids = getattr(self, f"{kind}_{filter_key}")
        names = getattr(self, f"{kind}_{filter_key}_names")
        matched = catalog_id in ids or f"#{catalog_id}" in names or any(word in name for word in names)
        return matched if mode == "Whitelist" else not matched

    def _eligible_drops(self, snapshot, position, entities, now):
        candidates = []
        for (kind, gid), drop in entities:
            if kind != "drop" or drop.ownership not in ("MINE", "FREE"):
                continue
            if max(drop.ignore_until, drop.public_at, drop.unreachable_until) > now:
                continue
            name = self.names.get("drop", {}).get(drop.catalog_id, "").strip().lower()
            tier = self.classify_item_tier(drop.catalog_id, name)
            if tier == 3 and self._skip_hud_tier3(snapshot.weight_ratio * 100):
                continue
            if self._passes_target_filter("drop", drop.catalog_id, name, bypass_whitelist=tier == 1):
                candidates.append((tier, math.dist(position, drop.track.target), gid, drop))
        candidates.sort(key=lambda candidate: (
            candidate[0] != 1, candidate[1],
            math.dist(self.last_kill_pos, candidate[3].track.target) if self.last_kill_pos else 0,
            candidate[0], candidate[2],
        ))
        return candidates

    def _try_auto_loot(self, snapshot, position, drops, *, stand_first=False, direct_only=False):
        selected = {gid for _, _, gid, _ in drops}
        if self.locked_drop_gid not in selected:
            self.locked_drop_gid = None
            if self._engagement and self._engagement["key"][0] == "drop":
                self._engagement = None
        if not drops:
            return False
        if stand_first:
            return True
        if self.locked_drop_gid is not None:
            drops = sorted(drops, key=lambda entry: (entry[0] != 1, entry[2] != self.locked_drop_gid))
        for _, _, gid, drop in drops:
            self.locked_drop_gid = gid
            if self._smart_engage_target((gid, drop), "drop", snapshot, position, direct_only=direct_only):
                return True
        self.locked_drop_gid = None
        return False

    @staticmethod
    def _path_cost(path):
        return sum(14 if a[0] != b[0] and a[1] != b[1] else 10 for a, b in zip(path, path[1:]))

    def _approach_path(self, gat, start, target_cell, blocked, *, ring=False, ground_allowed=None):
        if gat is None:
            return []
        if not ring:
            path = gat.find_path(start, target_cell, blocked_cells=blocked)
            if path and path[-1] == target_cell:
                return path
            if start not in blocked and math.dist(start, target_cell) <= 2.2 and self._has_walkable_line(start, target_cell, gat):
                return [start]
        return gat.find_path(
            start, None, blocked_cells=blocked, max_iterations=3500,
            goal_test=lambda cell: (
                cell != start and cell not in blocked
                and 1 <= math.dist(cell, target_cell) <= 2.2
                and self._has_walkable_line(cell, target_cell, gat)
                and (ground_allowed is None or ground_allowed(cell))
            ),
        )

    def _smart_engage_target(self, target, kind, snapshot, position, *, direct_only=False):
        gid, entity = target
        world = self.feed.world
        now = time.monotonic()
        key = (kind, gid)
        with world.lock:
            live = world.entities.get(key)
            if (not live or world.map != snapshot.map or live.unreachable_until > now
                    or (kind == "drop" and (live.ownership == "LOCKED" or live.created_at != entity.created_at))):
                return False
            if kind == "mob":
                world.farm_target_gid = gid
        if not self._game_is_foreground() or self._manual_mouse_button_pressed():
            return True
        if self._click_in_progress:
            return True
        if time.perf_counter() - getattr(self, "_last_click_ts", 0.0) < 0.35:
            return True
        hwnd = self.get_game_hwnd()
        if not hwnd:
            return True
        if world.player_is_sitting:
            self._request_survival_posture(False, now)
            return True

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(world.map) if terrain and world.map else None
        cell = live.track.cell()
        distance = math.dist(position, cell)
        close = distance <= (2.5 if kind == "mob" else 2.2)
        walking = world._is_player_walking()
        state = self._engagement
        if state is None or state["key"] != key:
            state = self._engagement = {
                "key": key, "phase": None, "ring_failures": 0, "micro_steps": 0,
                "ring_required": False, "ring_goal": None, "micro_cell": None, "failed_steps": set(),
            }

        if kind == "mob" and now - world.mobs_damaged_by_me.get(gid, 0.0) < 1.2:
            state.update(phase=None, ring_required=False, ring_goal=None, micro_steps=0, ring_failures=0)
            state["failed_steps"].clear()
            self.active_nav_path = None
            return True

        def pause_target(seconds):
            with world.lock:
                current = world.entities.get(key)
                if current and world.map == snapshot.map:
                    current.unreachable_until = now + seconds
                    if kind == "mob" and world.locked_combat_mob_gid == gid:
                        world.locked_combat_mob_gid = None
            self._engagement = None
            self.active_nav_path = None
            return False

        def ring_failed(step=None):
            state["ring_failures"] += 1
            if step:
                state["failed_steps"].add(step)
            state["ring_goal"] = None
            if state["ring_failures"] >= 2:
                return pause_target(10.0)
            return True

        def click(point, phase, delay):
            clicked_at = time.monotonic()
            if not self.patrol_move_and_click(*point, hwnd=hwnd):
                return True
            state.update(phase=phase, deadline=clicked_at + delay, origin=position,
                         click_point=point, clicked_at=clicked_at)
            self._click_debug(f"{kind.upper()} #{gid}: {phase} em ({point[0]}, {point[1]})")
            if kind == "drop":
                self.last_loot_click = clicked_at
            else:
                self.last_farm_click = clicked_at
            return True

        def screen_point(u, v):
            left, top, width, height = snapshot.viewport
            x, y = self._projection_pixels(u, v, width, height)
            point = left + x, top + y
            if (0.05 <= u <= 0.95 and 0.05 <= v <= 0.95
                    and not self._is_point_in_game_ui(*point)
                    and not self._is_point_in_hud(*point, margin=10)):
                return point
            return None

        def ground_point(ground):
            u, v, _, _ = self._project_ground_cell(snapshot, *ground)
            return screen_point(u, v) if u is not None else None

        phase = state["phase"]
        need_micro = False
        if phase and self._touch_click_aborted(state["click_point"], state["clicked_at"]):
            state["phase"] = phase = None
        if phase:
            if now < state["deadline"]:
                return True
            if walking:
                return True
            moved = math.dist(position, state["origin"]) >= 0.3
            state["phase"] = None
            if phase == "direct_far" and not moved:
                state["ring_required"] = True
            elif phase == "ring" and not moved:
                return ring_failed(state["ring_step"])
            elif phase == "direct_close":
                # Atacar um mob adjacente não exige deslocamento do personagem.
                need_micro = kind == "drop"
            elif phase == "micro" and not moved:
                need_micro = True

        point = None
        if close or direct_only or gat is None:
            item = next((obj for obj in snapshot.objects if obj.key == f"{kind}:{gid}"), None)
            if item:
                point = screen_point(item.u, item.v)

        start = tuple(round(coord) for coord in position)
        target_cell = tuple(round(coord) for coord in cell)
        portals = self._get_portal_blocked_cells(radius=0, allow_current_goal=False)
        if target_cell in portals:
            point = None
        if state["ring_goal"] is not None and close:
            state.update(ring_goal=None, ring_required=False)
        direct_line = self._has_walkable_line(position, cell, gat)
        if gat and direct_line:
            direct_line = abs(gat.get_elevation(*position) - gat.get_elevation(*cell)) <= 14
        if kind == "mob" and close and point and gid == world.locked_combat_mob_gid:
            direct_line = True
            state["ring_required"] = False
        if direct_only:
            if not point or not direct_line or distance > (2.2 if kind == "drop" else 2.5) or need_micro:
                return False
        elif need_micro or (close and not point and not state["ring_required"]):
            if state["micro_steps"] >= 3:
                return pause_target(4.0)
            blocked = self._get_all_blocked_cells() | portals | {target_cell}
            with world.lock:
                occupied = {tuple(round(coord) for coord in other.track.cell())
                            for other in world.entities.values()}
            options = []
            if gat:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ground = (start[0] + dx, start[1] + dy)
                    if ground in blocked or ground in occupied or not gat.is_walkable(*ground):
                        continue
                    screen = ground_point(ground)
                    if screen:
                        options.append((ground == state["micro_cell"], abs(math.dist(ground, cell) - distance), ground, screen))
            if options:
                _, _, ground, screen = min(options)
                state["micro_cell"] = ground
                state["micro_steps"] += 1
                return click(screen, "micro", 0.42)
            state["ring_required"] = True

        if point and direct_line and (direct_only or not state["ring_required"]):
            self.active_nav_path = None
            if kind == "drop":
                with world.lock:
                    current = world.entities.get(key)
                    if not current or current.ownership == "LOCKED" or current.created_at != live.created_at:
                        return False
                    world.pending_pickup = (gid, now)
            delay = (0.38 if kind == "drop" else 0.45) if close else 0.42
            return click(point, "direct_close" if close else "direct_far", delay)

        if walking:
            return True
        state["ring_required"] = True
        blocked = self._get_all_blocked_cells() | portals | {target_cell} | state["failed_steps"]
        goal = state["ring_goal"]
        if goal and (not 1 <= math.dist(goal, target_cell) <= 2.2 or goal in blocked):
            goal = None
        path = gat.find_path(start, goal, blocked_cells=blocked) if gat and goal else []
        if not path or path[-1] != goal:
            path = self._approach_path(
                gat, start, target_cell, blocked, ring=True,
                ground_allowed=lambda ground: math.dist(start, ground) > 6 or ground_point(ground),
            )
        if len(path) < 2:
            return ring_failed()

        state["ring_goal"] = path[-1]
        self.active_nav_path = path
        self.active_nav_color = "#F87171" if kind == "mob" else "#FBBF24"
        dx, dy = path[1][0] - start[0], path[1][1] - start[1]
        segment = []
        for index, ground in enumerate(path[1:7], start=1):
            if ground != (start[0] + dx * index, start[1] + dy * index):
                break
            segment.append(ground)
        for ground in reversed(segment):
            screen = ground_point(ground)
            if screen:
                state["ring_step"] = path[1]
                return click(screen, "ring", 0.42)
        return ring_failed(path[1])

    def get_game_hwnd(self, hwnd=None):
        """Retorna o handle válido do jogo ou redetecta automaticamente se IsWindow retornar 0."""
        if hwnd and self.user32.IsWindow(hwnd):
            self._remember_game_window(hwnd)
            return hwnd
        vp_obj = getattr(self.feed, "viewport", None)
        cached_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        if cached_hwnd and self.user32.IsWindow(cached_hwnd):
            self._remember_game_window(cached_hwnd)
            return cached_hwnd

        # IsWindow(game_hwnd) retornou 0 (ou hwnd inexistente): re-detecta automaticamente a janela 'Ragnarok' (Ragexe.exe) em tempo real
        new_hwnd, new_pid, new_title = find_ragnarok_window(getattr(self, "_ragexe_pid", None))
        if new_hwnd and self.user32.IsWindow(new_hwnd):
            self._remember_game_window(new_hwnd)
            self._ragexe_pid = new_pid
            if vp_obj:
                vp_obj.handle = new_hwnd
            print(f"[Janela] Ragnarok redetectado em tempo real: hwnd=0x{new_hwnd:X} (PID={new_pid}, título='{new_title}')", flush=True)
            return new_hwnd
        return None

    def is_valid_ragexe_hwnd(self, hwnd):
        if not hwnd or not u32.IsWindow(hwnd):
            return False
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            return bool(pid.value and psutil.Process(pid.value).name().casefold() == "ragexe.exe")
        except psutil.Error:
            return False

    def _move_and_click_sync(self, sx, sy, hwnd):
        # Os controles da Kafra precisam esperar o clique anterior antes de avançar.
        deadline = time.perf_counter() + 2.0
        while (self._click_in_progress
               or time.perf_counter() - getattr(self, "_last_click_ts", 0.0) < 0.35):
            if time.perf_counter() >= deadline:
                raise ValueError("tempo limite aguardando o clique anterior.")
            time.sleep(0.01)
        completed_at = getattr(self, "_last_click_end_ts", 0.0)
        if not self._schedule_game_click(sx, sy, hwnd):
            raise ValueError("não foi possível iniciar o clique no controle da Kafra.")
        while self._click_in_progress:
            if time.perf_counter() >= deadline:
                raise ValueError("tempo limite aguardando o clique na Kafra.")
            time.sleep(0.01)
        if getattr(self, "_last_click_end_ts", 0.0) == completed_at:
            raise ValueError("o clique no controle da Kafra foi interrompido.")

    def _move_pointer_with_touch(self, sx, sy):
        ok_init, err_init = ensure_touch_injection()
        contact = POINTER_TOUCH_INFO()
        contact.pointerInfo.pointerType = PT_TOUCH  # 2
        contact.pointerInfo.pointerId = 0
        contact.pointerInfo.ptPixelLocation = wintypes.POINT(sx, sy)
        contact.touchFlags = 0
        contact.touchMask = 0

        contact.pointerInfo.pointerFlags = 0x00010006  # POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        ctypes.set_last_error(0)
        r_down = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
        err_down = ctypes.get_last_error()
        time.sleep(0.015)

        contact.pointerInfo.pointerFlags = 0x00040000  # POINTER_FLAG_UP
        ctypes.set_last_error(0)
        r_up = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
        err_up = ctypes.get_last_error()
        return ok_init, err_init, r_down, err_down, r_up, err_up

    def _schedule_game_click(self, sx, sy, game_hwnd=None):
        """Agenda o clique do jogo e mantém o estado da automação."""
        now = time.perf_counter()
        if getattr(self, "_click_in_progress", False):
            return False
        last_ts = getattr(self, "_last_click_ts", 0.0)
        if getattr(self, "_click_busy", False) and (now - getattr(self, "_click_start_ts", 0.0) < 0.25):
            return False
        if now - last_ts < 0.35:
            return False

        # Valida hwnd: nunca aceitar a janela do overlay/HUD (somente Ragexe.exe)
        hwnd = self.get_game_hwnd(game_hwnd or getattr(self, "game_hwnd", None))
        if not hwnd or not self.is_valid_ragexe_hwnd(hwnd):
            self._click_debug("move_and_click: janela do Ragexe indisponível")
            return False
        self.game_hwnd = hwnd
        game_window = self._remember_game_window(hwnd)
        root_hwnd = game_window[1]

        sx, sy = int(sx), int(sy)

        def _do_smooth_click():
            sent = False
            try:
                sent = win32_input.move_and_click(
                    sx, sy, hwnd, root_hwnd,
                    is_focused=lambda: self._is_game_focused(game_window=game_window),
                    debug=self._click_debug, log=log_click_file,
                    is_admin=getattr(self, "is_admin", False),
                )
            except Exception as exc:
                print(f"[ClickDebug] move_and_click erro: {exc}", flush=True)
            finally:
                finished_at = time.perf_counter()
                self._last_click_ts = self.last_bot_click_ts = finished_at
                if sent:
                    self._last_click_end_ts = finished_at
                self._last_touch_result = ((int(sx), int(sy)), time.monotonic(), sent)
                win32_input.finish_bot_click()
                self._click_busy = False
                self._click_in_progress = False

        started = False
        try:
            self._click_in_progress = True
            self._click_busy = True
            self._click_start_ts = now
            self._last_click_ts = self.last_bot_click_ts = now
            win32_input.begin_bot_click()
            threading.Thread(target=_do_smooth_click, daemon=True).start()
            started = True
        finally:
            if not started:
                win32_input.finish_bot_click()
                self._click_busy = self._click_in_progress = False
        return True

    def _touch_click_aborted(self, point, clicked_at):
        result = getattr(self, "_last_touch_result", None)
        return (result is not None
                and not result[2] and result[1] >= clicked_at and result[0] == point)

    def _smooth_move_to(self, sx: int, sy: int):
        if self._click_in_progress:
            return
        self._click_in_progress = True
        try:
            win32_input.move_cursor_relative(sx, sy)
        finally:
            self._click_in_progress = False

    def _send_game_key(self, vk_code: int, *, restore_focus=False) -> bool:
        hwnd = self.get_game_hwnd()
        if not hwnd or not self.is_valid_ragexe_hwnd(hwnd):
            return False
        return win32_input._send_game_key(hwnd, vk_code, restore_focus=restore_focus)

    def _test_sit_key(self):
        sent = self._send_game_key(0x28, restore_focus=True)
        status = "↓ enviada ao jogo." if sent else "Falha ao enviar ↓ ao jogo."
        self._hud_sit_test_status.configure(text=status)
        print(f"[Teste Sentar] {status}", flush=True)

    def _send_menu_selection(self, index: int):
        """Após 220ms de estabilização desde last_ts, envia VK_DOWN index vezes (intervalo 75ms) e VK_RETURN."""
        def _worker():
            self._click_debug(f"[ClickDebug] _send_menu_selection: iniciando para opção #{index}")
            dialog = getattr(getattr(self.feed, "world", None), "npc_dialog", {})
            last_ts = dialog.get("last_ts", 0.0)
            now_perf = time.perf_counter()
            elapsed = now_perf - last_ts
            if elapsed < 0.220:
                time.sleep(0.220 - elapsed)

            VK_DOWN = 0x28
            VK_RETURN = 0x0D
            for i in range(index):
                self._click_debug(f"[ClickDebug] Enviando tecla VK_DOWN ({i + 1}/{index})")
                self._send_game_key(VK_DOWN)
                time.sleep(0.075)

            time.sleep(0.075)
            self._click_debug(f"[ClickDebug] Enviando tecla VK_RETURN (Enter) para confirmar opção #{index}")
            self._send_game_key(VK_RETURN)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _navigate_menu_keyboard(self, option_index: int):
        """Compatibilidade: navega pelo menu delegando para _send_menu_selection."""
        self._send_menu_selection(option_index)

    def patrol_move_and_click(self, sx, sy, *, hwnd=None):
        """Agenda o movimento e clique usados pelo Patrol em coordenadas de tela."""
        return self._dispatch_game_click(hwnd, sx, sy, method="touch_postmsg")

    def _send_click(self, hwnd, screen_x, screen_y, test_mode=False, **_kwargs):
        return self._dispatch_game_click(hwnd, screen_x, screen_y,
                                         method=self._click_method, test_mode=test_mode)

    def _dispatch_game_click(self, hwnd, screen_x, screen_y, *, method, test_mode=False):
        """Envia clique na posição física (screen_x, screen_y)."""
        # ── Proteção de Concorrência: previne cliques sobrepostos entre ticks ──
        if not test_mode and getattr(self, "_click_in_progress", False):
            self._click_debug(f"CLIQUE IGNORADO (concorrência): ({int(screen_x)},{int(screen_y)})")
            return

        sx = int(screen_x)
        sy = int(screen_y)

        # Validação de limites: descarta cliques fora da área de tela
        sw = getattr(self, "_screen_w", 1920)
        sh = getattr(self, "_screen_h", 1080)
        if not (0 <= sx < sw and 0 <= sy < sh):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) fora da tela ({sw}x{sh})")
            return

        # Zona Proibida do Retângulo do HUD (com margem de 10px): nunca clique ali
        if not test_mode and self._is_point_in_hud(sx, sy, margin=10):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está dentro do HUD (zona proibida)")
            return

        # Zona Proibida da Interface Nativa do Jogo (chat, status, minimapa)
        if not test_mode and self._is_point_in_game_ui(sx, sy):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está sobre a UI nativa do Ragnarok")
            return

        if not test_mode and self._action_click_used is not None:
            if self._action_click_used:
                return False
            self._action_click_used = True

        owns_click_lock = not test_mode and method != "touch_postmsg"
        if owns_click_lock:
            self._click_in_progress = True
        try:
            # Registra clique no CameraTracker para auto-calibração PnP
            if not test_mode and hasattr(self, "feed") and hasattr(self.feed, "camera"):
                vp = getattr(self, "current_viewport", None) or (self.feed.viewport() if callable(getattr(self.feed, "viewport", None)) else None)
                if vp and hasattr(self.feed.camera, "record_click"):
                    self.feed.camera.record_click(sx, sy, vp)

            if method == "touch_postmsg":
                ensure_input_desktop()
                return self._schedule_game_click(sx, sy, self.get_game_hwnd(hwnd))

            # ── 0. Hardware USB HID (Serial) Bypass se conectado ────────────────
            if not test_mode and hasattr(self, "serial_hid") and self.serial_hid and self.serial_hid.is_connected:
                ok_serial = self.serial_hid.send_click(sx, sy)
                if ok_serial:
                    self._click_debug(f"CLIQUE em ({sx},{sy}) via Serial HID ({self.serial_hid.port}): Sucesso")
                    return

            # ── 0.1 Driver Kernel HID (interception.dll) ─────────────────────
            if not test_mode and hasattr(self, "interception") and self.interception and self.interception.is_available:
                ok_inter = self.interception.send_click(sx, sy, self._screen_w, self._screen_h)
                if ok_inter:
                    self._click_debug(f"CLIQUE em ({sx},{sy}) via Interception Driver: Sucesso [Kernel Filter Bypass]")
                    return

            # Revalida/redetecta a janela do jogo
            hwnd = self.get_game_hwnd(hwnd)
            ensure_input_desktop()

            # ── Coordenadas normalizadas para Virtual Screen (MOUSEEVENTF_ABSOLUTE) ──
            SM_XVIRTUALSCREEN = 76
            SM_YVIRTUALSCREEN = 77
            SM_CXVIRTUALSCREEN = 78
            SM_CYVIRTUALSCREEN = 79
            v_left = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            v_top  = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            v_w    = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or self._screen_w
            v_h    = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or self._screen_h
            norm_x = max(0, min(65535, int(((sx - v_left) * 65535) / max(1, v_w - 1))))
            norm_y = max(0, min(65535, int(((sy - v_top)  * 65535) / max(1, v_h - 1))))
            MOUSEEVENTF_VIRTUALDESK = 0x4000
            abs_flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

            is_admin_now = bool(ctypes.windll.shell32.IsUserAnAdmin())

            if method == "touch_win32":
                # ── Método touch_win32: InjectTouchInput para posicionar → 30ms → SendInput clique ──
                # InjectTouchInput serve APENAS para mover o ponteiro do sistema sem bloquear o cursor.
                # Depois, SendInput exclusivo (LEFTDOWN → 55ms → LEFTUP).
                ok_init, err_init, r_touch_down, err_touch_down, r_touch_up, err_touch_up = (
                    self._move_pointer_with_touch(sx, sy)
                )

                # Captura cursor antes
                pt_before = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt_before))

                # SendInput LEFTDOWN (40 bytes, exclusivo)
                inp_down = INPUT(type=INPUT_MOUSE)
                inp_down.mi.dx = norm_x
                inp_down.mi.dy = norm_y
                inp_down.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN | abs_flags
                ctypes.windll.kernel32.SetLastError(0)
                r_si_down = int(u32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT)))
                err_si_down = ctypes.get_last_error()

                time.sleep(0.055)

                # SendInput LEFTUP (40 bytes, exclusivo)
                inp_up = INPUT(type=INPUT_MOUSE)
                inp_up.mi.dx = norm_x
                inp_up.mi.dy = norm_y
                inp_up.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP | abs_flags
                ctypes.windll.kernel32.SetLastError(0)
                r_si_up = int(u32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT)))
                err_si_up = ctypes.get_last_error()

                pt_after = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt_after))

                fg_hwnd = u32.GetForegroundWindow()
                fg_pid_v = wintypes.DWORD()
                u32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid_v))

                msg = (
                    f"touch_win32 ({sx},{sy}): "
                    f"is_admin={is_admin_now} fg_pid={fg_pid_v.value} "
                    f"cursor_antes=({pt_before.x},{pt_before.y}) cursor_depois=({pt_after.x},{pt_after.y}) "
                    f"TouchInit={ok_init}(err={err_init}) "
                    f"TouchDown={r_touch_down}(err={err_touch_down}) TouchUp={r_touch_up}(err={err_touch_up}) "
                    f"SI_DOWN={r_si_down}(err={err_si_down}) SI_UP={r_si_up}(err={err_si_up})"
                )
                log_click_file(msg)
                if test_mode:
                    for line in msg.split(" "):
                        print(f"    [Passo] {line}", flush=True)

            elif method == "win32":
                # ── Método win32: SetCursorPos + micro-deslocamento relativo + 30ms + SendInput ──
                # Foca janela do jogo se necessário
                if hwnd and u32.IsWindow(hwnd):
                    fg = u32.GetForegroundWindow()
                    if not fg or int(fg) != int(hwnd):
                        u32.SetForegroundWindow(hwnd)
                        u32.BringWindowToTop(hwnd)

                # Posiciona cursor
                ctypes.windll.kernel32.SetLastError(0)
                ok_pos = bool(u32.SetCursorPos(sx, sy))
                err_pos = ctypes.get_last_error()

                # Micro-deslocamento relativo (força atualização de janela hover)
                inp_rel1 = INPUT(type=INPUT_MOUSE)
                inp_rel1.mi.dx = 1
                inp_rel1.mi.dy = 0
                inp_rel1.mi.dwFlags = MOUSEEVENTF_MOVE  # relativo (sem ABSOLUTE)
                u32.SendInput(1, ctypes.byref(inp_rel1), ctypes.sizeof(INPUT))

                inp_rel2 = INPUT(type=INPUT_MOUSE)
                inp_rel2.mi.dx = -1
                inp_rel2.mi.dy = 0
                inp_rel2.mi.dwFlags = MOUSEEVENTF_MOVE
                u32.SendInput(1, ctypes.byref(inp_rel2), ctypes.sizeof(INPUT))

                pt_before = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt_before))

                time.sleep(0.030)

                # SendInput LEFTDOWN exclusivo
                inp_down = INPUT(type=INPUT_MOUSE)
                inp_down.mi.dx = norm_x
                inp_down.mi.dy = norm_y
                inp_down.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN | abs_flags
                ctypes.windll.kernel32.SetLastError(0)
                r_si_down = int(u32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT)))
                err_si_down = ctypes.get_last_error()

                time.sleep(0.055)

                # SendInput LEFTUP exclusivo
                inp_up = INPUT(type=INPUT_MOUSE)
                inp_up.mi.dx = norm_x
                inp_up.mi.dy = norm_y
                inp_up.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP | abs_flags
                ctypes.windll.kernel32.SetLastError(0)
                r_si_up = int(u32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT)))
                err_si_up = ctypes.get_last_error()

                pt_after = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt_after))

                fg_hwnd = u32.GetForegroundWindow()
                fg_pid_v = wintypes.DWORD()
                u32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid_v))

                msg = (
                    f"win32 ({sx},{sy}): "
                    f"is_admin={is_admin_now} fg_pid={fg_pid_v.value} "
                    f"cursor_antes=({pt_before.x},{pt_before.y}) cursor_depois=({pt_after.x},{pt_after.y}) "
                    f"SetCursorPos={ok_pos}(err={err_pos}) "
                    f"SI_DOWN={r_si_down}(err={err_si_down}) SI_UP={r_si_up}(err={err_si_up})"
                )
                log_click_file(msg)
                if test_mode:
                    for line in msg.split(" "):
                        print(f"    [Passo] {line}", flush=True)

            elif method == "postmsg":
                self._postmsg_click(hwnd, sx, sy, test_mode)

            else:
                # Fallback para métodos legados (absolute, touch, touch_hybrid)
                # Mantém comportamento anterior para compatibilidade
                self._send_click_legacy(hwnd, sx, sy, test_mode=test_mode,
                                         norm_x=norm_x, norm_y=norm_y, abs_flags=abs_flags,
                                         is_admin_now=is_admin_now)
        finally:
            if owns_click_lock:
                self._click_in_progress = False

    def _send_click_legacy(self, hwnd, sx, sy, test_mode=False, norm_x=0, norm_y=0, abs_flags=0, is_admin_now=False):
        """Fallback para métodos de clique legados (absolute, touch, touch_hybrid)."""
        method = getattr(self, "_click_method", "touch_win32").lower()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ensure_input_desktop()

        vp_obj = getattr(self.feed, "viewport", None)
        vp = vp_obj() if callable(vp_obj) else None
        if vp:
            cx = max(0, min(vp[2], sx - vp[0]))
            cy = max(0, min(vp[3], sy - vp[1]))
        elif hwnd and u32.IsWindow(hwnd):
            pt = wintypes.POINT(sx, sy)
            u32.ScreenToClient(hwnd, ctypes.byref(pt))
            cx, cy = max(0, pt.x), max(0, pt.y)
        else:
            cx, cy = sx, sy

        h_str = f"0x{hwnd:X}" if hwnd else "None"
        ok_pos = False
        r_down_si = r_up_si = 0
        err_si_down = err_si_up = 0

        if method in ("absolute", "win32"):
            if hwnd and u32.IsWindow(hwnd):
                fg = u32.GetForegroundWindow()
                if not fg or int(fg) != int(hwnd):
                    u32.SetForegroundWindow(hwnd)
                    u32.BringWindowToTop(hwnd)

            k32.SetLastError(0)
            ok_pos = bool(u32.SetCursorPos(sx, sy))
            err_pos = ctypes.get_last_error()

            inp_move = INPUT(type=INPUT_MOUSE)
            inp_move.mi.dx = norm_x
            inp_move.mi.dy = norm_y
            inp_move.mi.dwFlags = MOUSEEVENTF_MOVE | abs_flags
            u32.SendInput(1, ctypes.byref(inp_move), ctypes.sizeof(INPUT))

            time.sleep(0.025)

            r_down_pm = 0
            err_pm_down = 0
            if hwnd and u32.IsWindow(hwnd):
                lparam_click = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                k32.SetLastError(0)
                u32.PostMessageW(hwnd, 0x0200, 0, lparam_click)
                k32.SetLastError(0)
                r_down_pm = int(u32.PostMessageW(hwnd, 0x0201, 0x0001, lparam_click))
                err_pm_down = ctypes.get_last_error()

            inp_down = INPUT(type=INPUT_MOUSE)
            inp_down.mi.dx = norm_x
            inp_down.mi.dy = norm_y
            inp_down.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN | abs_flags
            k32.SetLastError(0)
            r_down_si = int(u32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT)))
            err_si_down = ctypes.get_last_error()

            time.sleep(0.050)

            inp_up = INPUT(type=INPUT_MOUSE)
            inp_up.mi.dx = norm_x
            inp_up.mi.dy = norm_y
            inp_up.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP | abs_flags
            k32.SetLastError(0)
            r_up_si = int(u32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT)))
            err_si_up = ctypes.get_last_error()

            r_up_pm = 0
            err_pm_up = 0
            if hwnd and u32.IsWindow(hwnd):
                lparam_click = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                k32.SetLastError(0)
                r_up_pm = int(u32.PostMessageW(hwnd, 0x0202, 0, lparam_click))
                err_pm_up = ctypes.get_last_error()

            log_click_file(
                f"{method} ({sx},{sy}) [legado]: "
                f"is_admin={is_admin_now} Pos={ok_pos}(err={err_pos}) "
                f"SI=(D:{r_down_si},U:{r_up_si},err:{err_si_down}) "
                f"PM=(D:{r_down_pm},U:{r_up_pm},err:{err_pm_down})"
            )

        elif method in ("touch", "touch_hybrid"):
            ok_init, err_init = ensure_touch_injection()
            ok_pos = bool(u32.SetCursorPos(sx, sy))
            err_pos = ctypes.get_last_error()

            contact = POINTER_TOUCH_INFO()
            contact.pointerInfo.pointerType = PT_TOUCH
            contact.pointerInfo.pointerId = 0
            contact.pointerInfo.ptPixelLocation.x = sx
            contact.pointerInfo.ptPixelLocation.y = sy
            contact.touchFlags = 0
            contact.touchMask = 0

            contact.pointerInfo.pointerFlags = 0x00010006
            k32.SetLastError(0)
            r_down = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
            err_down = ctypes.get_last_error()
            time.sleep(0.045)

            contact.pointerInfo.pointerFlags = 0x00020006
            k32.SetLastError(0)
            r_update = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
            time.sleep(0.045)

            contact.pointerInfo.pointerFlags = 0x00040000
            k32.SetLastError(0)
            r_up = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
            err_up = ctypes.get_last_error()

            log_click_file(
                f"touch_legacy [{method}] ({sx},{sy}): "
                f"is_admin={is_admin_now} Init={ok_init}(err={err_init}) "
                f"D={r_down}(err={err_down}) U={r_up}(err={err_up})"
            )

    def _try_auto_farm(self, snapshot, position, entities, *, defense=False, direct_only=False):
        world = self.feed.world
        now = time.monotonic()
        combat_gid = world.locked_combat_mob_gid
        self.locked_combat_mob_gid = combat_gid
        warps = self._get_warp_positions()
        candidates = []
        for (kind, gid), mob in entities:
            if kind != "mob" or mob.unreachable_until > now:
                continue
            cell = mob.track.cell()
            distance = math.dist(position, cell)
            active = (gid == combat_gid and distance <= 2.5) or self._is_active_threat(world, gid, mob, position, now)
            if defense:
                if not active and not (distance <= 2.5 and self._is_mob_hostile(world, mob)):
                    continue
                if direct_only and distance > 2.5:
                    continue
            elif distance > 22 and gid != combat_gid:
                continue
            if mob.engaged_by_other_until > now and gid != combat_gid and not active:
                continue
            if not defense and not active and any(math.dist(cell, warp) <= 6.5 for warp in warps):
                continue
            name = self.names.get("mob", {}).get(mob.catalog_id, "").strip().lower()
            if not defense and gid != combat_gid and not active:
                if not self._passes_target_filter("mob", mob.catalog_id, name):
                    continue
                if getattr(self, "_monster_filter", "").lower() not in name:
                    continue
            candidates.append((gid, mob, cell, distance, active))

        if not candidates:
            if not defense:
                self.locked_mob_gid = None
            return False

        if not defense:
            for locked_gid in dict.fromkeys((combat_gid, self.locked_mob_gid)):
                locked = next((mob for gid, mob, _, _, _ in candidates if gid == locked_gid), None)
                if locked is not None:
                    self.locked_mob_gid = locked_gid
                    if self._smart_engage_target((locked_gid, locked), "mob", snapshot, position):
                        return True
            candidates = [candidate for candidate in candidates
                          if candidate[0] not in (combat_gid, self.locked_mob_gid)
                          and candidate[1].unreachable_until <= now]

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(world.map) if terrain and world.map else None
        start = tuple(round(coord) for coord in position)
        blocked = self._get_all_blocked_cells()
        ranked = []
        for gid, mob, cell, distance, active in candidates:
            locked = gid == combat_gid or gid == self.locked_mob_gid
            target_cell = tuple(round(coord) for coord in cell)
            if gat and not locked and not direct_only and not (defense and distance <= 2.5):
                path = self._approach_path(gat, start, target_cell, blocked)
                if not path:
                    continue
                cost = self._path_cost(path)
            else:
                cost = distance * 10
            ranked.append((defense and not active, gid != combat_gid, not locked, cost, gid, mob))

        for _, _, _, _, gid, mob in sorted(ranked):
            self.locked_mob_gid = gid
            if self._smart_engage_target((gid, mob), "mob", snapshot, position, direct_only=direct_only):
                return True
        if not defense:
            self.locked_mob_gid = None
        return False

    def _position_render_window(self, viewport):
        previous = self._render_layout
        if viewport is None:
            if previous[4]:
                # SWP_HIDEWINDOW | SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOMOVE | SWP_NOSIZE
                if not self.user32.SetWindowPos(self.handle, None, 0, 0, 0, 0, 0x0097):
                    return False
                self._render_layout = (*previous[:4], False)
                self.canvas_pool.hide_all()
            return True

        layout = (*viewport, True)
        restack = (self._hud_restack_render or not previous[4]
                   or not u32.GetWindowLongW(self.handle, -20) & 0x0008)  # WS_EX_TOPMOST
        if layout == previous and not restack:
            return True
        flags = 0x0010 if restack else 0x0014  # SWP_NOACTIVATE, opcionalmente SWP_NOZORDER
        if not previous[4]:
            flags |= 0x0040  # SWP_SHOWWINDOW
        if layout[:4] == previous[:4]:
            flags |= 0x0003  # SWP_NOMOVE | SWP_NOSIZE
        insert_after = wintypes.HWND(-1) if restack else None
        if not self.user32.SetWindowPos(self.handle, insert_after, *viewport, flags):
            return False
        self._render_layout = layout
        self._hud_restack_render = False
        if restack:
            self._hud_panel._hud_restack = True
            self._hud_drawer._hud_restack = True
        return True

    def _overlay_active_target(self, snapshot, player_cell, now):
        if not self._hud_can_run():
            return None
        objects = {item.key: item for item in snapshot.objects}
        cycle = self._kafra_cycle
        if cycle and cycle["phase"] in ("store", "closing"):
            return None
        if cycle and cycle["phase"] in ("approach", "dialog", "opening") and cycle["city"] == snapshot.map:
            goal = self.nav_goal
            return (objects.get(f"npc:{cycle.get('npc')}")
                    or (goal[1:3] if goal and goal[0] == snapshot.map else cycle["position"]))

        selection = self._pending_hud_interaction
        if selection:
            (kind, aid), _, target_map, _ = selection
            target_key = f"{kind}:{aid}"
        elif self._hud_npc_line_target and now < self._hud_npc_line_target[2]:
            target_key, target_map, _ = self._hud_npc_line_target
        else:
            target_map = None
        if target_map is not None and target_map == snapshot.map:
            if item := objects.get(target_key):
                return item

        route = self.patrol_target_route
        if (route and route[0].lower().removesuffix(".gat").removesuffix(".rsw") == snapshot.map
                and isinstance(route[3], int)):
            if item := (objects.get(f"npc:{route[3]}") or objects.get(f"warp:{route[3]}")):
                return item

        travelling = self._has_hud_travel() or bool(cycle)
        if not travelling and (self.auto_farm or self.auto_loot):
            world = getattr(self.feed, "world", None)
            combat_gid = getattr(world, "locked_combat_mob_gid", None) or self.locked_combat_mob_gid
            focused = self._engagement["key"] if self._engagement else None
            for key in (focused, ("drop", self.locked_drop_gid), ("mob", combat_gid), ("mob", self.locked_mob_gid)):
                if key:
                    item = objects.get(f"{key[0]}:{key[1]}")
                    if item and item.ownership != "LOCKED":
                        return item
            if world and world.map == snapshot.map and any(item.kind == "drop" for item in snapshot.objects):
                with world.lock:
                    entities = tuple(world.entities.items())
                for _, _, gid, _ in self._eligible_drops(snapshot, player_cell, entities, now):
                    if item := objects.get(f"drop:{gid}"):
                        return item

        if not travelling and not self.auto_patrol:
            return None
        if self._last_patrol_click_target and (self.current_patrol_path or travelling):
            return self._last_patrol_click_target[3:5]
        if self.current_patrol_path and getattr(snapshot.player, "is_moving", False):
            return snapshot.player.target
        for goal in (self.nav_goal, route):
            if goal and goal[0].lower().removesuffix(".gat").removesuffix(".rsw") == snapshot.map:
                return goal[1:3]
        if self.current_waypoint:
            return self.current_waypoint
        if self.travel_plan:
            steps = self.travel_plan["steps"]
            index = self.travel_plan["step_idx"]
            if index < len(steps):
                step = steps[index]
                if step.get("from_map") == snapshot.map:
                    return step["wx"], step["wy"]
                if step.get("from_city") == snapshot.map:
                    return step["kafra_pos"]
        return None

    def _draw_overlay_routes(self, snapshot, width, height):
        camera = getattr(self.feed, "camera", None)
        if not self._show_routes.get() or camera is None:
            return

        path = self.active_nav_path
        color = self.active_nav_color if path else "#38BDF8"
        if not path and self.auto_patrol:
            path = self.current_patrol_path
        player_cell = snapshot.player.cell() if snapshot.player else None
        if player_cell and None in player_cell:
            player_cell = None
        target = self._overlay_active_target(snapshot, player_cell, time.monotonic()) if player_cell else None
        if not path and not self.current_waypoint and target is None:
            return

        height_fn = flat_height
        terrain = getattr(self.feed, "terrain", None)
        if snapshot.map and terrain:
            try:
                gat = terrain.for_map(snapshot.map)
                if gat:
                    height_fn = gat.get_elevation
            except Exception:
                pass
        cam_pos = getattr(self.feed, "_cam", None) or player_cell or (0, 0)
        project = camera.projector(snapshot.viewport, cam_pos, height_fn)
        points = [project(*cell) for cell in path] if path else []

        for (u1, v1), (u2, v2) in zip(points, points[1:]):
            if (not all(math.isfinite(value) for value in (u1, v1, u2, v2))
                    or max(u1, u2) < 0 or min(u1, u2) > 1
                    or max(v1, v2) < 0 or min(v1, v2) > 1):
                continue
            self.canvas_pool.get_line(
                u1 * width, v1 * height, u2 * width, v2 * height,
                fill=color, width=3, dash=(6, 4),
            )

        if target is not None:
            player_u, player_v = project(*player_cell, 5.5)
            line_color, marker_color = "#8b5cf6", "#3b82f6"
            if isinstance(target, ScreenObject):
                u, v = target.u, target.v
                if target.kind == "mob":
                    line_color, marker_color = "#ef4444", "#f97316"
                elif target.kind == "drop":
                    line_color, marker_color = "#f59e0b", "#06b6d4"
            else:
                u, v = project(*target)
            if all(math.isfinite(value) for value in (player_u, player_v, u, v)):
                x, y = u * width, v * height
                self.canvas_pool.get_line(player_u * width, player_v * height, x, y,
                                          fill=line_color, width=2, dash=())
                if 0 <= u <= 1 and 0 <= v <= 1:
                    self.canvas_pool.get_oval(x - 9, y - 9, x + 9, y + 9,
                                              outline=marker_color, width=2, fill="", dash=())

        if self.current_waypoint:
            u, v = project(*self.current_waypoint)
            if 0 <= u <= 1 and 0 <= v <= 1:
                x, y = self.canvas_pool.apply_hysteresis("waypoint", u * width, v * height, threshold=0.65)
                self.canvas_pool.get_oval(x - 6, y - 6, x + 6, y + 6, outline=color, width=2)
                text = self.canvas_pool.get_text(x, y - 14, text="[Waypoint]", fill=color, font=("Tahoma", 8, "bold"))
                bbox = self.canvas.bbox(text)
                if bbox:
                    x1, y1, x2, y2 = bbox
                    background = self.canvas_pool.get_rect(x1 - 3, y1 - 1, x2 + 3, y2 + 1,
                                                           fill="#0B1120", outline=color, width=1)
                    self.canvas.tag_lower(background, text)

    @staticmethod
    def _projection_pixels(u, v, width, height):
        return int(u * width), int(v * height)

    def draw(self, snapshot, stats=None):
        if not self._hud_render_foreground:
            self._position_render_window(None)
            return False
        if not self._overlay_visible:
            self._position_render_window(None)
            return True
        if not self._position_render_window(snapshot.viewport):
            self.canvas_pool.hide_all()
            return False

        _, _, width, height = snapshot.viewport
        self.canvas_pool.begin_frame()
        self._draw_overlay_routes(snapshot, width, height)

        is_moving = False
        if hasattr(self.feed, "world") and self.feed.world and self.feed.world.player:
            is_moving = getattr(self.feed.world.player, "is_moving", False)

        occupied = set()
        active_keys = set()
        for item in snapshot.objects:
            if item.kind == "mob" and not (self._show_mobs.get() if hasattr(self, "_show_mobs") else True):
                continue
            if item.kind == "drop" and not (self._show_drops.get() if hasattr(self, "_show_drops") else True):
                continue
            if item.kind == "npc" and not (self._show_npcs.get() if hasattr(self, "_show_npcs") else True):
                continue
            if item.kind == "warp" and not (self._show_warps.get() if hasattr(self, "_show_warps") else True):
                continue
            if item.kind == "player" and not (self._show_players.get() if hasattr(self, "_show_players") else True):
                continue

            active_keys.add(item.key)
            # 1. Âncora no chão (sprite_z = 0.0) com histerese anti-tremor estável
            au, av = item.get_anchor()
            ax, ay = self.canvas_pool.apply_hysteresis(f"anc:{item.key}", au * width, av * height, threshold=0.65)

            # 2. Marcador central visual do sprite (sprite_z = 5.5 mob/npc/player, 1.2 drop/warp)
            if item.kind in ("mob", "drop"):
                cx, cy = self._projection_pixels(item.u, item.v, width, height)
            else:
                snap_val = 2 if is_moving else None
                cx, cy = self.canvas_pool.apply_hysteresis(
                    f"lbl:{item.key}", item.u * width, item.v * height, threshold=0.65, snap_px=snap_val,
                )

            label = item.name if item.kind == "npc" else self.names.get(item.kind, {}).get(item.catalog_id)
            if not label:
                if item.kind == "drop":
                    prefix = "Item"
                elif item.kind == "warp":
                    prefix = "Portal"
                elif item.kind == "npc":
                    prefix = "NPC"
                elif item.kind == "player":
                    prefix = "Player"
                else:
                    prefix = "Monstro"
                label = f"{prefix} #{item.key.partition(':')[2] if item.kind == 'npc' else item.catalog_id}"
            if item.amount > 1:
                label += f" ×{item.amount}"

            border_outline = "#3B4A60"
            border_width = 1

            if item.kind == "drop":
                is_locked_drop = (self.locked_drop_gid is not None and item.key == f"drop:{self.locked_drop_gid}")
                tier = get_item_tier(item.catalog_id, label)
                if tier == 1:
                    color = "#FFD700"  # Dourado vibrante para Tier 1
                    border_outline = "#FFD700"
                    border_width = 2
                    label = f"★ [T1] {label}"
                elif tier == 3:
                    label = f"[T3] {label}"
                    if item.ownership == "MINE":
                        color = "#4ADE80"
                    elif item.ownership == "LOCKED":
                        color = "#F87171"
                        label += " [Não dá para pegar]"
                    else:
                        color = "#94A3B8"  # Cinza discreto para lixo
                else:
                    if item.ownership == "MINE":
                        color = "#4ADE80"  # Verde (Seu)
                    elif item.ownership == "LOCKED":
                        color = "#F87171"  # Vermelho (Bloqueado)
                        label += " [Não dá para pegar]"
                    else:
                        color = "#FBBF24"  # Amarelo (Livre)

                if is_locked_drop:
                    label += " [COLETANDO]"
                    border_outline = "#4ADE80"
                    border_width = 2
                    self.canvas_pool.get_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="#4ADE80", width=3)
                else:
                    self.canvas_pool.get_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=2)
                self.canvas_pool.get_oval(ax - 3, ay - 3, ax + 3, ay + 3, outline=color, width=1)
            elif item.kind == "warp":
                color = "#22D3EE"  # Ciano para portal
                border_outline = "#0891B2"
                border_width = 2
                self.canvas_pool.get_oval(cx - 7, cy - 7, cx + 7, cy + 7, outline="#06B6D4", width=2)
                self.canvas_pool.get_oval(ax - 9, ay - 9, ax + 9, ay + 9, outline="#22D3EE", width=1, dash=(3, 3))
            elif item.kind == "npc":
                color, border_outline = npc_colors(item.catalog_id, item.name)
                if color == "#6b7280":
                    self.canvas_pool.get_oval(ax - 2, ay - 2, ax + 2, ay + 2, fill=color, outline=color)
                    continue
                border_width = 2
                self.canvas_pool.get_rect(cx - 7, cy - 7, cx + 7, cy + 7, outline=border_outline, width=2)
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline=color, width=1)
            elif item.kind == "player":
                color = "#2DD4BF"  # Verde-água / ciano discreto para outros players
                border_outline = "#0D9488"
                border_width = 1
                p_name = getattr(getattr(self.feed, "world", None), "entity_names", {}).get(item.catalog_id, f"Player #{item.catalog_id}")
                label = f"{p_name}"
                self.canvas_pool.get_rect(cx - 5, cy - 5, cx + 5, cy + 5, outline=color, width=1)
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline="#0D9488", width=1)
            else:
                is_locked_mob = (self.locked_mob_gid is not None and item.key == f"mob:{self.locked_mob_gid}")
                if is_locked_mob:
                    color = "#FFA500"  # Laranja vibrante para o mob em foco
                    border_outline = "#FF8C00"
                    border_width = 2
                    self.canvas_pool.get_rect(cx - 7, cy - 7, cx + 7, cy + 7, outline="#FFA500", width=2)
                else:
                    color = "#FFA294"
                    self.canvas_pool.get_rect(cx - 6, cy - 6, cx + 6, cy + 6, outline=color, width=2)
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline=color, width=1)

            # Texto da etiqueta posicionado exatamente 16px acima do marcador central do sprite
            text_x = max(5.0, min(width - 5.0, cx))
            text_y = max(9.0, cy - 16.0)
            if is_moving:
                text_x = float(round(text_x / 2.0) * 2)
                text_y = float(round(text_y / 2.0) * 2)
            else:
                text_x = float(round(text_x))
                text_y = float(round(text_y))
            lane = round(text_x / 120.0), round(text_y / 18.0)
            while lane in occupied and text_y > 25.0:
                text_y -= 18.0
                lane = lane[0], lane[1] - 1
            occupied.add(lane)
            text = self.canvas_pool.get_text(text_x, text_y, text=label, font=("Tahoma", 8, "bold"), fill=color)
            bbox = self.canvas.bbox(text)
            if bbox:
                x1, y1, x2, y2 = bbox
                shift = max(5 - x1, min(width - 5 - x2, 0))
                if shift != 0:
                    self.canvas.move(text, shift, 0)
                    x1 += shift
                    x2 += shift
                background = self.canvas_pool.get_rect(
                    x1 - 4, y1 - 2, x2 + 4, y2 + 2,
                    fill="#0B1120", outline=color, width=1,
                )
                self.canvas.tag_lower(background, text)

        if len(self.canvas_pool.pos_cache) > 400:
            allowed = {f"anc:{k}" for k in active_keys} | {f"lbl:{k}" for k in active_keys} | {"waypoint"}
            self.canvas_pool.pos_cache = {k: v for k, v in self.canvas_pool.pos_cache.items() if k in allowed}

        self.canvas_pool.end_frame()
        return True

    def _draw_hud(self, stats):
        tcp_bytes = stats.get("tcp_bytes", 0)
        if tcp_bytes < 1024:
            bytes_str = f"{tcp_bytes} B"
        elif tcp_bytes < 1024 * 1024:
            bytes_str = f"{tcp_bytes / 1024:.1f} KB"
        else:
            bytes_str = f"{tcp_bytes / (1024 * 1024):.2f} MB"

        pkts_ok = stats.get("packets_ok", 0)
        map_name = stats.get("current_map") or stats.get("map") or "Aguardando mapa"
        pos = stats.get("pos")
        pos_str = f"({int(pos[0])}, {int(pos[1])})" if pos else "---"
        mobs = stats.get("mobs", 0)
        drops = stats.get("drops", 0)
        npcs = stats.get("npcs", 0)
        warps = stats.get("warps", 0)
        port = stats.get("port")
        port_str = f"Porta: {port}" if port else "Porta: ---"
        loot_tag = "[Loot (←): ON]" if self.auto_loot else "[Loot (←): OFF]"
        farm_tag = "[Farm (→): ON]" if self.auto_farm else "[Farm (→): OFF]"
        patrol_tag = "[Patrol (F6): ON]" if self.auto_patrol else "[Patrol (F6): OFF]"

        admin_tag = "" if self.is_admin else " (Sem Admin)"
        line1 = f"● Ragnalok Live{admin_tag} | Clique: {self._click_method} | TCP: {bytes_str} | {port_str} | Pkts OK: {pkts_ok}"
        line2 = f"Mapa: {map_name} {pos_str} | Mobs: {mobs} | Drops: {drops} | NPCs: {npcs} | Portais: {warps}"
        line3 = f"{loot_tag} | {farm_tag} | {patrol_tag}"
        if hasattr(self.feed, "camera"):
            cam = self.feed.camera
            line3 += f" | Câmera: {cam.yaw_deg:.0f}° {cam.pitch_deg:.1f}° d={cam.distance:.0f}"
        if self.patrol_target_route:
            line3 += f" | Rota: {self.patrol_target_route[0]} ({self.patrol_target_route[1]},{self.patrol_target_route[2]})"

        hud_x, hud_y = 10, 10
        text1 = self.canvas_pool.get_text(
            hud_x + 8, hud_y + 5, anchor="nw",
            text=line1, font=("Segoe UI", 9, "bold"), fill="#6EE7B7"
        )
        status_color = "#38BDF8" if self.auto_patrol else ("#4ADE80" if (self.auto_loot or self.auto_farm) else "#CBD5E1")
        text2 = self.canvas_pool.get_text(
            hud_x + 8, hud_y + 21, anchor="nw",
            text=line2, font=("Segoe UI", 9), fill="#CBD5E1"
        )
        text3 = self.canvas_pool.get_text(
            hud_x + 8, hud_y + 37, anchor="nw",
            text=line3, font=("Segoe UI", 9, "bold"), fill=status_color
        )
        bbox1 = self.canvas.bbox(text1) or (hud_x + 8, hud_y + 5, hud_x + 200, hud_y + 20)
        bbox2 = self.canvas.bbox(text2) or (hud_x + 8, hud_y + 21, hud_x + 200, hud_y + 35)
        bbox3 = self.canvas.bbox(text3) or (hud_x + 8, hud_y + 37, hud_x + 200, hud_y + 50)
        total_w = max(bbox1[2], bbox2[2], bbox3[2]) + 8
        total_h = bbox3[3] + 5

        bg = self.canvas_pool.get_rect(
            hud_x, hud_y, total_w, total_h,
            fill="#0F172A", outline="#334155"
        )
        self.canvas.tag_lower(bg, text1)


def sample_objects(t):
    return [
        {"key": "d1", "kind": "drop", "id": 578, "u": .40 + .06 * math.sin(t), "v": .62},
        {"key": "d2", "kind": "drop", "id": 921, "u": .55, "v": .57},
        {"key": "m1", "kind": "mob", "id": 1014, "u": .70, "v": .48 + .09 * math.sin(t)},
    ]


def demo(root, path):
    root.deiconify()
    root.title("Ragnalok | Demonstração de dados ao vivo")
    root.geometry("760x440")
    canvas = tk.Canvas(root, bg="#243247", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    started = time.monotonic()

    def publish():
        elapsed = time.monotonic() - started
        visible = root.state() == "normal" and root.winfo_viewable()
        width, height = canvas.winfo_width(), canvas.winfo_height()
        objects = sample_objects(elapsed) if visible else []
        if int(elapsed / 4) % 2 == 1:
            objects = [item for item in objects if item["key"] != "d2"]
        data = {"version": 1, "viewport": {
            "left": canvas.winfo_rootx(), "top": canvas.winfo_rooty(),
            "width": width, "height": height,
        }, "objects": objects}
        building = path.with_suffix(".tmp")
        building.write_text(json.dumps(data), encoding="utf-8")
        os.replace(building, path)
        canvas.delete("all")
        canvas.create_text(25, 22, anchor="nw", text="Demonstração local do overlay", fill="#EAF2FF", font=("Segoe UI", 19, "bold"))
        canvas.create_text(27, 63, anchor="nw", text="Os dados desta janela são simulados. Esc para fechar.", fill="#B5C9E1", font=("Segoe UI", 11))
        for item in objects:
            x, y = width * item["u"], height * item["v"]
            canvas.create_oval(x - 8, y - 8, x + 8, y + 8, fill="#667D96", outline="")
        root.after(75, publish)

    root.bind("<Escape>", lambda _: root.destroy())
    publish()


def mock_viewport(root):
    root.deiconify()
    root.title("Ragnalok | Validação TCP em loopback")
    root.geometry("760x440")
    canvas = tk.Canvas(root, bg="#243247", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(25, 22, anchor="nw", text="Pacotes rAthena em 127.0.0.1",
                       fill="#EAF2FF", font=("Segoe UI", 19, "bold"))
    canvas.create_text(27, 63, anchor="nw", text="Auto-câmera 3D Ragnarok ativa. Esc para fechar.",
                       fill="#B5C9E1", font=("Segoe UI", 11))
    canvas.create_rectangle(280, 190, 370, 280, fill="#364E43", outline="#587962")
    root.bind("<Escape>", lambda _: root.destroy())

    def viewport():
        if root.state() == "normal" and canvas.winfo_viewable():
            width, height = canvas.winfo_width(), canvas.winfo_height()
            return canvas.winfo_rootx(), canvas.winfo_rooty(), width, height
        return None

    try:
        viewport.handle = u32.GetAncestor(root.winfo_id(), 2)
    except Exception:
        viewport.handle = None

    return viewport


def find_process_window(pid):
    ensure_input_desktop()
    windows = []

    def enum_cb(hwnd, lparam):
        if u32.IsWindowVisible(hwnd):
            p_id = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p_id))
            if p_id.value == pid:
                length = u32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    u32.GetWindowTextW(hwnd, buf, len(buf))
                    rect = wintypes.RECT()
                    u32.GetClientRect(hwnd, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w > 50 and h > 50:
                        windows.append((hwnd, buf.value))
        return True

    cb = WNDENUMPROC(enum_cb)
    u32.EnumWindows(cb, 0)
    return windows[0] if windows else (None, None)


def find_ragnarok_window(pid=None):
    """Localiza a janela do Ragnarok ('Ragexe.exe' ou janela com título 'Ragnarok') em tempo real."""
    if pid:
        try:
            p = psutil.Process(pid)
            if not p.is_running() or (p.name() and p.name().lower() != "ragexe.exe"):
                pid = None
        except Exception:
            pid = None

    if not pid:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if p.info["name"] and p.info["name"].lower() == "ragexe.exe":
                    pid = p.info["pid"]
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    if pid:
        hwnd, title = find_process_window(pid)
        if hwnd and u32.IsWindow(hwnd):
            return hwnd, pid, title

    # Fallback: enumera janelas visíveis buscando 'ragnarok' no título
    windows = []

    def enum_cb(h, lparam):
        if u32.IsWindowVisible(h):
            length = u32.GetWindowTextLengthW(h)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(h, buf, len(buf))
                title_val = buf.value
                if "ragnarok" in title_val.lower():
                    rect = wintypes.RECT()
                    u32.GetClientRect(h, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h_dim = rect.bottom - rect.top
                    if w > 50 and h_dim > 50:
                        p_id = wintypes.DWORD()
                        u32.GetWindowThreadProcessId(h, ctypes.byref(p_id))
                        windows.append((h, p_id.value, title_val))
        return True

    cb = WNDENUMPROC(enum_cb)
    u32.EnumWindows(cb, 0)
    if windows:
        return windows[0]

    return None, None, None


def autodetect_live_session(args):
    """Autodetecta interface, server, port e window a partir de Ragexe.exe ativo."""
    last_status = None
    target_name = "ragexe.exe"

    print("[Live] Iniciando autodetecção de ambiente para Ragexe.exe...", flush=True)

    while True:
        target_proc = None
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pname = p.info["name"]
                if pname and pname.lower() == target_name:
                    target_proc = p
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not target_proc:
            status = "Aguardando inicialização do processo Ragexe.exe..."
            if status != last_status:
                print(status, flush=True)
                last_status = status
            time.sleep(1)
            continue

        try:
            conns = target_proc.net_connections(kind="tcp")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            time.sleep(1)
            continue

        est_conns = [c for c in conns if c.status == psutil.CONN_ESTABLISHED and c.raddr]
        # Ignora conexões externas/auxiliares não relacionadas ao jogo (ex: porta 443)
        game_conns = [c for c in est_conns if c.raddr.port != 443]

        if not game_conns:
            status = f"Ragexe.exe (PID {target_proc.pid}) em execução. Aguardando conexão TCP do jogo..."
            if status != last_status:
                print(status, flush=True)
                last_status = status
            time.sleep(1)
            continue

        # Ignorar porta de login 6900 quando houver outra porta ativa
        non_login = [c for c in game_conns if c.raddr.port != 6900]
        if not non_login:
            status = "Ragexe.exe na tela de login (porta 6900). Aguardando conexão com map-server..."
            if status != last_status:
                print(status, flush=True)
                last_status = status
            time.sleep(1)
            continue

        # Ignorar tela de seleção de personagem (porta padrão 6121) se ainda não conectou ao map-server
        non_char = [c for c in non_login if c.raddr.port != 6121]
        if not non_char:
            status = "Ragexe.exe na seleção de personagem (porta 6121). Aguardando entrar no mapa..."
            if status != last_status:
                print(status, flush=True)
                last_status = status
            time.sleep(1)
            continue

        map_conn = non_char[0]
        break

    # 1. Servidor e Porta (com override manual)
    if not args.server:
        args.server = ipaddress.ip_address(map_conn.raddr.ip)
    if not args.port:
        args.port = map_conn.raddr.port
    if not args.local_port:
        args.local_port = map_conn.laddr.port

    # 2. Interface Npcap mapeada pelo IP local (com override manual)
    if not args.interface:
        local_ip = map_conn.laddr.ip
        matched_npcap = None
        matched_name = None

        for iface in conf.ifaces.values():
            ips = iface.ips.get(4, []) if hasattr(iface, "ips") else []
            if local_ip == getattr(iface, "ip", None) or local_ip in ips:
                matched_npcap = getattr(iface, "network_name", None)
                matched_name = getattr(iface, "name", None)
                break

        if matched_npcap:
            args.interface = matched_npcap
        elif matched_name:
            args.interface = matched_name
        else:
            for if_name, addrs in psutil.net_if_addrs().items():
                if any(addr.address == local_ip for addr in addrs):
                    args.interface = if_name
                    break

        if not args.interface:
            raise RuntimeError(f"Não foi possível mapear o IP local {local_ip} para uma interface Npcap.")

    # 3. Janela e handle pertencente ao Ragexe.exe (com override manual)
    hwnd, title = find_process_window(target_proc.pid)
    args.window_handle = hwnd
    args.ragexe_pid = target_proc.pid
    if not args.window:
        args.window = title if title else "Ragnarok"

    # Exibição no console dos valores detectados
    window_display = f"'{args.window}'"
    if args.window_handle:
        window_display += f" (HWND 0x{args.window_handle:X})"

    print("-" * 60, flush=True)
    print("Ambiente de jogo detectado com sucesso:", flush=True)
    print(f"  Interface:      {args.interface}", flush=True)
    print(f"  Servidor:Porta: {args.server}:{args.port}", flush=True)
    print(f"  Janela:         {window_display}", flush=True)
    print("-" * 60, flush=True)


def run_live(root, args, names):
    lengths = packet_lengths(args.packet_lengths)
    world = World(args.initial_map, args.legacy_fields, args.player_speed)
    viewport = mock_viewport(root) if args.mock else GameViewport(args.window, getattr(args, "window_handle", None))
    terrain = None if args.flat else Terrain(args.grf)
    world.terrain = terrain
    camera = CameraTracker(
        target_hwnd=getattr(viewport, "handle", None),
        pid=getattr(args, "ragexe_pid", None),
        world=world,
        terrain=terrain,
        yaw_deg=args.rotation,
        pitch_deg=args.tilt,
        distance=args.distance,
    )
    stop, ready = Event(), Event()
    loop = Loopback(world, lengths, stop, not args.interface) if args.mock else None
    threads = []
    errors = []

    def worker(action):
        try:
            action()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            errors.append(exc)
            world.reset()
            stop.set()

    def start(action):
        thread = Thread(target=worker, args=(action,), daemon=True)
        thread.start()
        threads.append(thread)

    if args.interface:
        server, port = ("127.0.0.1", loop.port) if loop else (str(args.server), args.port)
        ragexe_pid = getattr(args, "ragexe_pid", None)
        start(lambda: capture(world, lengths, args.interface, server, port, args.local_port, stop, ready, ragexe_pid))
        print(f"Captura TCP passiva em {args.interface}, servidor {server}:{port}.", flush=True)
    else:
        ready.set()
    if loop:
        def run_mock():
            while not ready.wait(.1):
                if stop.is_set():
                    return
            if not stop.is_set():
                loop.run()

        start(run_mock)
    overlay = Overlay(root, LiveFeed(world, terrain, camera, viewport), names)
    world.on_pickup_ack_callback = overlay._on_pickup_ack
    overlay._click_method = args.click_method
    overlay._ragexe_pid = getattr(args, "ragexe_pid", None)
    overlay._hardware_cursor = getattr(args, "hardware_cursor", False)
    if getattr(args, "auto_patrol", False):
        overlay.auto_patrol = True
    if getattr(args, "route", None):
        r_map, r_x, r_y, r_target = args.route
        overlay.patrol_target_route = (r_map, int(r_x), int(r_y), r_target)
    if getattr(args, "serial_port", None):
        overlay.serial_hid.connect(args.serial_port, getattr(args, "serial_baud", 115200))
    if getattr(args, "interception_dll", None):
        overlay.interception = InterceptionDriver(args.interception_dll)
    print(f"[Clique] Método de clique ativo: {overlay._click_method}", flush=True)

    def watch_worker():
        if errors:
            root.destroy()
        else:
            root.after(100, watch_worker)

    try:
        overlay.refresh()
        camera.start_tracking(root, viewport)
        watch_worker()
        root.mainloop()
    finally:
        stop.set()
        for thread in threads:
            thread.join(2)
        if loop:
            loop.close()
        if terrain:
            terrain.close()
        if hasattr(overlay, "close"):
            overlay.close()
    if errors:
        raise RuntimeError(f"Feed interrompido. {errors[0]}") from errors[0]


def tcp_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("A porta deve estar entre 1 e 65535.")
    return port


def main():
    parser = argparse.ArgumentParser(description="Overlay Windows para rAthena e snapshots JSON.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Captura passiva do servidor de mapas rAthena.")
    mode.add_argument("--mock", action="store_true", help="Pacotes mockados via TCP em 127.0.0.1.")
    mode.add_argument("--list-interfaces", action="store_true", help="Lista interfaces disponíveis para captura.")
    mode.add_argument("--source", type=Path, help="Arquivo local de snapshots JSON completos.")
    mode.add_argument("--demo", action="store_true", help="Janela de teste com dados simulados.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--interface", help="Nome ou identificador da interface Npcap (autodetectado se omitido).")
    parser.add_argument("--server", type=ipaddress.ip_address, help="IP do servidor de mapas (autodetectado se omitido).")
    parser.add_argument("--port", type=tcp_port, default=None, help="Porta TCP do servidor de mapas (autodetectado se omitido, padrão 5121).")
    parser.add_argument("--local-port", type=tcp_port, help="Porta TCP local para selecionar uma conexão.")
    parser.add_argument("--window", help="Texto no título da janela do jogo (autodetectado se omitido).")
    parser.add_argument("--initial-map", help="Mapa inicial, pois o pacote de entrada traz apenas a posição.")
    parser.add_argument("--grf", type=Path, default=DEFAULT_GRF)
    parser.add_argument("--flat", action="store_true", help="Usa terreno plano para o mock sem data.grf.")
    parser.add_argument("--packet-lengths", type=Path, default=DEFAULT_LENGTHS, help="Tabela de comprimentos do PACKETVER.")
    parser.add_argument("--legacy-fields", action="store_true", help="Usa layout de entidades anterior ao campo shield de 32 bits.")
    parser.add_argument("--player-speed", type=int, default=150, help="Tempo de caminhada por célula, em ms.")
    parser.add_argument("--rotation", type=float, default=0.0, help="Rotação horizontal inicial da câmera (yaw) em graus.")
    parser.add_argument("--tilt", type=float, default=50.0, help="Inclinação da câmera (pitch) em graus (padrão calibrado: 50.0°).")
    parser.add_argument("--distance", type=float, default=399.0, help="Distância da câmera em unidades de mundo (padrão real: 399.0).")
    parser.add_argument("--zoom", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--click-method", default=DEFAULT_CLICK_METHOD,
                        choices=["touch_postmsg", "touch_win32", "win32", "postmsg", "absolute", "touch", "touch_hybrid"],
                        help="Método de clique. Padrão: %(default)s (movimento relativo + mouse_event).")
    parser.add_argument("--click-mode", dest="click_mode_legacy", default=None,
                        choices=["absolute", "sendinput", "postmessage", "hybrid", "win32", "touch", "touch_hybrid", "postmsg"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--network-actions", action="store_true",
                        help="Envia ações de auto-loot e auto-farm diretamente via injeção TCP (pydivert).")
    parser.add_argument("--dump-outbound", action="store_true",
                        help="Grava em outbound_samples.log os próximos 30 frames OUTBOUND para análise de trailer.")
    parser.add_argument("--packet-keys",
                        help="Chaves de rotação LCG do rAthena para ofuscação de opcode (ex: 0x7E241DE0,0x5E805580,0x3D807D80).")
    parser.add_argument("--admin", action="store_true",
                        help="Relança o script com elevação UAC (Administrador) se necessário.")
    parser.add_argument("--hardware-cursor", action="store_true",
                        help="Belt-and-suspenders: envia PostMessageW E SetCursorPos+SendInput simultaneamente (requer admin para SendInput funcionar com Ragexe.exe elevado).")
    parser.add_argument("--serial-port", default=None,
                        help="Porta serial para envio de comandos (x,y,click) para microcontrolador USB HID (ex: COM3).")
    parser.add_argument("--serial-baud", type=int, default=115200,
                        help="Baudrate da porta serial para microcontrolador USB HID (padrão: 115200).")
    parser.add_argument("--interception-dll", type=Path, default=None,
                        help="Caminho explícito para interception.dll caso não esteja na raiz do projeto.")
    parser.add_argument("--auto-patrol", action="store_true",
                        help="Inicia o overlay com Auto-Patrol / Roam ativado (alternável via tecla F6).")
    parser.add_argument("--route", nargs=4, metavar=("MAP", "X", "Y", "TARGET"),
                        help="Define rota de destino: mapa x y alvo_npc_ou_portal (ex: gef_fild06 180 120 45)")
    args = parser.parse_args()
    if args.click_mode_legacy:
        legacy = args.click_mode_legacy.lower()
        if legacy in ("sendinput", "hybrid"):
            args.click_method = "win32"
        elif legacy == "postmessage":
            args.click_method = "postmsg"
        else:
            args.click_method = legacy
    # A autodetecção já consulta as conexões do processo do jogo.
    if args.admin or args.live or args.mock:
        shell = ctypes.windll.shell32
        if not shell.IsUserAnAdmin():
            relaunch = shell.ShellExecuteW
            relaunch.argtypes = (
                wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int
            )
            relaunch.restype = wintypes.HINSTANCE
            command_line = list2cmdline([str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
            result = relaunch(None, "runas", sys.executable, command_line, os.getcwd(), 1) or 0
            if result > 32:
                return
            print(f"[Admin] Elevação UAC não concedida (código {result}). O Windows pode bloquear cliques no jogo.", flush=True)
    if args.list_interfaces:
        try:
            list_interfaces()
        except RuntimeError as exc:
            parser.exit(1, f"{exc}\n")
        return
    if args.flat and not args.mock:
        parser.error("--flat requer --mock.")
    if args.mock and (args.legacy_fields or args.local_port):
        parser.error("O mock usa layout moderno e uma porta local automática.")
    if args.zoom is not None:
        args.distance = float(args.zoom)
    if not (math.isfinite(args.rotation) and math.isfinite(args.tilt) and math.isfinite(args.distance)
            and args.player_speed > 0):
        parser.error("Parâmetros de câmera ou velocidade inválidos.")

    if args.live:
        autodetect_live_session(args)
    elif args.mock and not args.port:
        args.port = 5121

    root = tk.Tk()
    root.configure(bg=BG_KEY, highlightthickness=0, bd=0)
    root.withdraw()
    names = read_catalogs(args.catalog)
    if args.live or args.mock:
        try:
            run_live(root, args, names)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.demo:
        with tempfile.TemporaryDirectory(prefix="ragnalok-feed-") as folder:
            path = Path(folder, "snapshot.json")
            overlay = Overlay(root, SnapshotFeed(path), names)
            overlay._click_method = args.click_method
            if getattr(args, "serial_port", None):
                overlay.serial_hid.connect(args.serial_port, getattr(args, "serial_baud", 115200))
            if getattr(args, "interception_dll", None):
                overlay.interception = InterceptionDriver(args.interception_dll)
            try:
                demo(root, path)
                overlay.refresh()
                root.mainloop()
            finally:
                overlay.close()
    else:
        overlay = Overlay(root, SnapshotFeed(args.source), names)
        overlay._click_method = args.click_method
        if getattr(args, "serial_port", None):
            overlay.serial_hid.connect(args.serial_port, getattr(args, "serial_baud", 115200))
        if getattr(args, "interception_dll", None):
            overlay.interception = InterceptionDriver(args.interception_dll)
        try:
            overlay.refresh()
            root.mainloop()
        finally:
            overlay.close()


if __name__ == "__main__":
    main()
