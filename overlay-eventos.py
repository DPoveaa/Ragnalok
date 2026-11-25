"""Draw a Windows overlay from rAthena, a local JSON snapshot or a simulation.

An external JSON producer replaces the snapshot at least every 750 ms.
"""

import argparse
import atexit
import datetime
import faulthandler
import sys
import traceback
import ctypes
from ctypes import wintypes
import ipaddress
import json
import math
import os
import random
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


# ── Registro da Última Sessão (session.log) ──
class _SessionLogTee:
    """Duplica streams de saída gravando no terminal e em session.log simultaneamente."""

    def __init__(self, original_stream, log_file, lock):
        self._orig = original_stream
        self._file = log_file
        self._lock = lock

    def write(self, data):
        with self._lock:
            try:
                self._orig.write(data)
                self._orig.flush()
            except Exception:
                pass
            try:
                self._file.write(data)
                self._file.flush()
            except Exception:
                pass

    def flush(self):
        with self._lock:
            try:
                self._orig.flush()
            except Exception:
                pass
            try:
                self._file.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self._orig, "isatty", lambda: False)()

    def fileno(self):
        return self._orig.fileno()

    def __getattr__(self, name):
        return getattr(self._orig, name)


_SESSION_LOG_FILE = None
_SESSION_LOG_LOCK = Lock()
_EXCEPTION_LOG_TIMES = {}


def log_overlay_exception(source):
    exc_type, exc, tb = sys.exc_info()
    filename, lineno = traceback.extract_tb(tb)[-1][:2]
    signature = (exc_type, filename, lineno)
    now = time.monotonic()
    if now - _EXCEPTION_LOG_TIMES.get(signature, -math.inf) < 10:
        return
    _EXCEPTION_LOG_TIMES[signature] = now
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"\n[CRASH PREVENTED - {source}] {timestamp}\n"
    message += "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(message)
    sys.stderr.flush()
    if _SESSION_LOG_FILE and not isinstance(sys.stderr, _SessionLogTee):
        with _SESSION_LOG_LOCK:
            _SESSION_LOG_FILE.write(message)
            _SESSION_LOG_FILE.flush()


def init_session_logging(log_path="session.log"):
    """Inicia o log da sessão atual. Limpa o arquivo anterior e salva tudo até fechar."""
    global _SESSION_LOG_FILE
    if _SESSION_LOG_FILE is not None:
        return
    try:
        log_file = open(log_path, "w", buffering=1, encoding="utf-8", errors="backslashreplace")
        _SESSION_LOG_FILE = log_file
    except Exception as exc:
        print(f"[Aviso] Não foi possível criar '{log_path}': {exc}", flush=True)
        return

    try:
        faulthandler.enable(log_file)
    except Exception:
        pass

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "=" * 80 + "\n"
        f" Ragnalok Overlay — Log da Última Sessão\n"
        f" Início: {now_str} | PID: {os.getpid()} | Python: {sys.version.split()[0]} on {sys.platform}\n"
        f" Comando: {' '.join(sys.argv)}\n"
        + "=" * 80 + "\n\n"
    )
    log_file.write(header)
    log_file.flush()

    sys.stdout = _SessionLogTee(sys.stdout, log_file, _SESSION_LOG_LOCK)
    sys.stderr = _SessionLogTee(sys.stderr, log_file, _SESSION_LOG_LOCK)

    orig_excepthook = sys.excepthook

    def _unhandled_exception_hook(exc_type, exc_val, exc_tb):
        err = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"\n[CRASH - MAIN THREAD] {ts}\n{err}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        try:
            log_file.write(msg)
            log_file.flush()
        except Exception:
            pass
        if orig_excepthook:
            try:
                orig_excepthook(exc_type, exc_val, exc_tb)
            except Exception:
                pass

    sys.excepthook = _unhandled_exception_hook

    if hasattr(threading, "excepthook"):
        orig_thread_excepthook = threading.excepthook

        def _thread_exception_hook(args):
            err = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tname = getattr(args.thread, "name", str(args.thread))
            msg = f"\n[CRASH - THREAD: {tname}] {ts}\n{err}\n"
            sys.stderr.write(msg)
            sys.stderr.flush()
            try:
                log_file.write(msg)
                log_file.flush()
            except Exception:
                pass
            if orig_thread_excepthook:
                try:
                    orig_thread_excepthook(args)
                except Exception:
                    pass

        threading.excepthook = _thread_exception_hook

    def _session_exit():
        try:
            end_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"\n" + "=" * 80 + f"\n Sessão encerrada: {end_str}\n" + "=" * 80 + "\n")
            log_file.flush()
            log_file.close()
        except Exception:
            pass

    atexit.register(_session_exit)


import win32_input
from win32_input import (
    INPUT, INPUT_KEYBOARD, INPUT_MOUSE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEINPUT,
    POINTER_TOUCH_INFO, PT_TOUCH, ULONG_PTR, ensure_input_desktop, ensure_touch_injection,
    kernel32, u32,
)

from rathena_feed import DEFAULT_GRF, DEFAULT_LENGTHS, World, capture, is_kafra, list_interfaces, packet_lengths
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


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _get_bundle_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).resolve().parent


def _resolve_resource(filename: str | Path) -> Path:
    p_name = Path(filename)
    cand1 = _get_base_dir() / p_name
    if cand1.exists():
        return cand1
    cand2 = _get_bundle_dir() / p_name
    if cand2.exists():
        return cand2
    return cand1


# Kafras oficiais RE/Pre-RE e coordenadas padronizadas RO LATAM.
KAFRA_NETWORK_FILE = _resolve_resource("kafra_network.json")


def load_kafra_network() -> dict:
    try:
        kf = _resolve_resource("kafra_network.json")
        if kf.exists():
            return json.loads(kf.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Kafra] Erro ao ler kafra_network.json: {exc}", flush=True)
    return {}


def get_kafra_network_map_entry(map_name: str, network: dict = None) -> tuple[str | None, dict | None]:
    if not map_name:
        return None, None
    if network is None:
        network = load_kafra_network()
    kafras = network.get("kafras", {})
    m = str(map_name).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    if m == "gef_f10":
        m = "gef_fild10"
    if m in kafras:
        return m, kafras[m]
    base = re.sub(r"_[a-z0-9]$", "", m)
    if base == "gef_f10":
        base = "gef_fild10"
    if base in kafras:
        return base, kafras[base]
    return None, None


def player_has_storage_ticket(world) -> bool:
    if not world or not hasattr(world, "inventory"):
        return False
    with world.lock:
        for item in getattr(world, "inventory", {}).values():
            name = (getattr(item, "name", None) or "").casefold()
            nameid = getattr(item, "nameid", 0)
            if "ticket de armaz" in name or nameid in (7033, 7620):
                return True
    return False


def player_has_airship_ticket(world) -> bool:
    if not world or not hasattr(world, "inventory"):
        return False
    with world.lock:
        for item in getattr(world, "inventory", {}).values():
            name = (getattr(item, "name", None) or "").casefold()
            nameid = getattr(item, "nameid", 0)
            if "ticket de aeroplano" in name or nameid in (7034, 7621):
                return True
    return False


KAFRA_LOCATIONS = {
    "prontera": ((152, 191), (146, 89), (151, 288), (282, 200), (29, 207)),
    "geffen": ((120, 62), (203, 123)), "payon": ((181, 104), (175, 226)),
    "morocc": ((156, 97), (160, 258)), "alberta": ((113, 60), (28, 229)),
    "aldebaran": ((143, 119),), "aldeba_in": ((96, 181),),
    "izlude": ((128, 148), (134, 87)), "comodo": ((195, 150),),
    "yuno": ((152, 187), (327, 108), (277, 221)), "amatsu": ((102, 149),),
    "gonryun": ((159, 122),), "umbala": ((87, 160),), "niflheim": ((202, 180),),
    "louyang": ((210, 104),), "ayothaya": ((212, 169),),
    "einbroch": ((242, 205), (59, 203)), "einbech": ((181, 132),),
    "lighthalzen": ((164, 100), (191, 320), (94, 248)), "lhz_in02": ((237, 284),),
    "hugel": ((88, 168),), "rachel": ((109, 138),), "veins": ((208, 128),),
    "moscovia": ((223, 191),), "brasilis": ((197, 221),), "mora": ((48, 128),),
    "dic_in01": ((254, 119),), "eclage": ((112, 39), (295, 307)),
    "malangdo": ((147, 117), (184, 138)), "malaya": ((71, 79), (234, 204)),
    "dewata": ((202, 184),), "pay_arche": ((55, 123),), "xmas": ((148, 134), (140, 297)),
    "prt_fild05": ((290, 224),), "moc_para01": ((10, 185),),
    "job3_rune01": ((92, 62),), "harboro1": ((291, 207),),
    "gef_f10": ((73, 340),), "gef_fild10": ((73, 340),),
    "mjolnir_02": ((82, 362),), "cmd_fild07": ((136, 134),),
    "moc_ruins": ((59, 157),), "izlu2dun": ((106, 58),),
    "alb2trea": ((59, 69),), "tur_dun01": ((148, 239),),
    "glast_01": ((200, 273),), "lhz_fild01": ((240, 107),),
    "ra_fild12": ((292, 208),), "ve_fild07": ((128, 131),),
    "mid_camp": ((216, 284),), "manuk": ((262, 177),), "splendide": ((194, 162),),
    "dicastes01": ((198, 187),), "verus04": ((123, 250),), "jor_tail": ((182, 204),),
    "icas_in": ((180, 62),), "lasagna": ((298, 238),), "moc_fild19": ((107, 100),),
}


KAFRA_STORAGE_PORTALS = {
    ("dicastes01", "dic_in01"): (255, 175),
    ("dic_in01", "dicastes01"): (342, 271),
}


def npc_colors(job, name):
    normalized = normalize_dialog_option(name)
    if is_kafra(job, normalized):
        return "#f59e0b", "#3b82f6"
    if any(term in normalized for term in ("placa", "soldado")):
        return "#94a3b8", "#334155"
    if any(term in normalized for term in ("eden", "quadro", "boya", "instrutor")) or "ur" in normalized.split():
        return "#10b981", "#10b981"
    if any(term in normalized for term in ("curandeira", "teleportadora", "utilidades", "estilista", "loja", "vendedor", "mercador")):
        return "#8b5cf6", "#22d3ee"
    return "#94a3b8", "#334155"


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
    ("survival_resume_pct", "HP Voltar %", 80, 1, 100),
)
SURVIVAL_RETREAT = "fleeing"
SURVIVAL_RECOVER = "recovering"
SURVIVAL_RETURN = "returning"


def load_filters_config() -> dict:
    cfg_path = _resolve_resource("filters_config.json")
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
    cfg_path = _get_base_dir() / "filters_config.json"
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
    routes_path = _resolve_resource("world_routes.json")
    if routes_path.exists():
        try:
            with open(routes_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cities": {}, "portals": [], "aliases": {}, "world_map_grid": []}


def travel_map_group(name):
    normalized = str(name or "").strip().lower().removesuffix(".gat").removesuffix(".rsw")
    base = re.sub(r"_[a-z]$", "", normalized)
    return "gef_fild10" if base == "gef_f10" else base


def travel_matches_map(actual, expected):
    actual = str(actual or "").lower().removesuffix(".gat").removesuffix(".rsw")
    expected = str(expected or "").lower().removesuffix(".gat").removesuffix(".rsw")
    if re.search(r"_[a-z]$", expected):
        return actual == expected
    return travel_map_group(actual) == travel_map_group(expected)


def travel_step_key(step):
    if step["type"] == "portal":
        return ("portal", travel_map_group(step["from_map"]), travel_map_group(step["to_map"]),
                (step["wx"], step["wy"]))
    return ("kafra", travel_map_group(step["from_city"]), travel_map_group(step["to_city"]), step["kafra_pos"])


def calculate_travel_route(start_map, dest_map, *, position=None, zeny=None, excluded=(), live_portals=(),
                           live_kafras=(), routes_data=None, network=None):
    """Compara deslocamentos até as saídas e Kafras, com saldo acumulado quando conhecido."""
    import heapq

    origin, target = travel_map_group(start_map), travel_map_group(dest_map)
    if not origin or not target or origin == target:
        return []
    routes_data = load_world_routes() if routes_data is None else routes_data
    network = load_kafra_network() if network is None else network
    kafras = network.get("kafras", {})
    portals = {(travel_map_group(src), travel_map_group(dst)): [(tuple(cell), None, 28)]
               for (src, dst), cell in {**MAP_PORTALS, **KAFRA_STORAGE_PORTALS}.items()}
    references = routes_data.get("reference_portals", ())
    unverified = set(map(tuple, routes_data.get("unverified_portals", ())))
    for row in routes_data.get("portals", ()):
        pair = travel_map_group(row["from_map"]), travel_map_group(row["to_map"])
        portals[pair] = [((int(row["wx"]), int(row["wy"])), None, 28)]
    for src, dst, x, y, ax, ay in references:
        pair = travel_map_group(src), travel_map_group(dst)
        samples = portals.setdefault(pair, [])
        if all(sample[0] != (x, y) for sample in samples):
            samples.append(((x, y), (ax, ay), 5))

    for link, coords in load_learned_warps().items():
        src, arrow, dst = link.partition("->")
        if not arrow or not isinstance(coords, (list, tuple)) or len(coords) != 2:
            continue
        pair = travel_map_group(src), travel_map_group(dst)
        if pair[0] == pair[1]:
            continue
        cell = tuple(map(int, coords))
        samples = portals.get(pair, [])
        confirmed = (src == start_map or pair[0] == origin) and any(
            math.dist(cell, observed) <= 5 for observed in live_portals
        )
        service = pair[1] in kafras.get(pair[0], {}).get("teleports", {})
        near_known = any(math.dist(cell, sample) <= 12 for sample, _, _ in samples)
        if not confirmed and (service and not near_known or not samples):
            continue
        if src == start_map or not re.search(r"_[a-z]$", src):
            samples = portals.setdefault(pair, [])
            if all(sample[0] != cell for sample in samples):
                arrival = next((arrive for sample, arrive, _ in samples
                                if arrive and math.dist(cell, sample) <= 12), None)
                samples.append((cell, arrival, 0 if confirmed or near_known else 44))

    adj = collections.defaultdict(list)
    for (src, dst), samples in portals.items():
        if src == dst:
            continue
        reverse = min(portals.get((dst, src), ()), key=lambda item: item[2], default=(None, None, 0))[0]
        for (x, y), arrival, uncertainty in samples:
            if (src, dst) in unverified and (src != origin or not any(
                math.dist((x, y), observed) <= 6 for observed in live_portals
            )):
                continue
            step = {"type": "portal", "from_map": src, "to_map": dst, "wx": x, "wy": y, "cost": 0}
            if travel_step_key(step) not in excluded:
                adj[src].append((dst, (x, y), arrival or reverse, 12 + uncertainty, step))
    kafra_arrivals = {
        "prontera": (116, 72), "payon": (161, 58), "geffen": (120, 39), "izlude": (128, 98),
        "aldebaran": (168, 112), "morocc": (156, 46), "alberta": (117, 56),
        "gef_fild10": (52, 326), "yuno": (158, 125),
    }
    for city, entry in kafras.items():
        if "teleport" not in entry.get("services", ()):
            continue
        src = travel_map_group(city)
        sites = [((int(loc["x"]), int(loc["y"])), 15) for loc in entry.get("locations", ())]
        if src == origin:
            sites = [(tuple(cell), 0) for cell in live_kafras] + sites
        for dest, price in entry.get("teleports", {}).items():
            dst, price = travel_map_group(dest), int(price)
            for cell, uncertainty in dict.fromkeys(sites):
                step = {"type": "kafra", "from_city": src, "to_city": dst, "kafra_pos": cell, "cost": price}
                if travel_step_key(step) not in excluded:
                    adj[src].append((dst, cell, kafra_arrivals.get(dst),
                                     48 + price / 100 + uncertainty, step))

    serial = 0
    entry = tuple(map(round, position)) if position is not None else None
    heap = [(0.0, 0, serial, origin, entry, [])]
    best = {(origin, entry): [(0.0, 0)]}
    while heap:
        score, spent, _, city, entry, steps = heapq.heappop(heap)
        if (score, spent) not in best.get((city, entry), ()):
            continue
        if city == target:
            return steps
        for dst, exit_cell, arrival, action_cost, step in adj.get(city, ()):
            price = spent + step["cost"]
            if zeny is not None and price > zeny:
                continue
            walking = math.dist(entry, exit_cell) if entry is not None else 105
            cost = score + walking + action_cost
            labels = best.get((dst, arrival), ())
            if any(old_cost <= cost and old_price <= price for old_cost, old_price in labels):
                continue
            best[dst, arrival] = [(old_cost, old_price) for old_cost, old_price in labels
                                  if old_cost < cost or old_price < price] + [(cost, price)]
            serial += 1
            heapq.heappush(heap, (cost, price, serial, dst, arrival, steps + [step]))
    return []


def find_cheapest_kafra_storage_route(start_map: str, network: dict = None) -> tuple[str | None, list[dict], int]:
    """Usa grafo ponderado por Zeny (BFS/Dijkstra) sobre kafras + transports + portais
    para traçar a rota mais barata/curta até a Kafra com armazém mais próxima."""
    if network is None:
        network = load_kafra_network()
    kafras = network.get("kafras", {})
    storage_maps = {m for m, data in kafras.items() if "storage" in data.get("services", [])}

    s_map = str(start_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    base_s = re.sub(r"_[a-z0-9]$", "", s_map)
    if base_s == "gef_f10":
        base_s = "gef_fild10"
    if s_map in storage_maps:
        return s_map, [], 0
    if base_s in storage_maps:
        return base_s, [], 0

    best_dest = None
    best_route = []
    best_total_weight = float("inf")
    best_zeny = 0

    for dest in storage_maps:
        route = calculate_travel_route(s_map, dest)
        if not route:
            continue
        zeny_cost = sum(step.get("cost", 0) for step in route)
        total_weight = zeny_cost + (len(route) * 10)
        if total_weight < best_total_weight:
            best_total_weight = total_weight
            best_zeny = zeny_cost
            best_dest = dest
            best_route = route

    return best_dest, best_route, (best_zeny if best_dest else 0)


BG_KEY = "#010101"
DEFAULT_CLICK_METHOD = "touch_postmsg"
CLICK_RETRY_OFFSETS = tuple(
    (dx, dy) for dx in range(-5, 6) for dy in range(-5, 6)
    if 9 <= dx * dx + dy * dy <= 25
)
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
    tag_uv: tuple[float, float] | None = None

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
        assets = _resolve_resource("assets")
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
    TAG_HEIGHT_CELLS = {"mob": 1.9, "npc": 2.3, "player": 1.9, "drop": 1.05, "warp": 1.05}

    def __init__(self, world, terrain, camera, viewport):
        self.world = world
        self.terrain = terrain
        self.camera = camera
        self.viewport = viewport
        self._cam = None

    def stats(self):
        return self.world.stats()

    def current(self):
        name, player, entities = self.world.snapshot()
        viewport = self.viewport()
        if not viewport:
            return None
        self.camera.on_map_change(name)
        self._cam = None
        objects = []
        if player and name:
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
            player_fx, player_fy = player.cell(now)
            self._cam = (player_fx, player_fy, height(player_fx, player_fy))

            project = self.camera.projector(viewport, self._cam, height, raw=self.camera.alignment_active)
            rendered_portals = set()
            for (kind, aid), entity in entities:
                fx, fy = entity.track.cell(now)
                if kind == "warp":
                    portal_cell = (round(fx), round(fy))
                    if portal_cell in rendered_portals:
                        continue
                    rendered_portals.add(portal_cell)
                anchor_u, anchor_v = project(fx, fy, 0.0)
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
                        tag_uv=project(fx, fy, self.TAG_HEIGHT_CELLS.get(kind, 1.9) * CELL_SIZE),
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
                            p_anc_u, p_anc_v,
                            tag_uv=project(p_fx, p_fy, self.TAG_HEIGHT_CELLS["player"] * CELL_SIZE),
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

LRESULT = ctypes.c_ssize_t


u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
u32.MapVirtualKeyW.restype = wintypes.UINT

VK_TO_SCANCODE = {
    0x1B: (0x01, False),
    0x0D: (0x1C, False),
    0x2D: (0x52, True),
    0x2E: (0x53, True),
    0x24: (0x47, True),
    0x23: (0x4F, True),
    0x21: (0x49, True),
    0x22: (0x51, True),
    0x26: (0x48, True),
    0x28: (0x50, True),
    0x25: (0x4B, True),
    0x27: (0x4D, True),
    **{vk: (0x3B + vk - 0x70, False) for vk in range(0x70, 0x7A)},
    0x7A: (0x57, False),
    0x7B: (0x58, False),
}

_keyboard_api = ctypes.WinDLL("user32", use_last_error=True)
_keyboard_api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_keyboard_api.SendInput.restype = wintypes.UINT


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


@dataclass(frozen=True)
class CameraCalibration:
    # Escala no centro da imagem para uma altura de viewport de 1080 px.
    pixels_per_cell: float
    default_pitch: float
    min_zoom: float
    default_zoom: float
    max_zoom: float


CAMERA_PROFILES = {
    "outdoor": CameraCalibration(68.36, 50.0, 230.0, 300.0, 600.0),
    "indoor": CameraCalibration(67.50, 45.0, 300.0, 465.0, 600.0),
}
INDOOR_CAMERA_MAPS = frozenset({"moc_para01", "moc_para02", "in_orcs01"})
CAMERA_CALIBRATION_PATH = _resolve_resource("camera_calibration.json")
CAMERA_FOV_DEG = 15.0


class CameraZoomObserver:
    """Mede o zoom e a translação do terreno na área de jogo."""

    def __init__(self):
        import cv2
        import numpy as np
        from PIL import Image, ImageGrab

        self._cv = cv2
        self._np = np
        self._grab = ImageGrab.grab
        self._resample = Image.Resampling.BILINEAR
        self._view = None
        self._frames = collections.deque(maxlen=6)
        self._pending = None
        self._result = None
        self._cell = None
        self._cell_since = 0.0
        self._ground_frame = None
        self._ground_distance = None
        self._last_zoom_event = 0.0
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._capture, name="camera-zoom", daemon=True)
        self._thread.start()

    def set_view(self, view, cell):
        with self._lock:
            if view != self._view:
                self._view = view
                self._frames.clear()
                self._pending = self._result = None
                self._ground_frame = self._ground_distance = None
                self._cell = None
            if cell != self._cell:
                self._cell = cell
                self._cell_since = time.monotonic()

    def observe(self, event_at, turns=None):
        with self._lock:
            view = self._view
            if view is None:
                return
            self._last_zoom_event = event_at
            self._ground_frame = self._ground_distance = None
            if self._pending is not None and event_at - self._pending[2] <= 0.45:
                _, before, _, previous_turns = self._pending
                turns = turns + previous_turns if turns is not None and previous_turns is not None else None
            else:
                before = next((image for timestamp, image in reversed(self._frames)
                               if 0.005 <= event_at - timestamp <= 0.45), None)
            if before is not None:
                self._pending = view, before, event_at, turns

    def take_scale(self):
        with self._lock:
            scale, self._result = self._result, None
            return scale

    def take_ground_distance(self):
        with self._lock:
            distance, self._ground_distance = self._ground_distance, None
            return distance

    def close(self):
        self._stop.set()

    def _capture(self):
        while not self._stop.wait(0.10):
            with self._lock:
                view = self._view
            if view is None:
                continue
            _, viewport, pitch_deg, yaw_deg = view
            left, top, width, height = viewport
            timestamp = time.monotonic()
            try:
                image = self._grab((left, top, left + width, top + height), all_screens=True).convert("L")
                image = self._np.asarray(image.resize((640, max(1, round(640 * height / width))), self._resample))
                with self._lock:
                    if self._view is not view:
                        continue
                    self._frames.append((timestamp, image))
                    pending = self._pending
                    zoom_event = self._last_zoom_event
                    cell = self._cell
                    previous = None
                    if cell is not None and timestamp >= max(self._cell_since + 0.22, zoom_event + 0.65):
                        previous = self._ground_frame
                        if (previous is None or timestamp - previous[0] > 8.0
                                or previous[1] == cell or math.dist(previous[1], cell) >= 2):
                            self._ground_frame = timestamp, cell, image
                if pending is not None and timestamp - pending[2] >= 0.16:
                    transform = self._measure_ground_transform(pending[1], image, pitch_deg)
                    scale = self._wheel_scale(transform, image.shape[1])
                    turns = pending[3]
                    if scale is not None and turns is not None and (scale - 1.0) * turns <= 0:
                        scale = None
                    with self._lock:
                        if self._pending is pending and self._view is view:
                            self._pending = None
                            if scale is not None:
                                self._result = scale
                elif previous is not None and timestamp - previous[0] <= 8.0:
                    distance = self._distance_from_pan(previous, cell, image, pitch_deg, yaw_deg)
                    if distance is not None:
                        with self._lock:
                            if self._view is view and self._last_zoom_event == zoom_event:
                                self._ground_distance = distance
            except (OSError, ValueError, self._cv.error):
                continue

    @staticmethod
    def _wheel_scale(transform, width):
        if transform is not None:
            scale, dx, dy, _ = transform
            if math.hypot(dx, dy) <= width * 0.08 and abs(scale - 1.0) >= 0.015:
                return scale
        return None

    def _distance_from_pan(self, previous, cell, image, pitch_deg, yaw_deg):
        _, origin, before = previous
        travel = math.dist(origin, cell)
        if not 2 <= travel <= 14:
            return None
        transform = self._measure_ground_transform(before, image, pitch_deg)
        if transform is None:
            return None
        scale, image_dx, image_dy, focal = transform
        if abs(scale - 1.0) > 0.035 or math.hypot(image_dx, image_dy) < 9:
            return None
        dx = (cell[0] - origin[0]) * CELL_SIZE
        dy = (cell[1] - origin[1]) * CELL_SIZE
        yaw = math.radians(yaw_deg)
        expected_x = -dx * math.cos(yaw) + dy * math.sin(yaw)
        expected_y = dx * math.sin(yaw) + dy * math.cos(yaw)
        along = image_dx * expected_x + image_dy * expected_y
        across = image_dx * expected_y - image_dy * expected_x
        if along <= 0 or abs(across) > 0.22 * along:
            return None
        return focal * (expected_x**2 + expected_y**2) / along

    def _measure_ground_transform(self, before, after, pitch_deg):
        cv = self._cv
        np = self._np
        if before.shape != after.shape:
            return None
        height, width = after.shape
        mask = np.zeros_like(after)
        mask[round(height * 0.19):round(height * 0.77), round(width * 0.19):round(width * 0.81)] = 255
        mask[round(height * 0.32):round(height * 0.58), round(width * 0.45):round(width * 0.55)] = 0
        features = cv.SIFT_create(nfeatures=350)
        points_before, original = features.detectAndCompute(before, mask)
        points_after, current = features.detectAndCompute(after, mask)
        if original is None or current is None or min(len(original), len(current)) < 12:
            return None
        pairs = cv.BFMatcher(cv.NORM_L2).knnMatch(original, current, k=2)
        matches = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.74 * pair[1].distance]
        if len(matches) < 12:
            return None
        start = np.float32([points_before[match.queryIdx].pt for match in matches])
        end = np.float32([points_after[match.trainIdx].pt for match in matches])
        center = np.array((width / 2, height / 2), dtype=np.float32)
        focal = (height / 2) / math.tan(math.radians(CAMERA_FOV_DEG / 2))
        sin_pitch = math.sin(math.radians(pitch_deg))
        cos_pitch = math.cos(math.radians(pitch_deg))
        if sin_pitch <= 0.15:
            return None

        def ground_points(points):
            relative = points - center
            # A inversa perspectiva no plano do chão torna o zoom uniforme para o ajuste de escala.
            depth = 1 + relative[:, 1] / focal * cos_pitch / sin_pitch
            relative[:, 1] /= sin_pitch
            return relative / depth[:, None]

        transform, inliers = cv.estimateAffinePartial2D(
            ground_points(start), ground_points(end), method=cv.RANSAC, ransacReprojThreshold=3.0,
        )
        if transform is None or inliers is None or int(inliers.sum()) < max(10, len(matches) * 0.5):
            return None
        used = start[inliers.ravel() != 0]
        if np.ptp(used[:, 0]) < width * 0.18 or np.ptp(used[:, 1]) < height * 0.13:
            return None
        a, b = transform[0, :2]
        scale = math.hypot(a, b)
        if not (0.33 <= scale <= 3.0 and abs(math.atan2(b, a)) <= math.radians(5)):
            return None
        image_dx, image_dy = transform[:, 2]
        return scale, image_dx, image_dy, focal


class CameraTracker:
    """Reconstrói a câmera a partir do perfil de mapa e dos eventos do cliente."""

    def __init__(self, target_hwnd=None, pid=None, world=None, terrain=None,
                 yaw_deg=None, pitch_deg=None, distance=None):
        self.target_hwnd = target_hwnd
        self.pid = pid
        self.world = world
        self.terrain = terrain
        self.current_map = None
        self._viewport_provider = None
        self._game_has_focus = None
        self._is_ui_point = None
        self._pending_map = None
        self._zoom_observer = None
        self._outdoor_zoom = {}
        self._observed_zoom_limits = {}
        self._zoom_corrections, self._screen_alignments = self._load_zoom_corrections()
        self._zoom_keys_down = set()
        self._alignment_slot = "Padrão"
        self._alignment_locked = False
        self.alignment_active = False
        self._alignment_mark = False
        self._alignment_references = []
        self._alignment_pick = None
        self._alignment_view = None
        self._alignment_message = ""
        self._alignment_message_until = 0.0
        self._mouse_events = collections.deque(maxlen=2048)
        self._mouse_click_events = collections.deque(maxlen=32)
        self._hook_rmb_down = False
        self._rmb_start = None
        self._rmb_last = None
        self._rmb_dragged = False
        self._last_rmb_click = None
        self._wheel_hook = None
        self._wheel_hook_proc = None
        self.on_lbutton_down_callback = None
        self.on_physical_click = None

        self._h_process = None
        self._init_memory_reader()

        self.on_map_change(getattr(world, "map", None))
        if yaw_deg is not None:
            self._rotate(float(yaw_deg) - self.yaw_deg)
        if pitch_deg is not None and not self.profile.is_indoor:
            self._adjust_pitch(float(pitch_deg) - self.pitch_deg)
        if distance is not None:
            self._adjust_zoom(float(distance) - self.distance)
        if self.world is not None:
            self.world.on_map_change_callback = self._queue_map_change

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

    def _queue_map_change(self, map_base, map_gat=None):
        self._pending_map = map_base or map_gat or ""

    def on_map_change(self, map_base):
        name = str(map_base or "").lower().removesuffix(".gat").removesuffix(".rsw")
        if name == self.current_map:
            return
        self._stop_alignment()
        self._alignment_locked = False
        native = get_map_camera_profile(name)
        indoor = (native.is_indoor or name.startswith("in_") or name.endswith("_in")
                  or name in INDOOR_CAMERA_MAPS)
        profile_key = "indoor" if indoor else "outdoor"
        calibration = CAMERA_PROFILES[profile_key]

        # A tabela do cliente tem precedência sobre os valores de fallback.
        map_defaults = get_map_camera_profile("_in" if native.is_indoor else "")
        native_zoom = (native.distance_min, native.distance_default, native.distance_max)
        default_zoom = (map_defaults.distance_min, map_defaults.distance_default, map_defaults.distance_max)
        uses_fallback = native_zoom == default_zoom
        if uses_fallback:
            focal = 540.0 / math.tan(math.radians(CAMERA_FOV_DEG / 2))
            calibrated_distance = focal * CELL_SIZE / calibration.pixels_per_cell
            factor = calibrated_distance / calibration.default_zoom
            native_zoom = (calibration.min_zoom * factor, calibrated_distance, calibration.max_zoom * factor)
        min_zoom, zoom, max_zoom = native_zoom
        native_pitch = (native.pitch_min, native.pitch_default, native.pitch_max)
        default_pitch = (map_defaults.pitch_min, map_defaults.pitch_default, map_defaults.pitch_max)
        pitch = calibration.default_pitch if native_pitch == default_pitch else native.pitch_default
        if indoor and not native.is_indoor:
            native = get_map_camera_profile("_in")
        pitch = max(native.pitch_min, min(native.pitch_max, pitch))

        self.current_map = name
        self._calibration_key = profile_key if uses_fallback else name
        self.profile = native
        self.is_indoor = indoor
        self.min_zoom, self.max_zoom = sorted((min_zoom, max_zoom))
        self.default_zoom = max(self.min_zoom, min(self.max_zoom, zoom))
        self.default_pitch = pitch
        self.yaw_deg = native.yaw_default
        self.pitch_deg = pitch
        zoom = self.default_zoom
        if not indoor:
            zoom = self._outdoor_zoom.get(native_zoom, zoom)
        self._zoom_limits = native_zoom
        observed_min, observed_max = self._observed_zoom_limits.get(native_zoom, (self.min_zoom, self.max_zoom))
        self.min_zoom, self.max_zoom = observed_min, observed_max
        self._set_zoom(zoom)
        self.fov_deg = CAMERA_FOV_DEG
        self._rmb_start = self._rmb_last = self._last_rmb_click = None

    def projector(self, viewport, cam_pos, height_fn=None, *, raw=False):
        alignment = None if raw else self._screen_alignment()
        reference_zoom = alignment[0] if alignment is not None else self.distance
        distance = reference_zoom * self._zoom_factor(reference_zoom)
        project = RagnarokProjector3D(self.yaw_deg, self.pitch_deg, distance, self.fov_deg).projector(
            viewport, cam_pos, height_fn)
        if alignment is None:
            return project
        _, a, b, tx, ty = alignment
        left, top, width, height = viewport
        aspect = width / height

        def aligned(fx, fy, sprite_z=0.0):
            u, v = project(fx, fy, sprite_z)
            x, y = (u - 0.5) * aspect, v - 0.5
            return (a * x - b * y + tx) / aspect + 0.5, b * x + a * y + ty + 0.5

        def aligned_screen(fx, fy, sprite_z=0.0):
            u, v = aligned(fx, fy, sprite_z)
            return left + u * width, top + v * height

        aligned.project_screen = aligned_screen
        return aligned

    @staticmethod
    def _load_zoom_corrections():
        try:
            data = json.loads(CAMERA_CALIBRATION_PATH.read_text(encoding="utf-8"))
            saved = data.pop("_screen_alignment", {})
            alignments = {}
            for profile, slots in saved.items():
                for slot, values in slots.items():
                    if (slot in ("Padrão", "Afastado") and isinstance(values, list) and len(values) == 5
                            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
                            and values[0] > 0 and 0.4 <= math.hypot(*values[1:3]) <= 2.5):
                        alignments.setdefault(profile, {})[slot] = values
            corrections = {
                profile: {float(distance): float(factor) for distance, factor in points.items()
                          if 0 < float(distance) < math.inf and 0.25 <= float(factor) <= 4.0}
                for profile, points in data.items()
            }
            return corrections, alignments
        except FileNotFoundError:
            return {}, {}
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            print(f"[Camera] Erro ao ler calibração: {exc}", flush=True)
            return {}, {}

    def _save_zoom_corrections(self):
        temporary = None
        target_path = _get_base_dir() / "camera_calibration.json"
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=target_path.parent) as stream:
                temporary = Path(stream.name)
                json.dump({**self._zoom_corrections, "_screen_alignment": self._screen_alignments},
                          stream, indent=2, ensure_ascii=False)
            temporary.replace(target_path)
            return True
        except OSError as exc:
            print(f"[Camera] Erro ao salvar calibração: {exc}", flush=True)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _zoom_factor(self, distance):
        points = sorted(self._zoom_corrections.get(self._calibration_key, {}).items())
        if not points:
            return 1.0
        for (near, near_factor), (far, far_factor) in zip(points, points[1:]):
            if near <= distance <= far:
                weight = (distance - near) / (far - near)
                return near_factor + (far_factor - near_factor) * weight
        return (points[0] if distance < points[0][0] else points[-1])[1]

    @staticmethod
    def _alignment_zoom(alignment):
        # A distância efetiva separa dois perfis salvos com a mesma distância estimada.
        original_zoom, a, b, _, _ = alignment
        return original_zoom / math.hypot(a, b)

    def _screen_alignment(self):
        slots = self._screen_alignments.get(self._calibration_key, {})
        preferred = slots.get(self._alignment_slot)
        if self._alignment_locked:
            return preferred
        if not slots:
            return None

        references = dict(slots)
        if "Padrão" not in references:
            references["Padrão"] = (self.default_zoom, 1.0, 0.0, 0.0, 0.0)
        anchors = sorted((self._alignment_zoom(alignment), slot, alignment)
                         for slot, alignment in references.items())
        near_zoom, _, near = anchors[0]
        far_zoom, _, far = anchors[-1]
        if far_zoom - near_zoom > near_zoom * 0.01 and near_zoom < self.distance < far_zoom:
            weight = (self.distance - near_zoom) / (far_zoom - near_zoom)
            a, b, tx, ty = (start + (end - start) * weight for start, end in zip(near[1:], far[1:]))
            return self.distance * math.hypot(a, b), a, b, tx, ty

        zoom, _, alignment = min(anchors, key=lambda anchor: (abs(self.distance - anchor[0]),
                                                               anchor[1] != self._alignment_slot))
        reference_zoom, *transform = alignment
        return (reference_zoom * self.distance / zoom, *transform)

    def _alignment_notice(self, message):
        self._alignment_message = message
        self._alignment_message_until = time.monotonic() + 5.0

    def _stop_alignment(self):
        self.alignment_active = False
        self._alignment_mark = False
        self._alignment_references.clear()
        self._alignment_pick = None
        self._alignment_view = None

    def _mark_alignment(self, snapshot, x, y):
        _, _, width, height = snapshot.viewport
        if not (0 <= x < width and 0 <= y < height):
            return
        player = snapshot.player
        if player is None or player.is_moving:
            self._alignment_notice("Pare o personagem antes de marcar um ponto.")
            return

        target = ((x - width / 2) / height, (y - height / 2) / height)
        if self._alignment_pick is None:
            markers = []
            for item in snapshot.objects:
                u, v = item.get_anchor()
                if 0 <= u <= 1 and 0 <= v <= 1:
                    positions = [(u, v)]
                    if item.tag_uv is not None:
                        positions.append(item.tag_uv)
                    error = min(math.hypot(pu * width - x, pv * height - y) for pu, pv in positions)
                    markers.append((error, item))
            choice = min(markers, key=lambda marker: marker[0], default=None)
            if choice is None or choice[0] > 40:
                self._alignment_notice("Aproxime o cursor de uma tag ou círculo do overlay.")
                return
            item = choice[1]
            u, v = item.get_anchor()
            source = ((u - 0.5) * width / height, v - 0.5)
            self._alignment_pick = (item.key, source, player.target)
            self._alignment_message_until = 0
            return

        key, source, origin = self._alignment_pick
        self._alignment_pick = None
        item = next((item for item in snapshot.objects if item.key == key), None)
        if item is None or player.target != origin:
            self._alignment_notice("A referência saiu do lugar. Selecione o marcador de novo.")
            return
        u, v = item.get_anchor()
        if math.dist(((u - 0.5) * width / height, v - 0.5), source) > 0.025:
            self._alignment_notice("A referência se moveu. Use uma Kafra, outro NPC ou um portal.")
            return
        if not self._alignment_references:
            self._alignment_references.append((source, target))
            self._alignment_message_until = 0
            return

        (x0, y0), (u0, v0) = self._alignment_references[0]
        dx, dy = source[0] - x0, source[1] - y0
        du, dv = target[0] - u0, target[1] - v0
        length_squared = dx * dx + dy * dy
        if length_squared < 0.15**2:
            self._alignment_notice("O segundo alvo precisa ficar mais longe do primeiro.")
            return
        a = (dx * du + dy * dv) / length_squared
        b = (dx * dv - dy * du) / length_squared
        tx, ty = u0 - a * x0 + b * y0, v0 - b * x0 - a * y0
        if not (0.4 <= math.hypot(a, b) <= 2.5 and abs(math.atan2(b, a)) <= math.pi / 4
                and abs(tx) <= 0.4 and abs(ty) <= 0.4):
            self._alignment_notice("Os dois pontos não conferem. Marque duas referências novamente.")
            self._alignment_references.clear()
            return
        slots = self._screen_alignments.setdefault(self._calibration_key, {})
        alignment = [self.distance, a, b, tx, ty]
        slots[self._alignment_slot] = alignment
        self._set_zoom(self._alignment_zoom(alignment), observed=True)
        self._alignment_locked = True
        self._stop_alignment()
        saved = self._save_zoom_corrections()
        self._alignment_notice(f"Zoom {self._alignment_slot}: " +
                               ("calibração salva." if saved else "ajuste aplicado, mas o arquivo não foi gravado."))

    def alignment_overlay(self, snapshot):
        if not self.alignment_active:
            message = self._alignment_message if time.monotonic() < self._alignment_message_until else ""
            return message, None

        view = (snapshot.map, snapshot.viewport, self.distance, self.yaw_deg, self.pitch_deg)
        if self._alignment_view != view:
            self._alignment_pick = None
            self._alignment_references.clear()
            self._alignment_view = view
        cursor = wintypes.POINT()
        point = None
        if u32.GetCursorPos(ctypes.byref(cursor)):
            left, top, _, _ = snapshot.viewport
            point = cursor.x - left, cursor.y - top
        if self._alignment_mark:
            self._alignment_mark = False
            if point is not None:
                self._mark_alignment(snapshot, *point)

        if time.monotonic() < self._alignment_message_until:
            message = self._alignment_message
        elif self._alignment_pick is None:
            number = len(self._alignment_references) + 1
            message = f"{self._alignment_slot} · {number}/2 · Aponte para a tag ou círculo e pressione Ctrl+F11"
        else:
            message = f"{self._alignment_slot} · Aponte para os pés do alvo real e pressione Ctrl+F11"
        return message, point if self._alignment_pick is not None else None

    def _poll_zoom_shortcuts(self):
        keys = {vk for vk in (0x21, 0x22, 0x24, 0x78, 0x79, 0x7A) if u32.GetAsyncKeyState(vk) & 0x8000}
        if not (u32.GetAsyncKeyState(0x11) & 0x8000):
            keys.clear()
        pressed = keys - self._zoom_keys_down
        self._zoom_keys_down = keys
        if not pressed or not self._game_has_focus or not self._game_has_focus():
            return
        if 0x78 in pressed:  # Ctrl+F9
            self._stop_alignment()
            self._alignment_slot = "Afastado" if self._alignment_slot == "Padrão" else "Padrão"
            slots = self._screen_alignments.get(self._calibration_key, {})
            alignment = slots.get(self._alignment_slot)
            self._alignment_locked = True
            if alignment is not None:
                self._set_zoom(self._alignment_zoom(alignment), observed=True)
            status = "aplicado" if alignment is not None else "sem ajuste salvo · Ctrl+F10 para calibrar"
            self._alignment_notice(f"Zoom {self._alignment_slot} · {status}")
            return
        if 0x79 in pressed:  # Ctrl+F10
            if self.alignment_active:
                self._stop_alignment()
                self._alignment_notice("Calibração cancelada.")
            else:
                self.alignment_active = True
                self._alignment_message_until = 0
            return
        if 0x7A in pressed:  # Ctrl+F11
            self._alignment_mark = self.alignment_active
            return
        if 0x24 in pressed:  # Ctrl+Home
            self._stop_alignment()
            self._screen_alignments.get(self._calibration_key, {}).pop(self._alignment_slot, None)
            self._zoom_corrections.pop(self._calibration_key, None)
            self._alignment_notice(f"Zoom {self._alignment_slot} restaurado.")
        else:
            alignment = self._screen_alignment()
            reference_zoom = alignment[0] if alignment is not None else self.distance
            factor = self._zoom_factor(reference_zoom) * (0.99 if 0x21 in pressed else 1.01)
            points = self._zoom_corrections.setdefault(self._calibration_key, {})
            for distance in tuple(points):
                if abs(distance / reference_zoom - 1.0) < 0.02:
                    del points[distance]
            points[reference_zoom] = max(0.25, min(4.0, factor))
        self._save_zoom_corrections()
        alignment = self._screen_alignment()
        reference_zoom = alignment[0] if alignment is not None else self.distance
        print(f"[Camera] {self._calibration_key} | distância {self.distance:.1f} | fator {self._zoom_factor(reference_zoom):.3f}",
              flush=True)

    def record_click(self, sx, sy, viewport=None):
        """Compatibilidade com os callbacks de clique."""

    def _set_zoom(self, distance, *, observed=False):
        if observed:
            # O zoom confirmado em tela pode exceder os limites da tabela do mapa.
            self.min_zoom = min(self.min_zoom, distance)
            self.max_zoom = max(self.max_zoom, distance)
            self._observed_zoom_limits[self._zoom_limits] = self.min_zoom, self.max_zoom
        self.distance = max(self.min_zoom, min(self.max_zoom, distance))
        if not self.is_indoor:
            self._outdoor_zoom[self._zoom_limits] = self.distance

    def _adjust_zoom(self, delta):
        self._set_zoom(self.distance + delta)

    def _adjust_pitch(self, delta):
        if not self.is_indoor:
            self.pitch_deg = max(self.profile.pitch_min, min(self.profile.pitch_max, self.pitch_deg + delta))

    def _rotate(self, delta):
        yaw = self.yaw_deg + delta
        if self.profile.yaw_max - self.profile.yaw_min >= 360.0:
            self.yaw_deg = (yaw + 180.0) % 360.0 - 180.0
        else:
            self.yaw_deg = max(self.profile.yaw_min, min(self.profile.yaw_max, yaw))

    def _is_game_point(self, x, y):
        if not self._viewport_provider:
            return False
        viewport = self._viewport_provider()
        if not viewport:
            return False
        left, top, width, height = viewport
        return (left <= x < left + width and top <= y < top + height
                and not (self._is_ui_point and self._is_ui_point(x, y)))

    def _camera_mouse_event(self, message, x, y, data, timestamp, shift, ctrl, event_at):
        point = (x, y)

        if message == 0x020A:  # WM_MOUSEWHEEL
            if self._is_game_point(x, y):
                turns = ctypes.c_short((data >> 16) & 0xFFFF).value / 120.0
                if shift and ctrl:
                    self._adjust_pitch(-turns * 3.0)
                elif turns and not shift and not ctrl and self._zoom_observer is not None:
                    self._alignment_locked = False
                    self._zoom_observer.observe(event_at, turns)
            return

        if message == 0x0200:  # WM_MOUSEMOVE
            if self._rmb_last is None:
                return
            dx, dy = x - self._rmb_last[0], y - self._rmb_last[1]
            if math.dist(point, self._rmb_start[1]) >= 4:
                self._rmb_dragged = True
            self._rmb_last = point
            if shift:
                self._adjust_pitch(dy * 0.15)
            elif ctrl:
                if dy and self._zoom_observer is not None:
                    self._zoom_observer.observe(event_at)
            else:
                self._rotate(dx * 0.28)
            return

        if message == 0x0205:  # WM_RBUTTONUP
            if self._rmb_start:
                down_time, origin, modifiers = self._rmb_start
                if (not self._rmb_dragged and ((timestamp - down_time) & 0xFFFFFFFF) < 300
                        and math.dist(point, origin) < 4):
                    self._last_rmb_click = down_time, origin, modifiers
            self._rmb_start = self._rmb_last = None
            return

        if message in (0x0204, 0x0206) and self._is_game_point(x, y):  # WM_RBUTTONDOWN / DBLCLK
            modifiers = (shift, ctrl)
            prev = self._last_rmb_click
            self._last_rmb_click = None
            double = (message == 0x0206 or (prev and ((timestamp - prev[0]) & 0xFFFFFFFF) < 300
                                           and math.dist(point, prev[1]) < 8 and prev[2] == modifiers))
            if double:
                if ctrl and not shift:
                    if self._zoom_observer is not None:
                        self._zoom_observer.observe(event_at)
                else:
                    self.yaw_deg = self.profile.yaw_default
                    self.pitch_deg = self.default_pitch
                self._rmb_start = self._rmb_last = None
                return
            self._rmb_start = (timestamp, point, modifiers)
            self._rmb_last = point
            self._rmb_dragged = False
        elif message in (0x0204, 0x0206):
            self._rmb_start = self._rmb_last = self._last_rmb_click = None

    def drain_mouse_events(self):
        if self.world is not None and self._pending_map == self.world.map:
            self.on_map_change(self._pending_map)
        game_focused = self._game_has_focus is not None and self._game_has_focus()
        viewport = self._viewport_provider() if game_focused and self.current_map else None
        if self._zoom_observer is not None:
            player = self.world.player if self.world is not None else None
            cell = player.target if viewport and player is not None and not player.is_moving else None
            view = (self.current_map, viewport, self.pitch_deg, self.yaw_deg) if viewport else None
            self._zoom_observer.set_view(view, cell)
            if (scale := self._zoom_observer.take_scale()) is not None:
                self._alignment_locked = False
                self._set_zoom(self.distance / scale, observed=True)
            elif (distance := self._zoom_observer.take_ground_distance()) is not None:
                if (not self.alignment_active and not self._alignment_locked
                        and self.min_zoom * 0.65 <= distance <= self.max_zoom * 2.0
                        and abs(distance / self.distance - 1.0) > 0.07):
                    self._set_zoom(distance, observed=True)
                    print(f"[Camera] Zoom medido pelo cenário: {self.distance:.1f}", flush=True)
        while self._mouse_click_events:
            sx, sy, left_button, clicked_at, foreground = self._mouse_click_events.popleft()
            if self.on_physical_click is not None:
                self.on_physical_click(sx, sy, left_button, clicked_at, foreground)
            if left_button and game_focused and self._game_has_focus(foreground):
                if self.on_lbutton_down_callback is not None:
                    self.on_lbutton_down_callback(sx, sy)
        while self._mouse_events:
            foreground, *event = self._mouse_events.popleft()
            if self._game_has_focus(foreground) and self._game_has_focus():
                self._camera_mouse_event(*event)
            else:
                self._rmb_start = self._rmb_last = self._last_rmb_click = None
        self._poll_zoom_shortcuts()

    def install_wheel_hook(self):
        """Captura os eventos do mouse sem consultar widgets dentro do callback Win32."""
        if self._wheel_hook is not None:
            return
        try:
            WH_MOUSE_LL = 14
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
                        if not ms.flags & 0x01 and wParam in (0x0201, 0x0204, 0x0207, 0x020B):
                            self._mouse_click_events.append((ms.pt.x, ms.pt.y, wParam == 0x0201,
                                                             time.monotonic(), u32.GetForegroundWindow()))
                        if not ms.flags & 0x01 and wParam in (0x0200, 0x0204, 0x0205, 0x0206, 0x020A):
                            if wParam in (0x0204, 0x0206):
                                self._hook_rmb_down = True
                            elif wParam == 0x0205:
                                self._hook_rmb_down = False
                            if wParam != 0x0200 or self._hook_rmb_down:
                                self._mouse_events.append((
                                    u32.GetForegroundWindow(), wParam, ms.pt.x, ms.pt.y, ms.mouseData, ms.time,
                                    bool(u32.GetAsyncKeyState(0x10) & 0x8000),
                                    bool(u32.GetAsyncKeyState(0x11) & 0x8000),
                                    time.monotonic(),
                                ))
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
            self._mouse_events.clear()
            self._mouse_click_events.clear()

    def start_tracking(self, root, viewport_provider, *, game_has_focus, is_ui_point):
        self._viewport_provider = viewport_provider
        self._game_has_focus = game_has_focus
        self._is_ui_point = is_ui_point
        try:
            self._zoom_observer = CameraZoomObserver()
        except ImportError:
            print("[Camera] Zoom visual requer OpenCV, NumPy e Pillow.", flush=True)
        self.install_wheel_hook()

        def stop_tracking(event):
            if event.widget is root:
                if self._zoom_observer is not None:
                    self._zoom_observer.close()
                self.uninstall_wheel_hook()

        root.bind("<Destroy>", stop_tracking, add="+")


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

    def get_line(self, *coords, **kwargs):
        if self._line_idx < len(self.lines):
            item = self.lines[self._line_idx]
            self.canvas.coords(item, *coords)
            self.canvas.itemconfigure(item, state="normal", **kwargs)
        else:
            item = self.canvas.create_line(*coords, state="normal", **kwargs)
            self.lines.append(item)
        self._line_idx += 1
        return item

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
        self.last_patrol_click_player_pos = None
        self._locked_mob_gid = None
        self.locked_combat_mob_gid = None
        self._locked_drop_gid = None
        self._combat_mob_gid = None
        self._mob_lock_until = 0.0
        self._drop_lock_until = 0.0
        self.current_patrol_path = []
        self.patrol_path = []
        self.patrol_waypoints = []
        self.current_waypoint = None
        self._patrol_scanned_ts = {}
        self._patrol_history_by_map = {}
        self._patrol_sector_visit_ts = {}
        self._patrol_heading = (0.0, 0.0)
        self._patrol_frontier_retry_at = 0.0
        self._patrol_finished_ts = {}
        self._patrol_unreachable_until = {}
        self._patrol_scan_zone = None
        self._patrol_visibility_key = None
        self._patrol_visible = ()
        self._patrol_wall_clearance = {}
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
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._kafra_cycle = None
        self._kafra_manual = False
        self._kafra_capacity_latched = False
        self._kafra_block = None
        self.center_popup_until = 0.0
        self._last_handled_invite_ts = 0.0
        self._engagement = None
        self._engage_state = None
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
        self._hud_edit_window = None
        self._world_map_keys = None
        self._key_message_hwnd = None
        self._last_key_error = None
        self._sit_test = None
        self._hud_choice = None
        self._hud_choice_button = None
        self._hud_hotkeys = self._load_hud_hotkeys()
        self._hud_panel = None
        self._hud_drawer = None
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
        self._click_in_progress = False
        self._click_retry_history = collections.OrderedDict()
        self._game_key_lock = threading.Lock()
        self._game_key_queue = collections.deque()
        self._game_key_worker_active = False
        self._last_sit_attempt_at = float("-inf")
        self._sit_confirmation = None
        self._action_click_used = None
        self._last_patrol_click_target = None
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
        if hasattr(self.feed, "camera"):
            self.feed.camera.on_lbutton_down_callback = self._on_game_lbutton_down
            self.feed.camera.on_physical_click = self._record_hud_click

    @staticmethod
    def _guard_hud_callback(callback):
        def guarded(*args):
            try:
                return callback(*args)
            except (tk.TclError, ValueError):
                return None
            except Exception:
                log_overlay_exception("HUD")
                return None

        return guarded

    def _create_hud_panel(self):
        guard = self._guard_hud_callback
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
            hud.bind("<Button-1>", guard(self._on_hud_control_click), add="+")
            hud.bind("<ButtonRelease-1>", guard(self._on_hud_control_release), add="+")
            return hud

        def row(parent):
            frame = tk.Frame(parent, bg=bg)
            frame.pack(fill="x", pady=2)
            return frame

        def label(parent, text, color=fg):
            return tk.Label(parent, text=text, bg=bg, fg=color, font=font, anchor="w")

        def button(parent, text, command, side="left"):
            def wrapped_cmd(*args):
                self._mark_hud_control_click()
                now = time.monotonic()
                self._hud_click_grace = (now, now + 0.8)
                self._hud_manual_clicks.clear()
                self._focus_hud_game()
                return command(*args)
            widget = tk.Button(parent, text=text, command=guard(wrapped_cmd), **button_style)
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
            update_caption = guard(update_caption)
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
                listing.bind("<ButtonRelease-1>", guard(choose))
                x = widget.winfo_rootx() - drawer.winfo_rootx()
                y = widget.winfo_rooty() - drawer.winfo_rooty() + widget.winfo_height()
                choices.update_idletasks()
                y = min(y, drawer.winfo_height() - choices.winfo_reqheight())
                choices.place(x=x, y=max(0, y), width=150)
                choices.lift()
                self._hud_choice = choices
                self._hud_choice_button = widget

            widget = tk.Button(parent, textvariable=caption, command=guard(open_choices),
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

            widget.bind("<Button-1>", guard(select))
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
        self._hud_play_button.configure(width=17)
        self._set_hud_running(False)
        for title, attr, action in (
            ("Farm", "auto_farm", self._toggle_auto_farm),
            ("Loot", "auto_loot", self._toggle_auto_loot),
            ("Patrulha livre", "auto_patrol", self._toggle_auto_patrol),
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
        label(maps, "Viagem usa Play/Pause e independe da patrulha livre.").pack(anchor="w")
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
                results.bind("<Double-Button-1>", guard(lambda _event: travel("farm")))
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

        canvas.bind("<Configure>", guard(draw_world_map))
        canvas.bind("<Button-1>", guard(select_map))
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
        self._npc_listbox.bind("<Double-Button-1>", guard(lambda _event: self._hud_interact()))
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
            tk.Checkbutton(parent, text=text, variable=variable, command=guard(action) if action else None,
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
            field._hud_finish = guard(lambda name=key, entry=field: self._save_kafra_field(name, entry))
        button(row(kafra), "Guardar na Kafra Agora", self._request_kafra_storage)

        survival = pages["Sobrevivência"]
        for key, title in (
            ("survival_enabled", "Auto-Fugir e Sentar"),
            ("survival_auto_respawn", "Auto-Ressuscitar e Voltar ao Farm"),
        ):
            enabled = tk.BooleanVar(value=self.filters_config[key])
            check(survival, title, enabled,
                  lambda name=key, value=enabled: self._save_hud_setting(name, value.get()))

        fields = {}

        @guard
        def save_survival_numbers(_event=None):
            for key, _, _, minimum, maximum in SURVIVAL_NUMBERS:
                field = fields[key]
                try:
                    value = int(field.get())
                except (tk.TclError, ValueError):
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
        button(test_row, "Testar Sentar (Insert)", self._test_sit_key)
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
                      command=guard(lambda name=action: self._begin_hud_binding(name))).grid(
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
        if isinstance(widget, (tk.Entry, tk.Spinbox)):
            return
        if isinstance(widget, (tk.Button, tk.Checkbutton, tk.Radiobutton)):
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
        guard = self._guard_hud_callback

        def begin_edit(event):
            if self._hud_edit is field:
                return None
            if isinstance(field, tk.Spinbox) and field.identify(event.x, event.y) in ("buttonup", "buttondown"):
                return None
            self._finish_hud_edit()
            self._dismiss_hud_choice()
            frame = field.winfo_toplevel()
            hwnd = getattr(frame, "_hud_hwnd", None)
            if hwnd:
                self._set_hud_edit_activation(hwnd, True)
                u32.SetForegroundWindow(hwnd)
            self._hud_edit_window = hwnd
            self._hud_edit = field
            field.configure(highlightbackground="#8b5cf6")
            field.focus_force()
            return None

        def finish_edit(event):
            self._finish_hud_edit()
            if event.keysym == "Escape":
                self._close_hud_drawer()
            elif submit := getattr(field, "_hud_submit", None):
                submit()
            self._focus_hud_game()
            return "break"

        def select_all(_event):
            field.selection_range(0, tk.END)
            return "break"

        field.bind("<Button-1>", guard(begin_edit))
        field.bind("<Return>", guard(finish_edit))
        field.bind("<KP_Enter>", guard(finish_edit))
        field.bind("<Escape>", guard(finish_edit))
        field.bind("<Control-a>", guard(select_all))

    @staticmethod
    def _set_hud_edit_activation(hwnd, enabled):
        old_style = u32.GetWindowLongW(hwnd, -20)
        style = old_style & ~0x08000000 if enabled else old_style | 0x08000000  # WS_EX_NOACTIVATE
        if old_style != style:
            u32.SetWindowLongW(hwnd, -20, style)

    def _finish_hud_edit(self):
        field, hwnd = self._hud_edit, self._hud_edit_window
        if field is None:
            return
        self._hud_edit = self._hud_edit_window = None
        self._hud_pressed_keys = None
        if hwnd and u32.IsWindow(hwnd):
            self._set_hud_edit_activation(hwnd, False)
        try:
            field.configure(highlightbackground=field._hud_border)
            if finish := getattr(field, "_hud_finish", None):
                finish()
        except (tk.TclError, ValueError):
            pass
        except Exception:
            log_overlay_exception("HUD")

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
        self._update_hud_play()

    def _update_hud_play(self):
        running = self._automation_running
        if self.travel_plan and not self._kafra_cycle:
            text = "Ⅱ Pausar viagem" if running else "▶ Retomar viagem"
        else:
            text = "Ⅱ Pause" if running else "▶ Play"
        display = (text, "#8b5cf6" if running else "#3b82f6")
        if getattr(self, "_hud_play_display", None) != display:
            self._hud_play_button.configure(text=display[0], bg=display[1])
            self._hud_play_display = display

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
        if win32_input.is_bot_clicking(0.65) or self._is_hud_foreground():
            return False
        pt = wintypes.POINT()
        if u32.GetCursorPos(ctypes.byref(pt)) and self._is_point_in_hud(pt.x, pt.y, margin=0):
            return False
        return any(u32.GetAsyncKeyState(key) & 0x8000 for key in (0x01, 0x02, 0x04))

    def _record_hud_click(self, sx, sy, left_button, clicked_at, fg):
        if (not self._hud_render_foreground or self._click_in_progress
                or not (self._automation_running or self._hud_edit is not None or self._hud_choice is not None)):
            return
        if self._is_hud_foreground(fg) or not self._is_game_focused(fg):
            return
        if win32_input.is_bot_clicking(0.65) or getattr(win32_input, "is_bot_keying", lambda *_: False)(0.65):
            return
        start, end = self._hud_click_grace
        if start <= clicked_at <= end:
            return
        if self._is_point_in_hud(sx, sy, margin=0):
            return
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
            if self._click_in_progress or win32_input.is_bot_clicking(0.65):
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
        if self._hud_edit is not None:
            return
        hwnd = self.get_game_hwnd(None)
        if hwnd:
            _, root_hwnd, _ = self._remember_game_window(hwnd)
            if u32.GetForegroundWindow() != root_hwnd:
                u32.SetForegroundWindow(root_hwnd)
        return hwnd

    def _show_hud_window(self, window, x, y, width=0, height=0, *, restack=True, move=True):
        opening = not window.winfo_ismapped()
        if opening:
            window.state("normal")
            window.update_idletasks()
        hwnd = u32.GetAncestor(window.winfo_id(), 2)
        if not hwnd:
            return False
        ws_ex_toolwindow, ws_ex_appwindow = 0x80, 0x40000
        previous_style = u32.GetWindowLongW(hwnd, -20)
        style = (previous_style | ws_ex_toolwindow) & ~ws_ex_appwindow
        if hwnd != self._hud_edit_window:
            style |= 0x08000000
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
        self._update_hud_play()
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
            self._locked_drop_gid = None
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
        if not target:
            return
        if self._kafra_cycle:
            print("[Viagem] O ciclo do armazém está em andamento.", flush=True)
            return
        self._close_hud_drawer()
        if self.start_travel_plan(target, mode=mode) and self.travel_plan:
            now = time.monotonic()
            self._hud_click_grace = (now - 0.3, now + 0.8)
            self._hud_manual_clicks.clear()
            self._set_hud_running(True)
            self._focus_hud_game()

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
            # Trava de segurança: verifica se a Kafra do mapa atual possui teleporte habilitado no JSON
            net = load_kafra_network()
            _, entry = get_kafra_network_map_entry(curr_map, net)
            if entry and "teleport" not in entry.get("services", []):
                self._click_debug(f"[Kafra] Kafra em {curr_map} não possui serviço de teleporte! Cancelando teleporte.")
                self._city_travel_state = None
                self.travel_plan = None
                return

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
                            if kafra_cell:
                                kx_i, ky_i = int(kafra_cell[0]), int(kafra_cell[1])
                                cached = self.feed.world.kafras_discovered.get(curr_map)
                                if not cached or cached.get("x") != kx_i or cached.get("y") != ky_i:
                                    self.feed.world._remember_kafra(
                                        self.feed.world.entities.get(("npc", int(item.key.partition(":")[2]))), item.name)
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


    def _poll_world_map_keys(self, foreground):
        if self._hud_edit is not None or not self._is_game_focused(foreground):
            self._world_map_keys = None
            return
        control = bool(u32.GetAsyncKeyState(0x11) & 0x8000)
        toggle = control and any(u32.GetAsyncKeyState(key) & 0x8000 for key in (0xDE, 0xC0))
        escape = bool(u32.GetAsyncKeyState(0x1B) & 0x8000)
        previous, self._world_map_keys = self._world_map_keys, (toggle, escape)
        if previous is None:
            return
        if toggle and not previous[0]:
            self._world_map_open = not self._world_map_open
            if not self._world_map_open:
                self._close_world_map_popup()
        if escape and not previous[1]:
            self._world_map_open = False
            self._close_world_map_popup()

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
        self._mark_hud_control_click()
        now = time.monotonic()
        self._hud_click_grace = (now, now + 1.0)
        self._hud_manual_clicks.clear()
        if hwnd := self.get_game_hwnd():
            self._focus_hud_game()
            win32_input.activate_game_window(hwnd)
        if self._kafra_cycle is None:
            self._kafra_manual = True
            self._close_hud_drawer()

    @classmethod
    def _kafra_locations_for_map(cls, current_map):
        if not current_map:
            return ()
        m = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        net = load_kafra_network()
        net_map, entry = get_kafra_network_map_entry(m, net)
        locs = []
        if entry and "locations" in entry:
            locs = [(loc["x"], loc["y"]) for loc in entry["locations"]]
        if locs:
            return tuple(locs)
        base = re.sub(r"_[a-z0-9]+$", "", m)
        return KAFRA_LOCATIONS.get(m) or KAFRA_LOCATIONS.get(base, ())

    def _find_nearest_kafra(self, world, current_map, position):
        locations = self._kafra_locations_for_map(current_map)
        with world.lock:
            candidates = []
            for (kind, gid), npc in world.entities.items():
                if kind != "npc":
                    continue
                cell = npc.track.target
                name = world.npc_name(gid, npc.catalog_id, cell, current_map)
                if npc.is_kafra or is_kafra(npc.catalog_id, name):
                    candidates.append((0, math.dist(position, cell), gid, cell, name, npc))
                elif cell in locations:
                    label = normalize_dialog_option(name)
                    service = ("cool event", "zonda", "drawer", "storekeeper", "storage", "armazem", "eventos incriveis", "pata de gato")
                    if not label or any(term in label for term in service):
                        candidates.append((1, math.dist(position, cell), gid, cell, name, npc))
        best = min(candidates, default=None)
        if best:
            gid, cell, name, npc_ent = best[2], best[3], best[4], best[5]
            # Auto-calibração com coordenadas confirmadas ao vivo via TCP
            x, y = int(cell[0]), int(cell[1])
            cached = world.kafras_discovered.get(current_map)
            if not cached or cached.get("x") != x or cached.get("y") != y:
                world._remember_kafra(npc_ent, name)
            return (gid, cell)
        return None

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

        # 1. Consulta kafra_network.json com normalização de instâncias
        has_ticket = player_has_storage_ticket(world)
        curr_m = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        net = load_kafra_network()
        net_map, entry = get_kafra_network_map_entry(curr_m, net)
        has_local_storage = entry and ("storage" in entry.get("services", []))

        travel_route = []
        travel_zeny = 0

        if has_local_storage:
            # Mapa atual (ou mapa-base) possui armazém no próprio mapa!
            if not has_ticket and zeny is not None and zeny < 40:
                blocker = "saldo insuficiente para abrir o armazém (mínimo de 40 Zeny ou Ticket de Armazém)."
                if manual or self._kafra_block != blocker:
                    print(f"[Kafra] {blocker}", flush=True)
                self._kafra_block = blocker
                return
            city = current_map
        else:
            # Mapa atual não possui Kafra com armazém: usa BFS ponderado por Zeny
            dest_city, travel_route, travel_zeny = find_cheapest_kafra_storage_route(curr_m, net)
            if not dest_city:
                print(f"[Kafra] Nenhuma cidade com armazém acessível de {current_map}.", flush=True)
                return
            total_req = travel_zeny + (0 if has_ticket else 40)
            if zeny is not None and zeny < total_req:
                blocker = f"saldo insuficiente ({zeny}z) para rota ({travel_zeny}z) e armazém ({0 if has_ticket else 40}z). Mínimo necessário: {total_req}z."
                if manual or self._kafra_block != blocker:
                    print(f"[Kafra] {blocker}", flush=True)
                self._kafra_block = blocker
                return
            city = dest_city

        self._kafra_capacity_latched = over_limit
        self._kafra_block = None
        try:
            vision = KafraVision()
        except (ImportError, OSError, ValueError) as exc:
            print(f"[Kafra] Captura visual indisponível ({exc}).", flush=True)
            return

        position = player.cell()
        live = self._find_nearest_kafra(world, city, position) if city == current_map else None
        cached = world.kafras_discovered.get(city) or (world.kafras_discovered.get(net_map) if net_map and city == current_map else None)
        locations = self._kafra_locations_for_map(city)
        if live:
            target = live[1]
        elif cached:
            target = cached["x"], cached["y"]
        elif locations:
            target = min(locations, key=lambda cell: math.dist(position, cell)) if city == current_map else locations[0]
        else:
            target = (150, 150)

        self._kafra_cycle = {
            "phase": "travel" if city != current_map else "approach",
            "city": city, "position": target,
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
            if travel_matches_map(current_map, destination):
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
                refused = "saldo suficiente" in dialog_text or "custa 50" in dialog_text or "custa 40" in dialog_text
                if refused:
                    world._set_player_zeny(0)
        try:
            if refused:
                raise ValueError("saldo insuficiente indicado no diálogo da Kafra. Operação abortada.")
            if phase == "approach":
                self._approach_kafra_storage(snapshot, world, hwnd, px, py, now, dialog, zeny)
            elif phase in ("dialog", "opening"):
                self._open_kafra_storage(snapshot, hwnd, now, phase, dialog, capacity, px=px, py=py, world=world)
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
        nearest = self._find_nearest_kafra(world, snapshot.map, (px, py))
        if nearest:
            gid, target = nearest
            cycle.update(position=target, approach_npc=gid)
            npc = next((item for item in snapshot.objects if item.key == f"npc:{gid}"), None)
            if npc and math.dist((px, py), target) <= 3.5:
                has_ticket = player_has_storage_ticket(world)
                if not has_ticket and zeny is not None and zeny < 40:
                    raise ValueError("saldo insuficiente ao chegar à Kafra (mínimo de 40 Zeny ou Ticket).")
                if dialog.get("open"):
                    raise ValueError("já existe um diálogo aberto.")
                self.nav_goal = self.patrol_target_route = None
                self._clear_patrol_route()
                left, top, width, height = snapshot.viewport
                if not self.patrol_move_and_click(left + npc.u * width, top + npc.v * height, hwnd=hwnd):
                    return
                click_ts = time.monotonic()
                cycle.update(phase="dialog", npc=gid,
                             seq=dialog.get("seq"), click_ts=click_ts, clicked_at=now,
                             retry_count=0, chat_confirmed=False, deadline=now + 25, next=now + 0.1)
                return
        else:
            cycle.pop("approach_npc", None)
        if math.dist((px, py), target) > 2:
            self.nav_goal = (snapshot.map, *map(int, target))
            self.active_nav_color = "#f59e0b"
            self._try_auto_patrol(snapshot, px, py)

    def _open_kafra_storage(self, snapshot, hwnd, now, phase, dialog, capacity, *, px=None, py=None, world=None):
        cycle = self._kafra_cycle
        if world is None:
            world = getattr(self.feed, "world", None)

        if phase == "opening":
            if capacity is not None or (world and getattr(world, "storage_capacity", None) is not None):
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

        if phase == "dialog":
            dialog_active = bool(getattr(world, "npc_dialog_active", False) or dialog.get("open") or dialog.get("active"))
            dialog_last_ts = float(getattr(world, "npc_dialog_last_ts", 0.0) or dialog.get("last_update", 0.0))
            dialog_opcode = int(getattr(world, "npc_dialog_opcode", 0) or dialog.get("opcode", 0))
            dialog_options = getattr(world, "npc_dialog_options", None) or dialog.get("options", ())
            click_ts = cycle.get("click_ts", cycle.get("clicked_at", now))

            chat_open = (
                (dialog_active and (dialog_last_ts >= click_ts - 0.05 or dialog_opcode in (0x00B4, 0x00B5, 0x00B7, 0x00F2, 0x0B18)))
                or (dialog_last_ts >= click_ts - 0.05 and dialog_opcode in (0x00B4, 0x00B5, 0x00B7, 0x00F2, 0x0B18))
            )

            if chat_open and not cycle.get("chat_confirmed"):
                cycle["chat_confirmed"] = True
                print(f"[Kafra] Chat aberto com sucesso (op=0x{dialog_opcode:04X})!", flush=True)

            if not cycle.get("chat_confirmed"):
                if (now - click_ts) >= 1.2:
                    retry_count = cycle.get("retry_count", 0) + 1
                    cycle["retry_count"] = retry_count
                    if retry_count > 4:
                        raise ValueError(f"falha ao abrir diálogo com a Kafra após {retry_count - 1} tentativas.")

                    print(f"[Kafra] Clique não abriu diálogo; reposicionando e tentando novamente (tentativa {retry_count}/4)...", flush=True)
                    left, top, width, height = snapshot.viewport
                    gid = cycle.get("npc")
                    npc = next((item for item in snapshot.objects if item.key == f"npc:{gid}"), None) if gid else None
                    if npc:
                        base_x = left + npc.u * width
                        base_y = top + npc.v * height
                    else:
                        kx, ky = cycle["position"]
                        u_proj, v_proj = snapshot.project(kx, ky, 5.0)
                        base_x = left + u_proj * width
                        base_y = top + v_proj * height

                    if retry_count == 1:
                        # Tentativa 1: re-clique na posição TCP atualizada da Kafra (y - 12px no tronco do sprite)
                        self.patrol_move_and_click(base_x, base_y - 12, hwnd=hwnd)
                        cycle.update(click_ts=time.monotonic(), clicked_at=now, next=now + 0.15)
                        return
                    elif retry_count == 2:
                        # Tentativa 2: caminhe para célula adjacente (dist 1..2 no .gat) e re-clique
                        gat = self.gat
                        kx, ky = cycle["position"]
                        cur_px = px if px is not None else (world.player.cell()[0] if world and world.player else kx)
                        cur_py = py if py is not None else (world.player.cell()[1] if world and world.player else ky)
                        candidates = []
                        for dx in (-2, -1, 0, 1, 2):
                            for dy in (-2, -1, 0, 1, 2):
                                if dx == 0 and dy == 0:
                                    continue
                                if 1.0 <= math.hypot(dx, dy) <= 2.2:
                                    cx, cy = int(kx + dx), int(ky + dy)
                                    if (cx, cy) != (int(cur_px), int(cur_py)) and gat and gat.is_walkable(cx, cy):
                                        candidates.append((cx, cy))
                        if candidates:
                            chosen = candidates[0]
                            u_w, v_w = snapshot.project(chosen[0], chosen[1], 0.0)
                            self.patrol_move_and_click(left + u_w * width, top + v_w * height, hwnd=hwnd)
                            time.sleep(0.1)
                        self.patrol_move_and_click(base_x, base_y - 12, hwnd=hwnd)
                        cycle.update(click_ts=time.monotonic(), clicked_at=now, next=now + 0.20)
                        return
                    elif retry_count == 3:
                        # Tentativa 3: offset vertical +10px e re-clique
                        self.patrol_move_and_click(base_x, base_y + 10, hwnd=hwnd)
                        cycle.update(click_ts=time.monotonic(), clicked_at=now, next=now + 0.15)
                        return
                    else:
                        # Tentativa 4: re-clique no centro
                        self.patrol_move_and_click(base_x, base_y, hwnd=hwnd)
                        cycle.update(click_ts=time.monotonic(), clicked_at=now, next=now + 0.15)
                        return
                return

            if dialog.get("npc_gid") and cycle.get("npc") and dialog.get("npc_gid") != cycle["npc"]:
                raise ValueError("diálogo recebido de outro NPC.")

            if dialog_opcode in (0x00F2, 0x0B18) or capacity is not None or (world and getattr(world, "storage_capacity", None) is not None):
                cycle.update(phase="store", deadline=now + 180, next=now + 0.25)
                return

            if dialog_opcode == 0x00B5 or dialog.get("type") == "wait":
                if dialog.get("seq") != cycle.get("handled_seq"):
                    cycle["handled_seq"] = dialog.get("seq")
                    self._send_game_key(0x0D)
                    cycle["next"] = now + 0.25
                return

            if dialog_opcode == 0x00B7 or dialog.get("type") == "menu":
                options = dialog_options or dialog.get("options", ())
                target_idx = None
                for index, option in enumerate(options):
                    norm = normalize_dialog_option(option)
                    if any(w in norm for w in ("usar o armazem", "usar o armazém", "armazem", "armazém", "storage", "guardar")):
                        target_idx = index
                        break
                if target_idx is not None:
                    self._send_menu_selection(target_idx)
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

    def _travel_route_from(self, world, current_map, position, target, excluded=()):
        with world.lock:
            zeny = world.player_zeny if world.zeny_known else None
            portals = [ent.track.target for (kind, _), ent in world.entities.items()
                       if kind == "warp" or ent.catalog_id == 45]
            kafras = [ent.track.target for (kind, gid), ent in world.entities.items()
                      if kind == "npc" and (ent.is_kafra or is_kafra(
                          ent.catalog_id, world.npc_name(gid, ent.catalog_id, ent.track.target, current_map)))]
        return calculate_travel_route(
            current_map, target, position=position, zeny=zeny, excluded=excluded,
            live_portals=portals, live_kafras=kafras, routes_data=self.world_routes_data,
        )

    def _reset_travel_leg(self, plan):
        plan.pop("leg", None)
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._reset_patrol(None, None)
        self.nav_goal = self.patrol_target_route = None
        self.active_nav_path = None

    def _finish_travel_plan(self, plan, reason=None):
        self.travel_plan = None
        self._reset_travel_leg(plan)
        if reason:
            print(f"[Viagem] {reason}", flush=True)
            if not self._kafra_cycle:
                self._set_hud_running(False)
            return
        target = self.feed.world.map
        if plan["mode"] == "farm":
            self.farm_map = target
            self.auto_farm = self.auto_loot = self.auto_patrol = True
        print(f"[Viagem] Chegada confirmada em {target}.", flush=True)

    def start_travel_plan(self, target_input, mode="travel"):
        world = getattr(self.feed, "world", None)
        current_map, player, _ = world.snapshot() if world else (None, None, ())
        if not current_map or player is None:
            print("[Viagem] Aguardando mapa e posição do personagem.", flush=True)
            return False
        target = str(target_input).strip().lower().removesuffix(".gat").removesuffix(".rsw")
        target = self.world_routes_data.get("aliases", {}).get(target, target)
        target = self.world_routes_data.get("cities", {}).get(target, {}).get("map", target).lower()
        if travel_matches_map(current_map, target):
            self._finish_travel_plan({"mode": mode})
            return True
        steps = self._travel_route_from(world, current_map, player.cell(), target)
        if not steps:
            print(f"[Viagem] Sem rota executável de {current_map} para {target} com o saldo disponível.", flush=True)
            return False
        self.travel_plan = {
            "target_map": target, "mode": mode, "steps": steps, "step_idx": 0,
            "excluded": set(),
        }
        self.manual_nav_goal = None
        self._pending_hud_interaction = None
        self._reset_travel_leg(self.travel_plan)
        print(f"[Viagem] {current_map} → {target}: {len(steps)} etapas, "
              f"{sum(step['cost'] for step in steps)}z previstos.", flush=True)
        return True

    def _replan_travel(self, plan, world, current_map, position, reason, failed_step=None):
        if failed_step:
            plan["excluded"].add(travel_step_key(failed_step))
        print(f"[Viagem] {reason} Recalculando a partir de {current_map} em {tuple(map(round, position))}.", flush=True)
        plan["replans"] = plan.get("replans", 0) + 1
        if plan["replans"] >= 12:
            self._finish_travel_plan(plan, f"Limite de alternativas para {plan['target_map']} atingido.")
            return
        steps = self._travel_route_from(world, current_map, position, plan["target_map"], plan["excluded"])
        if not steps:
            self._finish_travel_plan(plan, f"Sem alternativa executável para {plan['target_map']}.")
            return
        plan.update(steps=steps, step_idx=0)
        self._reset_travel_leg(plan)

    def _walk_travel_leg(self, snapshot, world, leg, position, target, *, portal=None, npc_gid=None):
        terrain = getattr(self.feed, "terrain", None)
        # O feed carrega o GAT do mapa atual. A execução não carrega mapas no loop do HUD.
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        now = time.monotonic()
        if gat is None:
            missing_since = leg.setdefault("missing_gat_since", now)
            if now - missing_since >= 8.0:
                return "missing_gat"
            return "waiting"
        leg.pop("missing_gat_since", None)
        start = tuple(map(round, position))
        walking = world._is_player_walking()
        if pending := leg.get("walk_click"):
            clicked_at, old_position, cell, point, enter_portal = pending
            aborted = self._touch_click_aborted(point, clicked_at)
            elapsed = now - self._touch_click_finished_at(point, clicked_at)
            if not aborted and (walking or elapsed < (2.0 if enter_portal else 0.85)):
                return "waiting"
            leg.pop("walk_click", None)
            if enter_portal or aborted or math.dist(position, old_position) < 0.8:
                leg["path"] = None
                if not enter_portal:
                    leg["blocked"].add(cell)
                leg["walk_failures"] = leg.get("walk_failures", 0) + 1
                if leg["walk_failures"] >= 5:
                    return "blocked"
            else:
                leg["walk_failures"] = 0
        if walking:
            return "waiting"

        height = gat.get_elevation
        project = self.feed.camera.projector(
            snapshot.viewport, getattr(self.feed, "_cam", None) or position, height,
        )
        resolve = self._safe_click_resolver(snapshot, project, allowed_portal=portal, allowed_npc=npc_gid)
        blocked = self._get_all_blocked_cells() | leg["blocked"]
        if portal:
            blocked.discard(portal)

        def ground_point(cell):
            if (cell in blocked or not gat.is_walkable(*cell)
                    or not self._has_gat_line_of_sight(*position, *cell, gat)):
                return None
            return resolve(cell)

        def click(cell, point, enter_portal=False):
            if self.patrol_move_and_click(*point, hwnd=self.get_game_hwnd(),
                                          retry_key=(snapshot.map, "ground", cell)):
                leg["walk_click"] = (now, position, cell, point, enter_portal)
            return "waiting"

        if portal and math.dist(position, portal) <= 3.0:
            if start != portal:
                attempts = leg.get("portal_attempts", 0)
                if attempts and attempts % 2:
                    nearby = sorted(self._patrol_cells_in_radius(*portal, 1),
                                    key=lambda cell: (math.dist(position, cell), cell))
                    for cell in nearby:
                        if cell not in (portal, start) and (point := ground_point(cell)):
                            leg["portal_attempts"] = attempts + 1
                            return click(cell, point, enter_portal=True)
                if point := resolve(portal):
                    leg["portal_attempts"] = attempts + 1
                    return click(portal, point, enter_portal=True)
            else:
                neighbors = self._patrol_cells_in_radius(*start, 2)
                for cell in neighbors:
                    if cell != start and (point := ground_point(cell)):
                        return click(cell, point)
                return "blocked"
        elif not portal and math.dist(position, target) <= 3.4:
            return "approached"

        path_key = target, portal, npc_gid
        path = leg.get("path")
        if path and leg.get("path_key") == path_key:
            closest = min(range(len(path)), key=lambda idx: math.dist(position, path[idx]))
            if math.dist(position, path[closest]) <= 3.0:
                path = path[closest:]
                leg["path"] = path
            else:
                path = None
        else:
            path = None
        if not path:
            job = leg.get("path_job")
            if job and not job["done"]:
                return "waiting"
            if job and job["key"] == path_key:
                leg.pop("path_job", None)
                path = job["path"]
                if len(path) <= 1:
                    return "blocked"
                closest = min(range(len(path)), key=lambda idx: math.dist(position, path[idx]))
                if math.dist(position, path[closest]) > 3:
                    return "waiting"
                path = path[closest:]
                leg["path"], leg["path_key"] = path, path_key
            else:
                job = {"key": path_key, "done": False, "path": []}
                leg["path_job"] = job

                def find_path():
                    try:
                        if portal and gat.is_walkable(*target):
                            candidate = gat.find_path(start, target, blocked_cells=blocked, max_iterations=18000)
                            if candidate and candidate[-1] == target:
                                job["path"] = candidate
                        else:
                            radius = 2 if portal else 3
                            approaches = sorted(
                                (cell for cell in self._patrol_cells_in_radius(*target, radius)
                                 if cell != start and cell not in blocked and gat.is_walkable(*cell)),
                                key=lambda cell: math.dist(start, cell),
                            )
                            for goal in approaches[:5]:
                                candidate = gat.find_path(start, goal, blocked_cells=blocked, max_iterations=9000)
                                if candidate and candidate[-1] == goal:
                                    job["path"] = candidate
                                    break
                    except (OSError, ValueError) as exc:
                        print(f"[Viagem] Erro ao calcular caminho em {snapshot.map}: {exc}", flush=True)
                    finally:
                        job["done"] = True

                threading.Thread(target=find_path, daemon=True, name="travel-path").start()
                return "waiting"

        self.active_nav_path = path
        self.active_nav_color = "#22D3EE"
        # O clique segue a ordem do A*, evitando saltar para outro trecho próximo em uma curva.
        stride = next(((cell, point) for cell in reversed(path[:15])
                       if 1.0 <= math.dist(position, cell) <= 14 and (point := ground_point(cell))), None)
        if stride:
            leg["side_steps"] = 0
            return click(*stride)
        if len(path) > 1 and leg.get("side_steps", 0) < 2:
            next_cell = path[min(len(path) - 1, 4)]
            neighbors = sorted(self._patrol_cells_in_radius(*start, 2),
                               key=lambda cell: math.dist(cell, next_cell))
            for cell in neighbors:
                if cell != start and (point := ground_point(cell)):
                    leg["side_steps"] = leg.get("side_steps", 0) + 1
                    return click(cell, point)
        return "blocked"

    def _execute_travel_kafra(self, snapshot, world, leg, step, position, now):
        target = step["kafra_pos"]
        with world.lock:
            dialog = dict(world.npc_dialog)
            zeny = world.player_zeny if world.zeny_known else None
            nearby = [(math.dist(ent.track.target, target), gid, ent.track.target)
                      for (kind, gid), ent in world.entities.items() if kind == "npc"
                      and math.dist(ent.track.target, target) <= 14
                      and (ent.is_kafra or is_kafra(ent.catalog_id, world.npc_name(
                          gid, ent.catalog_id, ent.track.target, snapshot.map)))]
            dialog_gid = world.aid_to_gid.get(dialog.get("npc_gid"), dialog.get("npc_gid"))
        if zeny is not None and zeny < step["cost"]:
            return "Saldo insuficiente para o teleporte."
        live = min(nearby, default=None)
        npc_gid = live[1] if live else leg.get("npc_gid")
        if live:
            target = tuple(map(round, live[2]))
            leg["npc_gid"] = npc_gid

        if dialog.get("open"):
            if not leg.get("npc_click_at") or (dialog_gid and dialog_gid != npc_gid):
                if now >= leg.get("next_dialog_at", 0.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 1.2
                return None
            seq = dialog.get("seq")
            if now < leg.get("next_dialog_at", 0.0):
                return None
            if seq != leg.get("dialog_seq"):
                leg["dialog_seq"] = seq
                leg["dialog_tries"] = 0
            if leg.get("dialog_tries", 0) >= 2:
                self._send_game_key(0x1B)
                return "O diálogo da Kafra não avançou."
            dialog_type = dialog.get("type")
            options = dialog.get("options") or ()
            if dialog_type == "wait":
                if not self._send_game_key(0x0D):
                    return None
            elif dialog_type in ("menu", "warplist"):
                normalized = [normalize_dialog_option(option) for option in options]
                aliases = {
                    "gef_fild10": ("caverna dos orcs", "vila dos orcs", "orc dungeon"),
                    "mjolnir_02": ("mina abandonada", "mjolnir"),
                    "cmd_fild07": ("farol de pharos", "pharos"),
                }
                destination = next((idx for idx, option in enumerate(normalized) if
                                    matches_city(option, step["to_city"]) or
                                    any(label in option for label in aliases.get(step["to_city"], ()))), None)
                teleport = next((idx for idx, option in enumerate(normalized)
                                 if "teleport" in option or "warp" in option), None)
                index = destination if destination is not None else teleport
                if index is None:
                    self._send_game_key(0x1B)
                    return f"A Kafra não ofereceu {step['to_city']} no diálogo."
                self._send_menu_selection(index)
            else:
                return None
            leg["dialog_tries"] = leg.get("dialog_tries", 0) + 1
            leg["next_dialog_at"] = now + 2.0 + len(options) * 0.10
            return None

        if now < max(leg.get("next_dialog_at", 0.0), leg.get("npc_click_at", 0.0) + 1.5):
            return None
        status = self._walk_travel_leg(snapshot, world, leg, position, target, npc_gid=npc_gid)
        if status == "missing_gat":
            return f"Terreno .gat de {snapshot.map} indisponível."
        if status == "blocked":
            return f"Sem caminho até a Kafra em {target}."
        if status != "approached":
            return None
        if leg.get("npc_clicks", 0) >= 3:
            return "A Kafra não abriu o diálogo."
        if not live:
            if "missing_npc_since" not in leg:
                leg["missing_npc_since"] = now
            if now - leg["missing_npc_since"] >= 4.0:
                return f"Nenhuma Kafra de teleporte identificada em {target}."
            return None
        item = next((obj for obj in snapshot.objects if obj.key == f"npc:{npc_gid}"), None)
        if item:
            left, top, width, height = snapshot.viewport
            point = (left + int(item.u * width), top + int(item.v * height))
            if self.patrol_move_and_click(*point, hwnd=self.get_game_hwnd()):
                leg["npc_click_at"] = now
                leg["npc_clicks"] = leg.get("npc_clicks", 0) + 1
        return None

    def _handle_travel_plan(self, snapshot, px, py):
        plan = self.travel_plan
        if not plan or px is None or not self._game_is_foreground():
            return
        world = self.feed.world
        current_map = snapshot.map
        if self.current_viewport != snapshot.viewport or world.map != current_map:
            return
        position = px, py
        if travel_matches_map(current_map, plan["target_map"]):
            self._finish_travel_plan(plan)
            return
        if plan["step_idx"] >= len(plan["steps"]):
            self._replan_travel(plan, world, current_map, position, "Etapas concluídas fora do destino.")
            return

        step = plan["steps"][plan["step_idx"]]
        portal_step = step["type"] == "portal"
        source = step["from_map" if portal_step else "from_city"]
        destination = step["to_map" if portal_step else "to_city"]
        if travel_matches_map(current_map, destination):
            plan["step_idx"] += 1
            self._reset_travel_leg(plan)
            return
        if not travel_matches_map(current_map, source):
            self._replan_travel(plan, world, current_map, position, "Chegada em mapa diferente do previsto.")
            return

        now = time.monotonic()
        if "leg" not in plan:
            plan["leg"] = {"deadline": now + 120, "last_tick": now, "blocked": set()}
            cell = (step["wx"], step["wy"]) if portal_step else step["kafra_pos"]
            print(f"[Viagem] {step['type']} {current_map} {cell} → {destination}.", flush=True)
        leg = plan["leg"]
        leg["deadline"] += max(0.0, now - leg["last_tick"] - 1.0)
        leg["last_tick"] = now
        if now >= leg["deadline"]:
            failure = f"Prazo da etapa para {destination} esgotado."
        elif portal_step:
            if world.npc_dialog.get("open"):
                if now >= leg.get("next_dialog_at", 0.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 1.2
                return
            target = step["wx"], step["wy"]
            observed = self._get_portal_blocked_cells(radius=0, allow_current_goal=False)
            target = min((cell for cell in observed if math.dist(cell, target) <= 6),
                         key=lambda cell: math.dist(cell, target), default=target)
            self._travel_allowed_portal = target
            status = self._walk_travel_leg(snapshot, world, leg, position, target, portal=target)
            failure = {
                "blocked": f"Sem passagem pelo portal em {target}.",
                "missing_gat": f"Terreno .gat de {current_map} indisponível.",
            }.get(status)
        else:
            failure = self._execute_travel_kafra(snapshot, world, leg, step, position, now)
        if failure:
            self._replan_travel(plan, world, current_map, position, failure, failed_step=step)

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
            self._hud_travel(entry["map"], mode="travel")
            if self.travel_plan:
                self._send_game_key(0x1B, restore_focus=True)

        def _on_farm():
            self._close_world_map_popup()
            self._world_map_open = False
            self._hud_travel(entry["map"], mode="farm")
            if self.travel_plan:
                self._send_game_key(0x1B, restore_focus=True)

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
        except Exception:
            log_overlay_exception("REFRESH")
        finally:
            self.window.after(16 if self._hud_render_foreground else 40, self.refresh)

    def _refresh_frame(self):
        camera = getattr(self.feed, "camera", None)
        if isinstance(camera, CameraTracker):
            camera.drain_mouse_events()
        if self._hud_tray_restore.is_set():
            self._hud_tray_restore.clear()
            self._restore_hud()

        viewport_provider = getattr(self.feed, "viewport", None)
        game_hwnd = getattr(viewport_provider, "handle", None) or self._game_window[0]
        game_window = self._remember_game_window(game_hwnd)
        fg = u32.GetForegroundWindow() or 0
        self._poll_world_map_keys(fg)
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
                try:
                    if self.draw(snapshot, stats):
                        self.last = snapshot
                        self.last_stats = stats
                except Exception:
                    log_overlay_exception("DRAW")
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
            except Exception:
                log_overlay_exception("_HANDLE_ACTIONS")
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
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._retreat_started_at = 0.0
        self._retreat_safe_since = None
        self._survival_next_action = 0.0
        self._retreat_click = None
        self._last_retreat_target = None
        self._retreat_goal = None
        self._retreat_portal_attempts = {}
        self._last_retreat_step_at = 0.0

    def _clear_recovery_navigation(self):
        self._clear_patrol_route()
        self.manual_nav_goal = None
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._last_patrol_click_target = None
        self._locked_mob_gid = None
        self.locked_combat_mob_gid = None
        self._locked_drop_gid = None
        self._combat_mob_gid = None
        self._mob_lock_until = self._drop_lock_until = 0.0
        self._engagement = None
        self._engage_state = None
        self.post_kill_wait_until = 0.0
        self.last_kill_pos = None

    def _handle_respawn(self, world, current_map, now):
        with world.lock:
            dead = world.player_is_dead or (world.player_hp == 0 and world.player_max_hp > 0)
            death_ts = world.last_player_death_ts
            warp_ts = world.last_map_warp_ts
            warped = bool(warp_ts > death_ts)
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
        if dead and hp <= 0 and not warped:
            if now >= self._respawn_next_attempt:
                hwnd = self.get_game_hwnd()
                if hwnd:
                    bounds = wintypes.RECT()
                    if u32.GetClientRect(hwnd, ctypes.byref(bounds)) and bounds.right > 0 and bounds.bottom > 0:
                        client_w = bounds.right
                        client_h = bounds.bottom
                        cx = int(client_w * 0.50)
                        cy = int(client_h * 0.608)
                        pt = wintypes.POINT(cx, cy)
                        if u32.ClientToScreen(hwnd, ctypes.byref(pt)):
                            sx, sy = pt.x, pt.y
                        else:
                            origin = wintypes.POINT()
                            if u32.ClientToScreen(hwnd, ctypes.byref(origin)):
                                sx, sy = origin.x + cx, origin.y + cy
                            else:
                                sx, sy = cx, cy
                        self.touch_postmsg(sx, sy, game_hwnd=hwnd)
                        self._click_debug(f"RESSUSCITAR: Clique em Voltar ao Ponto de Retorno ({sx}, {sy}).")
                self._send_game_key(0x0D)
                self._respawn_attempts += 1
                self._respawn_next_attempt = now + 2.5
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

    def _retreat_portal_cells(self, current_map):
        cells = self._get_portal_blocked_cells(radius=0, allow_current_goal=False)
        cells.update(tuple(cell) for (source, _), cell in MAP_PORTALS.items() if source == current_map)
        cells.update(tuple(cell) for (source, _), cell in KAFRA_STORAGE_PORTALS.items() if source == current_map)
        cells.update((int(portal["wx"]), int(portal["wy"]))
                     for portal in self.world_routes_data.get("portals", ())
                     if portal["from_map"].lower() == current_map)
        cells.update(tuple(map(int, cell)) for link, cell in load_learned_warps().items()
                     if link.lower().partition("->")[0] == current_map
                     and isinstance(cell, (list, tuple)) and len(cell) == 2)
        return cells

    def _ensure_sitting(self, should_sit: bool):
        if self._automation_click_active() or self.feed.world.player_is_sitting == should_sit:
            return
        now = time.monotonic()
        if now - self._last_sit_attempt_at < 2.0:
            return
        self._last_sit_attempt_at = now
        if self._send_game_key(0x2D):
            self._sit_confirmation = (should_sit, now + 1.0)

    def _move_on_retreat_path(self, snapshot, gat, player_cell, path, mobs, *, portal=None, blocked=()):
        self.active_nav_path = path
        self.active_nav_color = "#F87171"
        if not path or self._automation_click_active() or self._manual_mouse_button_pressed():
            return

        world = self.feed.world
        now = time.monotonic()
        project = self.feed.camera.projector(
            snapshot.viewport, getattr(self.feed, "_cam", None) or player_cell, gat.get_elevation)
        resolve = self._safe_click_resolver(snapshot, project, allowed_portal=portal)

        left, top, width, height = snapshot.viewport
        monster_centers = [(left + u * width, top + v * height)
                           for mob in mobs if math.dist(mob, player_cell) <= 28
                           for u, v in (project(*mob, 5.5),)]

        def ground_point(cell):
            if (cell in blocked or self.temp_blocked_cells.get(cell, 0.0) > now
                    or not gat.is_walkable(*cell)
                    or not self._has_gat_line_of_sight(*player_cell, *cell, gat)):
                return None
            point = resolve(cell)
            if point and not any(abs(point[0] - mx) <= 55 and my - 75 <= point[1] <= my + 25
                                 for mx, my in monster_centers):
                return point
            return None

        stride = None
        if portal and math.dist(player_cell, portal) <= 4 and (point := ground_point(portal)):
            stride = (portal, point)
        if stride is None:
            stride = self._pick_long_stride_cell(path, 9, 14, position=player_cell, ground_point=ground_point)
        if stride is None:
            stride = self._pick_long_stride_cell(path, 1, 9, position=player_cell, ground_point=ground_point)
        if stride is None and len(path) > 1:
            start = tuple(map(round, player_cell))
            goal = path[min(len(path) - 1, 4)]
            neighbors = ((start[0] + dx, start[1] + dy)
                         for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)
            stride = next(((cell, point) for cell in sorted(neighbors, key=lambda cell: math.dist(cell, goal))
                           if (point := ground_point(cell))), None)
        if stride:
            cell, point = stride
            def safe_retreat_point(_lead):
                if not self._game_is_foreground():
                    return None
                with world.lock:
                    if world.map != snapshot.map or self.current_viewport != snapshot.viewport:
                        return None
                    current_mobs = [ent.track.cell() for (kind, _), ent in world.entities.items() if kind == "mob"]
                    current_player = world.player.cell() if world.player else player_cell
                live_project = self.feed.camera.projector(
                    snapshot.viewport, getattr(self.feed, "_cam", None) or current_player, gat.get_elevation)
                for mob in current_mobs:
                    if math.dist(mob, current_player) <= 28:
                        u, v = live_project(*mob, 5.5)
                        mx, my = left + u * width, top + v * height
                        # Inclui a margem máxima do microdeslocamento de uma retentativa.
                        if abs(point[0] - mx) <= 60 and my - 80 <= point[1] <= my + 30:
                            return None
                return point

            if self.patrol_move_and_click(*point, hwnd=self.get_game_hwnd(), target_point=safe_retreat_point,
                                          retry_key=(snapshot.map, "ground", cell)):
                self._last_retreat_target = cell
                self._last_retreat_step_at = now
                self._retreat_click = (now, player_cell, cell, point, portal == cell)
                self._click_debug(f"FUGA: Passo para {cell} em {point}")

    def _handle_survival(self, snapshot, world, player, current_map, now, hp_pct, entities):
        cfg = self.filters_config
        if not cfg["survival_enabled"]:
            if self._survival_state:
                self._reset_survival()
                self.active_nav_path = None
            return False

        if self._survival_state is None:
            if (not current_map or not player or world.player_max_hp <= 0 or world.player_is_dead
                    or hp_pct > cfg["survival_hp_pct"]):
                return False
            self._reset_survival()
            self._survival_state = SURVIVAL_RETREAT
            self.retreat_origin_map = current_map
            self._retreat_started_at = now
            self.travel_plan = None
            self._pending_hud_interaction = None
            self._clear_recovery_navigation()
            self._click_debug(f"SOBREVIVÊNCIA: Fugindo de {current_map} com {hp_pct:.0f}% de HP.")
            if self._kafra_cycle:
                cycle = self._kafra_cycle
                if cycle["phase"] in ("dialog", "opening", "store", "closing"):
                    self._send_game_key(0x1B)
                self._finish_kafra_storage("ciclo interrompido pela rotina de sobrevivência.", current_map)
                self.auto_farm, self.auto_loot, self.auto_patrol = cycle["modes"]
                self._set_hud_running(True)
                return True

        if not player or not current_map:
            return True
        player_cell = player.cell()
        start = tuple(map(round, player_cell))
        walking = world._is_player_walking()

        if self._survival_state == SURVIVAL_RETREAT and current_map != self.retreat_origin_map:
            arrival = world.last_map_arrival
            if not arrival or arrival[0] != current_map or arrival[2] < self._retreat_started_at:
                return True
            self.retreat_return_portal_pos = arrival[1]
            self._survival_state = SURVIVAL_RECOVER
            self._retreat_click = None
            self._last_retreat_target = None

        if self._survival_state == SURVIVAL_RECOVER:
            self.active_nav_path = None
            if not walking:
                if not world.player_is_sitting:
                    self._ensure_sitting(True)
                elif hp_pct >= cfg["survival_resume_pct"]:
                    self._survival_state = SURVIVAL_RETURN
                    self._survival_next_action = 0.0
            return True

        if self._survival_state == SURVIVAL_RETURN:
            crossed_back = (current_map == self.retreat_origin_map == world.last_map_warp
                            and world.last_map_warp_ts >= self._retreat_started_at)
            if crossed_back or self.retreat_return_portal_pos is None:
                if world.player_is_sitting:
                    self._ensure_sitting(False)
                    return True
                self._reset_survival()
                self.active_nav_path = None
                self.farm_map = current_map
                return True

        if world.player_is_sitting:
            self._ensure_sitting(False)
            return True
        if world.vending_window_open and self._dismiss_accidental_vending_window(world, player, now):
            return True
        if self._retreat_click:
            clicked_at, previous, clicked_cell, point, is_portal = self._retreat_click
            if self._touch_click_aborted(point, clicked_at):
                self._retreat_click = None
            elif self._movement_confirmed(world, clicked_at, player_cell, previous):
                self._retreat_click = None
                if is_portal:
                    key = (current_map, clicked_cell)
                    self._retreat_portal_attempts[key] = self._retreat_portal_attempts.get(key, 0) + 1
            elif now - self._touch_click_finished_at(point, clicked_at) < 0.85:
                return True
            else:
                self._retreat_click = None
                if is_portal:
                    key = (current_map, clicked_cell)
                    self._retreat_portal_attempts[key] = self._retreat_portal_attempts.get(key, 0) + 1
                else:
                    self.temp_blocked_cells[clicked_cell] = now + 25.0
        if walking:
            self._retreat_safe_since = None
            if now - self._last_retreat_step_at < 0.65:
                return True
        if now < self._survival_next_action:
            return True
        self._survival_next_action = now + 0.25

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(current_map) if terrain else None
        mobs = [ent.track.cell() for (kind, _), ent in entities if kind == "mob"]
        mob_cells = {tuple(map(round, cell)) for cell in mobs}
        costs = {}
        for x, y in mob_cells:
            for dx in range(-4, 5):
                for dy in range(-4, 5):
                    if dx * dx + dy * dy <= 16:
                        costs[x + dx, y + dy] = 35
        blocked = mob_cells | self._npc_dialog_blocked_cells(now)
        blocked.update(cell for cell, until in self.temp_blocked_cells.items() if until > now)
        portals = self._retreat_portal_cells(current_map)

        returning = self._survival_state == SURVIVAL_RETURN
        if returning and current_map == self.retreat_origin_map:
            return True
        if not returning and not portals:
            clear = all(math.dist(player_cell, mob) >= 10 for mob in mobs)
            approaching_goal = self._retreat_goal and math.dist(player_cell, self._retreat_goal) > 1.5
            if clear and not walking and (not approaching_goal or self._retreat_goal in blocked):
                if self._retreat_safe_since is None:
                    self._retreat_safe_since = now
                elif now - self._retreat_safe_since >= 2.0:
                    self._survival_state = SURVIVAL_RECOVER
                    self.active_nav_path = None
                return True
            self._retreat_safe_since = None
        if gat is None:
            return True

        if portals or returning:
            goals = ([self.retreat_return_portal_pos] if returning
                     else sorted(portals, key=lambda cell: (math.dist(start, cell), cell)))
            for goal in goals:
                key = current_map, goal
                attempts = self._retreat_portal_attempts.get(key, 0)
                near_portal = math.dist(player_cell, goal) <= 5
                if near_portal and walking:
                    return True
                if attempts >= 3 and not walking:
                    if attempts == 3:
                        self._retreat_portal_attempts[key] = 4
                        self._click_debug(f"SOBREVIVÊNCIA: Travessia em {goal} não confirmada; aguardando servidor.")
                    continue
                barriers = blocked | (portals - {goal})
                if start == goal:
                    # Reentrar na célula é necessário se o personagem já chegou sobre o portal.
                    for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2), (1, 0), (-1, 0), (0, 1), (0, -1)):
                        step = start[0] + dx, start[1] + dy
                        if step not in barriers and gat.is_walkable(*step):
                            self._move_on_retreat_path(snapshot, gat, player_cell,
                                                       [start, step], mobs, blocked=barriers)
                            if self._retreat_click:
                                break
                    return True
                path = gat.find_path(start, goal, blocked_cells=barriers, cell_costs=costs)
                if path and path[-1] == goal:
                    self._move_on_retreat_path(snapshot, gat, player_cell, path, mobs, portal=goal, blocked=barriers)
                    return True
            return True

        def clearance(cell):
            return min((math.dist(cell, mob) for mob in mobs), default=math.inf)

        goal = self._retreat_goal
        if goal and goal not in blocked and clearance(goal) >= 11:
            path = gat.find_path(start, goal, blocked_cells=blocked, cell_costs=costs)
            if len(path) > 1 and path[-1] == goal:
                self._move_on_retreat_path(snapshot, gat, player_cell, path, mobs, blocked=blocked)
                return True

        best = None
        seen = set()
        start_clearance = clearance(start)

        def consider(cell):
            nonlocal best
            if cell != start and cell not in seen:
                seen.add(cell)
                room = clearance(cell)
                score = (room, math.dist(start, cell))
                if room > start_clearance and (best is None or score > best[0]):
                    best = score, cell
            return False

        gat.find_path(start, None, blocked_cells=blocked, cell_costs=costs, goal_test=consider)
        if best:
            self._retreat_goal = best[1]
            path = gat.find_path(start, best[1], blocked_cells=blocked, cell_costs=costs)
            if path and path[-1] == best[1]:
                self._move_on_retreat_path(snapshot, gat, player_cell, path, mobs, blocked=blocked)
        return True

    def _dismiss_accidental_npc_dialog(self, world, player, current_map, entities, now):
        with world.lock:
            dialog = dict(world.npc_dialog)
            aid = world.aid_to_gid.get(dialog.get("npc_gid"), dialog.get("npc_gid"))
        opcode = dialog.get("opcode")
        updated = dialog.get("last_update", 0.0)
        stamp = (current_map, updated, dialog.get("seq"))
        if opcode not in (0x00B4, 0x00B5, 0x00B6, 0x00B7, 0x00C4, 0x00C6):
            self._npc_dismissal = None
            return False

        directed = getattr(self, "_hud_npc_line_target", None)
        intentional = getattr(self, "_intentional_npc_dialog", None)
        if (self._kafra_cycle or self._city_travel_state or self._pending_hud_interaction
                or (directed and directed[0] == f"npc:{aid}" and directed[1] == current_map
                    and updated <= directed[2])
                or (aid and intentional == (current_map, aid))):
            self._intentional_npc_dialog = (current_map, aid) if dialog.get("open") else None
            self._handled_npc_dialog = stamp
            self._npc_dismissal = None
            return False
        self._intentional_npc_dialog = None

        if stamp != getattr(self, "_handled_npc_dialog", None):
            self._handled_npc_dialog = stamp
            self._npc_dismissal = None
            if now - updated > 2.0:
                return False
            if player:
                position = player.cell()
                nearby = [(math.dist(position, npc.track.target), gid, npc)
                          for (kind, gid), npc in entities if kind == "npc"]
                closest = min(nearby, default=None, key=lambda row: (row[0], row[1]))
                if closest and closest[0] <= 5.0:
                    with world.lock:
                        if npc := world.entities.get(("npc", closest[1])):
                            npc.avoid_until = now + 25.0
            if opcode == 0x00B7:
                exits = {"sair", "cancelar", "fechar", "nada", "nao", "desistir", "voltar", "exit", "cancel", "close"}
                options = dialog.get("options", ())
                fallback = max(0, len(options) - 1)
                index = next((i for i, option in enumerate(options)
                              if exits.intersection(normalize_dialog_option(option).split())), fallback)
                keys = [0x28] * index + [0x0D] if options else [0x1B]
            elif opcode == 0x00B4:
                keys = [0x1B]
            elif opcode == 0x00B5:
                keys = [0x0D]
            else:
                keys = [0x1B]
            self._npc_dismissal = (stamp, keys, updated + 0.16)

        dismissal = getattr(self, "_npc_dismissal", None)
        if not dismissal:
            return False
        _, keys, due = dismissal
        if now < due:
            return True
        if not keys:
            self._npc_dismissal = None
            return False
        key = keys.pop(0)
        self._send_game_key(key)
        if keys:
            self._npc_dismissal = (stamp, keys, now + 0.075)
        elif opcode in (0x00B5, 0x00B7):
            self._npc_dismissal = (stamp, [0x1B], now + 0.40)
            # Uma resposta nova do servidor substitui esta tentativa.
            with world.lock:
                if world.npc_dialog.get("last_update") == updated:
                    world.npc_dialog["open"] = False
            if key == 0x1B:
                self._npc_dismissal = None
        else:
            self._npc_dismissal = None
            with world.lock:
                if world.npc_dialog.get("last_update") == updated:
                    world.npc_dialog["open"] = False
        return True

    def _find_vending_close_button(self, viewport):
        if not viewport:
            return None
        vision = getattr(self, "_vending_vision", None)
        if vision is None:
            try:
                vision = KafraVision()
            except (ImportError, OSError, ValueError):
                vision = False
            self._vending_vision = vision
        if not vision or vision.ocr is None:
            return None

        left, top, width, height = viewport
        x0, y0 = width // 7, height // 9
        screen = vision.capture(viewport)[y0:height * 4 // 5, x0:width * 6 // 7]
        words = vision._ocr_words(screen)
        purchase = [row for row in words if row[0] in ("comprar", "buy", "compra", "purchase")]
        for label, x, y, w, h in words:
            if label in ("cancel", "cancelar") and any(
                abs(y - buy_y) <= max(h, buy_h) and abs(x - buy_x) <= 180
                for _, buy_x, buy_y, _, buy_h in purchase
            ):
                return left + x0 + x + w // 2, top + y0 + y + h // 2

        # Usa o X do tema somente perto de um cabeçalho de loja identificado.
        for label, x, y, w, h in words:
            if label not in ("loja", "shop", "vending") or not any(
                35 <= buy_y - y <= 420 and abs(buy_x - x) <= 380 for _, buy_x, buy_y, _, _ in purchase
            ):
                continue
            edge = x + w
            for mark, mx, my, mw, mh in words:
                if mark == "x" and edge < mx <= x + 500 and abs(my - y) <= max(h, mh):
                    return left + x0 + mx + mw // 2, top + y0 + my + mh // 2
        return None

    def _dismiss_accidental_vending_window(self, world, player, now):
        if not world.vending_window_open:
            return False
        opened = world.vending_opened_ts
        stamp, stage, ready_at = getattr(self, "_vending_dismissal", (None, 0, 0.0))
        if stamp != opened:
            if not self._send_game_key(0x1B):
                return True
            self._vending_dismissal = opened, 1, time.monotonic() + 0.8
            if player:
                with world.lock:
                    merchants = (ent for (_, eid), ent in world.entities.items()
                                 if ent.has_vending_shop or eid in world.vending_aids)
                    nearest = min(merchants, key=lambda ent: math.dist(player.cell(), ent.track.cell()),
                                  default=None)
                    if nearest:
                        nearest.avoid_until = max(nearest.avoid_until, now + 30.0)
                        self.temp_blocked_cells[tuple(map(round, nearest.track.cell()))] = now + 30.0
            return True
        if now < ready_at:
            return True
        if stage == 1:
            hwnd = self.get_game_hwnd()
            try:
                point = self._find_vending_close_button(self.current_viewport) if self._game_is_foreground() else None
            except (OSError, ValueError):
                point = None
            if (point and not self._is_point_in_hud(*point, margin=10)
                    and self._schedule_game_click(*point, hwnd)):
                self._vending_dismissal = opened, 2, time.monotonic() + 0.8
                return True
            if not self._send_game_key(0x1B):
                return True
            self._vending_dismissal = opened, 2, time.monotonic() + 0.8
            return True
        with world.lock:
            if world.vending_opened_ts == opened:
                world.vending_window_open = False
        return True

    def _automation_click_active(self):
        return self._click_in_progress or win32_input.is_bot_clicking(0.0)

    def _handle_actions(self, snapshot):
        if self._automation_click_active():
            return
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
        if self._sit_confirmation:
            expected, deadline = self._sit_confirmation
            if world.player_is_sitting == expected:
                self._sit_confirmation = None
            elif now >= deadline:
                self._sit_confirmation = None
                print("[Sentar] Cliente não confirmou a mudança de postura.", flush=True)
        if self._kafra_cycle and world.player_is_dead:
            self._finish_kafra_storage("ciclo interrompido pela morte do personagem.", curr_m)
        if self._handle_respawn(world, curr_m, now):
            return
        if (self.auto_farm or self.auto_patrol) and self.farm_map is None and curr_m:
            self.farm_map = curr_m
        hp_pct = 100.0 * world.player_hp / world.player_max_hp if world.player_max_hp > 0 else 100.0
        if self._handle_survival(snapshot, world, player, curr_m, now, hp_pct, entities):
            return
        if self.travel_plan:
            if self._kafra_cycle:
                self._tick_kafra_storage(snapshot, world, curr_m, px, py, now)
            else:
                self._handle_travel_plan(snapshot, px, py)
            return
        if (self.auto_farm or self.auto_loot or self.auto_patrol
                or self._kafra_cycle or self._city_travel_state):
            if self._dismiss_accidental_npc_dialog(world, player, curr_m, entities, now):
                return
            if self._dismiss_accidental_vending_window(world, player, now):
                return
        if self._kafra_cycle is None:
            mob_gid = self._locked_mob_gid
            last_hit = world.mobs_damaged_by_me.get(mob_gid, 0.0)
            fighting = (mob_gid is not None and any(key == ("mob", mob_gid) for key, _ in entities)
                        and (now < self._mob_lock_until or self._combat_mob_gid == mob_gid
                             or last_hit and last_hit >= self._mob_lock_until - 1.5))
            picking_up = (self._locked_drop_gid is not None
                          and (now < self._drop_lock_until
                               or self._engage_state and self._engage_state["kind"] == "drop"))
            if not fighting and not picking_up:
                self._start_kafra_storage(world, player, curr_m, now, manual_kafra)
        if self._kafra_cycle:
            self._tick_kafra_storage(snapshot, world, curr_m, px, py, now)
            return
        if not self._hud_can_run():
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


        if px is not None and self.auto_patrol:
            self._scan_patrol_area(world, curr_m, (px, py), now)

        if px is not None and (self.auto_farm or self.auto_loot or self.auto_patrol):
            position = (px, py)
            self._update_post_kill(world, curr_m)
            drops = self._eligible_drops(snapshot, position, entities, now) if self.auto_loot or self.auto_farm else []
            if self._continue_locked_target(snapshot, position, entities, drops, now):
                self._reset_patrol(px, py, keep_waypoint=True)
                return
            if now < self.post_kill_wait_until:
                started = self.post_kill_wait_until - 0.35
                if any(drop.created_at >= started for _, _, _, drop in drops):
                    self.post_kill_wait_until = 0.0
            waiting = now < self.post_kill_wait_until
            walking = world._is_player_walking()
            tier1 = [entry for entry in drops if entry[0] == 1 and (not waiting or entry[1] <= 2.2)]
            if tier1 and self._try_auto_loot(snapshot, position, tier1, direct_only=waiting or walking):
                self._reset_patrol(px, py, keep_waypoint=True)
                return

            if self.auto_farm or self.auto_loot:
                if self._try_auto_farm(snapshot, position, entities, defense=True, direct_only=waiting):
                    self._reset_patrol(px, py, keep_waypoint=True)
                    return
                if not waiting and self._try_auto_loot(snapshot, position, drops, direct_only=walking):
                    self._reset_patrol(px, py, keep_waypoint=True)
                    return

            if waiting:
                return

        if self._has_hud_travel():
            if self._city_travel_state:
                self._handle_city_travel(snapshot, px, py)
            if px is not None and (self.nav_goal or self.patrol_target_route):
                self._try_auto_patrol(snapshot, px, py)
            return

        if px is not None and self.auto_farm:
            if self._try_auto_farm(snapshot, (px, py), entities):
                self._reset_patrol(px, py, keep_waypoint=True)
                return

        # Preserva self._engage_state entre ticks se o alvo ainda existir e estiver acessível
        if self._engage_state:
            curr_target = world.entities.get((self._engage_state["kind"], self._engage_state["gid"]))
            if not curr_target or world.map != snapshot.map:
                self._engage_state = None
                self._engagement = None

        if self.auto_patrol and px is not None:
            self._try_auto_patrol(snapshot, px, py)

    def _update_post_kill(self, world, current_map):
        with world.lock:
            deaths = tuple(world.recent_dead_mobs)
            zone = (current_map, world.last_map_warp_ts)
        if self._engagement_zone != zone:
            self._engagement_zone = zone
            self._combat_mob_gid = self._locked_mob_gid = self._locked_drop_gid = None
            self._mob_lock_until = self._drop_lock_until = 0.0
            self._engagement = None
            self._engage_state = None
            self.post_kill_wait_until = 0.0
            self.last_kill_pos = None
        for dead in deaths:
            if dead["time"] <= self._last_kill_event:
                continue
            self._last_kill_event = dead["time"]
            d_gid = dead.get("gid")
            d_aid = dead.get("aid")
            if self._combat_mob_gid in (d_gid, d_aid):
                self._combat_mob_gid = None
            if self._engage_state and self._engage_state.get("kind") == "mob" and self._engage_state.get("gid") in (d_gid, d_aid):
                self._engage_state = None
                self._engagement = None
            if (dead["near_player"] or dead["was_target"]
                    or self._locked_mob_gid in (dead["gid"], dead["aid"])):
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

    @staticmethod
    def _has_gat_line_of_sight(x0, y0, x1, y1, gat):
        if gat is None:
            return False
        x, y, end_x, end_y = (round(value) for value in (x0, y0, x1, y1))
        dx, dy = abs(end_x - x), -abs(end_y - y)
        sx, sy = (1 if x < end_x else -1), (1 if y < end_y else -1)
        error = dx + dy
        while (x, y) != (end_x, end_y):
            twice = 2 * error
            next_x, next_y = x, y
            if twice >= dy:
                error += dy
                next_x += sx
            if twice <= dx:
                error += dx
                next_y += sy
            if next_x != x and next_y != y:
                if not (gat.is_walkable(next_x, y) and gat.is_walkable(x, next_y)):
                    return False
            x, y = next_x, next_y
            if not gat.is_walkable(x, y):
                return False
        return gat.is_walkable(end_x, end_y)

    def _safe_click_resolver(self, snapshot, project, *, allowed_portal=None, allowed_npc=None):
        left, top, width, height = snapshot.viewport
        world = self.feed.world
        now = time.monotonic()
        visible = {obj.key: (obj.u, obj.v) for obj in snapshot.objects if obj.kind in ("npc", "warp")}
        with world.lock:
            interactive = [(kind, gid, ent.track.target, ent.avoid_until)
                           for (kind, gid), ent in world.entities.items() if kind in ("npc", "warp")]
        known_portals = {tuple(cell) for (source, _), cell in MAP_PORTALS.items() if source == snapshot.map}
        known_portals.update(cell for (source, _), cell in KAFRA_STORAGE_PORTALS.items() if source == snapshot.map)
        known_portals.update((portal["wx"], portal["wy"]) for portal in self.world_routes_data.get("portals", ())
                             if portal["from_map"].lower() == snapshot.map)
        interactive.extend(("warp", None, cell, 0.0) for cell in known_portals)

        protected = []
        for kind, gid, cell, avoid_until in interactive:
            center = visible.pop(f"{kind}:{gid}", None)
            if ((kind == "warp" and allowed_portal and math.dist(cell, allowed_portal) <= 2.5)
                    or (kind == "npc" and allowed_npc is not None and gid == allowed_npc)):
                continue
            if center is None:
                center = project(*cell, 5.5 if kind == "npc" else 1.2)
            u, v = center
            protected.append((cell, 4.0 if avoid_until > now else 2.5,
                              (left + u * width, top + v * height)))
        screen_only = [(left + u * width, top + v * height) for u, v in visible.values()]

        def resolve(cell, uv=None):
            u, v = project(*cell) if uv is None else uv
            if not (0 <= u < 1 and 0 <= v < 1):
                return None
            point = (left + int(u * width), top + int(v * height))
            if self._is_point_in_game_ui(*point):
                return None
            if self._is_point_in_vending_shop(*point):
                return None
            if any(math.dist(cell, npc) <= radius or math.dist(point, center) <= 60
                   for npc, radius, center in protected):
                return None
            if any(math.dist(point, center) <= 60 for center in screen_only):
                return None
            return point

        return resolve

    @staticmethod
    def _pick_long_stride_cell(path_cells, min_dist=9, max_dist=14, *, position=None, ground_point):
        if not path_cells:
            return None
        start = path_cells[0] if position is None else position
        candidates = [(math.dist(start, cell), cell) for cell in path_cells]
        for distance, cell in sorted(candidates, reverse=True):
            if min_dist <= distance <= max_dist and (point := ground_point(cell)):
                return cell, point
        return None

    @staticmethod
    def _movement_confirmed(world, clicked_at, position, start):
        return (world.last_player_move_pkt_time >= clicked_at
                or tuple(map(round, position)) != tuple(map(round, start)))

    @staticmethod
    def _retry_lateral_cells(failed, start):
        return sorted(
            ((failed[0] + dx, failed[1] + dy) for dx in range(-4, 5) for dy in range(-4, 5)
             if 2 <= math.hypot(dx, dy) <= 4),
            key=lambda cell: (abs(math.dist(start, cell) - 7), math.dist(failed, cell), cell),
        )

    @staticmethod
    def _patrol_cells_in_radius(cx, cy, radius):
        for dx in range(-radius, radius + 1):
            limit = math.isqrt(radius * radius - dx * dx)
            for dy in range(-limit, limit + 1):
                yield cx + dx, cy + dy

    def _scan_patrol_area(self, world, map_id, position, now):
        zone = (map_id, world.last_map_warp_ts)
        if zone != self._patrol_scan_zone:
            self._patrol_scan_zone = zone
            self._patrol_scanned_ts, self._patrol_sector_visit_ts = self._patrol_history_by_map.setdefault(
                map_id, ({}, {}),
            )
            self._patrol_heading = (0.0, 0.0)
            self._patrol_frontier_retry_at = 0.0
            if self.nav_goal is None and not self.patrol_target_route:
                self._reset_patrol(None, None)
                self.active_nav_path = None
            self._patrol_finished_ts.clear()
            self._patrol_unreachable_until.clear()
            self._patrol_visibility_key = None
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(map_id) if terrain and map_id else None
        if gat is None:
            return None

        center = tuple(map(round, position))
        self._patrol_sector_visit_ts[self._patrol_sector(center)] = now
        key = (gat, center)
        if key != self._patrol_visibility_key:
            self._patrol_visibility_key = key
            self._patrol_visible = tuple(
                cell for cell in self._patrol_cells_in_radius(*center, 14)
                if self._has_gat_line_of_sight(*center, *cell, gat)
            )
        for cell in self._patrol_visible:
            if math.dist(position, cell) <= 13:
                self._patrol_scanned_ts[cell] = now
        return gat

    def _get_patrol_wall_clearance(self, map_id, gat):
        cached = self._patrol_wall_clearance.get(map_id)
        if cached and cached[0] is gat:
            return cached[1:]

        width = gat.width + 2
        height = gat.height + 2
        distances = bytearray(width * height)
        for y in range(1, height - 1):
            offset = y * width
            distances[offset + 1:offset + width - 1] = bytes(
                4 if gat.is_walkable(x, y - 1) else 0 for x in range(gat.width)
            )

        # A borda preenchida com zero conta como parede. As duas passagens incluem diagonais.
        for y in range(1, height - 1):
            for i in range(y * width + 1, (y + 1) * width - 1):
                if distances[i]:
                    distances[i] = min(distances[i], 1 + min(
                        distances[i - 1], distances[i - width - 1],
                        distances[i - width], distances[i - width + 1],
                    ))
        for y in range(height - 2, 0, -1):
            for i in range((y + 1) * width - 2, y * width, -1):
                if distances[i]:
                    distances[i] = min(distances[i], 1 + min(
                        distances[i + 1], distances[i + width - 1],
                        distances[i + width], distances[i + width + 1],
                    ))
        minimum = 1 if max(distances) < 2 else 2
        self._patrol_wall_clearance[map_id] = (gat, distances, width, minimum)
        return distances, width, minimum

    @staticmethod
    def _patrol_sector(cell):
        return cell[0] // 12, cell[1] // 12

    def _patrol_turn(self, origin, target):
        dx, dy = target[0] - origin[0], target[1] - origin[1]
        distance = math.hypot(dx, dy)
        if not distance:
            return 1
        hx, hy = self._patrol_heading
        alignment = (dx * hx + dy * hy) / distance
        if alignment >= 0.55:
            return 0
        return 1 if alignment >= -0.30 else 2

    def _choose_patrol_waypoint(self, map_id, gat, position, blocked, now):
        distances, width, minimum = self._get_patrol_wall_clearance(map_id, gat)
        center = tuple(map(round, position))
        unexplored = []
        revisits = []
        wall_costs = {}

        def clearance_at(cell):
            x, y = cell
            return distances[(y + 1) * width + x + 1]

        def available(cell):
            return (clearance_at(cell) >= minimum and cell not in blocked
                    and now - self._patrol_finished_ts.get(cell, -math.inf) > 18
                    and now >= self._patrol_unreachable_until.get(cell, 0.0))

        for cell in self._patrol_cells_in_radius(*center, 49):
            x, y = cell
            if not (0 <= x < gat.width and 0 <= y < gat.height):
                continue
            clearance = clearance_at(cell)
            if clearance == 1 and minimum == 2:
                wall_costs[cell] = 8
            distance = math.dist(position, cell)
            if not 5.5 < distance <= 48 or not available(cell):
                continue
            sector = self._patrol_sector(cell)
            visited_at = self._patrol_sector_visit_ts.get(sector, -math.inf)
            turn = self._patrol_turn(center, cell)
            preferred_distance = not 18 <= distance <= 48, abs(distance - 32)
            if cell not in self._patrol_scanned_ts:
                score = (turn, visited_at > now - 35, clearance < 3, *preferred_distance)
                unexplored.append((score, cell))
            else:
                scanned_at = self._patrol_scanned_ts[cell]
                score = (max(visited_at, scanned_at) > now - 35, turn, visited_at,
                         scanned_at, clearance < 3, *preferred_distance)
                revisits.append((score, cell))

        def reachable(candidates):
            budget = 12
            attempts_by_sector = collections.Counter()
            reverse_path = None
            for _, cell in sorted(candidates):
                sector = self._patrol_sector(cell)
                if attempts_by_sector[sector] >= 2:
                    continue
                attempts_by_sector[sector] += 1
                budget -= 1
                path = gat.find_path(center, cell, blocked_cells=blocked,
                                     cell_costs=wall_costs, max_iterations=2500)
                if len(path) > 1 and path[-1] == cell:
                    first_leg = path[min(len(path) - 1, 6)]
                    if self._patrol_turn(center, first_leg) < 2:
                        return cell, path
                    if reverse_path is None:
                        reverse_path = cell, path
                else:
                    self._patrol_unreachable_until[cell] = now + 8.0
                if budget == 0:
                    break
            return reverse_path or (None, None)

        waypoint, path = reachable(unexplored)
        if waypoint is not None:
            return waypoint, path

        if now >= self._patrol_frontier_retry_at:
            # Procura uma fronteira alcançável além da área local, inclusive atrás de curvas.
            self._patrol_frontier_retry_at = now + 3.0
            path = gat.find_path(
                center, None, blocked_cells=blocked, cell_costs=wall_costs, max_iterations=14000,
                goal_test=lambda cell: (cell not in self._patrol_scanned_ts
                                        and math.dist(center, cell) >= 6 and available(cell)),
            )
            if len(path) > 1:
                return path[-1], path
        return reachable(revisits)

    def _try_auto_patrol(self, snapshot, px, py):
        """Auto-Patrol: patrulha mapa por A*, calculando sub-passos a 8-11 células e clicando via movimento relativo."""
        if not snapshot or not hasattr(self.feed, "world"):
            return

        if self._automation_click_active():
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
        if now < getattr(self, "_patrol_no_wp_cooldown", 0.0):
            return
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

        world = self.feed.world
        curr_map = world.map
        gat = self._scan_patrol_area(world, curr_map, (px, py), now)
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
                    if r_target == "warp":
                        gate_resolve = self._safe_click_resolver(snapshot, project, allowed_portal=(r_x, r_y))
                        if point := gate_resolve((r_x, r_y)):
                            self.patrol_move_and_click(*point, hwnd=target_hwnd,
                                                       retry_key=(curr_map, "ground", (r_x, r_y)))
                        return
                    if r_target:
                        left, top, width, height = snapshot.viewport
                        for item in snapshot.objects:
                            if item.kind in ("npc", "warp"):
                                if item.key.partition(":")[2] == str(r_target):
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

        allowed_portal = self._travel_allowed_portal
        directed_route = self.patrol_target_route
        if allowed_portal is None and directed_route and directed_route[3] == "warp":
            allowed_portal = directed_route[1:3]
        allowed_npc = (self._kafra_cycle.get("approach_npc") if self._kafra_cycle
                       else directed_route[3] if directed_route and isinstance(directed_route[3], int) else None)
        resolve = self._safe_click_resolver(
            snapshot, project, allowed_portal=allowed_portal, allowed_npc=allowed_npc)

        def ground_point(cell):
            if (cell not in self.temp_blocked_cells and gat.is_walkable(*cell)
                    and self._has_gat_line_of_sight(px, py, *cell, gat)
                    and abs(gat.get_elevation(px, py) - gat.get_elevation(*cell)) <= 16):
                return resolve(cell)
            return None

        route_target = self.patrol_target_route and curr_map == self.patrol_target_route[0].lower().removesuffix(".gat")
        roaming = not route_target and self.nav_goal is None
        pending = self._last_patrol_click_target
        if pending:
            clicked_at, last_x, last_y, gx, gy, sx, sy, attempts = pending
            failed = (gx, gy)
            aborted = self._touch_click_aborted((sx, sy), clicked_at)
            advanced = (cur_cell != (round(last_x), round(last_y))
                        and math.dist((px, py), (last_x, last_y)) >= 0.8)
            walking = world._is_player_walking()
            elapsed = now - self._touch_click_finished_at((sx, sy), clicked_at)
            # O pacote de início de caminhada também pode chegar sem deslocamento.
            if not aborted and advanced:
                if walking:
                    return
                self._last_patrol_click_target = None
                dx, dy = px - last_x, py - last_y
                distance = math.hypot(dx, dy)
                if roaming and attempts == 0 and distance >= 2.5:
                    self._patrol_heading = dx / distance, dy / distance
            elif not aborted and elapsed < 0.85:
                return
            else:
                self.temp_blocked_cells[failed] = now + 25.0
                if attempts < 2:
                    blocked = self._get_all_blocked_cells()

                    def alternate_point(cell):
                        if cell in blocked or math.dist((px, py), cell) < 1.0:
                            return None
                        point = ground_point(cell)
                        return point if point and math.dist(point, (sx, sy)) >= 4 else None

                    nearby = sorted(self._patrol_cells_in_radius(gx, gy, 2),
                                    key=lambda cell: (math.dist(failed, cell), math.dist(cur_cell, cell)))
                    retry = next(((cell, point) for cell in nearby
                                  if (point := alternate_point(cell))), None)
                    if retry is None:
                        retry = self._pick_long_stride_cell(
                            self.current_patrol_path, 2.2, 8, position=(px, py), ground_point=alternate_point)
                    if retry is None:
                        retry = next(((cell, point) for cell in self._patrol_cells_in_radius(*cur_cell, 2)
                                      if (point := alternate_point(cell))), None)
                    if retry:
                        cell, point = retry
                        if self.patrol_move_and_click(*point, hwnd=target_hwnd, retry_key=(curr_map, "ground", cell)):
                            self._current_patrol_sub_step = cell
                            self._last_patrol_click_target = (now, px, py, *cell, *point, attempts + 1)
                            self._click_debug(f"PATROL: Sem avanço para {failed}. Tentando {cell}.")
                        return
                if roaming and self.current_waypoint:
                    self._patrol_unreachable_until[self.current_waypoint] = now + 25.0
                self._reset_patrol(px, py)
                self.active_nav_path = None
                self._click_debug("PATROL: Sem avanço após tentativas. Recalculando a rota.")

        if roaming and world._is_player_walking():
            return

        # ── Planejamento de Waypoint e Rota A* ──
        arrival_dist = 2.75 if roaming else 2.0
        if not self.current_waypoint or math.dist((px, py), self.current_waypoint) <= arrival_dist:
            if roaming and self.current_waypoint:
                self._patrol_finished_ts[self.current_waypoint] = now
                self._reset_patrol(px, py)
                self.active_nav_path = None
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
                next_wp, next_path = self._choose_patrol_waypoint(
                    curr_map, gat, (px, py), blocked, now,
                )
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
                self.visited_cells.clear()
                self._patrol_no_wp_cooldown = now + 0.45
                self._click_debug("PATROL: Nenhum waypoint caminhável disponível.")
                return

        if not self.current_patrol_path:
            blocked = self._get_all_blocked_cells()
            path = gat.find_path(cur_cell, self.current_waypoint, blocked_cells=blocked)
            if not path or (roaming and path[-1] != self.current_waypoint):
                if roaming:
                    self._patrol_unreachable_until[self.current_waypoint] = now + 12.0
                self._reset_patrol(px, py)
                self.active_nav_path = None
                return
            self.current_patrol_path = path
            self.active_nav_path = list(path)

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

        stride = self._pick_long_stride_cell(
            self.current_patrol_path, position=(px, py), ground_point=ground_point)
        if stride is None:
            stride = self._pick_long_stride_cell(
                self.current_patrol_path, 2.2, 9, position=(px, py), ground_point=ground_point)
        if stride is None and roaming:
            stride = self._pick_long_stride_cell(
                self.current_patrol_path, 1.0, 2.2, position=(px, py), ground_point=ground_point)
        if stride is None:
            if roaming:
                self._patrol_unreachable_until[self.current_waypoint] = now + 12.0
                self._patrol_no_wp_cooldown = now + 0.25
            self._reset_patrol(px, py)
            self.active_nav_path = None
            return

        current_sub = getattr(self, "_current_patrol_sub_step", None)
        if current_sub and world._is_player_walking() and math.dist((px, py), current_sub) > 3.5:
            return
        cell, point = stride
        if self.patrol_move_and_click(*point, hwnd=target_hwnd, retry_key=(curr_map, "ground", cell)):
            self._current_patrol_sub_step = cell
            self._last_patrol_click_target = (now, px, py, *cell, *point, 0)
            self._click_debug(f"PATROL: Passo em {cell} -> tela {point}")

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

    def _npc_dialog_blocked_cells(self, now):
        world = self.feed.world
        with world.lock:
            centers = [ent.track.target for (kind, _), ent in world.entities.items()
                       if kind == "npc" and ent.avoid_until > now]
        return {(round(x) + dx, round(y) + dy) for x, y in centers
                for dx in range(-4, 5) for dy in range(-4, 5) if math.hypot(dx, dy) <= 4}

    def _get_all_blocked_cells(self):
        now = time.monotonic()
        temporary = {cell for cell, until in self.temp_blocked_cells.items() if until > now}
        return temporary | self._get_portal_blocked_cells(radius=2.5) | self._npc_dialog_blocked_cells(now)

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
            if (self._automation_click_active() or self._survival_state
                    or self._respawn_started or self.feed.world.player_is_dead):
                return
            target_hwnd = self.get_game_hwnd(None)
            if target_hwnd and u32.IsWindow(target_hwnd):
                u32.SetForegroundWindow(target_hwnd)
            self._send_game_key(0x1B)
        self.root.after(delay_ms, _fire)

    def _handle_map_change(self, new_map):
        """Ao detectar troca de mapa (0x0091 / current_map != farm_map), atualiza rotas ou desativa farm."""
        norm_new = str(new_map or "").lower().removesuffix(".gat").removesuffix(".rsw")
        self._click_debug(f"MAPA ALTERADO ({self.farm_map} -> {new_map}): Desativando Auto-Farm e Auto-Patrol!")
        self.auto_farm = False
        self.auto_patrol = False
        self.farm_map = None
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._locked_mob_gid = None
        self.locked_combat_mob_gid = None
        if hasattr(getattr(self.feed, "world", None), "locked_combat_mob_gid"):
            self.feed.world.locked_combat_mob_gid = None
        self._locked_drop_gid = None
        self._clear_patrol_route()

    def _toggle_auto_farm(self):
        """Alterna estado do Auto-Farm e gerencia farm_map."""
        self.auto_farm = not self.auto_farm
        if self.auto_farm:
            self.farm_map = getattr(getattr(self.feed, "world", None), "map", None) or getattr(self, "current_map", "")
        else:
            self._locked_mob_gid = self._combat_mob_gid = None
            self._mob_lock_until = 0.0
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
        curr_map = snapshot.map
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
        result = proj(gx, gy, sprite_z)
        if result is None or not all(math.isfinite(value) for value in result):
            return None, None, None, None
        u, v = result
        sx = left + int(u * width)
        sy = top + int(v * height)
        return u, v, sx, sy

    def _reset_patrol(self, px, py, *, keep_waypoint=False):
        """Limpa os passos pendentes e registra a posição atual."""
        self.current_patrol_path = []
        self.patrol_path = []
        if not (keep_waypoint and self.auto_patrol and self.nav_goal is None and not self.patrol_target_route):
            self.current_waypoint = None
        self.patrol_waypoint = None
        self._current_patrol_sub_step = None
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

    def _continue_locked_target(self, snapshot, position, entities, drops, now):
        world = self.feed.world
        live = dict(entities)
        mob_gid = self._locked_mob_gid
        mob = live.get(("mob", mob_gid))
        if mob is None:
            self._combat_mob_gid = self._locked_mob_gid = None
        elif self.auto_farm or self.auto_loot:
            attacked_at = world.mobs_damaged_by_me.get(mob_gid, 0.0)
            if attacked_at and attacked_at >= self._mob_lock_until - 1.5:
                self._combat_mob_gid = mob_gid
            if self._combat_mob_gid == mob_gid or now < self._mob_lock_until:
                self._smart_engage_target((mob_gid, mob), "mob", snapshot, position)
                return True

        drop_gid = self._locked_drop_gid
        drop = live.get(("drop", drop_gid))
        ack = world.last_pickup_ack
        if drop is None or (ack and ack[0] == drop_gid and ack[1] >= self._drop_lock_until - 1.5):
            self._locked_drop_gid = None
            if self._engage_state and self._engage_state["kind"] == "drop":
                self._engage_state = self._engagement = None
            return False
        state = self._engage_state
        has_attempt = bool(state and state["kind"] == "drop" and state["gid"] == drop_gid)
        if (self.auto_farm or self.auto_loot) and (now < self._drop_lock_until or has_attempt):
            eligible = any(entry[2] == drop_gid for entry in drops)
            if eligible:
                self._smart_engage_target((drop_gid, drop), "drop", snapshot, position)
                return True
            if now < self._drop_lock_until:
                return True
            if has_attempt:
                self._engage_state = self._engagement = None
        return False

    def _try_auto_loot(self, snapshot, position, drops, *, direct_only=False):
        if not drops:
            return False
        world = self.feed.world
        if world._is_player_walking() and all(distance > 2.0 for _, distance, _, _ in drops):
            return True
        _, _, gid, drop = drops[0]
        if gid != self._locked_drop_gid:
            self._locked_drop_gid = gid
            self._drop_lock_until = time.monotonic() + 1.5
        self._smart_engage_target((gid, drop), "drop", snapshot, position, direct_only=direct_only)
        return True

    @staticmethod
    def _path_cost(path):
        return sum(14 if a[0] != b[0] and a[1] != b[1] else 10 for a, b in zip(path, path[1:]))

    def _approach_path(self, gat, start, target_cell, blocked, *, ring=None):
        if gat is None:
            return []
        if ring is None:
            path = gat.find_path(start, target_cell, blocked_cells=blocked)
            return path if path and path[-1] == target_cell else []
        return gat.find_path(start, None, blocked_cells=blocked, max_iterations=3500, goal_test=ring)

    def _live_sprite_aim(self, key, map_id, viewport, height, expected, at):
        world = self.feed.world
        kind, _ = key
        with world.lock:
            target = world.entities.get(key)
            player = world.player
            if not target or not player or world.map != map_id or self.current_viewport != viewport:
                return None
            if (target.catalog_id != expected.catalog_id
                    or (target.unreachable_until > time.monotonic() and key != ("mob", self._combat_mob_gid))):
                return None
            if kind == "drop" and (target.created_at != expected.created_at or target.ownership == "LOCKED"):
                return None
            target_track = target.track

        cell = target_track.cell(at)
        px, py = player.cell(at)
        camera = getattr(self.feed, "_cam", None) or (px, py, height(px, py))
        project = self.feed.camera.projector(viewport, camera, height)
        uv = project(*cell, 5.5 if kind == "mob" else 1.2)
        if not all(0 <= value < 1 for value in uv):
            return None
        return cell, uv

    def _smart_engage_target(self, target, kind, snapshot, position, *, direct_only=False):
        gid, entity = target
        world = self.feed.world
        now = time.monotonic()
        key = (kind, gid)
        with world.lock:
            live = world.entities.get(key)
            committed = kind == "mob" and gid == self._combat_mob_gid
            if not live or world.map != snapshot.map or (live.unreachable_until > now and not committed):
                return False
            if kind == "drop" and (live.ownership == "LOCKED" or live.created_at != entity.created_at):
                return False
            if kind == "mob":
                world.farm_target_gid = gid
            pickup_ack = world.last_pickup_ack
            attack_at = world.mobs_damaged_by_me.get(gid, 0.0)
            player_track = world.player
            moved_at = world.last_player_move_pkt_time
        if not self._game_is_foreground() or self._manual_mouse_button_pressed() or self._automation_click_active():
            return True
        if world.player_is_sitting:
            self._ensure_sitting(False)
            return True

        state = self._engage_state
        if state is None or (state["kind"], state["gid"], state["map"]) != (*key, snapshot.map):
            state = self._engage_state = {"kind": kind, "gid": gid, "map": snapshot.map,
                                          "selected_at": now, "pending": None,
                                          "failed": set(), "direct_failures": 0}
        self._engagement = {"key": key, "phase": state.get("phase", "direct")}

        def release():
            self._engage_state = None
            self._engagement = None
            self.active_nav_path = None
            return False

        def pause_target():
            if kind == "mob" and gid == self._combat_mob_gid:
                state["pending"] = None
                state["direct_failures"] = 0
                state["failed"].clear()
                state["retry_at"] = now + 0.35
                return True
            with world.lock:
                current = world.entities.get(key)
                if current and world.map == snapshot.map:
                    current.unreachable_until = now + (4.0 if kind == "drop" else 6.0)
                    if kind == "mob" and world.locked_combat_mob_gid == gid:
                        world.locked_combat_mob_gid = None
            return release()

        if now < state.get("retry_at", 0.0):
            return True
        failed = None
        walking = world._is_player_walking()
        if kind == "mob" and attack_at >= state["selected_at"]:
            self._combat_mob_gid = gid

        if state["pending"]:
            clicked_at, start_xy, last_cell, last_point, phase = state["pending"]
            aborted = self._touch_click_aborted(last_point, clicked_at)
            elapsed = now - self._touch_click_finished_at(last_point, clicked_at)
            if kind == "drop":
                if pickup_ack and pickup_ack[0] == gid and pickup_ack[1] >= clicked_at:
                    return release()
                distance = math.dist(position, live.track.cell())
                if not aborted:
                    if phase == "direct":
                        if elapsed < 0.75:
                            return True
                        if state.get("loot_lateral_used"):
                            return pause_target()
                        state["loot_needs_lateral"] = True
                    elif phase == "approach":
                        if distance > 2.0 and (walking or elapsed < 0.95):
                            return True
                        if distance > 2.0:
                            state["failed"].add(last_cell)
                    elif walking:
                        return True
                    elif math.dist(position, last_cell) > 0.55:
                        if elapsed < 0.65:
                            return True
                        return pause_target()
                if phase == "direct":
                    with world.lock:
                        if world.pending_pickup == (gid, clicked_at):
                            world.pending_pickup = None
                elif phase == "reposition" and aborted:
                    state["loot_lateral_used"] = False
                    state["loot_needs_lateral"] = True
                state["pending"] = None
            else:
                started_walking = phase == "direct" and moved_at >= clicked_at
                moved = phase != "direct" and self._movement_confirmed(world, clicked_at, position, start_xy)
                if attack_at >= clicked_at or started_walking or moved:
                    state["pending"] = None
                    state["failed"].clear()
                    if phase == "direct":
                        state["direct_failures"] = 0
                        if started_walking:
                            state["following"] = True
                    elif phase == "reposition":
                        state["reposition_goal"] = last_cell
                elif aborted:
                    state["pending"] = None
                    return True
                elif elapsed < (0.7 if phase == "direct" else 0.85):
                    return True
                else:
                    state["pending"] = None
                    if phase == "direct":
                        state["direct_failures"] += 1
                    else:
                        failed = last_cell
                        state["failed"].add(failed)
                        if len(state["failed"]) >= 3:
                            return pause_target()

        if kind == "mob" and now - attack_at < 1.2:
            self.active_nav_path = None
            return True
        recovery_goal = state.get("reposition_goal")
        if recovery_goal is not None:
            arrived = math.dist(position, recovery_goal) <= 1.0
            if walking and not arrived:
                return True
            del state["reposition_goal"]
            if arrived:
                state["direct_failures"] = 0
                state["failed"].clear()
            else:
                failed = recovery_goal
                state["failed"].add(failed)

        if state.pop("following", False) and walking and player_track:
            if math.dist(position, live.track.cell()) > 2.0:
                state["following"] = True
                return True

        hwnd = self.get_game_hwnd()
        if not hwnd:
            return True
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
        cell = live.track.cell()
        start = tuple(map(round, position))
        target_cell = tuple(map(round, cell))
        height = gat.get_elevation if gat else flat_height
        viewport = snapshot.viewport
        project = self.feed.camera.projector(viewport, getattr(self.feed, "_cam", None) or position, height)
        resolve = self._safe_click_resolver(snapshot, project)
        aim_at = time.perf_counter() + getattr(self, "_sprite_click_lead", 0.085)

        def aim(at):
            return self._live_sprite_aim(key, snapshot.map, viewport, height, live, at)

        visible = any(obj.key == f"{kind}:{gid}" for obj in snapshot.objects)
        planned = aim(aim_at) if visible else None
        aimed_cell, uv = planned if planned is not None else (cell, None)
        point = resolve(aimed_cell, uv) if uv is not None else None
        protected_target = visible and point is None

        def current_target_point(lead):
            if not self._game_is_foreground() or self._manual_mouse_button_pressed():
                return None
            latest = aim(time.perf_counter() + lead)
            if latest is None:
                return None
            cell_now, uv_now = latest
            screen_now = resolve(cell_now, uv_now)
            if screen_now and not self._is_point_in_hud(*screen_now, margin=10):
                return screen_now
            return None

        def click(ground, screen, phase):
            clicked_at = time.monotonic()
            pickup = kind == "drop" and phase == "direct"
            if pickup:
                with world.lock:
                    current = world.entities.get(key)
                    if current is None or current.created_at != live.created_at or current.ownership == "LOCKED":
                        return False
                    world.pending_pickup = (gid, clicked_at)
            target_point = current_target_point if phase == "direct" else None
            retry_key = ((snapshot.map, kind, gid, live.created_at) if phase == "direct"
                         else (snapshot.map, "ground", ground))
            if not self.patrol_move_and_click(*screen, hwnd=hwnd, target_point=target_point, retry_key=retry_key):
                if pickup:
                    with world.lock:
                        if world.pending_pickup == (gid, clicked_at):
                            world.pending_pickup = None
                return True
            state["pending"] = (clicked_at, position, ground, screen, phase)
            state["phase"] = self._engagement["phase"] = phase
            if kind == "drop":
                self.last_loot_click = clicked_at
            else:
                self.last_farm_click = clicked_at
            return True

        distance = math.dist(position, aimed_cell)
        direct_range = 7 if kind == "drop" else 13
        direct_line = (self._has_gat_line_of_sight(*position, *aimed_cell, gat)
                       if gat else distance <= 2.2)
        aimed_tile = tuple(map(round, aimed_cell))
        if kind == "drop":
            distance = math.dist(position, cell)
            if state.get("loot_needs_lateral"):
                if walking:
                    return True
                if gat is None:
                    return pause_target()
                blocked = self._get_all_blocked_cells()
                nearby = ((start[0] + dx, start[1] + dy) for dx, dy in
                          ((1, 0), (-1, 0), (0, 1), (0, -1)))
                for step in sorted(nearby, key=lambda step: math.dist(step, cell)):
                    if (math.dist(step, cell) <= 2.0 and step not in blocked
                            and gat.is_walkable(*step) and (screen := resolve(step))):
                        click(step, screen, "reposition")
                        if state["pending"]:
                            state["loot_lateral_used"] = True
                            state["loot_needs_lateral"] = False
                        return True
                return pause_target()
            if distance <= 2.0 and point and direct_line:
                last_attack = world.last_player_attack_ts
                if now - last_attack < 0.28 and state.get("loot_attack_at") != last_attack:
                    state["loot_attack_at"] = last_attack
                    state["loot_click_after"] = now + 0.2
                if walking or now < state.get("loot_click_after", 0.0):
                    return True
                self.active_nav_path = None
                return click(aimed_tile, point, "direct")
            if walking or direct_only:
                return True
            if gat is None or len(state["failed"]) >= 3:
                return pause_target()

        reposition = state["direct_failures"] >= 2
        can_aim = kind == "mob" and point and direct_line and distance <= direct_range
        if can_aim and not reposition:
            self.active_nav_path = None
            return click(aimed_tile, point, "direct")
        if reposition and direct_only and gat is not None:
            return True
        if direct_only or gat is None or (walking and failed is None and not reposition):
            return pause_target() if failed or reposition else release()

        blocked = self._get_all_blocked_cells() | state["failed"] | {target_cell}

        def ground_point(ground):
            if ground in blocked or not gat.is_walkable(*ground):
                return None
            if not self._has_gat_line_of_sight(*position, *ground, gat):
                return None
            return resolve(ground)

        if reposition:
            nearby = sorted(
                ((target_cell[0] + dx, target_cell[1] + dy)
                 for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                key=lambda ground: (math.dist(ground, cell), math.dist(ground, position)),
            )
            for ground in nearby:
                if ground != start and (screen := ground_point(ground)):
                    self.active_nav_path = [start, ground]
                    self.active_nav_color = "#F87171" if kind == "mob" else "#FBBF24"
                    return click(ground, screen, "reposition")
            return pause_target()

        ring_min, ring_max = (2.5, 4.0) if protected_target and kind == "mob" else (1.0, 2.0)

        def ring(ground):
            return (ground != start and ground not in blocked and ring_min <= math.dist(ground, cell) <= ring_max
                    and (resolve(ground) is not None if protected_target
                         else self._has_gat_line_of_sight(*ground, *cell, gat)))

        path = self._approach_path(gat, start, target_cell, blocked, ring=ring)
        self.active_nav_path = path or None
        self.active_nav_color = "#F87171" if kind == "mob" else "#FBBF24"
        if failed:
            stride = self._pick_long_stride_cell(path, 6, 8, position=position, ground_point=ground_point)
            if stride is None:
                stride = next(((ground, screen) for ground in self._retry_lateral_cells(failed, position)
                               if (screen := ground_point(ground))), None)
        else:
            stride = self._pick_long_stride_cell(path, position=position, ground_point=ground_point)
        if stride is None:
            stride = self._pick_long_stride_cell(path, 1, 9, position=position, ground_point=ground_point)
        if stride:
            return click(*stride, "approach")
        return pause_target()

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
        # Nunca aceitar a janela do próprio overlay ou do HUD
        overlay_hwnd = getattr(self, "overlay_hwnd", None)
        if overlay_hwnd and hwnd == overlay_hwnd:
            return False
        for hud in (getattr(self, "_hud_panel", None), getattr(self, "_hud_drawer", None)):
            if hud and getattr(hud, "_hud_hwnd", None) == hwnd:
                return False

        # 1. Se for o mesmo HWND do viewport já detectado no boot
        vp_obj = getattr(getattr(self, "feed", None), "viewport", None)
        if vp_obj and getattr(vp_obj, "handle", None) == hwnd:
            return True

        # 2. Se pertencer ao PID do Ragexe.exe conhecido
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        known_pid = getattr(self, "_ragexe_pid", None)
        if known_pid and pid.value == known_pid:
            return True

        # 3. Consulta psutil com tolerância a AccessDenied
        try:
            if pid.value and psutil.Process(pid.value).name().casefold() in ("ragexe.exe", "ragna.exe"):
                return True
        except psutil.Error:
            pass

        # 4. Fallback: verifica se o título da janela contém 'Ragnarok'
        length = u32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, buf, len(buf))
            if "ragnarok" in buf.value.lower():
                return True
        return False

    def _move_and_click_sync(self, sx, sy, hwnd):
        # Os controles da Kafra precisam esperar o clique anterior antes de avançar.
        deadline = time.perf_counter() + 2.0
        while (self._automation_click_active()
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

    def _click_retry_offset(self, key, point, now, allowed):
        previous = self._click_retry_history.get(key)
        if previous is None or now - previous[0] > 20:
            return (0, 0), tuple(random.sample(CLICK_RETRY_OFFSETS, len(CLICK_RETRY_OFFSETS)))
        _, previous_offset, remaining = previous
        if not remaining:
            remaining = tuple(random.sample(CLICK_RETRY_OFFSETS, len(CLICK_RETRY_OFFSETS)))
        for index, offset in enumerate(remaining):
            if (math.dist(offset, previous_offset) >= 3
                    and allowed((point[0] + offset[0], point[1] + offset[1]))):
                return offset, remaining[:index] + remaining[index + 1:]
        # Reembaralha quando as direções restantes encostam em uma zona protegida.
        for offset in random.sample(CLICK_RETRY_OFFSETS, len(CLICK_RETRY_OFFSETS)):
            if math.dist(offset, previous_offset) >= 3 and allowed((point[0] + offset[0], point[1] + offset[1])):
                return offset, ()
        return (0, 0), remaining

    def _schedule_game_click(self, sx, sy, game_hwnd=None, *, target_point=None, retry_key=None):
        """Agenda o clique do jogo e mantém o estado da automação."""
        now = time.perf_counter()
        if self._automation_click_active():
            return False
        last_ts = getattr(self, "_last_click_ts", 0.0)
        if getattr(self, "_click_busy", False) and (now - getattr(self, "_click_start_ts", 0.0) < 0.25):
            return False
        if now - last_ts < (0.06 if target_point else 0.35):
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
        click_x, click_y = sx, sy
        aim = target_point
        offset = (0, 0)
        if retry_key is not None:
            retry_key = (root_hwnd, retry_key)
            viewport = self.current_viewport

            def allowed(point):
                if not viewport:
                    return False
                vx, vy, vw, vh = viewport
                x, y = point
                return (vx <= x < vx + vw and vy <= y < vy + vh
                        and not self._is_point_in_game_ui(x, y)
                        and not self._is_point_in_vending_shop(x, y))

            offset, remaining = self._click_retry_offset(retry_key, (sx, sy), now, allowed)
            click_x, click_y = sx + offset[0], sy + offset[1]
            if target_point is not None and offset != (0, 0):
                def aim(lead):
                    point = target_point(lead)
                    if point is None:
                        return None
                    shifted = point[0] + offset[0], point[1] + offset[1]
                    return shifted if allowed(shifted) else point

        def can_click():
            return self._is_game_focused(game_window=game_window)

        def _do_smooth_click():
            sent = False
            try:
                if not can_click() and target_point is not None:
                    return
                sent = win32_input.move_and_click(
                    click_x, click_y, hwnd, root_hwnd,
                    is_focused=can_click, target_point=aim,
                    cursor_tolerance=1 if offset != (0, 0) else 3,
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
                    measured = max(0.04, min(0.16, (finished_at - now) * 0.7))
                    lead = getattr(self, "_sprite_click_lead", 0.085)
                    self._sprite_click_lead = lead * 0.75 + measured * 0.25
                self._last_touch_result = ((int(sx), int(sy)), time.monotonic(), sent)
                self._click_busy = False
                self._click_in_progress = False

        started = False
        try:
            self._click_in_progress = True
            self._click_busy = True
            self._click_start_ts = now
            self._last_click_ts = self.last_bot_click_ts = now
            threading.Thread(target=_do_smooth_click, daemon=True).start()
            started = True
            if retry_key is not None:
                self._click_retry_history[retry_key] = (now, offset, remaining)
                self._click_retry_history.move_to_end(retry_key)
                if len(self._click_retry_history) > 256:
                    self._click_retry_history.popitem(last=False)
        finally:
            if not started:
                self._click_busy = self._click_in_progress = False
        return True

    def _touch_click_finished_at(self, point, clicked_at):
        result = getattr(self, "_last_touch_result", None)
        return result[1] if result and result[2] and result[1] >= clicked_at and result[0] == point else clicked_at

    def _touch_click_aborted(self, point, clicked_at):
        result = getattr(self, "_last_touch_result", None)
        return (result is not None
                and not result[2] and result[1] >= clicked_at and result[0] == point)

    def _smooth_move_to(self, sx: int, sy: int):
        if self._automation_click_active():
            return
        self._click_in_progress = True
        try:
            win32_input.move_cursor_relative(sx, sy)
        finally:
            self._click_in_progress = False

    def _send_game_key(self, vk: int, *, restore_focus=False) -> bool:
        if self._automation_click_active():
            return False
        hwnd = self.get_game_hwnd()
        if not hwnd or not self.is_valid_ragexe_hwnd(hwnd):
            return False
        game_window = self._remember_game_window(hwnd)
        if restore_focus and not self._is_game_focused(game_window=game_window):
            self._focus_hud_game()
        if not self._is_game_focused(game_window=game_window):
            print(f"[Teclado] VK=0x{vk:02X} descartado: jogo sem foco.", flush=True)
            return False

        size = ctypes.sizeof(INPUT)
        if size != 40:
            print(f"[Teclado] INPUT inválido para Win32 x64: {size} bytes.", flush=True)
            return False
        mapping = VK_TO_SCANCODE.get(vk)
        if mapping is None:
            translated = u32.MapVirtualKeyW(vk, 4)  # MAPVK_VK_TO_VSC_EX
            mapping = (translated & 0xFF, translated >> 8 == 0xE0)
        scan, is_extended = mapping
        if not scan:
            print(f"[Teclado] VK=0x{vk:02X} descartado: scan code indisponível.", flush=True)
            return False
        with self._game_key_lock:
            self._game_key_queue.append((game_window, vk, scan, is_extended))
            if not self._game_key_worker_active:
                worker = threading.Thread(target=self._drain_game_keys, name="game-keyboard", daemon=True)
                self._game_key_worker_active = True
                try:
                    worker.start()
                except RuntimeError as exc:
                    self._game_key_worker_active = False
                    self._game_key_queue.pop()
                    print(f"[Teclado] Falha ao iniciar envio: {exc}", flush=True)
                    return False
        return True

    def _drain_game_keys(self):
        while True:
            with self._game_key_lock:
                if not self._game_key_queue:
                    self._game_key_worker_active = False
                    return
                game_window, vk, scan, is_extended = self._game_key_queue.popleft()

            if not u32.IsWindow(game_window[0]) or not self._is_game_focused(game_window=game_window):
                print(f"[Teclado] VK=0x{vk:02X} descartado: janela mudou antes do envio.", flush=True)
                continue
            try:
                self._send_game_scancode(vk, scan, is_extended)
            except (OSError, ctypes.ArgumentError) as exc:
                print(f"[Teclado] Falha no envio de VK=0x{vk:02X}: {exc}", flush=True)
            # Separa o KeyUp do próximo KeyDown em sequências de teclas iguais.
            time.sleep(0.025)

    @staticmethod
    def _send_game_scancode(vk, scan, is_extended):
        event = INPUT(type=INPUT_KEYBOARD)
        event.ki.wVk = 0
        event.ki.wScan = scan
        event.ki.dwFlags = 0x0008 | (0x0001 if is_extended else 0)
        size = ctypes.sizeof(event)
        win32_input.LAST_BOT_KEY_TS = time.monotonic()
        ctypes.set_last_error(0)
        pressed = _keyboard_api.SendInput(1, ctypes.byref(event), size)
        press_error = ctypes.get_last_error() if not pressed else 0
        release_log = "up=omitido"
        if pressed:
            try:
                time.sleep(0.050)
            finally:
                event.ki.dwFlags |= 0x0002
                ctypes.set_last_error(0)
                released = _keyboard_api.SendInput(1, ctypes.byref(event), size)
                release_error = ctypes.get_last_error() if not released else 0
                release_log = f"up={released}(e={release_error})"
                win32_input.LAST_BOT_KEY_TS = time.monotonic()
        print(
            f"[Teclado] VK=0x{vk:02X} SCAN=0x{scan:02X} EXT={int(is_extended)} "
            f"SendInput down={pressed}(e={press_error}) {release_log} hold={50 if pressed else 0}ms",
            flush=True,
        )

    def _test_sit_key(self):
        if self._sit_test is not None:
            return
        world = getattr(self.feed, "world", None)
        if world is None or world.player is None:
            self._report_sit_test("Aguardando o personagem no jogo.")
            return
        self._sit_test = world, world.map, world.player_is_sitting

        def send():
            if not self._send_game_key(0x2D, restore_focus=True):
                self._report_sit_test("Falha ao enviar Insert.")
                return
            deadline = time.monotonic() + 1.5

            def check():
                if self._sit_test is None:
                    return
                _, map_id, was_sitting = self._sit_test
                if world.map != map_id:
                    self._report_sit_test("O mapa mudou durante o teste.")
                elif world.player_is_sitting != was_sitting:
                    posture = "sentou" if world.player_is_sitting else "levantou"
                    self._report_sit_test(f"Confirmado pelo jogo: personagem {posture}.")
                elif time.monotonic() < deadline:
                    self.root.after(50, check)
                else:
                    self._report_sit_test("Insert enviado. O jogo não confirmou mudança de postura.")

            check()

        self.root.after(90, self._guard_hud_callback(send))

    def _report_sit_test(self, status):
        self._sit_test = None
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

    def touch_postmsg(self, sx, sy, game_hwnd=None, *, test_mode=False):
        """Dispara clique via touch_postmsg (pipeline funcional de 9e1ac16)."""
        return self._dispatch_game_click(game_hwnd, sx, sy, method="touch_postmsg", test_mode=test_mode)

    def patrol_move_and_click(self, sx, sy, *, hwnd=None, target_point=None, retry_key=None):
        return self._dispatch_game_click(
            hwnd, sx, sy, method="touch_postmsg", target_point=target_point, retry_key=retry_key,
        )

    def _send_click(self, hwnd, screen_x, screen_y, test_mode=False, **_kwargs):
        return self._dispatch_game_click(hwnd, screen_x, screen_y,
                                         method=self._click_method, test_mode=test_mode)

    def _is_point_in_vending_shop(self, click_x, click_y) -> bool:
        """Verifica se (click_x, click_y) está dentro da placa de loja de mercador (sx-75..sx+75, sy-125..sy+20)."""
        feed = getattr(self, "feed", None)
        if not feed or not hasattr(feed, "world"):
            return False
        world = feed.world
        snap = getattr(self, "_last_snapshot", None) or getattr(feed, "last_snapshot", None)
        vending_cells = []
        with world.lock:
            for (kind, aid), ent in world.entities.items():
                if getattr(ent, "has_vending_shop", False):
                    vending_cells.append(ent.track.cell())
            for aid in getattr(world, "vending_aids", set()):
                if aid in world.other_players:
                    vending_cells.append(world.other_players[aid][0])
                elif aid in world.players:
                    vending_cells.append(world.players[aid][0])
        if not vending_cells:
            return False

        vp = getattr(self, "current_viewport", None) or (feed.viewport() if callable(getattr(feed, "viewport", None)) else None)
        if not vp and snap:
            vp = getattr(snap, "viewport", None)
        if not vp or not hasattr(feed, "camera"):
            return False

        left, top, width, height = vp
        terrain = getattr(feed, "terrain", None)
        curr_map = getattr(world, "map", None) or (snap.map if snap else None)
        gat = terrain.for_map(curr_map) if terrain and curr_map else None
        h_fn = getattr(gat, "get_elevation", lambda x, y: 0.0) if gat else lambda x, y: 0.0
        cam_pos = getattr(feed, "_cam", None)
        if cam_pos is None and world.player:
            cam_pos = world.player.cell()
        if cam_pos is None and vending_cells:
            cam_pos = vending_cells[0]
        if cam_pos is None:
            return False

        proj = feed.camera.projector(vp, cam_pos, h_fn)
        for cell in vending_cells:
            res = proj(cell[0], cell[1], 0.0)
            if res is None or not all(math.isfinite(val) for val in res):
                continue
            u, v = res
            if not (0 <= u <= 1 and 0 <= v <= 1):
                continue
            sx = left + int(u * width)
            sy = top + int(v * height)
            if (sx - 75 <= click_x <= sx + 75) and (sy - 125 <= click_y <= sy + 20):
                return True
        return False

    def _dispatch_game_click(self, hwnd, screen_x, screen_y, *, method, test_mode=False,
                             target_point=None, retry_key=None):
        """Envia clique na posição física (screen_x, screen_y)."""
        # ── Proteção de Concorrência: previne cliques sobrepostos entre ticks ──
        if self._automation_click_active():
            self._click_debug(f"CLIQUE IGNORADO (concorrência): ({int(screen_x)},{int(screen_y)})")
            return False

        sx = int(screen_x)
        sy = int(screen_y)

        # Validação de limites: descarta cliques fora da área de tela
        sw = getattr(self, "_screen_w", 1920)
        sh = getattr(self, "_screen_h", 1080)
        if not (0 <= sx < sw and 0 <= sy < sh):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) fora da tela ({sw}x{sh})")
            return False

        # Zona Proibida do Retângulo do HUD (com margem de 10px): nunca clique ali
        if not test_mode and self._is_point_in_hud(sx, sy, margin=10):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está dentro do HUD (zona proibida)")
            return False

        # Zona Proibida da Interface Nativa do Jogo (chat, status, minimapa)
        if not test_mode and self._is_point_in_game_ui(sx, sy):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está sobre a UI nativa do Ragnarok")
            return False

        # Zona Proibida de Placas de Loja de Mercadores (sx-75..sx+75, sy-125..sy+20)
        if not test_mode and self._is_point_in_vending_shop(sx, sy):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está sobre placa de loja de mercador")
            return False

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
                if retry_key is None and not test_mode:
                    world = getattr(self.feed, "world", None)
                    retry_key = ("screen", getattr(world, "map", None), sx, sy)
                return self._schedule_game_click(
                    sx, sy, self.get_game_hwnd(hwnd), target_point=target_point, retry_key=retry_key,
                )

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
            name = self.names.get("mob", {}).get(mob.catalog_id, "").strip().lower()
            if not defense and gid != combat_gid and not active:
                if not self._passes_target_filter("mob", mob.catalog_id, name):
                    continue
                if getattr(self, "_monster_filter", "").lower() not in name:
                    continue
            candidates.append((gid, mob, cell, distance, active))

        if not candidates:
            return False

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(world.map) if terrain and world.map else None
        start = tuple(round(coord) for coord in position)
        blocked = self._get_all_blocked_cells()
        ranked = []
        for gid, mob, cell, distance, active in candidates:
            locked = gid == combat_gid or gid == self._locked_mob_gid
            target_cell = tuple(round(coord) for coord in cell)
            cost = distance * 10
            if gat and not locked and not direct_only and not self._has_gat_line_of_sight(*start, *target_cell, gat):
                path = self._approach_path(gat, start, target_cell, blocked)
                if path:
                    cost = self._path_cost(path)
            ranked.append((defense and not active, gid != combat_gid, not locked, cost, gid, mob))

        _, _, _, _, gid, mob = min(ranked)
        if gid != self._locked_mob_gid:
            self._locked_mob_gid = gid
            self._mob_lock_until = now + 1.5
        self._smart_engage_target((gid, mob), "mob", snapshot, position, direct_only=direct_only)
        return True

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
        if not (self._hud_can_run() or self._kafra_cycle):
            return None
        if self._survival_state == SURVIVAL_RETREAT:
            return self._last_retreat_target or (self.active_nav_path[-1] if self.active_nav_path else None)
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
            focused = (self._engage_state["kind"], self._engage_state["gid"]) if self._engage_state else (self._engagement["key"] if self._engagement else None)
            for key in (focused, ("drop", self._locked_drop_gid), ("mob", combat_gid), ("mob", self._locked_mob_gid)):
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
        if getattr(self, "_current_patrol_sub_step", None) and self.auto_patrol:
            return self._current_patrol_sub_step
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

    def _player_overlay_status(self, player):
        world = getattr(self.feed, "world", None)
        if getattr(world, "player_is_dead", False):
            return ("Ressuscitando" if self._respawn_started else "Derrotado"), "#f87171"
        if cycle := self._kafra_cycle:
            if cycle["phase"] == "return":
                return "Voltando da Kafra", "#fbbf24"
            if cycle["phase"] in ("travel", "approach"):
                return "Indo à Kafra", "#fbbf24"
            return "Guardando itens", "#fbbf24"
        if self._hud_can_run():
            if self._survival_state == SURVIVAL_RETREAT:
                return "Fugindo", "#f87171"
            if self._survival_state == SURVIVAL_RECOVER:
                return "Recuperando HP", "#4ade80"
            if self._survival_state == SURVIVAL_RETURN:
                return "Voltando ao farm", "#4ade80"
            if self.travel_plan:
                return f"Viagem: {self.travel_plan['target_map']}", "#a78bfa"
            if self._has_hud_travel():
                return "Em deslocamento", "#a78bfa"
            if self._pending_hud_interaction or self._hud_npc_line_target:
                target = self._hud_npc_line_target
                if self._pending_hud_interaction or time.monotonic() < target[2]:
                    return "Interagindo", "#a78bfa"
            if engagement := self._engagement:
                kind = engagement["key"][0]
                phase = engagement["phase"]
                if phase == "reposition":
                    return "Reposicionando", "#fbbf24"
                if phase == "approach":
                    return ("Indo ao item" if kind == "drop" else "Aproximando do monstro"), "#fbbf24"
                if kind == "drop":
                    return "Coletando", "#fbbf24"
                return "Lutando", "#fb923c"
            if self.auto_patrol and (self.current_patrol_path or self._last_patrol_click_target):
                return "Patrulhando", "#38bdf8"
        if player.is_moving:
            return "Caminhando", "#cbd5e1"
        return None

    def _draw_overlay_routes(self, snapshot, width, height):
        camera = getattr(self.feed, "camera", None)
        player = snapshot.player
        if camera is None or player is None or not snapshot.map:
            return

        moment = time.perf_counter()
        player_cell = player.cell(moment)
        if not all(value is not None and math.isfinite(value) for value in player_cell):
            return

        height_fn = flat_height
        terrain = getattr(self.feed, "terrain", None)
        if terrain:
            try:
                gat = terrain.for_map(snapshot.map)
            except (OSError, ValueError):
                gat = None
            if gat:
                height_fn = gat.get_elevation
        cam_pos = getattr(self.feed, "_cam", None) or player_cell
        project = camera.projector(snapshot.viewport, cam_pos, height_fn)

        def pixels(cell, z=0.0):
            result = project(*cell, z)
            if result is None or not all(math.isfinite(value) for value in result):
                return None
            u, v = result
            return u * width, v * height

        if self._show_routes.get():
            retreating = self._survival_state == SURVIVAL_RETREAT
            running = self._hud_can_run() or bool(self._kafra_cycle)
            same_map = not self.last or self.last.map == snapshot.map
            path = (self.active_nav_path or self.current_patrol_path) if running and same_map else None
            color = "#f87171" if retreating else (self.active_nav_color if path else "#38bdf8")
            if path:
                closest = min(range(len(path)), key=lambda index: math.dist(player_cell, path[index]))
                distance = math.dist(player_cell, path[closest])
                if distance <= 2.5:
                    path = path[closest + (distance <= 0.35):]
                else:
                    path = None
            if not path and player.is_moving:
                path = [cell for _, end, _, cell in player.segments if end > moment] or [player.target]
            target = self._overlay_active_target(snapshot, player_cell, time.monotonic())
            self._draw_player_route(path, target, player_cell, pixels, width, height, color)

        if status := self._player_overlay_status(player):
            label, color = status
            point = pixels(player_cell, 15.0)
            if point and 0 <= point[0] < width and 0 <= point[1] < height:
                text = self.canvas_pool.get_text(
                    round(point[0]), max(12, round(point[1]) - 10),
                    text=label, fill=color, font=("Tahoma", 9, "bold"),
                )
                if bbox := self.canvas.bbox(text):
                    x1, y1, x2, y2 = bbox
                    box = self.canvas_pool.get_rect(x1 - 5, y1 - 2, x2 + 5, y2 + 2,
                                                    fill="#0B1120", outline="#334155", width=1)
                    self.canvas.tag_lower(box, text)

    def _draw_player_route(self, path, target, player_cell, pixels, width, height, color):
        if path:
            coords = []
            for cell in (player_cell, *path):
                point = pixels(cell)
                if point is None:
                    break
                x, y = point
                coords.extend((max(-2 * width, min(3 * width, x)),
                               max(-2 * height, min(3 * height, y))))
            if len(coords) >= 4:
                self.canvas_pool.get_line(*coords, fill=color, width=2, dash=())
        if isinstance(target, ScreenObject):
            end = target.u * width, target.v * height
            color = {"mob": "#fb923c", "drop": "#fbbf24"}.get(target.kind, color)
        else:
            end = pixels(target or path[-1]) if target or path else None
        if end is None or not all(math.isfinite(value) for value in end):
            return
        x, y = end
        if not path and 0 <= x < width and 0 <= y < height:
            if start := pixels(player_cell):
                self.canvas_pool.get_line(*start, x, y, fill=color, width=2, dash=())
        if 0 <= x < width and 0 <= y < height:
            self.canvas_pool.get_oval(x - 7, y - 7, x + 7, y + 7, outline=color, width=2, fill="", dash=())

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

            ax, ay = self._projection_pixels(*item.get_anchor(), width, height)
            cx, cy = self._projection_pixels(item.u, item.v, width, height)

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
                is_locked_drop = (self._locked_drop_gid is not None and item.key == f"drop:{self._locked_drop_gid}")
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
                if color != "#94a3b8":
                    border_width = 2
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
                is_locked_mob = (self._locked_mob_gid is not None and item.key == f"mob:{self._locked_mob_gid}")
                if is_locked_mob:
                    color = "#FFA500"  # Laranja vibrante para o mob em foco
                    border_outline = "#FF8C00"
                    border_width = 2
                    self.canvas_pool.get_rect(cx - 7, cy - 7, cx + 7, cy + 7, outline="#FFA500", width=2)
                else:
                    color = "#FFA294"
                    self.canvas_pool.get_rect(cx - 6, cy - 6, cx + 6, cy + 6, outline=color, width=2)
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline=color, width=1)

            if item.tag_uv is None:
                # Snapshots legados fornecem só o ponto de clique em tela.
                text_x, text_y = cx, cy - (24 if item.kind == "npc" else 16)
            else:
                text_x, text_y = self._projection_pixels(*item.tag_uv, width, height)
            if not (0 <= text_x < width and 0 <= text_y < height):
                continue
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

        camera = getattr(self.feed, "camera", None)
        if isinstance(camera, CameraTracker):
            message, point = camera.alignment_overlay(snapshot)
            if message:
                text = self.canvas_pool.get_text(width / 2, 85, text=message,
                                                 font=("Tahoma", 10, "bold"), fill="#E0F2FE")
                if bounds := self.canvas.bbox(text):
                    x1, y1, x2, y2 = bounds
                    box = self.canvas_pool.get_rect(x1 - 8, y1 - 5, x2 + 8, y2 + 5,
                                                   fill="#0B1120", outline="#22D3EE")
                    self.canvas.tag_lower(box, text)
            if point is not None:
                x, y = point
                self.canvas_pool.get_oval(x - 8, y - 8, x + 8, y + 8, outline="#22D3EE", width=2)
                self.canvas_pool.get_line(x - 11, y, x + 11, y, fill="#22D3EE")
                self.canvas_pool.get_line(x, y - 11, x, y + 11, fill="#22D3EE")
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

    # Autodetecta data.grf e catálogo se não existirem no caminho padrão
    try:
        proc_exe = Path(target_proc.exe())
        proc_dir = proc_exe.parent
        if not args.grf or not Path(args.grf).is_file():
            cand_grf = proc_dir / "data.grf"
            if cand_grf.is_file():
                args.grf = cand_grf
        if not args.catalog or not Path(args.catalog).is_file():
            cand_lua = proc_dir / "System" / "itemInfo.lua"
            if cand_lua.is_file():
                args.catalog = cand_lua
            else:
                cand_lub = proc_dir / "System" / "itemInfo.lub"
                if cand_lub.is_file():
                    args.catalog = cand_lub
    except Exception:
        pass

    # Exibição no console dos valores detectados
    window_display = f"'{args.window}'"
    if args.window_handle:
        window_display += f" (HWND 0x{args.window_handle:X})"

    print("-" * 60, flush=True)
    print("Ambiente de jogo detectado com sucesso:", flush=True)
    print(f"  Interface:      {args.interface}", flush=True)
    print(f"  Servidor:Porta: {args.server}:{args.port}", flush=True)
    print(f"  Janela:         {window_display}", flush=True)
    if args.grf and Path(args.grf).is_file():
        print(f"  GRF:            {args.grf}", flush=True)
    if args.catalog and Path(args.catalog).is_file():
        print(f"  Catálogo:       {args.catalog}", flush=True)
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
        camera.start_tracking(root, viewport, game_has_focus=overlay._is_game_focused,
                              is_ui_point=overlay._is_point_in_game_ui)
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
    init_session_logging()
    if len(sys.argv) == 1:
        sys.argv.append("--live")
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
    parser.add_argument("--rotation", type=float, help="Rotação horizontal inicial da câmera (yaw) em graus.")
    parser.add_argument("--tilt", type=float, help="Inclinação inicial da câmera (pitch) em graus.")
    parser.add_argument("--distance", type=float, help="Distância inicial da câmera em unidades de mundo.")
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
            if getattr(sys, "frozen", False):
                command_line = list2cmdline(sys.argv[1:])
            else:
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
    if (args.player_speed <= 0
            or any(value is not None and not math.isfinite(value)
                   for value in (args.rotation, args.tilt, args.distance))):
        parser.error("Parâmetros de câmera ou velocidade inválidos.")

    if args.live:
        autodetect_live_session(args)
    elif args.mock and not args.port:
        args.port = 5121

    root = tk.Tk()

    def _tk_report_callback_exception(exc, val, tb):
        err = "".join(traceback.format_exception(exc, val, tb))
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"\n[CRASH - TKINTER CALLBACK] {ts}\n{err}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        if _SESSION_LOG_FILE:
            try:
                _SESSION_LOG_FILE.write(msg)
                _SESSION_LOG_FILE.flush()
            except Exception:
                pass

    root.report_callback_exception = _tk_report_callback_exception
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
    init_session_logging()
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            try:
                input("\n[Pressione ENTER para fechar]")
            except Exception:
                pass
        sys.exit(1)
