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
import unicodedata
from bisect import insort
from concurrent.futures import Future
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

from rathena_feed import (
    DEFAULT_GRF, DEFAULT_LENGTHS, TcpCaptureLog, World, capture, is_kafra, list_interfaces, packet_lengths,
)
from rathena_mock import Loopback
from rathena_terrain import (
    CELL_SIZE, Camera, Gat, RagnarokProjector3D, Terrain, get_map_camera_profile, MapCameraProfile,
    CITY_NAMES, CITY_KAFRA, MAP_PORTALS, find_map_route, get_city_travel_plan,
    get_mob_catalog, load_item_catalog, get_item_tier, search_mob_catalog,
    KNOWN_CITIES, load_heatmap,
    matches_city, save_portal_trigger, load_learned_warps
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


def _normalize_menu_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    plain = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(plain.lower().split())


def _kafra_destination_index(options, raw_options, destination, entry):
    target = travel_map_group(_normalize_menu_text(destination))
    aliases = {
        "gef_fild10": ("caverna dos orcs", "vila dos orcs", "orc dungeon"),
        "mjolnir_02": ("mina abandonada", "mjolnir"),
        "cmd_fild07": ("farol de pharos", "pharos"),
    }
    # 1. Caso venha em texto puro, casa pelo nome da cidade destino
    for idx, (option, raw) in enumerate(zip(options, raw_options)):
        normalized = _normalize_menu_text(option)
        if normalized == "cancelar" or raw == "1Wa0AQ":
            continue
        if matches_city(normalized, target) or any(label in normalized for label in aliases.get(target, ())):
            return idx

    # 2. Caso venha em tokens Base64 desconhecidos, use a posição da cidade na lista ordenada
    # cadastrada para aquela Kafra no arquivo de Kafras do projeto (desconsiderando "Cancelar"/"1Wa0AQ")
    if not entry or not options:
        return None
    destinations = [name for name in entry.get("teleports", {})
                    if name != "1Wa0AQ" and _normalize_menu_text(name) != "cancelar"]
    target_pos = None
    for idx, name in enumerate(destinations):
        if travel_map_group(name) == target or matches_city(target, name) or name == destination:
            target_pos = idx
            break
    if target_pos is None:
        return None

    valid_indices = [
        idx for idx, (opt, raw) in enumerate(zip(options, raw_options))
        if raw != "1Wa0AQ" and _normalize_menu_text(opt) != "cancelar"
    ]
    if 0 <= target_pos < len(valid_indices):
        return valid_indices[target_pos]
    return None


def normalize_dialog_option(text: str) -> str:
    """Normaliza texto de opção de menu: minúsculas, sem acentos, sem «», sem preços (ex: 120z) e sem códigos de cores."""
    if not text:
        return ""
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
        "kafra_storage_number": 3,
        "kafra_pin": "",
        "kafra_pin_debug": False,
        "keep_in_bag": ["Asa de Mosca", "Asa de Borboleta"],
        "restock_items": {},
        "hud_x": None,
        "hud_y": None,
        **{key: default for key, _, default, _, _ in SURVIVAL_NUMBERS},
    }
    if not cfg_path.exists():
        save_filters_config(default_cfg)
        return default_cfg
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = {**default_cfg, **cfg}
        for key in ("hud_x", "hud_y"):
            val = merged.get(key)
            if val is not None and type(val) is not int:
                merged[key] = None
        for key in ("kafra_trigger_weight_pct", "kafra_trigger_slots"):
            value = merged[key]
            if type(value) is not int or not 1 <= value <= 100:
                merged[key] = default_cfg[key]
        if type(merged["kafra_storage_number"]) is not int or not 1 <= merged["kafra_storage_number"] <= 3:
            merged["kafra_storage_number"] = default_cfg["kafra_storage_number"]
        if not isinstance(merged["keep_in_bag"], list):
            merged["keep_in_bag"] = default_cfg["keep_in_bag"]
        merged["keep_in_bag"] = [name for name in merged["keep_in_bag"] if isinstance(name, str) and name.strip()]
        if not isinstance(merged["restock_items"], dict):
            merged["restock_items"] = {}
        if not re.fullmatch(r"[0-9]*", str(merged["kafra_pin"])):
            merged["kafra_pin"] = ""
        if type(merged["kafra_pin_debug"]) is not bool:
            merged["kafra_pin_debug"] = False
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


SKILL_MODES = (
    "Pré-Combate (Abertura / Furto)",
    "Combate (Alvo / Área)",
    "Auto-Buff / Suporte",
)
_LEGACY_SKILL_MODE_MAP = {
    "Pré-Combate (Abertura)": "Pré-Combate (Abertura / Furto)",
    "Repetir até Confirmar no Alvo": "Pré-Combate (Abertura / Furto)",
    "Ofensiva em Alvo (Unitária / Splash 3x3)": "Combate (Alvo / Área)",
    "Combate / Área": "Combate (Alvo / Área)",
    "Auto-Buff / Postura": "Auto-Buff / Suporte",
}
SKILL_HOTKEYS = tuple(f"F{i}" for i in range(1, 10)) + tuple(str(i) for i in range(1, 10))
SKILL_HOTKEY_TO_VK = {
    **{f"F{i}": 0x6F + i for i in range(1, 10)},
    **{str(i): 0x30 + i for i in range(1, 10)},
}


def normalize_skill_slot(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    hotkey = str(raw.get("hotkey", "F1")).strip().upper()
    if hotkey not in SKILL_HOTKEY_TO_VK:
        hotkey = "F1"
    raw_mode = str(raw.get("mode", SKILL_MODES[0])).strip()
    mode = _LEGACY_SKILL_MODE_MAP.get(raw_mode, raw_mode)
    if mode not in SKILL_MODES:
        mode = SKILL_MODES[0]
    try:
        skill_id = max(0, int(raw.get("skill_id", 0) or 0))
    except (TypeError, ValueError):
        skill_id = 0
    try:
        status_id = max(0, int(raw.get("status_id", 0) or 0))
    except (TypeError, ValueError):
        status_id = 0
    try:
        cooldown_sec = min(300.0, max(0.3, float(raw.get("cooldown_sec", 0.8) or 0.8)))
    except (TypeError, ValueError):
        cooldown_sec = 0.8
    try:
        cast_time_sec = min(30.0, max(0.0, float(raw.get("cast_time_sec", 0.0) or 0.0)))
    except (TypeError, ValueError):
        cast_time_sec = 0.0
    if "repeat_until_success" in raw:
        repeat_until_success = bool(raw.get("repeat_until_success"))
    else:
        repeat_until_success = str(raw.get("pre_combat_mode", "try_until_works")).strip() != "one_per_mob"
    is_precombat = mode == "Pré-Combate (Abertura / Furto)"
    default_range = 2.3 if is_precombat else 9.0
    try:
        skill_range = min(15.0, max(1.0, float(raw.get("skill_range", default_range) or default_range)))
    except (TypeError, ValueError):
        skill_range = default_range
    max_attempts = (3 if repeat_until_success else 1) if is_precombat else 5
    return {
        "enabled": bool(raw.get("enabled", True)),
        "name": str(raw.get("name", "")).strip()[:40],
        "hotkey": hotkey,
        "mode": mode,
        "repeat_until_success": repeat_until_success,
        "skill_id": skill_id,
        "status_id": status_id,
        "cooldown_sec": cooldown_sec,
        "cast_time_sec": cast_time_sec,
        "min_sp_pct": 0,
        "min_mobs_3x3": 1,
        "max_attempts": max_attempts,
        "max_range": skill_range,
        "whitelist": [],
        "blacklist": [],
        "pre_combat_mode": "try_until_works" if repeat_until_success else "one_per_mob",
        "skill_range": skill_range,
    }


def load_skills_config() -> list[dict]:
    cfg_path = _resolve_resource("skills_config.json")
    if not cfg_path.exists():
        save_skills_config([])
        return []
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [normalize_skill_slot(item) for item in data]
    except Exception:
        return []


def save_skills_config(slots: list[dict]):
    cfg_path = _get_base_dir() / "skills_config.json"
    normalized = [normalize_skill_slot(item) for item in slots]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cfg_path.parent,
                                         prefix=".skills_config-", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(normalized, f, indent=2, ensure_ascii=False)
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


WORLD_MAP_REGION_LABELS = {
    "all": "Todas",
    "rune_midgarts": "Rune-Midgarts",
    "schwartzvalt": "Schwartzvalt",
    "arunafeltz": "Arunafeltz",
    "dungeons_submaps": "Dungeons/Submapas",
}


def _infer_world_map_region_layer(map_name: str) -> tuple[str, str]:
    name = str(map_name or "").strip().lower()
    if any(name.startswith(p) for p in ("lhz_dun", "lhz_cube", "lhz_in", "ein_dun", "ein_in", "juperos", "jupe_", "kh_dun")):
        return "schwartzvalt", "factory_lab"
    if any(name.startswith(p) for p in ("yuno_in",)):
        return "schwartzvalt", "submap"
    if any(name.startswith(p) for p in ("odin_tem", "abyss_", "tha_t")):
        return "schwartzvalt", "dungeon"
    if any(name.startswith(p) for p in ("yuno", "ein", "lhz", "hu_")) or name in ("yuno", "einbroch", "einbech", "lighthalzen", "hugel"):
        layer = "city" if name in ("yuno", "einbroch", "einbech", "lighthalzen", "hugel") else "field"
        return "schwartzvalt", layer
    if any(name.startswith(p) for p in ("ra_san", "ra_temin", "thor_v", "ice_dun", "nameless", "abbey")):
        return "arunafeltz", "dungeon"
    if any(name.startswith(p) for p in ("ra_", "ve_")) or name in ("rachel", "veins"):
        layer = "city" if name in ("rachel", "veins") else "field"
        return "arunafeltz", layer
    if ("_dun" in name or "_in" in name or name.startswith(("in_", "orcs", "anthell", "prt_sewb", "moc_pryd", "moc_para", "gl_", "c_tower", "mag_dun", "tur_dun", "beach_dun"))
            or name in ("pay_arche", "moc_ruins", "izlu2dun", "valkyrie", "xmas_in")):
        layer = "submap" if ("_in" in name or name.startswith(("in_", "moc_para")) or name in ("pay_arche", "moc_ruins", "izlu2dun", "valkyrie", "xmas_in")) else "dungeon"
        return "dungeons_submaps", layer
    if name in CITY_NAMES or name in ("comodo", "umbala", "xmas", "glast_01"):
        return "rune_midgarts", "city"
    return "rune_midgarts", "field"


def load_world_map_regions(routes_data: dict | None = None) -> dict:
    """Carrega world_map_regions.json e mescla automaticamente mapas de .gat, portais e Kafras."""
    regions_path = _resolve_resource("world_map_regions.json")
    data = {"version": 1, "regions": dict(WORLD_MAP_REGION_LABELS), "maps": []}
    if regions_path.exists():
        try:
            loaded = json.loads(regions_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass

    entries_by_map = {}
    for item in data.get("maps", []):
        if isinstance(item, dict) and item.get("map_name"):
            key = str(item["map_name"]).strip().lower()
            entries_by_map[key] = item

    routes = routes_data or {}
    # Indexar aliases e mobs de world_routes.json
    aliases_by_map = collections.defaultdict(list)
    for alias, target in routes.get("aliases", {}).items():
        t_key = str(target).strip().lower()
        if t_key and alias not in aliases_by_map[t_key]:
            aliases_by_map[t_key].append(alias)
    for key, label in CITY_NAMES.items():
        t_key = "yuno" if key == "juno" else key
        if key not in aliases_by_map[t_key]:
            aliases_by_map[t_key].append(key)

    grid_by_map = {}
    for g in routes.get("world_map_grid", ()):
        m = str(g.get("map", "")).strip().lower()
        if m:
            grid_by_map[m] = g

    # Coletar todos os mapas conhecidos do projeto (.gat_cache, portais, Kafras)
    discovered_maps = set(entries_by_map.keys())
    gat_dir = _get_base_dir() / ".gat_cache"
    if gat_dir.is_dir():
        for gat_file in gat_dir.glob("*.gat"):
            discovered_maps.add(gat_file.stem.strip().lower())
    for src, dst in MAP_PORTALS:
        discovered_maps.add(src.strip().lower())
        discovered_maps.add(dst.strip().lower())
    for portal in routes.get("portals", ()):
        discovered_maps.add(str(portal.get("from_map", "")).strip().lower())
        discovered_maps.add(str(portal.get("to_map", "")).strip().lower())
    for portal in routes.get("reference_portals", ()):
        if len(portal) >= 2:
            discovered_maps.add(str(portal[0]).strip().lower())
            discovered_maps.add(str(portal[1]).strip().lower())
    for link in load_learned_warps():
        if "->" in link:
            src, dst = link.split("->", 1)
            discovered_maps.add(src.strip().lower())
            discovered_maps.add(dst.strip().lower())
    for city_key in CITY_KAFRA:
        discovered_maps.add("yuno" if city_key == "juno" else city_key)
    for kafra_file in ("kafra_network.json", "kafras_discovered.json"):
        kf_path = _resolve_resource(kafra_file)
        if kf_path.exists():
            try:
                kf_data = json.loads(kf_path.read_text(encoding="utf-8"))
                kf_map = kf_data.get("kafras", kf_data) if isinstance(kf_data, dict) else {}
                if isinstance(kf_map, dict):
                    for m_name in kf_map:
                        discovered_maps.add(str(m_name).strip().lower())
            except Exception:
                pass
    discovered_maps.discard("")

    # Construir grafo de adjacência para herdar (rx, ry) de mapas vizinhos sem calibração manual
    adj = collections.defaultdict(set)
    for src, dst in MAP_PORTALS:
        s, d = src.strip().lower(), dst.strip().lower()
        adj[s].add(d)
        adj[d].add(s)
    for portal in routes.get("portals", ()):
        s = str(portal.get("from_map", "")).strip().lower()
        d = str(portal.get("to_map", "")).strip().lower()
        if s and d:
            adj[s].add(d)
            adj[d].add(s)
    for link in load_learned_warps():
        if "->" in link:
            s, d = (part.strip().lower() for part in link.split("->", 1))
            if s and d:
                adj[s].add(d)
                adj[d].add(s)

    def _infer_coords(m_name: str) -> tuple[float, float, float]:
        if m_name in grid_by_map:
            g = grid_by_map[m_name]
            rx = (g["u_min"] + g["u_max"]) / 2.0
            ry = (g["v_min"] + g["v_max"]) / 2.0
            rad = max(0.045, min(0.075, math.hypot(g["u_max"] - g["u_min"], g["v_max"] - g["v_min"]) * 0.5))
            return round(rx, 3), round(ry, 3), round(rad, 3)
        # Tentar variante base (ex.: gef_f10_a -> gef_fild10, orcsdun01_c -> orcsdun01)
        base = travel_map_group(m_name)
        if base != m_name and base in entries_by_map:
            b = entries_by_map[base]
            return b["rx"], b["ry"], b.get("radius", 0.050)
        # BFS no grafo de portais até um mapa já posicionado
        visited = {m_name}
        queue = collections.deque([m_name])
        while queue:
            cur = queue.popleft()
            if cur in entries_by_map:
                ref = entries_by_map[cur]
                return ref["rx"], ref["ry"], 0.048
            for nxt in adj.get(cur, ()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        # Fallback por prefixo regional
        reg, _ = _infer_world_map_region_layer(m_name)
        if reg == "schwartzvalt":
            return 0.500, 0.500, 0.045
        if reg == "arunafeltz":
            return 0.420, 0.500, 0.045
        return 0.470, 0.450, 0.045

    changed = False
    for m_name in sorted(discovered_maps):
        if m_name not in entries_by_map:
            region, layer = _infer_world_map_region_layer(m_name)
            rx, ry, radius = _infer_coords(m_name)
            g = grid_by_map.get(m_name, {})
            label = g.get("name") or CITY_NAMES.get(m_name) or m_name
            entries_by_map[m_name] = {
                "map_name": m_name,
                "label": label,
                "aliases": list(aliases_by_map.get(m_name, [])),
                "region": region,
                "layer": layer,
                "rx": rx,
                "ry": ry,
                "radius": radius,
                "mobs": g.get("mobs", ""),
            }
            changed = True
        else:
            entry = entries_by_map[m_name]
            existing_aliases = entry.setdefault("aliases", [])
            for a in aliases_by_map.get(m_name, []):
                if a not in existing_aliases:
                    existing_aliases.append(a)
                    changed = True

    data["maps"] = sorted(entries_by_map.values(), key=lambda e: (e.get("region", ""), e.get("map_name", "")))
    if changed or not regions_path.exists():
        save_path = _get_base_dir() / "world_map_regions.json"
        try:
            tmp = save_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(save_path)
        except OSError:
            pass
    return data


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
                           routes_data=None, network=None, terrains=None, navigation_obstacles=None,
                           locked_kafras=None):
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
    registered = collections.defaultdict(list)
    for row in routes_data.get("portals", ()):
        pair = travel_map_group(row["from_map"]), travel_map_group(row["to_map"])
        sample = ((int(row["wx"]), int(row["wy"])), None, 28)
        if sample not in registered[pair]:
            registered[pair].append(sample)
    portals.update(registered)
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
        for (x, y), arrival, _ in samples:
            if (src, dst) in unverified and (src != origin or not any(
                math.dist((x, y), observed) <= 6 for observed in live_portals
            )):
                continue
            step = {"type": "portal", "from_map": src, "to_map": dst, "wx": x, "wy": y, "cost": 0}
            if travel_step_key(step) not in excluded:
                adj[src].append((dst, (x, y), arrival or reverse, 15, step))
    kafra_arrivals = {
        "prontera": (116, 72), "payon": (161, 58), "geffen": (120, 39), "izlude": (128, 98),
        "aldebaran": (168, 112), "morocc": (156, 46), "alberta": (117, 56),
        "gef_fild10": (52, 326), "yuno": (158, 125),
    }
    for city, entry in kafras.items():
        if "teleport" not in entry.get("services", ()):
            continue
        src = travel_map_group(city)
        sites = [((int(loc["x"]), int(loc["y"])), loc) for loc in entry.get("locations", ())]
        if locked_kafras and (locked_pos := locked_kafras.get(src)):
            locked_sites = [site for site in sites if site[0] == tuple(locked_pos)]
            if locked_sites:
                sites = locked_sites
        elif src == origin and position is not None:
            sites.sort(key=lambda site: math.dist(position, site[0]))
        for dest, price in entry.get("teleports", {}).items():
            dst, price = travel_map_group(dest), int(price)
            for cell, location in sites:
                step = {"type": "kafra", "from_city": src, "to_city": dst, "kafra_pos": cell,
                        "kafra_location": location, "cost": price}
                if travel_step_key(step) not in excluded:
                    adj[src].append((dst, cell, kafra_arrivals.get(dst), 12, step))

    walking_costs = {}

    def walking_distance(map_id, entry, exit_cell, is_portal):
        if entry is None:
            return 105
        key = map_id, entry, exit_cell, is_portal
        if key not in walking_costs:
            gat = terrains.get(map_id) if terrains else None
            path = None
            if gat is not None:
                blocked, penalties = (navigation_obstacles(map_id, gat, exit_cell if is_portal else None)
                                      if navigation_obstacles else (set(), {}))
                goal = gat.find_nearest_walkable(*exit_cell, blocked_cells=blocked)
                if goal is not None:
                    path = gat.find_path(entry, goal, blocked_cells=blocked, penalty_cells=penalties)
            walking_costs[key] = (sum(math.dist(a, b) + penalties.get(b, 0.0) / 10
                                      for a, b in zip(path, path[1:]))
                                  if path else math.dist(entry, exit_cell))
        return walking_costs[key]

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
            cost = score + walking_distance(city, entry, exit_cell, step["type"] == "portal") + action_cost
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
PIN_CLICK_METHOD = "SendMessageTimeoutW"
PIN_UIPI_MESSAGE = "[PIN] Erro 5 (UIPI): execute o overlay-eventos.py como Administrador (mesmo nível do Ragexe.exe)."
KAFRA_INTERACTION_RANGE = 6.0
LOOT_CLICK_RADIUS = 5
LOOT_CLICK_OFFSETS = ((0, -6), (-3, -4), (3, -4))
LOOT_MAX_ATTEMPTS = 2 * len(LOOT_CLICK_OFFSETS)
FARM_MAX_PATH_STEPS = 45
FARM_MAX_DETOUR_RATIO = 2.4
FARM_LAST_POSITION_TIMEOUT = 4.5
FARM_IGNORE_DURATION = 1.5
CLICK_RETRY_OFFSETS = (
    (5, 0), (4, 3), (0, 5), (-4, 3), (-5, 0), (-4, -3), (0, -5), (4, -3),
)
MAX_SNAPSHOT_BYTES = 256_000
MAX_OBJECTS = 200
SNAPSHOT_TTL = .75
DEFAULT_CATALOG = Path("D:/Jogos/Ragnarok/System/itemInfo.lua")
MOB_CATALOG = Path(__file__).with_name("monstros-pt-br.json")
ITEM_LABEL_DELIMITERS = re.compile(
    r"\[?(?P<equals>=+)[\[{](?P<name>.*?)[\]}](?P=equals)\]?"
)
OVERLAY_LABEL_GROUP_RADIUS = 32


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
    retry_after: float = 0.0
    name: str = None
    tag_uv: tuple[float, float] | None = None
    destination_uv: tuple[float, float] | None = None

    def get_anchor(self):
        au = self.anchor_u if self.anchor_u is not None else self.u
        av = self.anchor_v if self.anchor_v is not None else self.v
        return au, av


@dataclass
class EntityOverlayLabel:
    x: int
    y: int
    text: str
    color: str
    amount: int
    fixed: bool = False


@dataclass(frozen=True)
class Snapshot:
    viewport: tuple[int, int, int, int]
    objects: tuple[ScreenObject, ...]
    player: object = None
    weight_ratio: float = 0.0
    base_level: int = 1
    map: str = None
    last_attack_target_id: int | None = None
    last_attack_action: int | None = None
    last_attack_ts: float = 0.0
    last_move_ts: float = 0.0
    npc_dialog: dict | None = None
    frame_time: float = 0.0
    player_cell: tuple[float, float] | None = None
    last_warp_from: dict | None = None
    warp_context: tuple = ((), False)


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
        reference_centers = ((22, 35), (52, 35), (82, 35), (22, 65), (52, 65),
                              (82, 65), (22, 95), (52, 95), (82, 95), (22, 125))
        self.digits = {digit: self._pin_glyph(self.pin, x, y)
                       for digit, (x, y) in zip("5497638201", reference_centers)}
        self.pin_button = self.pin[25:46, 12:32]
        # Localiza a borda 3D do botão sem comparar o dígito no centro.
        self.pin_button_border = np.ones(self.pin_button.shape, dtype="uint8")
        self.pin_button_border[3:-3, 3:-3] = 0
        try:
            import pytesseract
            self.ocr = pytesseract
        except ImportError:
            self.ocr = None

    def _grab_viewport(self, viewport):
        left, top, width, height = viewport
        return self.grab((left, top, left + width, top + height), all_screens=True,
                         include_layered_windows=False)

    def capture(self, viewport):
        return self.np.array(self._grab_viewport(viewport).convert("L"))

    def capture_pin(self, viewport):
        image = self._grab_viewport(viewport)
        return self.np.array(image.convert("L")), self.np.array(image.convert("RGB"))

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

    def _pin_glyph(self, panel, x, y):
        if y < 7 or x < 7 or y + 7 > panel.shape[0] or x + 7 > panel.shape[1]:
            return self.np.zeros((14, 14), dtype=bool)
        patch = panel[y - 7:y + 7, x - 7:x + 7]
        background = self.np.minimum(patch[:, :2].mean(axis=1), patch[:, -2:].mean(axis=1))
        return patch >= self.np.maximum(200, background[:, None] + 26)

    def _pin_button_centers(self, panel, highlighted, *, partial=False):
        np = self.np
        top = 18
        area = panel[top:141, :106]
        button = self.pin_button
        height, width = button.shape
        border = self.pin_button_border
        if self.cv2 is not None:
            errors = self.cv2.matchTemplate(area, button, self.cv2.TM_SQDIFF, mask=border)
        else:
            rows, cols = area.shape[0] - height + 1, area.shape[1] - width + 1
            errors = np.zeros((rows, cols), dtype="float32")
            for y, x in np.argwhere(border):
                delta = area[y:y + rows, x:x + cols].astype("float32") - button[y, x]
                errors += delta * delta

        centers = []
        if highlighted is not None:
            x, y = highlighted
            centers.append(highlighted)
            x -= width // 2
            y -= top + height // 2
            errors[max(0, y - height + 1):y + height, max(0, x - width + 1):x + width] = np.inf
        limit = int(np.count_nonzero(border)) * 27 ** 2
        while len(centers) < 10:
            y, x = map(int, np.unravel_index(errors.argmin(), errors.shape))
            if not errors[y, x] <= limit:
                return tuple(centers) if partial else ()
            centers.append((x + width // 2, top + y + height // 2))
            errors[max(0, y - height + 1):y + height, max(0, x - width + 1):x + width] = np.inf
        return tuple(centers)

    def has_pin_dialog(self, screen):
        return self._match(screen, self.pin[2:15, 3:109], confidence=0.85) is not None

    def settings_window_return_button(self, viewport):
        if self.ocr is None:
            return False, None
        left, top, width, height = viewport
        x0, y0 = width // 4, height // 5
        x1, y1 = width * 3 // 4, height * 4 // 5
        if x1 <= x0 or y1 <= y0:
            return False, None
        screen = self.capture(viewport)[y0:y1, x0:x1]
        words = self._ocr_words(screen)
        if not words:
            return False, None
        has_title = any(word in ("configuracoes", "configuracao", "settings") for word, *_ in words)
        has_context = any(word in ("personagem", "selecionar", "sair", "atalhos", "janela") for word, *_ in words)
        return_word = next((row for row in words if row[0] in ("retornar", "return")), None)
        game_word = None
        if return_word is not None:
            _, rx, ry, rw, rh = return_word
            game_word = next(
                (row for row in words
                 if row[0] in ("jogo", "game")
                 and abs(row[2] - ry) <= max(row[4], rh) + 8
                 and 0 < row[1] - rx <= 220),
                None,
            )
        if not (has_title or (return_word and game_word and has_context)):
            return False, None
        if return_word is not None:
            _, rx, ry, rw, rh = return_word
            if game_word is not None:
                _, jx, jy, jw, jh = game_word
                cx = left + x0 + (rx + jx + jw) // 2
                cy = top + y0 + (ry + rh // 2 + jy + jh // 2) // 2
            else:
                cx = left + x0 + rx + rw // 2
                cy = top + y0 + ry + rh // 2
            return True, (cx, cy)
        return True, None

    def pin_window_rect(self, screen):
        title = self._match(screen, self.pin[2:15, 3:109], confidence=0.85)
        if title is None:
            return None
        x, y = title[0] - 3, title[1] - 2
        height, width = self.pin.shape
        panel = screen[y:y + height, x:x + width]
        if x < 0 or y < 0 or panel.shape != self.pin.shape:
            return None
        return x, y, width, height

    def _ocr_pin_digit(self, glyph):
        if self.ocr is None:
            return None
        try:
            patch = self.np.pad(self.np.where(glyph, 0, 255).astype("uint8"), 4, constant_values=255)
            img = self.Image.fromarray(patch).resize((patch.shape[1] * 4, patch.shape[0] * 4))
            text = self.ocr.image_to_string(
                img,
                config="--psm 10 -c tessedit_char_whitelist=0123456789",
                timeout=0.6,
            ).strip()
        except (OSError, RuntimeError):
            return None
        return text if len(text) == 1 and "0" <= text <= "9" else None

    def pin_field_count(self, panel):
        return self._pin_mask_count(panel[42:55, 109:211])

    def pin_progress(self, viewport, rect, *, with_ink=False):
        left, top, _, _ = viewport
        x, y, _, _ = rect
        strip = self.capture((left + x + 3, top + y + 2, 208, 53))
        if self._match(strip[:13, :106], self.pin[2:15, 3:109], confidence=0.85) != (0, 0):
            return None
        field = strip[40:53, 106:208]
        count = self._pin_mask_count(field)
        return (count, int(self.np.count_nonzero(field < 160))) if with_ink else count

    def _pin_mask_count(self, field):
        dark = field < 160
        # Asteriscos consecutivos encostam nas laterais. Cada um tem uma
        # haste vertical própria e uma barra central de três pixels.
        stems = (dark[:-4, 1:-1] & dark[1:-3, 1:-1] & dark[2:-2, 1:-1]
                 & dark[3:-1, 1:-1] & dark[4:, 1:-1])
        crosses = stems & dark[2:-2, :-2] & dark[2:-2, 2:]
        return int(self.np.count_nonzero(crosses))

    def pin_highlight(self, colors):
        # O realce azul marca a tecla mesmo quando a mão aparece fora da borda.
        top = 18
        area = colors[top:140, :106].astype("int16")
        red, green, blue = self.np.moveaxis(area, -1, 0)
        mask = (blue >= 180) & (blue - red >= 60) & (blue - green >= 24)
        rows = self.np.flatnonzero(mask.sum(axis=1) >= 4)
        cols = self.np.flatnonzero(mask.sum(axis=0) >= 4)
        if not rows.size or not cols.size:
            return None, None
        height, width = rows[-1] - rows[0] + 1, cols[-1] - cols[0] + 1
        if not 13 <= height <= 25:
            return None, None
        point = (int(cols[0] + width // 2), int(top + rows[0] + height // 2))
        if 13 <= width <= 25:
            return point, None
        if 32 <= width <= 55 and point[1] >= 112:
            return None, point
        return None, None

    def _pin_template_digit(self, glyph):
        scores = sorted((self.np.count_nonzero(glyph != template), digit)
                        for digit, template in self.digits.items())
        distance, digit = scores[0]
        if distance <= 20 and scores[1][0] - distance >= 4:
            return digit
        return None

    def pin_grid_changed(self, panel, glyphs, highlighted):
        changed = 0
        for (x, y), (digit, previous) in glyphs.items():
            if previous is None or (highlighted is not None and math.dist((x, y), highlighted) <= 22):
                continue
            glyph = self._pin_glyph(panel, x, y)
            if self.np.count_nonzero(glyph != previous) <= 14:
                continue
            detected = self._pin_template_digit(glyph)
            if detected is not None and detected != digit:
                changed += 1
                if changed == 2:
                    return True
        return False


    def pin_reset_point(self, panel, highlighted_reset):
        if highlighted_reset is not None:
            return highlighted_reset
        match = self._match(panel[111:140, :106], self.pin[117:133, 47:85])
        return (match[0] + 19, 111 + match[1] + 8) if match is not None else None

    def pin_controls(self, panel, highlighted, *, partial=False):
        ok_match = self._match(panel[143:167, 130:170], self.pin[146:164, 136:164])
        if ok_match is None and not partial:
            return None
        centers = self._pin_button_centers(panel, highlighted, partial=partial)
        if not centers and not partial:
            return None
        readings = {}
        for point in centers:
            if point == highlighted:
                readings[point] = (None, None)
                continue
            glyph = self._pin_glyph(panel, *point)
            digit = self._pin_template_digit(glyph) if glyph.any() else None
            readings[point] = (digit, glyph)

        known = [digit for digit, _ in readings.values() if digit is not None]
        complete_by_elimination = len(centers) == 10 and len(known) == len(set(known)) == 9
        if not complete_by_elimination:
            for point, (digit, glyph) in readings.items():
                if digit is None and glyph is not None and glyph.any():
                    readings[point] = (self._ocr_pin_digit(glyph), glyph)

        buttons = {}
        glyphs = {}
        ambiguous = set()
        for point, (digit, glyph) in readings.items():
            if digit is None:
                continue
            if digit in buttons:
                glyphs.pop(buttons.pop(digit))
                ambiguous.add(digit)
            if digit in ambiguous:
                continue
            buttons[digit] = point
            glyphs[point] = (digit, glyph)

        if len(centers) == 10 and len(buttons) == 9 and not ambiguous:
            # Com dez botões localizados e nove IDs únicos, sobra uma única tecla.
            digit, = self.digits.keys() - buttons.keys()
            point, = readings.keys() - glyphs.keys()
            buttons[digit] = point
            glyphs[point] = (digit, readings[point][1])
        if not partial and len(buttons) != 10:
            return None
        ok_point = (130 + ok_match[0] + 14, 143 + ok_match[1] + 9) if ok_match is not None else None
        return buttons, ok_point, glyphs

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

    def __init__(self, world, terrain, camera, viewport, traffic_log=None):
        self.world = world
        self.terrain = terrain
        self.camera = camera
        self.viewport = viewport
        self.traffic_log = traffic_log
        self._cam = None

    def stats(self):
        return self.world.stats()

    def _get_interpolated_player_pos(self, player, now):
        prediction = self.world.predicted_player
        track = prediction[0] if prediction and now < prediction[1] else player
        return track.cell(now)

    @staticmethod
    def _get_interpolated_entity_pos(entity, now):
        return entity.track.cell(now)

    def current(self):
        name, player, entities, dialog, last_warp_from, warp_context = self.world.snapshot(
            include_dialog=True, include_warp=True)
        viewport = self.viewport()
        if not viewport:
            return None
        self.camera.on_map_change(name)
        self._cam = None
        objects = []
        now = time.perf_counter()
        player_cell = None
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

            player_cell = player_fx, player_fy = self._get_interpolated_player_pos(player, now)
            frame_anchor = (player_fx, player_fy, height(player_fx, player_fy))
            self._cam = frame_anchor
            project = self.camera.projector(viewport, frame_anchor, height, raw=self.camera.alignment_active)
            rendered_portals = set()
            for (kind, aid), entity in entities:
                fx, fy = self._get_interpolated_entity_pos(entity, now)
                if kind == "warp":
                    portal_cell = (round(fx), round(fy))
                    if portal_cell in rendered_portals:
                        continue
                    rendered_portals.add(portal_cell)
                anchor_u, anchor_v = project(fx, fy, 0.0)
                sprite_z = 5.5 if kind in ("mob", "npc", "player") else 1.2
                click_u, click_v = project(fx, fy, sprite_z)
                if (-0.2 <= click_u <= 1.2 and -0.2 <= click_v <= 1.2) or (-0.2 <= anchor_u <= 1.2 and -0.2 <= anchor_v <= 1.2):
                    ownership = getattr(entity, "ownership", "FREE")
                    track = entity.track
                    destination_uv = (project(*track.target)
                                      if kind == "mob" and track.started <= now < track.end_time else None)
                    objects.append(ScreenObject(
                        f"{kind}:{aid}", kind, entity.catalog_id,
                        click_u, click_v, entity.amount, ownership,
                        anchor_u, anchor_v, entity.retry_after,
                        (self.world.npc_name(aid, entity.catalog_id, entity.track.target, name)
                         if kind == "npc" else self.world.entity_names.get(aid) if kind == "player" else None),
                        tag_uv=project(fx, fy, self.TAG_HEIGHT_CELLS.get(kind, 1.9) * CELL_SIZE),
                        destination_uv=destination_uv,
                    ))

        attack_target, attack_action, attack_ts, move_ts = self.world.client_combat_snapshot()
        return Snapshot(
            viewport, tuple(objects),
            player=player,
            weight_ratio=getattr(player, "weight_ratio", 0.0) if player else 0.0,
            base_level=getattr(player, "base_level", 1) if player else 1,
            map=name,
            last_attack_target_id=attack_target,
            last_attack_action=attack_action,
            last_attack_ts=attack_ts,
            last_move_ts=move_ts,
            npc_dialog=dialog,
            frame_time=now,
            player_cell=player_cell,
            last_warp_from=last_warp_from,
            warp_context=warp_context,
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
    0x24: (0x47, True),
    0x23: (0x4F, True),
    0x21: (0x49, True),
    0x22: (0x51, True),
}

_keyboard_api = ctypes.WinDLL("user32", use_last_error=True)
_keyboard_api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_keyboard_api.SendInput.restype = wintypes.UINT

u32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
u32.FindWindowW.restype = wintypes.HWND
u32.WindowFromPoint.argtypes = (wintypes.POINT,)
u32.WindowFromPoint.restype = wintypes.HWND
u32.RealChildWindowFromPoint.argtypes = (wintypes.HWND, wintypes.POINT)
u32.RealChildWindowFromPoint.restype = wintypes.HWND
u32.MapWindowPoints.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT)
u32.MapWindowPoints.restype = ctypes.c_int

OSK_ABNT2_KEYS = {
    0x2D: (0.819, 0.500),  # Insert
    0x0D: (0.735, 0.400),  # Enter
    0x1B: (0.025, 0.100),  # Esc
    0x09: (0.030, 0.300),  # Tab
    0x20: (0.350, 0.900),  # Espaço
    0x2E: (0.485, 0.900),  # Delete
    0x26: (0.745, 0.700),  # Cima
    0x25: (0.585, 0.900),  # Esquerda
    0x28: (0.635, 0.900),  # Baixo
    0x27: (0.685, 0.900),  # Direita
}


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

        # Se o handle estiver ausente ou foi fechado, tenta redetectar a janela do jogo
        if not self.handle or not self.user.IsWindow(self.handle):
            if now - getattr(self, "_last_redetect_time", 0.0) >= 1.0:
                self._last_redetect_time = now
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
        self._alignment_locked = True
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
        self._alignment_locked = True
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
        alignment = self._screen_alignments.get(self._calibration_key, {}).get(self._alignment_slot)
        if alignment is not None:
            self._set_zoom(self._alignment_zoom(alignment), observed=True)
        else:
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
        aligned.camera_ground = project.camera_ground
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
            else:
                self._zoom_observer.take_ground_distance()
        while self._mouse_click_events:
            sx, sy, left_button, clicked_at, foreground, ctrl = self._mouse_click_events.popleft()
            if (left_button and game_focused and self._game_has_focus(foreground)
                    and self.on_lbutton_down_callback is not None
                    and self.on_lbutton_down_callback(sx, sy, ctrl)):
                continue
            if self.on_physical_click is not None:
                self.on_physical_click(sx, sy, left_button, clicked_at, foreground)
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
                        if not ms.flags & 0x01 and wParam in (0x0201, 0x0202, 0x0203):
                            win32_input.observe_physical_left(wParam != 0x0202)
                        if not ms.flags & 0x01 and wParam in (0x0201, 0x0204, 0x0207, 0x020B):
                            self._mouse_click_events.append((ms.pt.x, ms.pt.y, wParam == 0x0201,
                                                             time.monotonic(), u32.GetForegroundWindow(),
                                                             bool(u32.GetAsyncKeyState(0x11) & 0x8000)))
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
            win32_input.track_physical_left(bool(self._wheel_hook))
        except Exception:
            self._wheel_hook = None

    def uninstall_wheel_hook(self):
        if self._wheel_hook:
            try:
                u32.UnhookWindowsHookEx(self._wheel_hook)
            except Exception:
                pass
            self._wheel_hook = None
            win32_input.track_physical_left(False)
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

    def send_left_click(self, hold_sec: float = 0.05) -> bool:
        """Envia clique esquerdo físico na posição atual do cursor (sem mover)."""
        if not self.is_available or not self.context or not self.mouse_dev:
            return False
        try:
            stroke_down = InterceptionMouseStroke(
                state=0x0001, flags=0, rolling=0, x=0, y=0, information=0
            )
            if self.dll.interception_send(self.context, self.mouse_dev, ctypes.byref(stroke_down), 1) != 1:
                return False
            time.sleep(hold_sec)
            stroke_up = InterceptionMouseStroke(
                state=0x0002, flags=0, rolling=0, x=0, y=0, information=0
            )
            return self.dll.interception_send(self.context, self.mouse_dev, ctypes.byref(stroke_up), 1) == 1
        except Exception as exc:
            print(f"[HID] Erro ao enviar clique atual via Interception: {exc}", flush=True)
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
        self._prev_rect_idx = 0
        self._prev_oval_idx = 0
        self._prev_text_idx = 0
        self._prev_line_idx = 0
        self._rect_cache = {}
        self._oval_cache = {}
        self._text_cache = {}
        self._line_cache = {}

    def begin_frame(self):
        self._rect_idx = 0
        self._oval_idx = 0
        self._text_idx = 0
        self._line_idx = 0

    def end_frame(self):
        # Oculta apenas itens que estavam visíveis no frame anterior e não foram usados neste
        for i in range(self._rect_idx, self._prev_rect_idx):
            self.canvas.itemconfigure(self.rects[i], state="hidden")
        for i in range(self._oval_idx, self._prev_oval_idx):
            self.canvas.itemconfigure(self.ovals[i], state="hidden")
        for i in range(self._text_idx, self._prev_text_idx):
            self.canvas.itemconfigure(self.texts[i], state="hidden")
        for i in range(self._line_idx, self._prev_line_idx):
            self.canvas.itemconfigure(self.lines[i], state="hidden")
        self._prev_rect_idx = self._rect_idx
        self._prev_oval_idx = self._oval_idx
        self._prev_text_idx = self._text_idx
        self._prev_line_idx = self._line_idx

    def hide_all(self):
        self.begin_frame()
        self.end_frame()

    def get_rect(self, x1, y1, x2, y2, **kwargs):
        idx = self._rect_idx
        coords = (x1, y1, x2, y2)
        cfg = tuple(sorted(kwargs.items()))
        if idx < len(self.rects):
            item = self.rects[idx]
            was_hidden = idx >= self._prev_rect_idx
            prev = self._rect_cache.get(item)
            if prev is None or prev[0] != coords:
                self.canvas.coords(item, x1, y1, x2, y2)
            if was_hidden or prev is None or prev[1] != cfg:
                self.canvas.itemconfigure(item, state="normal", **kwargs)
            self._rect_cache[item] = (coords, cfg)
        else:
            item = self.canvas.create_rectangle(x1, y1, x2, y2, state="normal", **kwargs)
            self.rects.append(item)
            self._rect_cache[item] = (coords, cfg)
        self._rect_idx += 1
        return item

    def get_oval(self, x1, y1, x2, y2, **kwargs):
        kwargs.setdefault("fill", "")
        kwargs.setdefault("dash", "")
        idx = self._oval_idx
        coords = (x1, y1, x2, y2)
        cfg = tuple(sorted(kwargs.items()))
        if idx < len(self.ovals):
            item = self.ovals[idx]
            was_hidden = idx >= self._prev_oval_idx
            prev = self._oval_cache.get(item)
            if prev is None or prev[0] != coords:
                self.canvas.coords(item, x1, y1, x2, y2)
            if was_hidden or prev is None or prev[1] != cfg:
                self.canvas.itemconfigure(item, state="normal", **kwargs)
            self._oval_cache[item] = (coords, cfg)
        else:
            item = self.canvas.create_oval(x1, y1, x2, y2, state="normal", **kwargs)
            self.ovals.append(item)
            self._oval_cache[item] = (coords, cfg)
        self._oval_idx += 1
        return item

    def get_text(self, x, y, **kwargs):
        idx = self._text_idx
        cfg = tuple(sorted(kwargs.items()))
        if idx < len(self.texts):
            item = self.texts[idx]
            was_hidden = idx >= self._prev_text_idx
            prev_cfg = self._text_cache.get(item)
            self.canvas.coords(item, x, y)
            if was_hidden or prev_cfg != cfg:
                self.canvas.itemconfigure(item, state="normal", **kwargs)
            self._text_cache[item] = cfg
        else:
            item = self.canvas.create_text(x, y, state="normal", **kwargs)
            self.texts.append(item)
            self._text_cache[item] = cfg
        self._text_idx += 1
        return item

    def get_line(self, *coords, **kwargs):
        kwargs.setdefault("dash", "")
        idx = self._line_idx
        cfg = tuple(sorted(kwargs.items()))
        if idx < len(self.lines):
            item = self.lines[idx]
            was_hidden = idx >= self._prev_line_idx
            prev = self._line_cache.get(item)
            if prev is None or prev[0] != coords:
                self.canvas.coords(item, *coords)
            if was_hidden or prev is None or prev[1] != cfg:
                self.canvas.itemconfigure(item, state="normal", **kwargs)
            self._line_cache[item] = (coords, cfg)
        else:
            item = self.canvas.create_line(*coords, state="normal", **kwargs)
            self.lines.append(item)
            self._line_cache[item] = (coords, cfg)
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
        self._portal_boxes_draw_cache = None
        self._nav_obstacles_cache = None
        self._portal_guard_parsed_cache = None
        self._topmost_check_at = 0.0
        self.last = None
        self.last_stats = None
        self.active_nav_path = None
        self._active_nav_path = []
        self._active_nav_idx = 0
        self._active_nav_goal = None
        self._active_nav_wp_dist = float("inf")
        self._active_nav_wp_ts = 0.0
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
        self._locked_farm_target_id = None
        self._in_active_combat = False
        self._farm_lock_started_at = 0.0
        self._farm_attack_ts = 0.0
        self._farm_target_missing_since = None
        self._farm_progress = None
        self._farm_route = None
        self._farm_pursuit = None
        self._farm_patrol_until = 0.0
        self.ignored_mobs = {}
        self._loot_attempts = {}
        self._farm_seen_attack_ts = 0.0
        self._farm_attack_action = None
        self._farm_retry_at = 0.0
        self._farm_range = 1.0
        self._farm_range_hit_ts = 0.0
        self._farm_reachability_check = None
        self.locked_combat_mob_gid = None
        self._locked_drop_gid = None
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
        self._patrol_no_wp_cooldown = 0.0
        self._patrol_finished_ts = {}
        self._patrol_unreachable_until = {}
        self._patrol_scan_zone = None
        self._patrol_visibility_key = None
        self._patrol_visible = ()
        self.patrol_waypoint = None
        self.visited_cells = []
        self.temp_blocked_cells = {}
        self.visual_occluded_cells = {}
        self._visual_walk_probe = None
        self._last_visual_failure = None
        self._plateau_short_step_until = 0.0
        self.stuck_count = 0
        self.patrol_target_route = None  # (map_name, x, y, target_npc_or_warp)
        self.nav_goal = None  # (map_name, x, y) — rota direcionada INDEPENDENTE do auto_patrol
        self.manual_nav_goal = None
        self._minimap_goal = None
        self._minimap_path_request = None
        self._minimap_path_worker = None
        self.minimap_nav = True
        self.travel_plan = None
        self._travel_allowed_portal = None
        self._world_map_open = False
        self._world_map_popup = None
        self._world_map_region = "all"
        self._world_map_candidates = []
        self._world_map_notice = None
        self.filters_config = load_filters_config()
        self.skills_config = load_skills_config()
        self._skill_cooldowns = {}
        self._skill_target_attempts = {}
        self._skill_cast_until = 0.0
        self._pending_buff_key = None
        self._pending_skill_target = None
        self._skill_approach_step = None
        self._skill_cast_started_at = 0.0
        self._skill_cast_target_gid = None
        self._skill_cast_max_range = 9.0
        self._skill_cast_skill_id = 0
        self._active_splash_gid = None
        # ── Parte 3: estado pré-combate por mob_gid ───────────────────────────
        # state ∈ {"PENDING", "ARMING", "WAITING", "CASTING", "DONE", "FAILED"}
        self._precombat_states: dict = {}          # {(mob_gid, idx): state}
        self._precombat_click_attempts: dict = {}  # {(mob_gid, idx): int}
        self._precombat_cast_until: float = 0.0    # monotonic: bloqueia ataques durante cast
        self._precombat_last_cs_ts: float = 0.0    # ts do último C->S de skill
        self._precombat_wait_until: float = 0.0    # aguarda resposta TCP até este ts
        self._precombat_active_slot: dict = {}      # slot em execução
        self._precombat_cursor_armed: bool = False
        self._precombat_key_future = None
        self._precombat_need_hover_reset: bool = False
        self._last_dispatched_skill_slot = None    # (ts_monotonic, idx)
        self._active_splash_until = 0.0
        self.tier1_items = self.filters_config.get("tier1_items", [])
        self.tier3_items = self.filters_config.get("tier3_items", [])
        if isinstance(feed, LiveFeed):
            feed.world.drop_is_priority = self._is_priority_drop
        self.tier3_max_weight_pct = self.filters_config.get("tier3_max_weight_pct", 55)
        self.world_routes_data = load_world_routes()
        self.world_map_regions_data = load_world_map_regions(self.world_routes_data)
        self._learned_warps = load_learned_warps()
        self._navigation_kafras = load_kafra_network()
        self._last_warp_seen = 0.0
        self._intentional_portal_click = None
        self.nav_path = None
        self.farm_map = None
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._kafra_cycle = None
        self._kafra_manual = False
        self._kafra_capacity_latched = False
        self._kafra_block = None
        self._map_stabilize_until = 0.0
        self._last_stabilized_map = None
        self._last_map_server_transition_ts = 0.0
        self._last_map_arrival_ts = 0.0
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
        self._hud_submenu = None
        self._hud_panel_visible = True
        self._hud_active_drawer = None
        self._hud_active_submenu = None
        self._hud_drawer_paused = False
        self._hud_drawer_walk_since = None
        self._hud_drawer_frames = {}
        self._hud_drawer_buttons = {}
        self._hud_submenu_frames = {}
        self._hud_submenu_buttons = {}
        self._hud_pos = (self.filters_config.get("hud_x"), self.filters_config.get("hud_y"))
        self._hud_drawer_pos = None
        self._hud_submenu_pos = None
        self._hud_dragging = False
        self._hud_drag_state = None
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
        self._show_players = tk.BooleanVar(value=False)
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
        self._click_in_progress = False
        self._click_retry_history = collections.OrderedDict()
        self._game_key_ready = threading.Condition()
        self._game_key_closed = False
        self._game_key_queue = collections.deque()
        self._game_key_worker_active = False
        self._game_key_pending = set()
        self._menu_selection_active = False
        self._menu_osk_progress = None
        self._osk_send_lock = threading.RLock()
        self._osk_retry_after = 0.0
        self._osk_hwnd = None
        self._osk_invoke_cache = {}
        self._osk_fn_keys = set()
        self._last_sit_attempt_at = float("-inf")
        self._sit_attempts = 0
        self._sit_confirmation = None
        self._action_click_used = None
        self._last_patrol_click_target = None
        self._directed_replan_at = 0.0
        self._directed_failures = 0
        self._patrol_interaction = None
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
            world.warp_intent_callback = self._is_intentional_warp
        self._create_hud_panel()
        if hasattr(self.feed, "camera"):
            self.feed.camera.on_lbutton_down_callback = self._on_game_lbutton_down
            self.feed.camera.on_physical_click = self._record_hud_click
        self.root.after(150, self._prepare_osk_startup)

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
        drawer.geometry("430x370")
        drawer_header = tk.Frame(drawer, bg="#181b28", padx=8, pady=3, cursor="fleur")
        drawer_header.pack(fill="x")
        drawer_grip = tk.Label(drawer_header, text="⠿ Painel (arrastar)", bg="#181b28", fg=purple,
                               font=("Segoe UI", 8, "bold"), cursor="fleur")
        drawer_grip.pack(side="left")
        drawer_close = tk.Label(drawer_header, text="✕", bg="#181b28", fg="#94a3b8",
                                font=("Segoe UI", 9, "bold"), cursor="hand2", padx=4)
        drawer_close.pack(side="right")
        drawer_close.bind("<Button-1>", guard(lambda _e: self._close_hud_drawer()))
        drawer_body = tk.Frame(drawer, bg=bg)
        drawer_body.pack(fill="both", expand=True)
        self._hud_drawer_body = drawer_body

        submenu = self._hud_submenu = window("Ragnalok Submenu")
        submenu.configure(highlightthickness=1, highlightbackground=purple)
        submenu_header = tk.Frame(submenu, bg="#181b28", padx=6, pady=2, cursor="fleur")
        submenu_header.pack(fill="x")
        submenu_grip = tk.Label(submenu_header, text="⠿ Submenu", bg="#181b28", fg=purple,
                                font=("Segoe UI", 8, "bold"), cursor="fleur")
        submenu_grip.pack(side="left")
        submenu_close = tk.Label(submenu_header, text="✕", bg="#181b28", fg="#94a3b8",
                                 font=("Segoe UI", 8, "bold"), cursor="hand2", padx=2)
        submenu_close.pack(side="right")
        submenu_close.bind("<Button-1>", guard(lambda _e: self._close_hud_submenu()))
        self._hud_submenu_body = tk.Frame(submenu, bg=bg, padx=4, pady=4)
        self._hud_submenu_body.pack(fill="both", expand=True)

        strip = tk.Frame(dock, bg=bg, padx=4)
        strip.pack(fill="both", expand=True)
        drag_handle = tk.Label(strip, text="⠿", bg=bg, fg=purple, font=("Segoe UI", 10, "bold"),
                               cursor="fleur", padx=4)
        drag_handle.pack(side="left")
        self._hud_status = tk.StringVar(value="Mapa —  |  Peso —  |  Slots: —/100  |  00:00:00")
        status_label = tk.Label(strip, textvariable=self._hud_status, bg=bg, fg=blue, width=38,
                                font=("Segoe UI", 9, "bold"), anchor="w", cursor="fleur")
        status_label.pack(side="left")
        self._hud_play_button = button(strip, "▶ Play", self._toggle_hud_run)
        self._hud_play_button.configure(width=14)
        self._set_hud_running(False)
        for target_kind, widgets in (
            ("panel", (strip, drag_handle, status_label)),
            ("drawer", (drawer_header, drawer_grip)),
            ("submenu", (submenu_header, submenu_grip)),
        ):
            for w in widgets:
                w.bind("<ButtonPress-1>", guard(lambda e, k=target_kind: self._begin_hud_drag(e, k)), add="+")
                w.bind("<B1-Motion>", guard(lambda e, k=target_kind: self._drag_hud(e, k)), add="+")
                w.bind("<ButtonRelease-1>", guard(lambda e, k=target_kind: self._end_hud_drag(e, k)), add="+")

        def add_submenu(title):
            frame = tk.Frame(self._hud_submenu_body, bg=bg)
            self._hud_submenu_frames[title] = frame
            self._hud_submenu_buttons[title] = button(strip, f"{title} ▾", lambda t=title: self._toggle_hud_submenu(t))
            return frame

        def sub_button(parent, text, command):
            def wrapped():
                self._mark_hud_control_click()
                now = time.monotonic()
                self._hud_click_grace = (now, now + 0.8)
                self._hud_manual_clicks.clear()
                self._focus_hud_game()
                return command()
            w = tk.Button(parent, text=text, command=guard(wrapped), anchor="w", width=18, **button_style)
            w.pack(fill="x", pady=1)
            return w

        sub_mov = add_submenu("Movimento/Rota")
        self._hud_btns["auto_patrol"] = sub_button(sub_mov, "Patrulha livre", self._toggle_auto_patrol)
        self._hud_btns["minimap_nav"] = sub_button(sub_mov, "Minimapa Nav", self._toggle_minimap_nav)
        self._hud_drawer_buttons["Mapas"] = sub_button(
            sub_mov, "Mapas", lambda: (self._close_hud_submenu(), self._toggle_hud_drawer("Mapas")))
        sub_button(sub_mov, "Parar", lambda: (self._close_hud_submenu(), self._stop_hud_actions()))

        sub_comb = add_submenu("Combate/Coleta")
        self._hud_btns["auto_farm"] = sub_button(sub_comb, "Farm", self._toggle_auto_farm)
        self._hud_btns["auto_loot"] = sub_button(sub_comb, "Loot", self._toggle_auto_loot)
        self._hud_drawer_buttons["Skills"] = sub_button(
            sub_comb, "Skills", lambda: (self._close_hud_submenu(), self._toggle_hud_drawer("Skills")))

        sub_info = add_submenu("Info/Listas")
        for title in ("Mobs W/B", "Itens W/B", "NPCs", "Resumo"):
            self._hud_drawer_buttons[title] = sub_button(
                sub_info, title, lambda t=title: (self._close_hud_submenu(), self._toggle_hud_drawer(t)))
        sub_button(sub_info, "Guardar na Kafra", lambda: (self._close_hud_submenu(), self._request_kafra_storage()))

        def add_drawer(title, create_button=False):
            frame = tk.Frame(drawer_body, bg=bg, padx=10, pady=6)
            self._hud_drawer_frames[title] = frame
            if create_button:
                self._hud_drawer_buttons[title] = button(strip, title, lambda t=title: self._toggle_hud_drawer(t))
            return frame

        skills_page = add_drawer("Skills")
        tk.Label(skills_page, text="Skills do Auto-Farm", bg=bg, fg=fg,
                 font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", pady=(0, 4))
        self._skills_listbox = tk.Listbox(skills_page, bg=surface, fg=fg, selectbackground=purple,
                                          selectforeground=fg, bd=0, highlightthickness=0, height=4,
                                          font=font, exportselection=False)
        self._skills_listbox.pack(fill="x", pady=(0, 4))
        skills_btn_row = row(skills_page)
        self._skill_selected_idx = 0 if self.skills_config else None

        skill_vars = {
            "enabled": tk.BooleanVar(value=True),
            "name": tk.StringVar(value=""),
            "hotkey": tk.StringVar(value="F1"),
            "mode": tk.StringVar(value=SKILL_MODES[0]),
            "repeat_until_success": tk.BooleanVar(value=True),
        }

        repeat_chk = tk.Checkbutton(
            skills_page,
            text="Repetir até funcionar / roubar (máx. 3x)",
            variable=skill_vars["repeat_until_success"],
            bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
            selectcolor=surface, font=font, bd=0, highlightthickness=0,
            anchor="w", takefocus=False,
        )

        def update_repeat_visibility(*_args):
            if skill_vars["mode"].get() == "Pré-Combate (Abertura / Furto)":
                if not repeat_chk.winfo_ismapped():
                    repeat_chk.pack(anchor="w", pady=(2, 0), before=self._skills_status)
            elif repeat_chk.winfo_ismapped():
                repeat_chk.pack_forget()

        def refresh_skills_list():
            self._skills_listbox.delete(0, tk.END)
            for idx, slot in enumerate(self.skills_config):
                flag = "ON" if slot["enabled"] else "OFF"
                tag = slot["name"] or f"Skill #{idx + 1}"
                id_tag = f" #{slot['skill_id']}" if slot.get("skill_id") else ""
                self._skills_listbox.insert(tk.END, f"[{flag}] {slot['hotkey']} • {tag}{id_tag} ({slot['mode'][:11]})")
            if self.skills_config and self._skill_selected_idx is not None and 0 <= self._skill_selected_idx < len(self.skills_config):
                self._skills_listbox.selection_set(self._skill_selected_idx)

        def load_slot_into_form(idx):
            if idx is None or not (0 <= idx < len(self.skills_config)):
                return
            slot = self.skills_config[idx]
            skill_vars["enabled"].set(slot["enabled"])
            skill_vars["name"].set(slot["name"])
            skill_vars["hotkey"].set(slot["hotkey"])
            skill_vars["mode"].set(slot["mode"])
            skill_vars["repeat_until_success"].set(slot.get("repeat_until_success", True))
            update_repeat_visibility()
            learned = slot.get("skill_id", 0)
            self._skills_status.configure(
                text=f"Skill ID aprendido via TCP: #{learned}" if learned else "Skill ID e delay detectados automaticamente via TCP."
            )

        def on_skill_select(_evt=None):
            sel = self._skills_listbox.curselection()
            if sel:
                self._skill_selected_idx = sel[0]
                load_slot_into_form(self._skill_selected_idx)

        self._skills_listbox.bind("<<ListboxSelect>>", guard(on_skill_select))

        def add_skill_slot():
            new_slot = normalize_skill_slot({"name": f"Skill {len(self.skills_config) + 1}", "hotkey": "F2"})
            self.skills_config.append(new_slot)
            self._skill_selected_idx = len(self.skills_config) - 1
            save_skills_config(self.skills_config)
            refresh_skills_list()
            load_slot_into_form(self._skill_selected_idx)

        def remove_skill_slot():
            idx = self._skill_selected_idx
            if idx is not None and 0 <= idx < len(self.skills_config):
                self.skills_config.pop(idx)
                self._skill_cooldowns.clear()
                self._skill_selected_idx = (min(idx, len(self.skills_config) - 1) if self.skills_config else None)
                save_skills_config(self.skills_config)
                refresh_skills_list()
                if self._skill_selected_idx is not None:
                    load_slot_into_form(self._skill_selected_idx)

        def save_skill_slot():
            idx = self._skill_selected_idx
            existing = self.skills_config[idx] if (idx is not None and 0 <= idx < len(self.skills_config)) else {}
            raw = {
                **existing,
                "enabled": skill_vars["enabled"].get(),
                "name": skill_vars["name"].get(),
                "hotkey": skill_vars["hotkey"].get(),
                "mode": skill_vars["mode"].get(),
                "repeat_until_success": skill_vars["repeat_until_success"].get(),
            }
            if existing.get("mode") != raw["mode"]:
                raw.pop("skill_range", None)
            slot = normalize_skill_slot(raw)
            if idx is not None and 0 <= idx < len(self.skills_config):
                self.skills_config[idx] = slot
            else:
                self.skills_config.append(slot)
                self._skill_selected_idx = len(self.skills_config) - 1
            save_skills_config(self.skills_config)
            refresh_skills_list()
            load_slot_into_form(self._skill_selected_idx)
            self._skills_status.configure(text=f"Skill '{slot['name'] or slot['hotkey']}' salva.")

        button(skills_btn_row, "+ Nova", add_skill_slot)
        button(skills_btn_row, "Remover", remove_skill_slot)
        button(skills_btn_row, "Salvar", save_skill_slot)

        tk.Checkbutton(skills_page, text="Ativa", variable=skill_vars["enabled"],
                       bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                       selectcolor=surface, font=font, bd=0, highlightthickness=0,
                       anchor="w", takefocus=False).pack(anchor="w")

        r1 = row(skills_page)
        label(r1, "Nome").pack(side="left")
        tk.Entry(r1, textvariable=skill_vars["name"], width=16, bg=surface, fg=fg,
                 insertbackground=fg, relief="flat", font=font).pack(side="left", padx=(4, 8))
        label(r1, "Tecla").pack(side="left")
        hk_menu = tk.OptionMenu(r1, skill_vars["hotkey"], *SKILL_HOTKEYS)
        hk_menu.configure(bg=surface, fg=fg, activebackground=purple, activeforeground=fg,
                          highlightthickness=0, bd=0, font=font, takefocus=False)
        hk_menu["menu"].configure(bg=surface, fg=fg, activebackground=purple, activeforeground=fg, font=font)
        hk_menu.pack(side="left", padx=(4, 0))

        r2 = row(skills_page)
        label(r2, "Modo").pack(side="left")
        mode_menu = tk.OptionMenu(r2, skill_vars["mode"], *SKILL_MODES)
        mode_menu.configure(bg=surface, fg=fg, activebackground=purple, activeforeground=fg,
                            highlightthickness=0, bd=0, font=font, takefocus=False, width=32)
        mode_menu["menu"].configure(bg=surface, fg=fg, activebackground=purple, activeforeground=fg, font=font)
        mode_menu.pack(side="left", padx=(4, 0))

        self._skills_status = label(skills_page, "Skill ID e delay detectados automaticamente via TCP.", purple)
        self._skills_status.pack(fill="x", pady=(4, 0))
        skill_vars["mode"].trace_add("write", update_repeat_visibility)
        update_repeat_visibility()
        refresh_skills_list()
        if self._skill_selected_idx is not None:
            load_slot_into_form(self._skill_selected_idx)

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
                        region_entries = {
                            e["map_name"]: e for e in self.world_map_regions_data.get("maps", ())
                            if isinstance(e, dict) and e.get("map_name")
                        }
                        known_maps.update(region_entries.keys())
                        q_low = query.casefold()
                        for name in sorted(known_maps):
                            entry = region_entries.get(name, {})
                            label_txt = entry.get("label") or name
                            aliases_txt = " ".join(entry.get("aliases", ()))
                            if (q_low in name.casefold() or q_low in label_txt.casefold()
                                    or q_low in aliases_txt.casefold()):
                                disp = f"{label_txt} ({name})" if label_txt != name else name
                                rows.append((name, disp))
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
                button(actions, "Viajar", lambda: travel("travel"))
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

        world_map = tk.Frame(drawer_body, bg=bg, padx=10, pady=6)
        button(row(maps), "Mapa Mundi", lambda: self._show_hud_world_map(maps, world_map))
        map_controls = row(world_map)
        button(map_controls, "Voltar", lambda: self._show_hud_world_map(world_map, maps))
        region_bar = row(world_map)
        region_buttons = {}
        map_target = tk.StringVar(value="")
        map_notice = tk.StringVar(value="Clique em um mapa ou use Ctrl+Clique no Mapa-Múndi do jogo.")
        self._world_map_hud_target_var = map_target
        self._world_map_hud_notice_var = map_notice
        canvas = tk.Canvas(world_map, bg=surface, highlightthickness=0, width=406, height=195)
        canvas.pack(fill="both", expand=True)
        tk.Label(world_map, textvariable=map_notice, bg=bg, fg="#cbd5e1", font=("Segoe UI", 8),
                 anchor="w").pack(fill="x", pady=(2, 0))

        cand_row = row(world_map)
        label(cand_row, "Candidato:").pack(side="left")
        self._world_map_cand_var = tk.StringVar(value="—")
        cand_frame = tk.Frame(cand_row, bg=bg)
        cand_frame.pack(side="left", fill="x", expand=True)

        def refresh_candidates_ui():
            for w in cand_frame.winfo_children():
                w.destroy()
            cands = self._world_map_candidates[:5]
            if not cands:
                tk.Label(cand_frame, text="—", bg=bg, fg="#94a3b8", font=("Segoe UI", 8)).pack(side="left", padx=4)
                return
            for cand in cands:
                m_id = cand["map_name"]
                lbl = cand.get("label") or m_id
                short = lbl if len(lbl) <= 14 else m_id
                btn_bg = purple if map_target.get() == m_id else surface
                tk.Button(
                    cand_frame, text=short, bg=btn_bg, fg=fg, relief="flat", bd=0,
                    font=("Segoe UI", 7), padx=4, pady=1, cursor="hand2",
                    command=guard(lambda m=m_id, c=cand: (
                        map_target.set(m),
                        map_notice.set(f"{c.get('label', m)} [{c.get('region', '')}]"),
                        refresh_candidates_ui(),
                    ))
                ).pack(side="left", padx=1)

        self._refresh_world_map_candidates_ui = refresh_candidates_ui

        def draw_world_map(_event=None):
            canvas.delete("all")
            w = max(1, canvas.winfo_width())
            h = max(1, canvas.winfo_height())
            entries = self._get_world_map_region_entries(self._world_map_region)
            if not entries:
                canvas.create_text(w // 2, h // 2, text="Sem mapas nesta região", fill=fg, font=font)
                return
            layer_colors = {
                "city": ("#3b2f63", "#8b5cf6"),
                "field": ("#1f2937", "#3b82f6"),
                "factory_lab": ("#3b2430", "#f43f5e"),
                "dungeon": ("#2d223b", "#a855f7"),
                "submap": ("#1e3a3a", "#14b8a6"),
            }
            for entry in entries:
                rx = entry.get("rx", 0.5)
                ry = entry.get("ry", 0.5)
                rad = entry.get("radius", 0.05)
                x1, x2 = (rx - rad * 0.7) * w, (rx + rad * 0.7) * w
                y1, y2 = (ry - rad * 0.7) * h, (ry + rad * 0.7) * h
                fill_c, out_c = layer_colors.get(entry.get("layer", "field"), ("#2b2543", purple))
                canvas.create_oval(x1, y1, x2, y2, fill=fill_c, outline=out_c, width=1)
                canvas.create_text(rx * w, ry * h, fill=fg,
                                   text=entry.get("label") or entry["map_name"], font=("Segoe UI", 7))

        def set_region(reg_key):
            self._world_map_region = reg_key
            for k, btn in region_buttons.items():
                btn.configure(bg=purple if k == reg_key else surface)
            draw_world_map()

        for reg_key, reg_label in WORLD_MAP_REGION_LABELS.items():
            short_lbl = {"all": "Todas", "rune_midgarts": "Midgarts", "schwartzvalt": "Schwartz.",
                         "arunafeltz": "Arunaf.", "dungeons_submaps": "Dungeons"}.get(reg_key, reg_label)
            region_buttons[reg_key] = tk.Button(
                region_bar, text=short_lbl, bg=purple if reg_key == self._world_map_region else surface,
                fg=fg, activebackground=blue, activeforeground="#ffffff", relief="flat", bd=0,
                font=("Segoe UI", 8), padx=4, pady=1, cursor="hand2",
                command=guard(lambda k=reg_key: set_region(k))
            )
            region_buttons[reg_key].pack(side="left", padx=1)

        def select_map(event):
            w = max(1, canvas.winfo_width())
            h = max(1, canvas.winfo_height())
            rx = max(0.0, min(1.0, event.x / w))
            ry = max(0.0, min(1.0, event.y / h))
            cands = self._get_world_map_candidates(rx, ry, region_filter=self._world_map_region)
            if not cands:
                msg = f"Nenhum mapa mapeado em ({rx:.3f}, {ry:.3f})"
                print(f"[WorldMap] {msg}", flush=True)
                map_notice.set(msg)
                self._world_map_candidates = []
                refresh_candidates_ui()
                return
            self._world_map_candidates = cands
            best = cands[0]
            map_target.set(best["map_name"])
            extra = f" (+{len(cands) - 1} próximos)" if len(cands) > 1 else ""
            map_notice.set(f"{best.get('label', best['map_name'])} [{best.get('region', '')}]{extra}")
            refresh_candidates_ui()

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

        settings = add_drawer("⚙", create_button=True)
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

        traffic_log = getattr(self.feed, "traffic_log", None)
        self._hud_tcp_log_check = None
        if traffic_log is not None:
            self._hud_tcp_log_check = tk.BooleanVar(value=False)
            self._hud_tcp_log_status = tk.StringVar(value="Captura desligada")
            check(execution, "Registrar todo o tráfego TCP do jogo", self._hud_tcp_log_check,
                  lambda: traffic_log.set_enabled(self._hud_tcp_log_check.get()))
            tk.Label(execution, textvariable=self._hud_tcp_log_status, bg=bg, fg=fg,
                     font=font, anchor="w", justify="left", wraplength=378).pack(fill="x", padx=6)
            self._hud_tcp_log_last_status = None

        kafra = pages["Kafra"]
        auto_store = tk.BooleanVar(value=self.filters_config["auto_store_kafra"])

        def toggle_kafra():
            self._kafra_capacity_latched = False
            self._kafra_block = None
            self._save_hud_setting("auto_store_kafra", auto_store.get())

        check(kafra, "Auto-Guardar na Kafra por peso ou slots", auto_store, toggle_kafra)
        pin_debug = tk.BooleanVar(value=self.filters_config["kafra_pin_debug"])
        check(kafra, "Debug da janela de senha", pin_debug,
              lambda: self._set_kafra_pin_debug(pin_debug.get()))
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
        button(row(kafra), "Testar senha", self._queue_pin_probe)
        self._pin_test_status = tk.StringVar(value="")
        tk.Label(kafra, textvariable=self._pin_test_status, bg=bg, fg=fg, font=font,
                 anchor="w", wraplength=340).pack(fill="x")
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
        if title == "Mapas":
            self._resume_hud_automation()
        active = self._hud_active_drawer
        self._close_hud_drawer(restore_focus=title == active or title == "⚙", hide_window=title == active)
        if title == active:
            return
        self._hud_active_drawer = title
        self._hud_drawer_walk_since = None
        self._hud_drawer_frames[title].pack(fill="both", expand=True)
        self._hud_drawer_paused = title != "Mapas" and self._hud_pause_var.get()
        self._sync_hud_windows(self.current_viewport, True)
        self._refresh_hud_drawer(time.monotonic())

    def _close_hud_drawer(self, restore_focus=True, *, hide_window=True):
        if self._hud_active_drawer is None:
            return
        had_focus = restore_focus and self._is_hud_foreground()
        self._finish_hud_edit()
        self._dismiss_hud_choice()
        for child in self._hud_drawer_body.pack_slaves():
            child.pack_forget()
        if hide_window:
            self._hide_hud_window(self._hud_drawer)
        self._cancel_hud_binding()
        self._hud_active_drawer = None
        self._hud_drawer_paused = False
        self._hud_drawer_walk_since = None
        if had_focus:
            self._focus_hud_game()

    def _toggle_hud_submenu(self, title):
        active = self._hud_active_submenu
        self._close_hud_submenu(hide_window=title == active)
        if title == active:
            return
        self._hud_active_submenu = title
        self._hud_submenu_pos = None
        for child in self._hud_submenu_body.pack_slaves():
            child.pack_forget()
        self._hud_submenu_frames[title].pack(fill="both", expand=True)
        self._hud_submenu.update_idletasks()
        self._sync_hud_windows(self.current_viewport, True)

    def _close_hud_submenu(self, *, hide_window=True):
        if self._hud_active_submenu is None:
            return
        for child in self._hud_submenu_body.pack_slaves():
            child.pack_forget()
        if hide_window and self._hud_submenu is not None:
            self._hide_hud_window(self._hud_submenu)
        self._hud_active_submenu = None
        self._hud_submenu_pos = None

    def _clamp_hud_pos(self, x, y, width, height):
        max_x = max(0, (self._screen_w or 1920) - max(40, width))
        max_y = max(0, (self._screen_h or 1080) - max(30, height))
        return max(0, min(int(x), max_x)), max(0, min(int(y), max_y))

    def _begin_hud_drag(self, event, kind):
        self._mark_hud_control_click()
        window_map = {"panel": self._hud_panel, "drawer": self._hud_drawer, "submenu": self._hud_submenu}
        win = window_map.get(kind)
        if not win:
            return
        layout = getattr(win, "_hud_layout", None)
        if layout:
            ox, oy = layout[0], layout[1]
        else:
            ox, oy = win.winfo_rootx(), win.winfo_rooty()
        self._hud_dragging = True
        self._hud_drag_state = (kind, event.x_root, event.y_root, ox, oy)

    def _drag_hud(self, event, kind):
        state = self._hud_drag_state
        if not state or state[0] != kind:
            return
        _, sx0, sy0, ox, oy = state
        nx, ny = ox + (event.x_root - sx0), oy + (event.y_root - sy0)
        window_map = {"panel": self._hud_panel, "drawer": self._hud_drawer, "submenu": self._hud_submenu}
        win = window_map.get(kind)
        if not win:
            return
        layout = getattr(win, "_hud_layout", None)
        w = layout[2] if layout else max(120, win.winfo_width())
        h = layout[3] if layout else max(30, win.winfo_height())
        cx, cy = self._clamp_hud_pos(nx, ny, w, h)
        if kind == "panel":
            self._hud_pos = (cx, cy)
            self._hud_submenu_pos = None
        elif kind == "drawer":
            self._hud_drawer_pos = (cx, cy)
        elif kind == "submenu":
            self._hud_submenu_pos = (cx, cy)
        self._mark_hud_control_click()
        self._sync_hud_windows(self.current_viewport, True)

    def _end_hud_drag(self, _event, kind):
        if self._hud_dragging and kind == "panel" and self._hud_pos and None not in self._hud_pos:
            self.filters_config["hud_x"] = int(self._hud_pos[0])
            self.filters_config["hud_y"] = int(self._hud_pos[1])
            save_filters_config(self.filters_config)
        self._hud_dragging = False
        self._hud_drag_state = None
        self._mark_hud_control_click()
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

    def _set_kafra_pin_debug(self, enabled):
        self._save_hud_setting("kafra_pin_debug", enabled)
        self._pin_debug_scan = None
        self._pin_debug_logged = None
        self._pin_debug_last_complete = None
        self._pin_probe_status = None
        self._pin_overlay_check_at = 0.0
        if self._kafra_cycle is not None:
            self._kafra_cycle.pop("pin_debug_aim", None)

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
            self._resume_hud_automation()
        else:
            self._set_hud_running(False)

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
        pin_cycle = getattr(self, "_pin_click_probe", None) or self._kafra_cycle
        pin_seq = (pin_cycle.get("pin_seq") or pin_cycle.get("pin_rescan")) if pin_cycle else None
        if pin_seq and left_button and self._is_game_focused(fg):
            pending = pin_seq.get("pending")
            press = pending[0] if pending else pin_seq.get("automatic_press")
            bot_press = press.get("bot_press") if press else None
            if bot_press is None or not (0 <= clicked_at - bot_press[0] <= 0.15
                                         and math.dist((sx, sy), bot_press[1]) <= 3):
                pin_seq["physical_click_at"] = clicked_at
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
        for hud in (self._hud_panel, self._hud_drawer, self._hud_submenu):
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
                    self._close_hud_submenu()
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
            if self._hud_active_submenu:
                self._close_hud_submenu()
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

    def _hide_overlapping_hud_windows(self, rect) -> bool:
        r_left, r_top, r_right, r_bottom = rect
        hid_any = False
        for hud in (self._hud_panel, self._hud_submenu, self._hud_drawer):
            try:
                if hud and hud.winfo_viewable():
                    hx, hy = hud.winfo_rootx(), hud.winfo_rooty()
                    hw, hh = hud.winfo_width(), hud.winfo_height()
                    if hx < r_right and hx + hw > r_left and hy < r_bottom and hy + hh > r_top:
                        self._hide_hud_window(hud)
                        hid_any = True
            except tk.TclError:
                pass
        if hid_any:
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass
        return hid_any

    def _sync_hud_windows(self, viewport, foreground, game_hwnd=None):
        if not self._hud_panel:
            return
        if self._hud_tcp_log_check is not None:
            traffic_log = self.feed.traffic_log
            status = traffic_log.enabled, traffic_log.path, traffic_log.error, traffic_log.dropped
            if status != self._hud_tcp_log_last_status:
                self._hud_tcp_log_last_status = status
                enabled, path, error, dropped = status
                self._hud_tcp_log_check.set(enabled)
                if error:
                    message = "Falha ao gravar o log TCP. Consulte o log da sessão."
                elif path:
                    message = f"{'Gravando' if enabled else 'Captura desligada'} · logs/{path.name}"
                else:
                    message = "Captura desligada"
                if dropped:
                    message += f" · {dropped} registros perdidos"
                self._hud_tcp_log_status.set(message)
        self._update_hud_play()
        anchor = game_hwnd or self._hud_render_anchor
        panel_w, panel_h = self._hud_dock_width, 30
        if viewport:
            left, top, vw, _ = viewport
            if self._hud_pos and self._hud_pos[0] is not None and self._hud_pos[1] is not None:
                px, py = self._clamp_hud_pos(self._hud_pos[0], self._hud_pos[1], panel_w, panel_h)
            else:
                px, py = self._clamp_hud_pos(left + (vw - panel_w) // 2, top, panel_w, panel_h)
        else:
            px, py = 0, 0

        sub_w = max(165, self._hud_submenu.winfo_reqwidth() if self._hud_submenu else 165)
        sub_h = max(60, self._hud_submenu.winfo_reqheight() if self._hud_submenu else 90)
        if self._hud_submenu_pos is not None:
            sx, sy = self._clamp_hud_pos(*self._hud_submenu_pos, sub_w, sub_h)
        else:
            btn = self._hud_submenu_buttons.get(self._hud_active_submenu)
            if btn is not None and self._hud_panel.winfo_ismapped():
                bx = px + max(0, btn.winfo_x())
            else:
                bx = px + (panel_w - sub_w) // 2
            sx, sy = self._clamp_hud_pos(bx, py + panel_h + 2, sub_w, sub_h)

        drw_w, drw_h = 430, 370
        if self._hud_drawer_pos is not None:
            dx, dy = self._clamp_hud_pos(*self._hud_drawer_pos, drw_w, drw_h)
        else:
            dx, dy = self._clamp_hud_pos(px + (panel_w - drw_w) // 2, py + panel_h + 2, drw_w, drw_h)

        windows = (
            (self._hud_panel, self._hud_panel_visible, panel_w, panel_h, px, py),
            (self._hud_submenu, self._hud_panel_visible and self._hud_active_submenu, sub_w, sub_h, sx, sy),
            (self._hud_drawer, self._hud_panel_visible and self._hud_active_drawer, drw_w, drw_h, dx, dy),
        )
        pin_rect = getattr(self, "_pin_dialog_screen_rect", None)
        pin_suppressed = pin_rect is not None and time.monotonic() < getattr(self, "_pin_hud_suppress_until", 0.0)
        if not pin_suppressed:
            self._pin_dialog_screen_rect = None
        check_topmost = time.monotonic() >= getattr(self, "_topmost_check_at", 0.0)
        if check_topmost:
            self._topmost_check_at = time.monotonic() + 0.4
        for window, visible, width, height, x, y in windows:
            if not window:
                continue
            if pin_suppressed and visible:
                r_left, r_top, r_right, r_bottom = pin_rect
                if x < r_right and x + width > r_left and y < r_bottom and y + height > r_top:
                    visible = False
            if not (foreground and viewport and visible):
                self._hide_hud_window(window)
                continue
            layout = (x, y, width, height, True)
            previous = getattr(window, "_hud_layout", None)
            hwnd = getattr(window, "_hud_hwnd", None)
            restack = (previous is None or not previous[4] or getattr(window, "_hud_anchor", None) != anchor
                       or getattr(window, "_hud_restack", False)
                       or (check_topmost and hwnd and not u32.GetWindowLongW(hwnd, -20) & 0x0008))  # WS_EX_TOPMOST
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
        self._close_hud_submenu()
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
        if not self.auto_loot and (self.travel_plan or self._city_travel_state or self._kafra_cycle):
            self._click_debug("[Auto-Loot] Bloqueado durante viagem ou fluxo da Kafra.")
            return
        self.auto_loot = not self.auto_loot
        if self.auto_loot:
            self._resume_hud_automation()
        if not self.auto_loot and not self.auto_farm:
            self._locked_drop_gid = None
        print(f"[Auto-Loot] {'ON' if self.auto_loot else 'OFF'}", flush=True)

    def _stop_hud_actions(self):
        self._set_hud_running(False)
        self._pending_hud_interaction = None
        self._pin_click_probe = None
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
            return False
        if self._kafra_cycle:
            print("[Viagem] O ciclo do armazém está em andamento.", flush=True)
            return False
        self._close_hud_drawer()
        if self.start_travel_plan(target, mode=mode):
            self._resume_hud_automation()
            self._focus_hud_game()
            return True
        return False

    def _resume_hud_automation(self):
        now = time.monotonic()
        self._hud_click_grace = (now - 0.3, now + 0.8)
        self._hud_manual_clicks.clear()
        self._set_hud_running(True)
        print("[Automação] Retomada", flush=True)

    def _toggle_minimap_nav(self):
        self.minimap_nav = not self.minimap_nav
        if self.minimap_nav:
            self._resume_hud_automation()
        else:
            self._minimap_path_request = None

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
        self._resume_hud_automation()
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
        pos = stats.get("pos") or (player.cell() if player else None)
        coords = f"({int(pos[0])}, {int(pos[1])})" if pos and None not in pos else "(—, —)"
        weight = stats.get("weight_ratio", getattr(snapshot, "weight_ratio", 0.0))
        slots = stats.get("estimated_slots_used", 0)
        if self._world_map_notice and now < self._world_map_notice[1]:
            status = f"[WorldMap] {self._world_map_notice[0]}"
        elif self._world_map_notice and now >= self._world_map_notice[1]:
            self._world_map_notice = None
            status = self._travel_caption(self.travel_plan) if self.travel_plan else (
                f"{map_name} {coords}  |  Peso {weight:.0%}  |  Slots: {slots}/100  |  {self._hud_elapsed()}"
            )
        else:
            status = self._travel_caption(self.travel_plan) if self.travel_plan else (
                f"{map_name} {coords}  |  Peso {weight:.0%}  |  Slots: {slots}/100  |  {self._hud_elapsed()}"
            )
        self._hud_status.set(status)
        for attr, button in self._hud_btns.items():
            button.configure(bg="#3b82f6" if getattr(self, attr) else "#222638")
        for title, button in self._hud_drawer_buttons.items():
            button.configure(bg="#8b5cf6" if title == self._hud_active_drawer else "#222638")
        for title, button in self._hud_submenu_buttons.items():
            button.configure(bg="#8b5cf6" if title == self._hud_active_submenu else "#222638")
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
            pos = world.player.cell() if world.player else None
            if pos and None in pos:
                pos = None
            current_map = world.map
            entities = [(kind, aid, ent.catalog_id, ent.track.target,
                         world.npc_name(aid, ent.catalog_id, ent.track.target, current_map) if kind == "npc" else None)
                        for (kind, aid), ent in world.entity_items() if kind in ("mob", "npc", "warp")]
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
        if self._is_pin_entry_active():
            return
        (kind, aid), _, selected_map, _ = selection
        world = getattr(self.feed, "world", None)
        if world is None:
            return
        with world.lock:
            target = world.entity(kind, aid)
            if (target is None or world.map != selected_map or not world.player
                    or world.npc_dialog.get("open")):
                return
            tx, ty = map(round, target.track.target)
        self._clear_patrol_route()
        self.manual_nav_goal = None
        self.patrol_target_route = (selected_map, tx, ty, aid)
        self.active_nav_color = "#A78BFA"
        self._focus_hud_game()

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

            world = self.feed.world
            with world.lock:
                dialog = world._npc_dialog_snapshot()
            state = self._city_travel_state
            if "kafra_location" not in state:
                locations = self._kafra_locations_for_map(curr_map)
                selected = state.get("kafra_pos")
                if selected is not None:
                    locations = tuple(loc for loc in locations if (loc["x"], loc["y"]) == tuple(selected))
                if not locations:
                    self._click_debug(f"[Kafra] Coordenada cadastrada indisponível em {curr_map}.")
                    self._city_travel_state = None
                    self.nav_goal = self.patrol_target_route = None
                    return
                state["kafra_location"] = min(locations, key=lambda loc: math.dist((px, py), (loc["x"], loc["y"])))
                state["kafra_leg"] = {"blocked": {}, "deadline": now + 120}
            location = state["kafra_location"]
            leg = state["kafra_leg"]
            if pending := leg.get("menu_action"):
                if not pending.done():
                    return
                leg.pop("menu_action")
                if (sent_at := pending.result()) is None:
                    self._city_travel_state = None
                    return
                leg["dialog_wait_until"] = sent_at + 1.5
                leg["next_dialog_at"] = sent_at + 4.5
                leg["menu_select_cooldown_until"] = max(leg.get("menu_select_cooldown_until", 0.0), sent_at + 1.5)
            if not self._kafra_dialog_ready(leg, dialog, world, leg, now):
                return
            matches = self._dialog_matches_target_kafra(world, dialog, leg, curr_map, location)
            new_dialog = dialog.get("seq") != leg.get("handled_seq")
            allowed = self._dialog_action_guard(dialog, lambda: self._city_travel_state is state)
            if matches and new_dialog and dialog["dialog_has_close"]:
                self._send_kafra_next(leg, dialog, allowed)
                return
            if not dialog.get("open"):
                status = self._approach_registered_kafra(snapshot, world, leg, location, (px, py), now, target_hwnd)
                if status in ("movement_unconfirmed", "no_clickable_step", "no_gat_path") and now < leg["deadline"]:
                    retries = leg.get("walk_retries", 0) + 1
                    if retries <= 3:
                        state["kafra_leg"] = {"blocked": {}, "deadline": leg["deadline"], "walk_retries": retries}
                        self.active_nav_path = None
                        self._clear_locked_nav_path()
                        self._click_debug(
                            f"[Kafra] {self._travel_walk_error(status, (location['x'], location['y']))} "
                            f"Recalculando a partir de {curr_map} em {(round(px), round(py))}."
                        )
                        return
                failure = self._travel_walk_error(status, (location["x"], location["y"]))
                if status == "missing_gat":
                    failure = f"Terreno .gat de {curr_map} indisponível."
                if now >= leg["deadline"] or failure:
                    self._click_debug(f"[Kafra] {failure or 'Tempo limite para abrir o diálogo.'}")
                    self._city_travel_state = None
                return
            if not matches:
                if self._dismiss_settings_window(now, force_check=True):
                    return
                if now >= leg.get("next_dialog_at", 0.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 2.0
                return
            if not new_dialog:
                if self._dismiss_settings_window(now, force_check=True):
                    return
                if now >= leg.get("next_dialog_at", now + 1.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 2.0
                return
            if options := self._kafra_menu_options(leg, dialog):
                selecting_service = not leg.get("teleport_selected")
                raw = dialog["dialog_menu_raw_options"]
                if selecting_service:
                    normalized = [_normalize_menu_text(option) for option in options]
                    index = self._kafra_keyword_index(normalized, ("teleporte", "teleport", "warp", "teletransporte"),
                                                      raw=raw, token="0ma0AQ", skip_cancel=True)
                    if index is None:
                        index = self._kafra_fallback_menu_index(options, raw=raw, action="teleport", submenu=False)
                else:
                    index = _kafra_destination_index(options, raw, target_city, entry)
                    if index is None:
                        for bridge_city in ("aldebaran", "geffen", "izlude", "prontera", "yuno",
                                            "rachel", "morocc", "payon", "alberta"):
                            index = _kafra_destination_index(options, raw, bridge_city, entry)
                            if index is not None:
                                break
                if index is not None:
                    if self._try_select_kafra_menu(leg, dialog, options, index, allowed, now) and selecting_service:
                        leg["teleport_selected"] = True
                else:
                    normalized = [_normalize_menu_text(option) for option in options]
                    index = self._kafra_keyword_index(normalized, ("cancelar", "cancel", "sair", "fechar"),
                                                      raw=raw, token="1Wa0AQ")
                    if index is None:
                        self._send_game_key(0x1B)
                    else:
                        self._try_select_kafra_menu(leg, dialog, options, index, allowed, now)
            elif dialog["dialog_has_next"]:
                self._send_kafra_next(leg, dialog, allowed)

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
        with self._game_key_ready:
            self._game_key_closed = True
            self._game_key_ready.notify()
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

    def _is_priority_drop(self, item_id):
        return self.classify_item_tier(item_id, self.names.get("drop", {}).get(item_id, "")) == 1

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

    @staticmethod
    def _kafra_locations_for_map(current_map, network=None):
        if not current_map:
            return ()
        m = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        _, entry = get_kafra_network_map_entry(m, network)
        if entry and entry.get("locations"):
            return tuple(entry["locations"])
        base = re.sub(r"_[a-z0-9]+$", "", m)
        return tuple({"x": x, "y": y} for x, y in
                     (KAFRA_LOCATIONS.get(m) or KAFRA_LOCATIONS.get(base, ())))

    @staticmethod
    def _registered_kafra(world, map_id, location):
        target = location["x"], location["y"]
        names = {normalize_dialog_option(location.get(key)) for key in ("name", "npc_name", "label")} - {""}
        ids = {str(location[key]) for key in ("id", "npc_id", "aid", "gid") if location.get(key) is not None}
        jobs = {112, 113, 114, 115, 116, 117, 860, 861}
        job = location.get("job", location.get("job_id"))
        explicit_identity = bool(ids or job is not None or location.get("name") or location.get("npc_name"))
        ranked = []
        for (kind, gid), ent in world.entity_items():
            if kind != "npc" or (distance := math.dist(ent.track.target, target)) > 2:
                continue
            name = normalize_dialog_option(world.npc_name(gid, ent.catalog_id, ent.track.target, map_id))
            listed = str(gid) in ids or str(ent.gid) in ids or name in names or str(ent.catalog_id) == str(job)
            event = any(term in name for term in ("evento", "cool event", "zonda", "orelhuda"))
            if not event and (listed or (not explicit_identity and ent.catalog_id in jobs)):
                ranked.append((not listed, distance, gid))
        best = min(ranked, default=None)
        return best[2] if best else None

    def _kafra_dialog_matches(self, world, dialog, leg):
        if (not leg.get("npc_click_at") or dialog.get("last_update", 0.0) < leg["npc_click_at"]
                or dialog.get("seq") == leg.get("npc_click_seq")):
            return False
        raw_gid = dialog.get("npc_gid")
        with world.lock:
            gid = world.aid_to_gid.get(raw_gid, raw_gid)
            return bool(raw_gid and (gid == leg.get("npc_gid") or raw_gid == leg.get("npc_raw_gid")))

    def _dialog_matches_target_kafra(self, world, dialog, leg, map_id=None, location=None):
        """Identifica diálogos abertos da Kafra-alvo (incluindo fala inicial antes do menu 0x00B7)."""
        if self._kafra_dialog_matches(world, dialog, leg):
            return True
        if not dialog.get("open") or dialog.get("opcode") not in (0x00B4, 0x00B5, 0x00B7):
            return False
        raw_gid = dialog.get("npc_gid")
        if not raw_gid:
            return False
        with world.lock:
            gid = world.aid_to_gid.get(raw_gid, raw_gid)
            target_gid = leg.get("npc_gid")
            target_raw = leg.get("npc_raw_gid")
            if target_gid is None and map_id and location is not None:
                target_gid = self._registered_kafra(world, map_id, location)
                npc = world.entity("npc", target_gid) if target_gid else None
                target_raw = npc.gid if npc and npc.gid else None
            return bool((target_gid and gid == target_gid) or (target_raw and raw_gid == target_raw))

    def _dialog_action_guard(self, dialog, active):
        world = self.feed.world
        map_id = world.map
        stamp = dialog.get("last_update"), dialog.get("seq"), dialog.get("opcode")

        def allowed():
            if not active() or not self._hud_can_run():
                return False
            with world.lock:
                live = world.npc_dialog
                return (world.map == map_id and (live.get("open") or live.get("opcode") == 0x00B6)
                        and (live.get("last_update"), live.get("seq"), live.get("opcode")) == stamp)

        return allowed

    @staticmethod
    def _kafra_menu_options(state, dialog):
        if (dialog.get("opcode") == 0x00B7
                and dialog["dialog_menu_seq"] != state.get("handled_menu_seq")
                and (dialog["dialog_menu_options"] or dialog["dialog_menu_raw_options"])):
            return dialog["dialog_menu_options"]
        return []

    @staticmethod
    def _kafra_keyword_index(normalized, keywords, *, raw=(), token=None, skip_cancel=False):
        for idx, option in enumerate(normalized):
            raw_tok = raw[idx] if idx < len(raw) else ""
            if skip_cancel and ("cancelar" in option or "desistir" in option or raw_tok == "1Wa0AQ"):
                continue
            if any(keyword in option for keyword in keywords) or (token and raw_tok == token):
                return idx
        return None

    @staticmethod
    def _is_encoded_kafra_menu(options, raw=()):
        """Retorna True quando todas as opções do menu 0x00B7 são tokens codificados não mapeados."""
        items = [((raw[i] if i < len(raw) else opt) or "").strip() for i, opt in enumerate(options)]
        if len(items) < 2:
            return False
        return all(
            re.fullmatch(r"[A-Za-z0-9+/]{6}", tok) and (i < len(options) and options[i].strip() == tok)
            for i, tok in enumerate(items)
        )

    @classmethod
    def _kafra_fallback_menu_index(cls, options, raw=(), *, action="storage", preferred_slot=3, submenu=False):
        """Fallback posicional para menus 0x00B7 legíveis ou em tokens codificados sem mapeamento."""
        if not options:
            return None
        normalized = [_normalize_menu_text(opt) for opt in options]
        if action == "storage":
            for idx, opt in enumerate(normalized):
                if any(term in opt for term in ("armazem", "storage", "deposito")):
                    return idx
        elif action == "teleport":
            for idx, opt in enumerate(normalized):
                if any(term in opt for term in ("teleporte", "teleport", "warp", "teletransporte")):
                    return idx
        if not cls._is_encoded_kafra_menu(options, raw):
            return None
        if not submenu and len(options) >= 5:
            if action == "storage":
                return 1
            if action == "teleport":
                return 2
        if submenu and action == "storage" and 2 <= len(options) <= 4:
            slot = max(1, min(int(preferred_slot or 3), len(options) - 1))
            return slot - 1
        return None

    @staticmethod
    def _kafra_storage_slot_index(options, raw=(), preferred=3):
        """Identifica o submenu de Armazéns (1..3) e retorna o índice do slot preferido ou o último disponível."""
        slot_tokens = {1: "iWe0AQ", 2: "ime0AQ", 3: "i2e0AQ"}
        normalized = [_normalize_menu_text(opt) for opt in options]
        storage_indices = []
        for idx, opt in enumerate(normalized):
            raw_tok = raw[idx] if idx < len(raw) else ""
            if "cancelar" in opt or raw_tok == "1Wa0AQ":
                continue
            if (raw_tok in slot_tokens.values()
                    or re.search(r"\b(?:armazem|storage)\s*[123]\b", opt)):
                storage_indices.append(idx)
        if not storage_indices:
            return None
        preferred = preferred if preferred in slot_tokens else 3
        target_token = slot_tokens[preferred]
        for idx in storage_indices:
            raw_tok = raw[idx] if idx < len(raw) else ""
            if raw_tok == target_token or re.search(rf"\b(?:armazem|storage)\s*{preferred}\b", normalized[idx]):
                return idx
        return storage_indices[-1]

    def _dismiss_unwanted_dialog(self, cycle, dialog, now):
        """Fecha diálogo de NPC indesejado sem avançar menus e retoma a Kafra alvo."""
        if self._dismiss_settings_window(now, force_check=True):
            return
        cycle["wrong_npc_dialog"] = True
        cycle["wrong_npc_gid"] = dialog.get("npc_gid")
        opcode = dialog.get("opcode")
        seq = dialog.get("seq")
        if opcode == 0x00B7:
            if (now >= cycle.get("close_dialog_at", 0.0)
                    and now >= getattr(self, "_settings_close_cooldown_until", 0.0)
                    and self._send_game_key(0x1B)):
                cycle["close_dialog_at"] = now + 0.55
                print(f"[Kafra] Cancelando menu indesejado (NPC={dialog.get('npc_gid')}) com Esc.", flush=True)
            return
        if dialog.get("dialog_has_close"):
            if seq != cycle.get("dismiss_enter_seq"):
                if self._send_game_key(0x0D):
                    cycle["dismiss_enter_seq"] = seq
                    cycle["close_dialog_at"] = now + 0.45
                    print(f"[Kafra] Fechando diálogo indesejado (NPC={dialog.get('npc_gid')}) com Enter.", flush=True)
                return
        elif (now >= cycle.get("close_dialog_at", 0.0)
                and now >= getattr(self, "_settings_close_cooldown_until", 0.0)
                and self._send_game_key(0x1B)):
            cycle["close_dialog_at"] = now + 0.55
            print(f"[Kafra] Fechando diálogo indesejado (NPC={dialog.get('npc_gid')}) com Esc.", flush=True)
            return
        if seq != cycle.get("dismiss_enter_seq") and now >= cycle.get("close_dialog_at", 0.0):
            if self._send_game_key(0x0D):
                cycle["dismiss_enter_seq"] = seq
                cycle["close_dialog_at"] = now + 0.45
                print(f"[Kafra] Avançando diálogo indesejado (NPC={dialog.get('npc_gid')}) para fechar.", flush=True)

    def _kafra_dialog_ready(self, state, dialog, world, leg, now):
        if previous := state.get("next_key"):
            if not previous.done():
                return False
            state.pop("next_key")
            if (sent_at := previous.result()) is None:
                state.pop("handled_seq", None)
            else:
                state["dialog_wait_until"] = sent_at + 1.5
                state["next_dialog_at"] = sent_at + 4.5

        if leg.get("awaiting_npc_click"):
            if self._automation_click_active():
                return False
            click_at = self._touch_click_finished_at(leg["npc_click_point"], leg["npc_click_at"])
            state["dialog_wait_until"] = click_at + 1.5
            leg.pop("awaiting_npc_click")

        if deadline := state.get("dialog_wait_until"):
            if now < deadline:
                new_response = (dialog.get("seq") != state.get("handled_seq")
                                and (dialog["dialog_has_next"] or dialog["dialog_has_close"]
                                     or self._kafra_menu_options(state, dialog))
                                and self._kafra_dialog_matches(world, dialog, leg))
                if not new_response:
                    return False
            state.pop("dialog_wait_until")
        return True

    def _select_kafra_menu(self, state, dialog, options, index, allowed):
        opt = options[index] if 0 <= index < len(options) else ""
        print(f"[Kafra] Selecionando índice {index} ('{opt}') via OSK", flush=True)
        action = self._send_menu_selection(index, allowed=allowed)
        if action is None:
            return False
        state.update(menu_action=action, handled_seq=dialog.get("seq"),
                     handled_menu_seq=dialog["dialog_menu_seq"])
        return True

    def _try_select_kafra_menu(self, state, dialog, options, index, allowed, now, cooldown=1.5):
        if self._menu_selection_active:
            return False
        menu_key = (dialog.get("dialog_menu_seq"), index)
        if state.get("menu_select_key") == menu_key and now < state.get("menu_select_cooldown_until", 0.0):
            return False
        state["menu_select_key"] = menu_key
        state["menu_select_cooldown_until"] = now + cooldown
        return self._select_kafra_menu(state, dialog, options, index, allowed)

    def _send_kafra_next(self, state, dialog, allowed):
        completion = Future()
        if not self._send_game_key(0x0D, completion=completion, allowed=allowed):
            return False
        state.update(next_key=completion, handled_seq=dialog.get("seq"))
        action = "Fechando" if dialog["dialog_has_close"] else "Avançando"
        print(f"[Kafra] {action} diálogo com Enter.", flush=True)
        return True

    def _kafra_safe_click_point(self, snapshot, project, resolve, npc, gid):
        """Escolhe o ponto de tela mais seguro da Kafra alvo, evitando NPCs adjacentes."""
        left, top, width, height = snapshot.viewport
        target_cell = tuple(map(round, npc.track.target))
        cell = npc.track.cell()

        other_centers = []
        other_boxes = []
        for item in snapshot.objects:
            if item.kind == "npc" and item.key != f"npc:{gid}":
                ox, oy = left + item.u * width, top + item.v * height
                other_centers.append((ox, oy))
                other_boxes.append((ox, oy))
        for (kind, other_gid), ent in self._navigation_entities(snapshot.map):
            if kind == "npc" and other_gid != gid:
                oc = ent.track.cell()
                fu, fv = project(*oc, 0.0)
                bu, bv = project(*oc, 4.2)
                other_boxes.append((left + fu * width, top + fv * height))
                other_centers.append((left + bu * width, top + bv * height))

        item = next((obj for obj in snapshot.objects if obj.key == f"npc:{gid}"), None)
        body_u, body_v = project(*cell, 4.2)
        body_pt = (left + body_u * width, top + body_v * height)
        uv_candidates = []
        if item is not None:
            uv_candidates.append((item.u, item.v))
        for z in (4.2, 5.0, 3.4, 5.5, 2.5):
            uv_candidates.append(project(*cell, z))
        for dx, dy in ((-8, 0), (8, 0), (0, -8), (0, 8), (-12, -6), (12, -6)):
            uv_candidates.append((body_u + dx / width, body_v + dy / height))

        best = None
        for cu, cv in uv_candidates:
            pt = resolve(target_cell, (cu, cv))
            if pt is None:
                continue
            cx, cy = pt
            if any(math.dist(pt, oc) < 44 for oc in other_centers):
                continue
            if any(abs(cx - gx) <= 44 and (gy - 105) <= cy <= (gy + 28) for gx, gy in other_boxes):
                continue
            min_other = min((math.dist(pt, oc) for oc in other_centers), default=999.0)
            body_dist = math.dist(pt, body_pt)
            score = (min(min_other, 160.0), -body_dist)
            if best is None or score > best[0]:
                best = (score, pt)
        return best[1] if best is not None else None

    def _approach_registered_kafra(self, snapshot, world, leg, location, position, now, hwnd):
        if self._is_pin_entry_active():
            return "waiting"
        target = location["x"], location["y"]
        with world.lock:
            gid = self._registered_kafra(world, snapshot.map, location)
            npc = world.entity("npc", gid)
            raw_gid = npc.gid if npc and npc.gid else None
            seq = world.npc_dialog.get("seq")
            if npc is not None:
                target = tuple(map(round, npc.track.target))
        status = self._walk_travel_leg(snapshot, world, leg, position, target, npc_gid=gid)
        if status != "approached":
            return status
        if now < getattr(self, "_map_stabilize_until", 0.0):
            return "waiting"
        self.active_nav_path = None
        if npc is None:
            self._click_debug(f"Kafra cadastrada em {target} ainda não encontrada no feed.")
            return "waiting"
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        project = self.feed.camera.projector(snapshot.viewport, self.feed._cam or position,
                                             gat.get_elevation if gat else flat_height)
        resolve = self._safe_click_resolver(snapshot, project, allowed_npc=gid)

        if "npc_click_at" in leg:
            click_at = self._touch_click_finished_at(leg["npc_click_point"], leg["npc_click_at"])
            if self._automation_click_active() or now - click_at < 1.5:
                return "waiting"
            if leg.get("npc_click_attempts", 0) >= 3:
                leg.pop("npc_click_at")
                leg["npc_click_attempts"] = 0
                leg["reposition_npc_from"] = tuple(map(round, position))
                self._click_debug(f"[Kafra] NPC {gid} sem resposta após três cliques. Reposicionando.")
                return "waiting"
            leg.pop("npc_click_at")

        target_cell = tuple(map(round, npc.track.target))
        point = self._kafra_safe_click_point(snapshot, project, resolve, npc, gid)
        if point is None:
            leg["reposition_npc_from"] = tuple(map(round, position))
            self._click_debug(f"[Kafra] Sem ponto seguro no sprite do NPC {gid}. Reposicionando.")
            return "waiting"

        def aim(_lead):
            if self._is_pin_entry_active():
                return None
            with world.lock:
                if (world.map == snapshot.map and self.current_viewport == snapshot.viewport
                        and not world.npc_dialog.get("open")
                        and self._registered_kafra(world, snapshot.map, location) == gid):
                    return point
            return None

        if self._navigation_click(snapshot, gat, target_cell, point, hwnd=hwnd, target_point=aim, npc=gid,
                                  ground=False, kafra=True, retry_key=(snapshot.map, "kafra", gid),
                                  hover_reset=leg.get("npc_click_attempts", 0) == 1):
            leg.update(npc_gid=gid, npc_raw_gid=raw_gid, npc_click_at=now, npc_click_seq=seq,
                       npc_click_point=point, npc_click_cell=tuple(map(round, position)),
                       awaiting_npc_click=True,
                       npc_click_attempts=leg.get("npc_click_attempts", 0) + 1)
            print(f"[Kafra] Interação com NPC {gid} em {npc.track.target} ({snapshot.map}).", flush=True)
            return "clicked"
        return "waiting"

    def _start_kafra_storage(self, world, player, current_map, now, manual):
        cfg = self.filters_config
        if not manual and not cfg["auto_store_kafra"]:
            return
        weight_pct = 100 * player.weight_ratio if player else 0
        with world.lock:
            slots_limit = world.estimated_slots_used >= cfg["kafra_trigger_slots"]
            pickup_blocked = world.inventory_pickup_blocked
            zeny = world.player_zeny if world.zeny_known else None
            busy = world.npc_dialog.get("open") or world.storage_open
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
        locations = self._kafra_locations_for_map(city)
        if not locations:
            print(f"[Kafra] Coordenada cadastrada indisponível em {city}.", flush=True)
            return
        location = (min(locations, key=lambda loc: math.dist(position, (loc["x"], loc["y"])))
                    if city == current_map else locations[0])
        target = location["x"], location["y"]

        self._kafra_cycle = {
            "phase": "travel" if city != current_map else "approach",
            "city": city, "position": target, "location": location, "approach_leg": {"blocked": {}, "deadline": now + 180},
            "farm_map": (self.farm_map or current_map).lower().removesuffix(".gat").removesuffix(".rsw"),
            "modes": (self.auto_farm, self.auto_loot, self.auto_patrol),
            "play": self._automation_running, "deadline": now + 180, "next": now,
            "vision": vision, "tabs": [2, 0, 1], "tab": None, "hover": None,
            "pending": None,
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
        print(f"[Kafra] Ciclo iniciado rumo à Kafra cadastrada em {city} {target}.", flush=True)

    def _finish_kafra_storage(self, message, current_map):
        cycle = self._kafra_cycle
        self._kafra_cycle = None
        self._pin_dialog_screen_rect = None
        self._pin_hud_suppress_until = 0.0
        self._pin_overlay_visible_cache = False
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
        if now >= cycle["deadline"] and not (phase == "opening" and "pin_seen_at" in cycle):
            if phase in ("dialog", "closing"):
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
                    locations = self._kafra_locations_for_map(current_map)
                    if locations:
                        nearest = min(locations, key=lambda loc: math.dist((px, py), (loc["x"], loc["y"])))
                        cycle["location"] = nearest
                        cycle["position"] = (nearest["x"], nearest["y"])
                    cycle.update(phase="approach", deadline=now + 60,
                                 approach_leg={"blocked": {}, "deadline": now + 60})
                    print(f"[Kafra] Chegada em {current_map}. Caminhando até {cycle['position']}.", flush=True)
            elif not self.travel_plan:
                self._finish_kafra_storage(f"rota interrompida antes de chegar a {destination}.", current_map)
            return
        if current_map != destination:
            self._finish_kafra_storage("mapa mudou durante a interação.", current_map)
            return
        hwnd = self.get_game_hwnd()
        if phase == "opening":
            focused, focus = self._pin_window_focus(hwnd)
            if not focused:
                cycle.pop("pin_grid_candidate", None)
                self._click_debug(f"[PIN Debug] Aguardando foco da janela do jogo. {focus}")
                return
        elif not hwnd or u32.GetForegroundWindow() != hwnd:
            return
        with world.lock:
            capacity = world.storage_capacity
            dialog = world._npc_dialog_snapshot()
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
                self._open_kafra_storage(snapshot, hwnd, now, phase, dialog, world, px, py)
            elif phase == "store":
                if not dialog["storage_open"]:
                    raise ValueError("armazém fechado durante a transferência.")
                if capacity is not None and capacity[0] >= capacity[1]:
                    raise ValueError(f"armazém cheio ({capacity[0]}/{capacity[1]}).")
                self._store_kafra_items(snapshot, hwnd, now, inventory, storage, inventory_ready=inventory_ready)
            elif phase == "closing":
                if dialog["storage_open"]:
                    return
                print("[Kafra] Armazém fechado. Iniciando retorno.", flush=True)
                cycle.update(phase="return", deadline=now + 180)
                self.start_travel_plan(cycle["farm_map"], mode="farm")
        except (OSError, ValueError) as exc:
            if phase == "dialog":
                self._send_game_key(0x1B)
            self._finish_kafra_storage(str(exc), current_map)

    def _select_best_kafra_location(self, world, current_map, position, *, excluded=()):
        """Seleciona a Kafra mais próxima e acessível no mapa atual, evitando coordenadas que falharam."""
        locations = self._kafra_locations_for_map(current_map)
        if not locations:
            return None
        excluded_set = set(excluded or ())
        candidates = [loc for loc in locations if (loc["x"], loc["y"]) not in excluded_set]
        if not candidates:
            candidates = list(locations)
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(current_map) if terrain else None
        start = tuple(map(round, position))
        preliminary = []
        for loc in candidates:
            pos = (loc["x"], loc["y"])
            dist = math.dist(position, pos)
            visible_npc = False
            if world is not None:
                with world.lock:
                    visible_npc = self._registered_kafra(world, current_map, loc) is not None
            preliminary.append((not visible_npc, dist, pos, loc))
        preliminary.sort(key=lambda item: (item[0], item[1]))
        if gat is None:
            return preliminary[0][3]
        scored = []
        for not_visible, dist, pos, loc in preliminary:
            accessible = any(
                gat.is_walkable(pos[0] + dx, pos[1] + dy)
                for dx in range(-3, 4) for dy in range(-3, 4)
                if max(abs(dx), abs(dy)) >= 1
            )
            if accessible and start != pos and dist > 4.0 and len(preliminary) > 1:
                goal_cell = next(
                    ((pos[0] + dx, pos[1] + dy)
                     for dx in range(-2, 3) for dy in range(-2, 3)
                     if max(abs(dx), abs(dy)) >= 1 and gat.is_walkable(pos[0] + dx, pos[1] + dy)),
                    None,
                )
                if goal_cell is not None:
                    path = gat.find_path(start, goal_cell, max_iterations=15000)
                    if not path or path[-1] != goal_cell:
                        accessible = False
            if accessible:
                return loc
            scored.append((not accessible, not_visible, dist, loc))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return scored[0][3]

    def _approach_kafra_storage(self, snapshot, world, hwnd, px, py, now, dialog, zeny):
        cycle = self._kafra_cycle
        if (math.dist((px, py), cycle["position"]) <= 4
                and zeny is not None and zeny < 40 and not player_has_storage_ticket(world)):
            raise ValueError("saldo insuficiente ao chegar à Kafra (mínimo de 40 Zeny ou Ticket).")
        leg = cycle["approach_leg"]
        failed_kafras = cycle.setdefault("failed_kafras", set())
        # Reavalia com cooldown se já existe uma Kafra válida bem mais próxima no mapa atual
        if (not dialog.get("open") and "npc_click_at" not in leg
                and now >= leg.get("recheck_kafra_at", 0.0)
                and math.dist((px, py), cycle["position"]) > 18.0):
            leg["recheck_kafra_at"] = now + 2.5
            alt = self._select_best_kafra_location(world, snapshot.map, (px, py), excluded=failed_kafras)
            if alt is not None:
                alt_pos = (alt["x"], alt["y"])
                alt_dist = math.dist((px, py), alt_pos)
                cur_dist = math.dist((px, py), cycle["position"])
                if alt_pos != cycle["position"] and (alt_dist <= 15.0 or alt_dist + 12.0 < cur_dist):
                    cycle["location"] = alt
                    cycle["position"] = alt_pos
                    cycle["approach_leg"] = {"blocked": {}, "deadline": cycle["deadline"]}
                    leg = cycle["approach_leg"]
                    self.active_nav_path = None
                    self._clear_locked_nav_path()
                    print(f"[Kafra] Reavaliando Kafra mais próxima em {snapshot.map}: {alt_pos} (dist {alt_dist:.1f}).", flush=True)
        location = cycle["location"]
        if dialog.get("open"):
            if self._dialog_matches_target_kafra(world, dialog, leg, snapshot.map, location):
                npc_gid = leg.get("npc_gid") or dialog.get("npc_gid")
                cycle.update(phase="dialog", npc=npc_gid, clicked_at=leg.get("npc_click_at", now),
                             dialog_started=True, next=now)
                return
            self._dismiss_unwanted_dialog(cycle, dialog, now)
            return
        status = self._approach_registered_kafra(snapshot, world, leg, location, (px, py), now, hwnd)
        if status in ("movement_unconfirmed", "no_clickable_step", "no_gat_path"):
            retries = leg.get("walk_retries", 0) + 1
            if retries <= 3 and now < cycle["deadline"]:
                failed_kafras.add(cycle["position"])
                alt = self._select_best_kafra_location(world, snapshot.map, (px, py), excluded=failed_kafras)
                if alt is not None and (alt["x"], alt["y"]) != cycle["position"]:
                    cycle["location"] = alt
                    cycle["position"] = (alt["x"], alt["y"])
                    print(f"[Kafra] Rota até Kafra anterior falhou ({status}); redirecionando para {cycle['position']}.", flush=True)
                moved_target = cycle["position"] != (location["x"], location["y"])
                cycle["approach_leg"] = {
                    "blocked": {} if moved_target else leg["blocked"].copy(),
                    "deadline": cycle["deadline"], "walk_retries": retries,
                }
                if not moved_target and "retry_stride_dist" in leg:
                    cycle["approach_leg"]["retry_stride_dist"] = leg["retry_stride_dist"]
                cycle["next"] = now + 0.5
                self.active_nav_path = None
                self._clear_locked_nav_path()
                print(
                    f"[Kafra] {self._travel_walk_error(status, cycle['position'])} "
                    f"Recalculando a partir de {snapshot.map} em {tuple(map(round, (px, py)))} "
                    f"(tentativa {retries}/3).",
                    flush=True,
                )
                return
        if failure := self._travel_walk_error(status, cycle["position"]):
            raise ValueError(failure)
        if status == "missing_gat":
            raise ValueError(f"Terreno .gat de {snapshot.map} indisponível.")
        if status == "clicked":
            cycle.update(phase="dialog", npc=leg["npc_gid"], clicked_at=leg["npc_click_at"], next=now + 0.1)

    def _report_pin_test(self, message):
        self._pin_probe_status = message
        self._pin_test_status.set(message)
        log_click_file(f"[PIN Teste] {message}")

    @staticmethod
    def _log_pin_uipi(cycle):
        if not cycle.get("pin_uipi_logged"):
            cycle["pin_uipi_logged"] = True
            log_click_file(PIN_UIPI_MESSAGE)

    def _queue_pin_probe(self):
        self._finish_hud_edit()
        pin = str(self.filters_config.get("kafra_pin") or self.filters_config.get("kafra_storage_pin") or "")
        if not re.fullmatch(r"[0-9]+", pin):
            self._report_pin_test("Configure uma senha numérica no campo Senha Kafra.")
            return
        if self._automation_click_active() or getattr(self, "_pin_click_probe", None) is not None:
            self._report_pin_test("Aguarde a ação anterior.")
            return
        vision = self._get_ui_vision()
        if not vision:
            self._report_pin_test("Leitura da janela de senha indisponível.")
            return
        now = time.monotonic()
        self._pin_click_probe = {"pin_test": True, "pin": pin, "vision": vision, "next": now,
                                 "deadline": now + 30.0}
        self._set_hud_running(False)
        self._close_hud_drawer()
        self._focus_hud_game()
        self._report_pin_test("Aguardando a janela Digite a Senha.")

    def _tick_pin_probe(self, snapshot):
        probe = getattr(self, "_pin_click_probe", None)
        if probe is None:
            return False
        now = time.monotonic()

        def finish(message):
            self._pin_click_probe = None
            self._report_pin_test(message)
            return True

        ok_at = probe.get("pin_ok_sent_at")
        if ok_at is not None and self.feed.world.storage_open:
            length = len(probe["pin"])
            return finish(f"Senha confirmada ({length}/{length}) e OK confirmado pela abertura do armazém.")
        if now >= probe["deadline"] and "pin_seen_at" not in probe:
            return finish("Tempo limite aguardando a janela Digite a Senha.")
        if now < probe["next"]:
            return True
        hwnd = self.get_game_hwnd()
        focused, focus = self._pin_window_focus(hwnd)
        if not self._game_input_ready(hwnd) or not focused:
            probe.pop("pin_grid_candidate", None)
            probe["next"] = now + 0.25
            self._click_debug(f"[PIN Debug] Aguardando foco da janela do jogo. {focus}")
            return True
        vision = probe["vision"]
        try:
            rect, screen, colors = self._pin_frame_for_input(probe, snapshot.viewport)
            if rect is None:
                if "pin_seen_at" in probe:
                    if ok_at is not None:
                        if now - ok_at >= 5.0:
                            return finish("A janela fechou após o OK. O feed ainda não confirmou a abertura do armazém.")
                    elif now - probe["pin_seen_at"] >= 0.35:
                        return finish("A janela Digite a Senha foi fechada.")
                probe["next"] = now + 0.35
                return True
            probe["pin_seen_at"] = now
            left, top, _, _ = snapshot.viewport
            x, y, width, height = rect
            self._pin_dialog_screen_rect = (left + x - 12, top + y - 12,
                                            left + x + width + 12, top + y + height + 12)
            self._pin_hud_suppress_until = now + 2.5
            if self._hide_overlapping_hud_windows(self._pin_dialog_screen_rect):
                probe["next"] = now + 0.08
                return True
            self._step_kafra_pin(probe, snapshot, hwnd, vision, screen, colors, rect, probe["pin"], now)
        except OSError as exc:
            if "pin_seen_at" not in probe:
                return finish(str(exc))
            self._click_debug(f"[PIN Debug] Captura indisponível. Nova leitura agendada. {exc}")
            probe["next"] = now + 0.35
        except ValueError as exc:
            return finish(str(exc))
        return True

    @staticmethod
    def _pin_frame_for_input(cycle, viewport):
        seq = cycle.get("pin_seq")
        vision = cycle["vision"]
        if (seq and seq["viewport"] == viewport
                and vision.pin_progress(viewport, seq["rect"]) is not None):
            return seq["rect"], None, None
        if seq:
            cycle.pop("pin_seq")
            cycle["pin_rescan"] = seq
        screen, colors = vision.capture_pin(viewport)
        rect = vision.pin_window_rect(screen)
        if rect is None:
            cycle.pop("pin_grid_candidate", None)
        return rect, screen, colors

    def _pin_window_focus(self, hwnd, point=None):
        foreground = u32.GetForegroundWindow()
        root = (u32.GetAncestor(hwnd, 2) or hwnd) if hwnd else None
        fg_root = (u32.GetAncestor(foreground, 2) or foreground) if foreground else None
        focused = bool(root and root == fg_root and self._window_pid(foreground) == self._window_pid(hwnd))
        details = f"jogo=0x{int(hwnd or 0):X}, raiz=0x{int(root or 0):X}, primeiro plano=0x{int(foreground or 0):X}"
        if point is not None:
            hit = u32.WindowFromPoint(wintypes.POINT(*point))
            hit_root = (u32.GetAncestor(hit, 2) or hit) if hit else None
            focused = focused and bool(hit_root == root and self._window_pid(hit) == self._window_pid(hwnd))
            details += f", sob a tecla=0x{int(hit or 0):X}"
        return focused, f"{details}, foco correto={focused}"

    def _move_pin_cursor_released(self, hwnd, sx, sy):
        if (self._automation_click_active() or not self._pin_window_focus(hwnd)[0]
                or self._mouse_buttons_down()):
            return False
        ensure_input_desktop()
        return self._move_cursor_released(sx, sy)

    def _prepare_pin_click(self, target, aim, press):
        cursor = wintypes.POINT()
        actual = None

        def centered():
            nonlocal actual
            actual = (cursor.x, cursor.y) if u32.GetCursorPos(ctypes.byref(cursor)) else None
            return actual is not None and math.dist(actual, target) <= 2

        if not centered():
            if aim(0.075) != target or self._mouse_buttons_down():
                return False
            if not self._position_cursor(*target):
                self._move_cursor_released(*target)
            deadline = time.monotonic() + 0.24
            while not centered() and time.monotonic() < deadline:
                if aim(0.075) != target or self._mouse_buttons_down():
                    return False
                time.sleep(0.015)
        if not centered():
            press["positioning_failed"] = True
            log_click_file(f"[PIN Debug] LEFTDOWN não enviado: cursor={actual}, centro={target}.")
            return False
        time.sleep(0.05)
        return aim(0.075) == target and centered() and not self._mouse_buttons_down()

    @staticmethod
    def _pin_auto_digit_matches(seq, field_count, now):
        press = seq.get("automatic_press") or {}
        pressed_at = press.get("pressed_at")
        return (press.get("kind") == "digit" and press.get("step") == seq["step"]
                and field_count == press.get("before", -1) + 1
                and pressed_at is not None and now - pressed_at <= 6.0
                and seq.get("physical_click_at", 0.0) < pressed_at)

    def _kafra_pin_click(self, cycle, snapshot, hwnd, seq, point, press):
        x, y, width, height = seq["rect"]
        px, py = point
        debug = cycle.get("pin_test") or self.filters_config["kafra_pin_debug"]
        if not (8 <= px <= width - 8 and 18 <= py <= height - 1):
            if debug:
                self._click_debug(f"[PIN Debug] Ponto {point} fora da janela reconhecida.")
            return None
        focused, focus = self._pin_window_focus(hwnd)
        if not focused or not self._game_input_ready(hwnd):
            self._click_debug(f"[PIN Debug] Clique adiado. {focus}")
            return None
        if not self.is_admin and win32_input.is_window_process_elevated(hwnd):
            self._log_pin_uipi(cycle)
            cycle["next"] = time.monotonic() + 1.0
            return None
        vx, vy, vw, vh = snapshot.viewport
        if not (0 <= x + px < vw and 0 <= y + py < vh):
            return None
        target = vx + x + px, vy + y + py
        if self._is_point_in_hud(*target, margin=2):
            self._click_debug("[PIN Debug] Clique adiado: HUD sobre a tecla.")
            return None
        if self._automation_click_active() or self._mouse_buttons_down():
            if debug:
                self._click_debug("[PIN Debug] Clique adiado enquanto o botão está pressionado ou a entrada está ocupada.")
            return None
        if debug:
            cycle["pin_debug_aim"] = target

        def aim(_lead):
            active = getattr(self, "_pin_click_probe", None) if cycle.get("pin_test") else self._kafra_cycle
            if (active is not cycle or cycle.get("pin_seq") is not seq
                    or self.current_viewport != snapshot.viewport):
                return None
            focused, focus = self._pin_window_focus(hwnd, target)
            if not focused:
                log_click_file(f"[PIN Debug] Mira cancelada. {focus}")
                return None
            if _lead <= 0.05:
                field = cycle["vision"].pin_progress(seq["viewport"], seq["rect"], with_ink=True)
                if field != press["field_state"]:
                    log_click_file("[PIN Debug] Pressão cancelada: a janela ou o campo mudou após o agendamento.")
                    return None
                cursor = wintypes.POINT()
                actual = (cursor.x, cursor.y) if u32.GetCursorPos(ctypes.byref(cursor)) else None
                if actual is None or math.dist(actual, target) > 3:
                    log_click_file(f"[PIN Debug] Pressão cancelada: cursor={actual}, centro={target}. {focus}")
                    return None
                if debug:
                    log_click_file(f"[PIN Debug] Antes da pressão: centro={target}, cursor={actual}. {focus}")
                if press["kind"] == "digit":
                    vision = cycle["vision"]
                    try:
                        _, colors = vision.capture_pin((vx + x, vy + y, 106, 140))
                        highlight, _ = vision.pin_highlight(colors)
                    except (OSError, ValueError):
                        highlight = None
                    if debug:
                        log_click_file(f"[PIN Debug] Realce no painel: tecla esperada={point}, visto={highlight}.")
                    if highlight is None or math.dist(highlight, point) > 10:
                        log_click_file("[PIN Debug] Pressão cancelada: o realce da tecla esperada não foi confirmado.")
                        return None
            return target

        if not self._position_render_window(None):
            return None
        self._pin_render_suspended = True
        completion = Future()
        def restore_render(_result):
            self._pin_render_suspended = False

        completion.add_done_callback(restore_render)

        def click_at(aimed_point):
            if not self._prepare_pin_click(aimed_point, aim, press):
                return False
            win32_input.begin_bot_click()
            try:
                pressed_at, failed, error = win32_input.click_window_mouse(
                    hwnd, aimed_point,
                    ready=lambda: aim(0.022) == aimed_point and not self._mouse_buttons_down(),
                )
            finally:
                win32_input.finish_bot_click()
            if error == 5:
                self._log_pin_uipi(cycle)
            elif failed:
                log_click_file(f"[PIN Debug] {PIN_CLICK_METHOD} falhou em {failed}: erro={error}.")
            if pressed_at is not None:
                press["pressed_at"] = pressed_at
                seq["automatic_press"] = press
                if press["kind"] == "ok":
                    cycle["pin_ok_sent_at"] = pressed_at
            return failed is None

        scheduled = False
        try:
            scheduled = self._schedule_game_click(
                *target, hwnd, target_point=aim, completion=completion,
                allow_offsets=False, trace_touch=debug,
                click_action=click_at,
            )
            return completion if scheduled else None
        finally:
            if not scheduled:
                self._pin_render_suspended = False

    def _step_kafra_pin(self, cycle, snapshot, hwnd, vision, screen, colors, rect, pin, now):
        if not re.fullmatch(r"[0-9]+", pin):
            if not cycle.get("pin_missing_logged"):
                cycle["pin_missing_logged"] = True
                print("[Kafra] Configure uma senha numérica no HUD.", flush=True)
            cycle["next"] = now + 1.0
            return

        cycle["next"] = now + 0.08
        debug = cycle.get("pin_test") or self.filters_config["kafra_pin_debug"]
        x, y, width, height = rect
        vx, vy, _, _ = snapshot.viewport
        neutral = vx + x + width - 10, vy + y + height // 2
        seq = cycle.get("pin_seq")
        if seq is not None and seq["pin"] != pin:
            cycle.pop("pin_seq")
            cycle.pop("pin_rescan", None)
            return
        if seq is None:
            panel = screen[y:y + height, x:x + width]
            field_count = vision.pin_field_count(panel)
            highlighted, reset = vision.pin_highlight(colors[y:y + height, x:x + width])
            needed = set(pin)
            buttons, ok, _ = vision.pin_controls(panel, highlighted, partial=True)
            needs_reset = field_count and cycle.get("pin_rescan") is None
            reset_point = vision.pin_reset_point(panel, reset) if field_count else None
            if len(buttons) != 10 or not needed <= buttons.keys() or ok is None or (needs_reset and reset_point is None):
                cycle.pop("pin_grid_candidate", None)
                self._click_debug(f"[PIN Debug] Grade incompleta: {len(buttons)}/10 teclas, "
                                  f"{len(needed & buttons.keys())}/{len(needed)} necessárias, "
                                  f"OK={ok is not None}, campo={field_count}.")
                if cycle.get("pin_read_park_rect") != rect and self._move_pin_cursor_released(hwnd, *neutral):
                    cycle["pin_read_park_rect"] = rect
                cycle["next"] = now + 0.30
                return
            candidate = (snapshot.viewport, rect, buttons, ok, reset_point, field_count)
            last_candidate, last_read_at = cycle.get("pin_grid_candidate", (None, 0.0))
            read_at = time.monotonic()
            if last_candidate != candidate or read_at - last_read_at > 2.0:
                cycle["pin_grid_candidate"] = candidate, read_at
                if debug:
                    self._click_debug("[PIN Debug] Grade completa; aguardando uma segunda leitura igual.")
                cycle["next"] = read_at + 0.18
                return
            cycle.pop("pin_grid_candidate", None)
            cycle.pop("pin_read_park_rect", None)
            seq = {"rect": rect, "viewport": snapshot.viewport, "pin": pin, "buttons": buttons,
                   "ok": ok, "reset": reset_point, "step": 0, "field_count": field_count, "attempt": 0}
            previous = cycle.pop("pin_rescan", None)
            if previous and previous["pin"] == pin:
                if field_count == previous["field_count"] + 1 and previous["step"] < len(pin):
                    after = vision.pin_progress(snapshot.viewport, rect, with_ink=True)
                    if (not self._pin_auto_digit_matches(previous, field_count, now)
                            or after is None or after[0] != field_count
                            or after[1] <= previous["automatic_press"]["field_state"][1]):
                        log_click_file("[PIN] Avanço incerto na releitura. Voltando à leitura inicial do campo.")
                        return
                    seq["step"] = previous["step"] + 1
                    log_click_file(f"[PIN] Resposta tardia do clique automático confirmada em {seq['step']}/{len(pin)}.")
                elif field_count == previous["field_count"]:
                    seq["step"] = previous["step"]
                    seq["attempt"] = previous["attempt"]
                    if previous.get("automatic_press"):
                        seq["automatic_press"] = previous["automatic_press"]
                        seq["physical_click_at"] = previous.get("physical_click_at", 0.0)
                        if seq["automatic_press"].get("pressed_at") is not None:
                            seq["attempt"] = max(1, seq["attempt"])
                elif field_count:
                    log_click_file("[PIN] Contagem inesperada na releitura. Voltando à leitura inicial do campo.")
                    return
            cycle["pin_seq"] = seq
            if debug:
                log_click_file(f"[PIN Debug] Grade estável em duas leituras: 10/10 teclas, "
                               f"{len(needed)} necessárias, campo={field_count}.")

        if pending := seq.get("pending"):
            press, completion = pending
            if not completion.done():
                return
            finished_at, sent = completion.result()
            if now < finished_at + 0.15:
                cycle["next"] = finished_at + 0.15
                return
            after = vision.pin_progress(snapshot.viewport, rect, with_ink=True)
            field_count, ink = after if after is not None else (None, None)
            kind, method, before = press["kind"], press["method"], press["field_state"]
            expected = before[0] + 1 if kind == "digit" else 0 if kind == "reset" else None
            pressed = press.get("pressed_at") is not None
            confirmed_press = (seq["automatic_press"] if self._pin_auto_digit_matches(seq, field_count, now)
                               else None)
            if confirmed_press is not None:
                previous_read = confirmed_press.get("field_confirmation")
                if ink <= confirmed_press["field_state"][1] or previous_read != after:
                    if now - finished_at >= 1.5:
                        cycle["pin_rescan"] = seq
                        cycle.pop("pin_seq")
                        return
                    confirmed_press["field_confirmation"] = after
                    cycle["next"] = now + 0.15
                    return
            elif automatic_press := seq.get("automatic_press"):
                automatic_press.pop("field_confirmation", None)
            wait_limit = 3.0 if kind == "ok" else 1.5
            if (pressed and now - finished_at < wait_limit
                    and (field_count == before[0] or (kind == "ok" and after is None))):
                cycle["next"] = now + 0.15
                return
            _, focus = self._pin_window_focus(hwnd)
            if after is None:
                reason = "painel saiu da posição lida ou fechou"
            elif field_count == expected:
                reason = ("campo confirmou a ação" if pressed or confirmed_press is not None
                          else "campo mudou sem pressão confirmada")
            elif field_count == before[0]:
                reason = "pressão não confirmada e campo sem avanço"
                if pressed:
                    reason = ("cliente não confirmou a abertura do armazém" if kind == "ok"
                              else "cliente não confirmou mudança no campo")
                if ink != before[1]:
                    reason += "; pixels do campo variaram"
            else:
                reason = "contagem inesperada no campo"
            log_click_file(f"[PIN Debug] {kind} posição={seq['step'] + 1}, tentativa={seq['attempt'] + 1}, "
                           f"via={method}, enviado={sent}, campo antes={before[0]}, depois={field_count}, "
                           f"esperado={expected}, pixels escuros={before[1]}→{ink}. {reason}. {focus}")
            seq.pop("pending")
            if (press.get("positioning_failed") and not pressed and field_count == before[0]
                    and not seq.get("positioning_retried")):
                seq["positioning_retried"] = True
                log_click_file("[PIN Debug] Retentativa de posicionamento antes de qualquer LEFTDOWN.")
                cycle["next"] = now + 0.18
                return
            if field_count is None:
                seq["attempt"] += 1
                cycle["pin_rescan"] = seq
                cycle.pop("pin_seq")
                return
            if kind == "digit" and field_count == expected:
                if confirmed_press is None:
                    cycle["pin_rescan"] = seq
                    cycle.pop("pin_seq")
                    return
                seq.pop("automatic_press", None)
                seq.pop("physical_click_at", None)
                seq["step"] += 1
                seq["field_count"] = field_count
                seq["attempt"] = 0
                seq.pop("positioning_retried", None)
                return
            if kind == "reset" and field_count == 0:
                cycle.pop("pin_seq")
                return
            if field_count != seq["field_count"]:
                cycle["pin_rescan"] = seq
                cycle.pop("pin_seq")
                return
            seq["attempt"] += 1
            cycle["pin_rescan"] = seq
            cycle.pop("pin_seq")
            cycle["next"] = now + 0.25
            return

        if seq["step"] == 0 and seq["field_count"]:
            kind, point = "reset", seq["reset"]
            if point is None:
                cycle.pop("pin_seq")
                cycle["next"] = now + 0.30
                return
        elif seq["step"] < len(pin):
            kind, point = "digit", seq["buttons"][pin[seq["step"]]]
        else:
            kind, point = "ok", seq["ok"]

        before = vision.pin_progress(snapshot.viewport, rect, with_ink=True)
        if before is None:
            cycle["pin_rescan"] = seq
            cycle.pop("pin_seq")
            self._click_debug("[PIN Debug] Clique adiado: painel não localizado antes da tentativa.")
            return
        if before[0] != seq["field_count"]:
            if self._pin_auto_digit_matches(seq, before[0], now):
                original_field = seq["automatic_press"]["field_state"]
                if before[1] <= original_field[1] or seq.get("late_field") != before:
                    seq["late_field"] = before
                    cycle["next"] = now + 0.15
                    return
                seq.pop("late_field", None)
                seq["step"] += 1
                seq["field_count"] = before[0]
                seq["attempt"] = 0
                seq.pop("automatic_press", None)
                seq.pop("physical_click_at", None)
                log_click_file(f"[PIN] Resposta tardia do clique automático confirmada em {seq['step']}/{len(pin)}.")
                return
            cycle["pin_rescan"] = seq
            cycle.pop("pin_seq")
            return
        method = PIN_CLICK_METHOD
        press = {"kind": kind, "step": seq["step"], "before": before[0], "field_state": before, "method": method}
        completion = self._kafra_pin_click(cycle, snapshot, hwnd, seq, point, press)
        if completion is not None:
            seq["pending"] = press, completion
            _, focus = self._pin_window_focus(hwnd)
            log_click_file(f"[PIN Debug] Agendado {kind}, posição={seq['step'] + 1}, "
                           f"tentativa={seq['attempt'] + 1}, via={method}, "
                           f"campo antes={before[0]}, centro={point}. {focus}")

    def _open_kafra_storage(self, snapshot, hwnd, now, phase, dialog, world, px, py):
        cycle = self._kafra_cycle
        if dialog["storage_open"]:
            if cycle.get("pin_ok_sent_at") is not None:
                log_click_file("[PIN] OK confirmado pela abertura do armazém no feed.")
            self._pin_dialog_screen_rect = None
            self._pin_hud_suppress_until = 0.0
            self._pin_overlay_visible_cache = False
            self._pin_debug_scan = None
            cycle.pop("pin_ok_sent_at", None)
            cycle.pop("pin_seen_at", None)
            cycle.pop("pin_seq", None)
            cycle.pop("pin_rescan", None)
            cycle.pop("pin_grid_candidate", None)
            print("[Kafra] Armazém aberto. Iniciando depósito.", flush=True)
            cycle.update(phase="store", deadline=now + 180, next=now + 0.25)
            return

        if phase == "opening":
            vision = cycle["vision"]
            try:
                pin_rect, screen, colors = self._pin_frame_for_input(cycle, snapshot.viewport)
            except OSError as exc:
                if "pin_seen_at" not in cycle:
                    raise
                self._click_debug(f"[PIN Debug] Captura indisponível. Nova leitura agendada. {exc}")
                cycle["next"] = now + 0.35
                return
            if pin_rect:
                rx, ry, rw, rh = pin_rect
                vx, vy, _, _ = snapshot.viewport
                self._pin_dialog_screen_rect = (vx + rx - 12, vy + ry - 12, vx + rx + rw + 12, vy + ry + rh + 12)
                self._pin_hud_suppress_until = now + 2.5
                cycle["pin_seen_at"] = now
                if self.filters_config["kafra_pin_debug"]:
                    cycle["pin_debug_rect"] = (snapshot.viewport, pin_rect, now)
                if self._hide_overlapping_hud_windows(self._pin_dialog_screen_rect):
                    cycle["next"] = now + 0.08
                    return
                if any(action is not None and not action.done()
                       for action in (cycle.get("menu_action"), cycle.get("next_key"))):
                    return
                pin = str(self.filters_config.get("kafra_pin") or self.filters_config.get("kafra_storage_pin") or "")
                try:
                    self._step_kafra_pin(cycle, snapshot, hwnd, vision, screen, colors, pin_rect, pin, now)
                except OSError as exc:
                    self._click_debug(f"[PIN Debug] Captura indisponível. Nova leitura agendada. {exc}")
                    cycle["next"] = now + 0.35
                return
            cycle.pop("pin_debug_rect", None)
            cycle.pop("pin_debug_aim", None)
            if "pin_seen_at" in cycle:
                if (ok_at := cycle.get("pin_ok_sent_at")) is not None:
                    if now - ok_at < 5.0:
                        cycle["next"] = now + 0.1
                        return
                    raise ValueError("a janela fechou após o OK e o feed não confirmou a abertura do armazém.")
                if now - cycle["pin_seen_at"] < 0.35:
                    cycle["next"] = now + 0.08
                    return
                raise ValueError("a janela Digite a Senha foi fechada.")

        if self._is_pin_entry_active():
            cycle["next"] = now + 0.1
            return

        if pending := cycle.get("menu_action"):
            if not pending.done():
                return
            cycle.pop("menu_action")
            if (sent_at := pending.result()) is None:
                raise ValueError("seleção do Armazém interrompida antes da confirmação.")
            cycle["dialog_wait_until"] = sent_at + 1.5
            cycle["menu_select_cooldown_until"] = max(cycle.get("menu_select_cooldown_until", 0.0), sent_at + 1.5)
        leg = cycle["approach_leg"]
        if not self._kafra_dialog_ready(cycle, dialog, world, leg, now):
            return
        matches = self._dialog_matches_target_kafra(world, dialog, leg, snapshot.map, cycle["location"])
        if matches:
            cycle["dialog_started"] = True
        if matches and dialog.get("seq") != cycle.get("handled_seq"):
            allowed = self._dialog_action_guard(dialog, lambda: self._kafra_cycle is cycle)
            if options := self._kafra_menu_options(cycle, dialog):
                preferred_slot = self.filters_config.get("kafra_storage_number", 3)
                storage_index = self._kafra_storage_slot_index(
                    options, raw=dialog["dialog_menu_raw_options"], preferred=preferred_slot,
                )
                if storage_index is not None:
                    if self._try_select_kafra_menu(cycle, dialog, options, storage_index, allowed, now):
                        cycle.update(phase="opening", deadline=now + 35)
                    return
                normalized = [_normalize_menu_text(option) for option in options]
                index = self._kafra_keyword_index(normalized, ("armazem", "storage", "deposito"),
                                                  raw=dialog["dialog_menu_raw_options"], token="0Wa0AQ",
                                                  skip_cancel=True)
                if index is None:
                    index = self._kafra_fallback_menu_index(
                        options, raw=dialog["dialog_menu_raw_options"], action="storage",
                        preferred_slot=preferred_slot, submenu=bool(cycle.get("storage_menu_selected")),
                    )
                if index is None:
                    unmatched_retries = cycle.get("unmatched_menu_retries", 0) + 1
                    cycle["unmatched_menu_retries"] = unmatched_retries
                    if unmatched_retries <= 4:
                        if dialog["dialog_has_next"] or dialog["dialog_has_close"]:
                            self._send_kafra_next(cycle, dialog, allowed)
                        else:
                            cycle["next"] = now + 0.5
                        print(f"[Kafra] Opções 0x00B7 sem correspondência ({options}); aguardando/revalidando ({unmatched_retries}/4).", flush=True)
                        return
                    failed_kafras = cycle.setdefault("failed_kafras", set())
                    failed_kafras.add(cycle["position"])
                    alt = self._select_best_kafra_location(world, snapshot.map, (px, py), excluded=failed_kafras)
                    self._send_game_key(0x1B)
                    if alt is not None and (alt["x"], alt["y"]) != cycle["position"]:
                        cycle["location"] = alt
                        cycle["position"] = (alt["x"], alt["y"])
                        cycle["unmatched_menu_retries"] = 0
                        cycle.pop("storage_menu_selected", None)
                        cycle.update(phase="approach", next=now + 0.4,
                                     approach_leg={"blocked": {}, "deadline": cycle["deadline"]})
                        print(f"[Kafra] Menu não reconhecido; reavaliando outra Kafra em {cycle['position']}.", flush=True)
                        return
                    raise ValueError("a Kafra não ofereceu a opção Usar o Armazém.")
                cycle["unmatched_menu_retries"] = 0
                if self._try_select_kafra_menu(cycle, dialog, options, index, allowed, now):
                    cycle["storage_menu_selected"] = True
                    cycle.update(phase="opening", deadline=now + 35)
                return
            if dialog["dialog_has_next"] or dialog["dialog_has_close"]:
                self._send_kafra_next(cycle, dialog, allowed)
                return

        if phase == "opening":
            if not cycle.get("next_key") and now >= cycle.get("opening_enter_at", 0.0):
                seq = dialog.get("seq")
                if seq != cycle.get("opening_enter_seq"):
                    cycle["opening_enter_seq"] = seq
                    cycle["opening_enter_count"] = 0
                dialog_text = _normalize_menu_text(dialog.get("text", ""))
                closing_speech = (
                    dialog.get("open")
                    and dialog.get("opcode") == 0x00B4
                    and now - dialog.get("last_update", now) >= 0.25
                    and any(term in dialog_text for term in ("vou abrir", "armazem", "storage"))
                )
                if (cycle.get("opening_enter_count", 0) < 3
                        and ((matches and (dialog["dialog_has_next"] or dialog["dialog_has_close"] or closing_speech))
                             or (cycle.get("dialog_started") and dialog.get("opcode") == 0x00B6 and dialog["dialog_has_close"]))):
                    allowed = self._dialog_action_guard(dialog, lambda: self._kafra_cycle is cycle)
                    if self._send_kafra_next(cycle, dialog, allowed):
                        cycle["opening_enter_count"] = cycle.get("opening_enter_count", 0) + 1
                        cycle["opening_enter_at"] = now + 1.2
                        return
            cycle["next"] = now + 0.18
        elif not dialog.get("open"):
            if self._is_pin_entry_active():
                cycle["next"] = now + 0.1
                return
            if cycle.pop("wrong_npc_dialog", None):
                leg.pop("npc_click_at", None)
                leg.pop("awaiting_npc_click", None)
                cycle.pop("dialog_started", None)
                cycle.pop("dismiss_enter_seq", None)
                cycle.pop("close_dialog_at", None)
                leg["npc_click_attempts"] = leg.get("npc_click_attempts", 0) + 1
                cycle.update(phase="approach", next=now + 0.2)
                print("[Kafra] Diálogo incorreto fechado; retomando interação com a Kafra alvo.", flush=True)
                return
            click_point = leg.get("npc_click_point")
            click_ts = leg.get("npc_click_at")
            clicked_at = (self._touch_click_finished_at(click_point, click_ts)
                          if click_point is not None and click_ts is not None
                          else cycle.get("clicked_at", now))
            far = math.dist((px, py), cycle["position"]) > 4.0
            if far and now - clicked_at >= 1.5:
                cycle.update(phase="approach", next=now)
            elif not cycle.get("dialog_started") and now - clicked_at >= 2.5:
                self._approach_registered_kafra(snapshot, world, leg, cycle["location"], (px, py), now, hwnd)
        elif not matches:
            self._dismiss_unwanted_dialog(cycle, dialog, now)
            if math.dist((px, py), cycle["position"]) > 4.0:
                cycle.update(phase="approach", next=now + 0.35)
            else:
                cycle["next"] = now + 0.35

    def _kafra_screen_point(self, snapshot, hwnd, point):
        left, top, width, height = snapshot.viewport
        x, y = point
        if (not self._game_input_ready(hwnd) or u32.GetForegroundWindow() != hwnd
                or not (0 <= x < width and 0 <= y < height)
                or self._is_point_in_hud(left + x, top + y, margin=2)):
            raise ValueError("controle da Kafra fora da área clicável do jogo.")
        return left + x, top + y

    def _kafra_ui_click(self, snapshot, hwnd, point, *, hover_reset=False):
        # O controle já foi identificado na janela do jogo, sobre a interface nativa.
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        ensure_input_desktop()
        self._move_and_click_sync(x, y, hwnd, hover_reset=hover_reset)

    def _kafra_hover(self, snapshot, hwnd, point):
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        ensure_input_desktop()
        if self._automation_click_active() or self._mouse_buttons_down():
            raise ValueError("clique anterior ainda em andamento.")
        if self._move_cursor_released(x, y):
            cursor = wintypes.POINT()
            if (u32.GetCursorPos(ctypes.byref(cursor))
                    and max(abs(cursor.x - x), abs(cursor.y - y)) <= 4):
                return
            if not self._mouse_buttons_down() and self._position_cursor(x, y):
                return
        raise ValueError("cursor não alcançou o slot indicado.")

    def _kafra_alt_right(self, snapshot, hwnd, point):
        x, y = self._kafra_screen_point(snapshot, hwnd, point)
        cursor = wintypes.POINT()
        if not u32.GetCursorPos(ctypes.byref(cursor)) or max(abs(cursor.x - x), abs(cursor.y - y)) > 4:
            raise ValueError("cursor saiu do slot antes da transferência.")
        win32_input.begin_bot_click()
        try:
            self._kafra_alt_input(hwnd, point=(x, y))
        finally:
            win32_input.finish_bot_click()

    def _kafra_alt_input(self, hwnd, key=None, *, point=None, window_message=False):
        if not self._game_input_ready(hwnd) or not self._is_game_focused():
            raise ValueError("janela do jogo indisponível para interação com a Kafra.")
        alt = u32.MapVirtualKeyW(0x12, 0)  # VK_MENU
        if not alt or any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in (1, 2, 0x10, 0x11, 0x12)):
            raise ValueError("tecla modificadora ou botão do mouse já pressionado.")
        if window_message:
            if win32_input.send_window_alt_action(hwnd, key=key, point=point):
                return
            raise ValueError("falha ao enviar o comando com Alt à janela do jogo.")
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
        if not sent:
            if win32_input.send_window_alt_action(hwnd, key=key, point=point):
                return
            raise ValueError("falha ao enviar o comando com Alt por SendInput e pela janela do jogo.")
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

    def _store_kafra_items(self, snapshot, hwnd, now, inventory, storage, *, inventory_ready):
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
            print(f"[Kafra] Depósito confirmado para item #{item[0]} (quantidade {item[1]}).", flush=True)
        screen = vision.capture(snapshot.viewport)
        grid = vision.inventory_grid(screen)
        cycle["inventory_visible"] = grid is not None
        if opening := cycle.get("inventory_open_action"):
            if not opening.done():
                return
            cycle.pop("inventory_open_action")
            if (sent_at := opening.result()) is not None:
                cycle["inventory_open_until"] = sent_at + 2.0
            else:
                cycle["inventory_open_until"] = now + 1.3
        if grid is None:
            if now < cycle.get("inventory_open_until", 0.0):
                cycle["next"] = now + 0.15
                return
            attempts = cycle.get("inventory_open_attempts", 0)
            if attempts >= 2:
                raise ValueError("Inventário não localizado após duas tentativas de Alt+E pelo OSK.")
            opening = Future()
            if self._send_game_key(ord("E"), osk_alt=True, completion=opening,
                                   allowed=lambda: (
                                       self._kafra_cycle is cycle and self.feed.world.storage_open
                                       and not cycle["inventory_visible"]
                                   )):
                cycle.update(inventory_open_action=opening, inventory_open_attempts=attempts + 1)
                print("[Kafra] Abrindo Inventário com Alt+E via OSK.", flush=True)
            cycle["next"] = now + 0.15
            return
        if not inventory_ready:
            if now >= cycle.setdefault("inventory_sync_until", now + 5.0):
                raise ValueError("Inventário aberto, mas a lista TCP completa não chegou. Reconecte com a captura ativa para transferir automaticamente.")
            cycle["next"] = now + 0.2
            return
        cycle.pop("inventory_sync_until", None)
        x, y, right, bottom, columns, rows = grid
        if cycle["tab"] is None:
            if not cycle["tabs"]:
                if not self._restock_kafra_items(snapshot, hwnd, now, screen, grid, inventory, storage):
                    if self._send_game_key(0x1B, allow_storage_escape=True):
                        print("[Kafra] Depósito concluído. Fechando com Esc.", flush=True)
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
        target_point = self._kafra_screen_point(snapshot, hwnd, target)
        if any(u32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 0x10, 0x11, 0x12)):
            raise ValueError("tecla modificadora ou botão do mouse já pressionado.")
        press, release = INPUT(type=INPUT_MOUSE), INPUT(type=INPUT_MOUSE)
        press.mi.dwFlags, release.mi.dwFlags = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        size = ctypes.sizeof(INPUT)
        self._click_in_progress = True
        try:
            if not u32.SendInput(1, ctypes.byref(press), size):
                raise ValueError("não foi possível iniciar a reposição por arraste.")
            try:
                if not self._position_cursor(*target_point):
                    raise ValueError("cursor não alcançou o slot de destino.")
            finally:
                if not u32.SendInput(1, ctypes.byref(release), size):
                    time.sleep(0.015)
                    if not u32.SendInput(1, ctypes.byref(release), size):
                        raise ValueError("não foi possível soltar o botão após a reposição.")
        finally:
            self._click_in_progress = False

    def _travel_route_from(self, world, current_map, position, target, excluded=(), locked_kafras=None):
        with world.lock:
            zeny = world.player_zeny if world.zeny_known else None
            portals = [ent.track.target for (kind, _), ent in world.entity_items()
                       if kind == "warp" or ent.catalog_id == 45]
        terrain = getattr(self.feed, "terrain", None)
        terrains = dict(terrain.cached_gats) if terrain else {}
        if terrain and current_map not in terrains:
            terrains[current_map] = world.get_gat()
        if current_map in terrains:
            terrains[travel_map_group(current_map)] = terrains[current_map]
        return calculate_travel_route(
            current_map, target, position=position, zeny=zeny, excluded=excluded,
            live_portals=portals, routes_data=self.world_routes_data, terrains=terrains,
            navigation_obstacles=lambda map_id, gat, portal: self._build_navigation_obstacles(
                current_map if map_id == travel_map_group(current_map) else map_id,
                self._navigation_entities(current_map) if map_id == travel_map_group(current_map) else (),
                gat, allowed_portal_cell=portal, block_portals=False),
            locked_kafras=locked_kafras,
        )

    @staticmethod
    def _travel_caption(plan):
        steps = plan["steps"]
        uses_kafra = any(step["type"] == "kafra" for step in steps)
        via = "Portais diretos"
        if uses_kafra:
            via = "Kafra -> Portais" if any(step["type"] == "portal" for step in steps) else "Kafra"
        return f"[Rota] Destino: {plan['target_map']} via {via}"

    def _reset_travel_leg(self, plan):
        plan.pop("leg", None)
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._reset_patrol(None, None)
        self.nav_goal = self.patrol_target_route = None
        self._clear_locked_nav_path()

    def _finish_travel_plan(self, plan, reason=None):
        self.travel_plan = None
        self._reset_travel_leg(plan)
        if reason:
            print(f"[Viagem] {reason}", flush=True)
            if not self._kafra_cycle and not self.feed.world.is_map_loading:
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
        locked_kafras = {}
        if steps[0]["type"] == "kafra":
            locked_kafras[travel_map_group(steps[0]["from_city"])] = steps[0]["kafra_pos"]
        self.travel_plan = {
            "target_map": target, "mode": mode, "steps": steps, "step_idx": 0,
            "excluded": set(), "locked_kafras": locked_kafras,
        }
        if not self._kafra_cycle:
            self.auto_farm = self.auto_loot = self.auto_patrol = False
            self._finish_farm_lock("viagem iniciada")
            self._locked_drop_gid = None
            self._drop_lock_until = 0.0
            self._engage_state = self._engagement = None
            self.post_kill_wait_until = 0.0
        self.manual_nav_goal = None
        self._minimap_goal = None
        self._pending_hud_interaction = None
        self._reset_travel_leg(self.travel_plan)
        print(self._travel_caption(self.travel_plan), flush=True)
        print(f"[Viagem] {current_map} → {target}: {len(steps)} etapas, "
              f"{sum(step['cost'] for step in steps)}z previstos.", flush=True)
        return True

    def _replan_travel(self, plan, world, current_map, position, reason, failed_step=None):
        locked_kafras = plan.setdefault("locked_kafras", {})
        is_kafra_walk_retry = (
            failed_step
            and failed_step.get("type") == "kafra"
            and travel_matches_map(current_map, failed_step["from_city"])
            and not any(err in reason for err in (
                "Saldo insuficiente", "não ofereceu", "Não foi possível identificar",
                "diálogo da Kafra", "Seleção de teleporte",
            ))
        )
        if failed_step and not is_kafra_walk_retry:
            plan["excluded"].add(travel_step_key(failed_step))
            if failed_step.get("type") == "kafra":
                locked_kafras.pop(travel_map_group(failed_step["from_city"]), None)
        print(f"[Viagem] {reason} Recalculando a partir de {current_map} em {tuple(map(round, position))}.", flush=True)
        plan["replans"] = plan.get("replans", 0) + 1
        if plan["replans"] >= 12:
            self._finish_travel_plan(plan, f"Limite de alternativas para {plan['target_map']} atingido.")
            return
        if is_kafra_walk_retry:
            locked_kafras[travel_map_group(current_map)] = failed_step["kafra_pos"]
            self._reset_travel_leg(plan)
            print(f"[Kafra] Mantendo destino travado em {current_map} {failed_step['kafra_pos']}.", flush=True)
            return
        steps = self._travel_route_from(
            world, current_map, position, plan["target_map"], plan["excluded"],
            locked_kafras=locked_kafras,
        )
        if not steps:
            self._finish_travel_plan(plan, f"Sem alternativa executável para {plan['target_map']}.")
            return
        if steps[0]["type"] == "kafra":
            locked_kafras.setdefault(travel_map_group(steps[0]["from_city"]), steps[0]["kafra_pos"])
        plan.update(steps=steps, step_idx=0)
        self._reset_travel_leg(plan)
        print(self._travel_caption(plan), flush=True)

    def _walk_travel_leg(self, snapshot, world, leg, position, target, *, portal=None, npc_gid=None):
        if world.is_map_loading:
            return "waiting"
        terrain = getattr(self.feed, "terrain", None)
        # O feed carrega o GAT do mapa atual. A execução não carrega mapas no loop do HUD.
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        now = time.monotonic()
        approach_range = KAFRA_INTERACTION_RANGE if npc_gid is not None else 4.0
        if gat is None:
            if (not portal and "reposition_npc_from" not in leg and not world._is_player_walking()
                    and math.dist(position, target) <= approach_range):
                return "approached"
            missing_since = leg.setdefault("missing_gat_since", now)
            if now - missing_since >= 8.0:
                return "missing_gat"
            return "waiting"
        leg.pop("missing_gat_since", None)
        start = tuple(map(round, position))
        walking = world._is_player_walking()
        if (reposition := leg.get("reposition_npc_from")) is not None and math.dist(start, reposition) >= 1.5:
            leg.pop("reposition_npc_from")
            reposition = None
        visited = leg.setdefault("recent_walk_cells", collections.deque(maxlen=12))
        ready_ts = world.last_map_ready_ts
        if leg.get("walk_click") and leg["walk_click"][0] < ready_ts:
            for key in ("walk_click", "path", "path_job", "failed_walk_step", "retry_walk_hover",
                        "retry_stride_dist", "walk_failures"):
                leg.pop(key, None)
            visited.clear()
        if pending := leg.get("walk_click"):
            clicked_at, old_position, cell, point, enter_portal = pending
            if enter_portal:
                elapsed = now - self._touch_click_finished_at(point, clicked_at)
                if (not self._touch_click_aborted(point, clicked_at) and not self._visual_walk_failed(point, clicked_at)
                        and (walking or elapsed < 2.0)):
                    return "waiting"
                result = "portal_unconfirmed"
            else:
                result = self._walk_step_result(world, position, old_position, point, clicked_at, now)
                if result == "stalled" and now - clicked_at < 0.35 and not self._visual_walk_failed(point, clicked_at):
                    return "waiting"
                if result == "waiting":
                    can_chain = (
                        walking
                        and math.dist(position, old_position) >= 0.8
                        and math.dist(position, cell) <= 3.5
                        and math.dist(position, target) > 6.5
                        and not (portal and math.dist(position, portal) <= 5.0)
                    )
                    if not can_chain:
                        return "waiting"
                    result = "moved"
            leg.pop("walk_click", None)
            if result == "portal_unconfirmed":
                leg["portal_failures"] = leg.get("portal_failures", 0) + 1
                if leg["portal_failures"] >= 4:
                    return result
            elif result == "stalled":
                leg["walk_failures"] = leg.get("walk_failures", 0) + 1
                leg.setdefault("retry_stride_dist", math.dist(old_position, cell))
                status = "entrada recusada" if self._touch_click_aborted(point, clicked_at) else "sem movimento confirmado"
                log_click_file(f"[Viagem] Passo {cell}: {status}.")
                if leg.get("last_walk_hover"):
                    leg["blocked"][cell] = now + 12.0
                    leg["failed_walk_step"] = (cell, point)
                elif not leg.get("failed_walk_step"):
                    leg["retry_walk_hover"] = cell
                else:
                    leg["blocked"][cell] = now + 12.0
                    leg["failed_walk_step"] = (cell, point)
                if leg["walk_failures"] >= 3:
                    leg.pop("path", None)
                    leg.pop("path_job", None)
                if leg["walk_failures"] >= 6:
                    return "movement_unconfirmed"
            else:
                log_click_file(f"[Viagem] Passo {cell}: movimento confirmado em {start}.")
                leg.pop("failed_walk_step", None)
                leg.pop("retry_walk_hover", None)
                leg.pop("retry_stride_dist", None)
                if start not in visited:
                    leg["walk_failures"] = 0
                    leg["deadline"] = max(leg["deadline"], now + 25.0)
                else:
                    leg["walk_failures"] = leg.get("walk_failures", 0) + 1
                    if leg["walk_failures"] >= 6:
                        return "movement_unconfirmed"
                visited.append(start)
        if walking:
            last_cell = leg.get("last_walk_cell")
            if (last_cell is None
                    or math.dist(position, last_cell) > 3.5
                    or math.dist(position, target) <= 6.5
                    or (portal and math.dist(position, portal) <= 5.0)):
                if leg.get("path"):
                    leg["nav_wp_ts"] = now
                return "waiting"

        height = gat.get_elevation
        project = self.feed.camera.projector(
            snapshot.viewport, getattr(self.feed, "_cam", None) or position, height,
        )
        resolve = self._safe_click_resolver(snapshot, project, allowed_portal=portal, allowed_npc=npc_gid)
        detour = leg.get("detour", False)
        blocked, penalties = self._navigation_obstacles(
            snapshot.map, gat, portal=portal, npc=npc_gid)
        target_distance = math.dist(position, target)
        near_kafra = portal is None and target_distance <= 14.0
        failed_points = {cell for cell, until in leg["blocked"].items() if until > now}
        leg["blocked"] = {cell: leg["blocked"][cell] for cell in failed_points}

        def ground_point(cell):
            if cell != portal and (cell in failed_points or cell in visited):
                return None
            return self._walk_ground_point(gat, position, cell, blocked, resolve, portal=portal)

        def recalculate(reason):
            if near_kafra and reason in ("no_clickable_step", "no_gat_path"):
                nearby = sorted(self._patrol_cells_in_radius(*start, 4),
                                key=lambda cell: (math.dist(cell, target), math.dist(cell, start)))
                for cell in nearby:
                    if cell != start and math.dist(cell, target) <= target_distance + 1.0:
                        if point := ground_point(cell):
                            click(cell, point)
                            if "walk_click" in leg:
                                return "waiting"
            origin = leg.get("detour_origin", start)
            if math.dist(origin, start) >= 8.0:
                leg["detour_attempts"] = 0
                origin = start
            attempts = leg.get("detour_attempts", 0)
            if attempts >= 4:
                return reason
            leg["detour_origin"] = origin
            leg["detour_attempts"] = attempts + 1
            leg["detour"] = True
            leg["avoid_path_cells"] = tuple((leg.pop("path", None) or ())[1:9])
            leg.pop("path_job", None)
            self.active_nav_path = None
            self._click_debug(f"VIAGEM: Contornando trecho em {start}, rumo a {target}.")
            return "waiting"

        def click(cell, point, enter_portal=False, *, retry_hover=False):
            if self._navigation_click(snapshot, gat, cell, point, portal=portal, npc=npc_gid,
                                      kafra=npc_gid is not None,
                                      retry_key=(snapshot.map, "ground", cell),
                                      hover_reset=retry_hover):
                leg["walk_click"] = (now, position, cell, point, enter_portal)
                leg["last_walk_cell"] = cell
                leg["last_walk_hover"] = retry_hover
                if enter_portal:
                    leg["portal_attempts"] = leg.get("portal_attempts", 0) + 1
                leg.pop("retry_walk_hover", None)
            return "waiting"

        if now < getattr(self, "_map_stabilize_until", 0.0):
            return "waiting"

        if portal and math.dist(position, portal) <= 3.0:
            if start != portal:
                attempts = leg.get("portal_attempts", 0)
                if attempts >= 2 and attempts % 2 == 0:
                    nearby = sorted(self._patrol_cells_in_radius(*portal, 1),
                                    key=lambda cell: (math.dist(position, cell), cell))
                    for cell in nearby:
                        if cell not in (portal, start) and (point := ground_point(cell)):
                            return click(cell, point, enter_portal=True)
                if point := ground_point(portal):
                    return click(portal, point, enter_portal=True, retry_hover=attempts == 1)
            else:
                neighbors = self._patrol_cells_in_radius(*start, 2)
                for cell in neighbors:
                    if cell != start and (point := ground_point(cell)):
                        return click(cell, point)
                return recalculate("no_clickable_step")
        elif (not portal and reposition is None
              and math.dist(position, target) <= approach_range):
            return "approached"

        if (retry_cell := leg.get("retry_walk_hover")) is not None:
            if point := ground_point(retry_cell):
                return click(retry_cell, point, retry_hover=True)
            leg.pop("retry_walk_hover", None)

        if reposition is not None:
            nearby = sorted(self._patrol_cells_in_radius(*start, 3),
                            key=lambda cell: (math.dist(start, cell), math.dist(cell, target)))
            for cell in nearby:
                if math.dist(cell, reposition) >= 1.5 and (point := ground_point(cell)):
                    return click(cell, point)

        path_target = (target, portal, npc_gid, detour, reposition)
        path = leg.get("path")
        if path and leg.get("path_target") == path_target:
            idx = min(leg.get("nav_idx", 0), len(path) - 1)
            lookahead_end = min(len(path), idx + 8)
            closest_ahead = min(range(idx, lookahead_end), key=lambda i: math.dist(position, path[i]))
            if closest_ahead > idx and math.dist(position, path[closest_ahead]) <= 4.0:
                idx = closest_ahead
            while idx < len(path) - 1 and math.dist(position, path[idx]) <= 2.5:
                idx += 1
                leg["nav_idx"] = idx
                leg["nav_wp_dist"] = math.dist(position, path[idx])
                leg["nav_wp_ts"] = now
            leg["nav_idx"] = idx
            wp = path[idx]
            dist_wp = math.dist(position, wp)
            if dist_wp < leg.get("nav_wp_dist", float("inf")) - 0.1:
                leg["nav_wp_dist"] = dist_wp
                leg["nav_wp_ts"] = now
            ahead = path[idx:min(len(path), idx + 3)]
            blocked_ahead = any(cell in blocked for cell in ahead if cell != start)
            stalled_wp = (now - leg.get("nav_wp_ts", now)) > 4.0
            if blocked_ahead or stalled_wp:
                path = None
        else:
            path = None
        path_key = path_target
        if not path:
            job = leg.get("path_job")
            if job and not job["done"]:
                return "waiting"
            if job and job["key"] == path_key:
                leg.pop("path_job", None)
                path = job["path"]
                if len(path) <= 1:
                    return recalculate("no_gat_path")
                closest = min(range(len(path)), key=lambda idx: math.dist(position, path[idx]))
                if (math.dist(position, path[closest]) > 3
                        or any(cell in blocked for cell in path[closest:closest + 4] if cell != start)):
                    return recalculate("no_clickable_step")
                leg["path"] = path
                leg["path_key"] = path_key
                leg["path_target"] = path_target
                leg["nav_idx"] = closest
                leg["nav_wp_dist"] = math.dist(position, path[closest])
                leg["nav_wp_ts"] = now
                self._set_locked_nav_path(path, (snapshot.map, target), position, now)
                self._active_nav_idx = closest
            else:
                if not portal:
                    radius = int(approach_range)
                    goals = sorted(
                        (cell for cell in self._patrol_cells_in_radius(*target, radius)
                         if cell != target and cell != start and cell not in blocked
                         and cell not in failed_points and cell not in visited
                         and (reposition is None or math.dist(cell, reposition) >= 1.5)
                         and gat.is_walkable(*cell)),
                        key=lambda cell: (math.dist(start, cell), math.dist(target, cell)),
                    )
                    if reposition is not None:
                        occupied = self._entity_occupied_cells(snapshot.map)
                        goals = [cell for cell in goals if cell not in occupied and resolve(cell) is not None]
                    elif near_kafra:
                        occupied = self._entity_occupied_cells(snapshot.map)
                        goals = [cell for cell in goals if cell not in occupied]
                        goals.sort(key=lambda cell: resolve(cell) is None)
                    approach_goals = goals
                job = {"key": path_key, "done": False, "path": []}
                leg["path_job"] = job
                avoided = (*visited, *failed_points, *leg.get("avoid_path_cells", ()))

                def find_path():
                    try:
                        costs = {cell: 30 for cell in (avoided if detour else failed_points)}
                        if portal:
                            radius = 2
                            candidates = sorted(
                                (cell for cell in self._patrol_cells_in_radius(*target, radius)
                                 if cell != start and cell not in blocked and gat.is_walkable(*cell)),
                                key=lambda cell: (cell != target, math.dist(start, cell)),
                            )
                        else:
                            candidates = approach_goals
                        for goal in candidates[:5 if portal else 8]:
                            candidate = gat.find_path(
                                start, goal, blocked_cells=blocked, penalty_cells=penalties, cell_costs=costs,
                                max_iterations=40000 if detour else 25000,
                            )
                            if candidate and candidate[-1] == goal:
                                job["path"] = candidate
                                break
                    except (OSError, ValueError) as exc:
                        print(f"[Viagem] Erro ao calcular caminho em {snapshot.map}: {exc}", flush=True)
                    finally:
                        job["done"] = True

                threading.Thread(target=find_path, daemon=True, name="travel-path").start()
                return "waiting"

        nav_idx = min(leg.get("nav_idx", 0), len(path) - 1)
        self._active_nav_path = list(path)
        self._active_nav_idx = nav_idx
        self.active_nav_path = list(path[nav_idx:])
        self.active_nav_color = "#22D3EE"
        local_path = path[nav_idx:nav_idx + 16]
        arrival = world.last_map_arrival
        if (ready_ts and now - ready_ts <= 10.0 and arrival and arrival[0] == snapshot.map
                and math.dist(position, arrival[1]) <= 8.0):
            spawn_step = self._pick_long_stride_cell(local_path, 5.0, 11.0,
                                                     position=position, ground_point=ground_point)
            if spawn_step:
                return click(*spawn_step)
        failed = leg.get("failed_walk_step")
        min_stride = max(4.0, leg.get("retry_stride_dist", 0.0) - 1.5)
        if failed:
            retry = self._pick_walk_retry(
                local_path, position, failed[0], ground_point, previous_point=failed[1], min_dist=min_stride,
            )
            if retry:
                return click(*retry)
        stride = self._pick_walk_stride(local_path, position, blocked, ground_point, min_dist=min_stride)
        if stride is None and near_kafra:
            stride = self._pick_walk_stride(local_path, position, blocked, ground_point)
        if stride is None and reposition is None and "retry_stride_dist" not in leg:
            stride = self._pick_walk_stride(local_path, position, blocked, ground_point, min_dist=1.0)
        if stride:
            return click(*stride)
        return recalculate("no_clickable_step")

    def _execute_travel_kafra(self, snapshot, world, leg, step, position, now):
        target = step["kafra_pos"]
        location = step.get("kafra_location", {"x": target[0], "y": target[1]})
        failed_kafras = leg.setdefault("failed_kafras", set())
        with world.lock:
            dialog = world._npc_dialog_snapshot()
            zeny = world.player_zeny if world.zeny_known else None
        if (not dialog.get("open") and "npc_click_at" not in leg
                and now >= leg.get("recheck_kafra_at", 0.0)
                and math.dist(position, target) > 18.0):
            leg["recheck_kafra_at"] = now + 2.5
            alt = self._select_best_kafra_location(world, snapshot.map, position, excluded=failed_kafras)
            if alt is not None:
                alt_pos = (alt["x"], alt["y"])
                alt_dist = math.dist(position, alt_pos)
                if alt_pos != target and (alt_dist <= 15.0 or alt_dist + 12.0 < math.dist(position, target)):
                    step["kafra_pos"] = alt_pos
                    step["kafra_location"] = alt
                    target = alt_pos
                    location = alt
                    leg.pop("path", None)
                    leg.pop("path_job", None)
                    self.active_nav_path = None
                    self._clear_locked_nav_path()
                    print(f"[Kafra] Reavaliando Kafra mais próxima para teleporte em {snapshot.map}: {alt_pos}.", flush=True)
        if zeny is not None and zeny < step["cost"]:
            return "Saldo insuficiente para o teleporte."

        if action := leg.get("menu_action"):
            if not action.done():
                return None
            leg.pop("menu_action")
            if (sent_at := action.result()) is None:
                self._send_game_key(0x1B)
                return "Seleção de teleporte interrompida antes da confirmação."
            leg["dialog_wait_until"] = sent_at + 1.5
            leg["next_dialog_at"] = sent_at + 4.5
            leg["menu_select_cooldown_until"] = max(leg.get("menu_select_cooldown_until", 0.0), sent_at + 1.5)
        if not self._kafra_dialog_ready(leg, dialog, world, leg, now):
            return None
        plan = self.travel_plan
        def active():
            return self.travel_plan is plan and plan.get("leg") is leg

        matches = self._dialog_matches_target_kafra(world, dialog, leg, snapshot.map, location)
        if dialog["dialog_has_close"] and matches:
            if dialog.get("seq") != leg.get("handled_seq"):
                self._send_kafra_next(leg, dialog, self._dialog_action_guard(dialog, active))
                return None
        if dialog.get("open"):
            if not matches:
                if self._dismiss_settings_window(now, force_check=True):
                    return None
                if now >= leg.get("next_dialog_at", 0.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 2.0
                return None
            seq = dialog.get("seq")
            if seq == leg.get("handled_seq"):
                if now >= leg.get("next_dialog_at", now + 1.0):
                    self._send_game_key(0x1B)
                    return "O diálogo da Kafra não avançou."
                return None
            allowed = self._dialog_action_guard(dialog, active)
            if options := self._kafra_menu_options(leg, dialog):
                selecting_service = not leg.get("teleport_selected")
                raw = dialog["dialog_menu_raw_options"]
                if selecting_service:
                    normalized = [_normalize_menu_text(option) for option in options]
                    index = self._kafra_keyword_index(normalized, ("teleporte", "teleport", "warp", "teletransporte"),
                                                      raw=raw, token="0ma0AQ", skip_cancel=True)
                    if index is None:
                        index = self._kafra_fallback_menu_index(options, raw=raw, action="teleport", submenu=False)
                else:
                    _, entry = get_kafra_network_map_entry(snapshot.map)
                    index = _kafra_destination_index(options, raw, step["to_city"], entry)
                if index is None:
                    unmatched = leg.get("unmatched_menu_retries", 0) + 1
                    leg["unmatched_menu_retries"] = unmatched
                    if unmatched <= 4:
                        if dialog["dialog_has_next"] or dialog["dialog_has_close"]:
                            self._send_kafra_next(leg, dialog, allowed)
                        else:
                            leg["next_dialog_at"] = now + 0.5
                        print(f"[Kafra] Opções 0x00B7 sem correspondência ({options}); aguardando/revalidando ({unmatched}/4).", flush=True)
                        return None
                    failed_kafras.add(target)
                    alt = self._select_best_kafra_location(world, snapshot.map, position, excluded=failed_kafras)
                    if alt is not None and (alt["x"], alt["y"]) != target:
                        self._send_game_key(0x1B)
                        step["kafra_pos"] = (alt["x"], alt["y"])
                        step["kafra_location"] = alt
                        leg.clear()
                        leg.update({"deadline": now + 120, "last_tick": now, "blocked": {}, "failed_kafras": failed_kafras})
                        print(f"[Kafra] Menu não reconhecido; reavaliando outra Kafra em {step['kafra_pos']}.", flush=True)
                        return None
                    self._send_game_key(0x1B)
                    if selecting_service:
                        return "A Kafra não ofereceu a opção Teleporte."
                    return f"Não foi possível identificar {step['to_city']} no menu da Kafra."
                leg["unmatched_menu_retries"] = 0
                if self._try_select_kafra_menu(leg, dialog, options, index, allowed, now) and selecting_service:
                    leg["teleport_selected"] = True
            elif dialog["dialog_has_next"]:
                self._send_kafra_next(leg, dialog, allowed)
            return None

        status = self._approach_registered_kafra(
            snapshot, world, leg, location, position, now, self.get_game_hwnd(),
        )
        if status == "missing_gat":
            return f"Terreno .gat de {snapshot.map} indisponível."
        if status in ("movement_unconfirmed", "no_clickable_step", "no_gat_path"):
            failed_kafras.add(target)
            alt = self._select_best_kafra_location(world, snapshot.map, position, excluded=failed_kafras)
            if alt is not None and (alt["x"], alt["y"]) != target:
                step["kafra_pos"] = (alt["x"], alt["y"])
                step["kafra_location"] = alt
                leg.pop("path", None)
                leg.pop("path_job", None)
                leg["walk_failures"] = 0
                leg["detour_attempts"] = 0
                self.active_nav_path = None
                self._clear_locked_nav_path()
                print(f"[Kafra] Rota até Kafra de teleporte falhou ({status}); redirecionando para {step['kafra_pos']}.", flush=True)
                return None
        return self._travel_walk_error(status, target)

    @staticmethod
    def _travel_walk_error(status, target):
        return {
            "no_gat_path": f"O .gat não encontrou caminho até {target}.",
            "no_clickable_step": f"Sem ponto de clique seguro para continuar rumo a {target}.",
            "movement_unconfirmed": f"O deslocamento rumo a {target} não foi confirmado.",
            "portal_unconfirmed": f"O servidor não confirmou a travessia do portal em {target}.",
        }.get(status)

    def _handle_travel_plan(self, snapshot, px, py):
        plan = self.travel_plan
        if not plan or px is None or not self._game_is_foreground():
            return
        if closing := plan.get("world_map_close"):
            if not closing.done():
                return
            sent_at = closing.result()
            if sent_at is None:
                self._finish_travel_plan(plan, "O mapa-múndi não pôde ser fechado.")
                return
            if time.monotonic() < sent_at + 0.2:
                return
            plan.pop("world_map_close")
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

        if not portal_step:
            plan.setdefault("locked_kafras", {}).setdefault(
                travel_map_group(source), step["kafra_pos"],
            )
        now = time.monotonic()
        if "leg" not in plan:
            plan["leg"] = {"deadline": now + 120, "last_tick": now, "blocked": {}}
            cell = (step["wx"], step["wy"]) if portal_step else step["kafra_pos"]
            if portal_step:
                print(f"[Viagem] Portal em {current_map} {cell} → {destination}.", flush=True)
            else:
                print(f"[Kafra] Caminhando até {current_map} {cell} para teleportar a {destination}.", flush=True)
        leg = plan["leg"]
        leg["deadline"] += max(0.0, now - leg["last_tick"] - 1.0)
        leg["last_tick"] = now
        if now >= leg["deadline"]:
            failure = f"Prazo da etapa para {destination} esgotado."
        elif portal_step:
            if world.npc_dialog.get("open") and not world.storage_open:
                if self._dismiss_settings_window(now, force_check=True):
                    return
                if now >= leg.get("next_dialog_at", 0.0) and self._send_game_key(0x1B):
                    leg["next_dialog_at"] = now + 2.0
                return
            target = step["wx"], step["wy"]
            observed = self._visible_portal_cells(current_map)
            target = min((cell for cell in observed if math.dist(cell, target) <= 6),
                         key=lambda cell: math.dist(cell, target), default=target)
            self._travel_allowed_portal = target
            status = self._walk_travel_leg(snapshot, world, leg, position, target, portal=target)
            failure = (f"Terreno .gat de {current_map} indisponível." if status == "missing_gat"
                       else self._travel_walk_error(status, target))
        else:
            failure = self._execute_travel_kafra(snapshot, world, leg, step, position, now)
        if failure:
            self._replan_travel(plan, world, current_map, position, failure, failed_step=step)

    def _on_game_lbutton_down(self, screen_x, screen_y, ctrl):
        if (not (ctrl or self._world_map_open) or self._automation_click_active()
                or self._hud_edit is not None or self._is_point_in_hud(screen_x, screen_y, margin=0)):
            return False
        viewport = self.feed.viewport()
        if not viewport:
            return False
        vx, vy, vw, vh = viewport
        if self._world_map_open:
            rx, ry = (screen_x - vx) / vw, (screen_y - vy) / vh
            if 0.15 <= rx <= 0.85 and 0.12 <= ry <= 0.88:
                u = max(0.0, min(1.0, (rx - 0.15) / 0.70))
                v = max(0.0, min(1.0, (ry - 0.12) / 0.76))
                candidates = self._get_world_map_candidates(u, v, region_filter=self._world_map_region)
                if not candidates:
                    msg = f"Nenhum mapa mapeado em ({u:.3f}, {v:.3f})"
                    print(f"[WorldMap] {msg}", flush=True)
                    self._show_world_map_notice(msg)
                    return False
                self._world_map_candidates = candidates
                entry = candidates[0]
                if getattr(self, "_world_map_hud_target_var", None) is not None:
                    self._world_map_hud_target_var.set(entry["map_name"])
                if getattr(self, "_refresh_world_map_candidates_ui", None) is not None:
                    self.root.after(0, self._refresh_world_map_candidates_ui)
                if ctrl:
                    if len(candidates) > 1:
                        others = ", ".join(c["map_name"] for c in candidates[1:4])
                        print(f"[WorldMap] Priorizando {entry['map_name']} (outros próximos: {others})", flush=True)
                    self._start_world_map_travel(entry["map_name"], "travel")
                    return True
                self._show_world_map_popup(entry, screen_x, screen_y, candidates=candidates)
            return False

        mm_x1, mm_y1 = vx + vw - 146, vy + 18
        if (self.minimap_nav and mm_x1 <= screen_x < mm_x1 + 128
                and mm_y1 <= screen_y < mm_y1 + 128):
            self._start_minimap_route((screen_x - mm_x1) / 128.0, (screen_y - mm_y1) / 128.0)
            return True
        return False

    def _start_minimap_route(self, rx, ry):
        world = self.feed.world
        map_id, player, _ = world.snapshot()
        if world.is_map_loading or not map_id or player is None or self._kafra_cycle:
            self._click_debug("PATROL bloqueado: minimapa indisponível durante a transição ou o ciclo da Kafra.")
            return
        gat = world.get_gat()
        if gat is None:
            self._click_debug(f"PATROL bloqueado: terreno .gat de {map_id} indisponível.")
            return
        raw = int(rx * gat.width), int((1.0 - ry) * gat.height)
        self._minimap_path_request = {"map": map_id, "gat": gat, "raw": raw, "result": None}

    def _tick_minimap_route(self, map_id, player):
        request = self._minimap_path_request
        if request is None:
            return False
        if map_id != request["map"] or player is None or self._kafra_cycle:
            self._minimap_path_request = None
            return False

        gat, raw = request["gat"], request["raw"]
        start = tuple(map(round, player.cell()))
        if request["result"] is None:
            if self._minimap_path_worker is not None and self._minimap_path_worker.is_alive():
                return True
            blocked, penalties = self._navigation_obstacles(map_id, gat, block_portals=True)

            def plan():
                path = []
                try:
                    goal = self._resolve_walkable_cell(gat, *raw, max_radius=15, forbidden_cells=blocked)
                    if goal is not None:
                        path = self._find_minimap_path(gat, start, goal, blocked, penalties)
                    if goal is not None and not path:
                        path = gat.find_path(
                            start, raw, blocked_cells=blocked, penalty_cells=penalties,
                            goal_test=lambda cell: cell != start and math.dist(cell, raw) <= 5,
                            goal_radius=5, max_iterations=max(7000, gat.width * gat.height),
                        )
                except (OSError, ValueError) as exc:
                    print(f"[Minimapa] Falha ao planejar rota em {map_id}: {exc}", flush=True)
                finally:
                    request["result"] = path

            worker = threading.Thread(target=plan, name="minimap-path", daemon=True)
            try:
                worker.start()
            except RuntimeError as exc:
                self._minimap_path_request = None
                print(f"[Minimapa] Falha ao iniciar o planejamento: {exc}", flush=True)
                return False
            self._minimap_path_worker = worker
            return True

        self._minimap_path_request = None
        path = request["result"]
        if not path:
            self._click_debug(f"PATROL bloqueado: minimapa sem rota caminhável até {raw}.")
            return False
        goal = path[-1]
        if start in path:
            path = path[path.index(start):]
        if self.travel_plan:
            self._reset_travel_leg(self.travel_plan)
            self.travel_plan = None
        if self.farm_map and self.farm_map != map_id:
            self._handle_map_change(map_id)
        self._clear_patrol_route()
        self._reset_patrol(*start)
        self._city_travel_state = None
        self._minimap_goal = self.manual_nav_goal = self.nav_goal = (map_id, *goal)
        self.current_waypoint = goal
        self.current_patrol_path = path
        self._set_locked_nav_path(path, (map_id, goal), player.cell(), time.monotonic())
        self.active_nav_color = "#A78BFA"
        self._resume_hud_automation()
        print(f"[Minimapa] {map_id} {start} → {goal}.", flush=True)
        return True

    @staticmethod
    def _find_minimap_path(gat, start, goal, blocked, penalties):
        if goal in blocked or not gat.is_walkable(*goal):
            return []
        path = gat.find_path(start, goal, blocked_cells=blocked, penalty_cells=penalties,
                             max_iterations=max(7000, gat.width * gat.height))
        return path if path and path[-1] == goal else []

    def _start_world_map_travel(self, target, mode):
        self._close_world_map_popup()
        if not self._hud_travel(target, mode):
            return
        completion = Future()
        if self._send_game_key(0x1B, restore_focus=True, completion=completion):
            self._world_map_open = False
            if self.travel_plan:
                self.travel_plan["world_map_close"] = completion
        elif self.travel_plan:
            self._finish_travel_plan(self.travel_plan, "O mapa-múndi não pôde ser fechado.")

    def _get_world_map_region_entries(self, region_filter=None):
        entries = self.world_map_regions_data.get("maps", [])
        reg = region_filter or getattr(self, "_world_map_region", "all") or "all"
        if reg == "all":
            return entries
        if reg == "dungeons_submaps":
            return [e for e in entries if e.get("region") == "dungeons_submaps"
                    or e.get("layer") in ("dungeon", "factory_lab", "submap")]
        return [e for e in entries if e.get("region") == reg]

    def _get_world_map_candidates(self, rx, ry, region_filter=None):
        reg = region_filter or getattr(self, "_world_map_region", "all") or "all"
        entries = self._get_world_map_region_entries(reg)
        if not entries:
            return []
        rx = max(0.0, min(1.0, float(rx)))
        ry = max(0.0, min(1.0, float(ry)))
        matches = []
        for entry in entries:
            ex = float(entry.get("rx", 0.5))
            ey = float(entry.get("ry", 0.5))
            rad = float(entry.get("radius", 0.05))
            dist = math.hypot(rx - ex, ry - ey)
            if dist <= rad:
                matches.append((dist, entry))
        if not matches:
            return []
        if reg == "dungeons_submaps":
            layer_rank = {"dungeon": 0, "factory_lab": 1, "submap": 2, "field": 3, "city": 4}
        else:
            layer_rank = {"city": 0, "field": 1, "factory_lab": 2, "dungeon": 3, "submap": 4}
        matches.sort(key=lambda item: (layer_rank.get(item[1].get("layer", "field"), 5), item[0]))
        return [entry for _, entry in matches]

    def _get_world_map_entry(self, u, v):
        candidates = self._get_world_map_candidates(u, v, region_filter=getattr(self, "_world_map_region", "all"))
        return candidates[0] if candidates else None

    def _show_world_map_notice(self, message, duration=3.0):
        self._world_map_notice = (str(message), time.monotonic() + duration)
        if getattr(self, "_world_map_hud_notice_var", None) is not None:
            self._world_map_hud_notice_var.set(str(message))

    def _show_world_map_popup(self, entry, sx, sy, candidates=None):
        self._close_world_map_popup()
        popup = tk.Toplevel(self.root)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True, "-toolwindow", True)
        popup.configure(bg="#0F172A", highlightthickness=1, highlightbackground="#3B82F6")

        px = max(10, min(sx + 12, self.root.winfo_screenwidth() - 250))
        py = max(10, min(sy + 12, self.root.winfo_screenheight() - 160))
        popup.geometry(f"+{px}+{py}")

        pad_f = tk.Frame(popup, bg="#0F172A", padx=8, pady=6)
        pad_f.pack()

        selected_entry = [entry]
        title_var = tk.StringVar(value=entry.get("label") or entry.get("map_name", ""))
        mobs_var = tk.StringVar(value=f"Monstros: {entry['mobs']}" if entry.get("mobs") else "")

        head_f = tk.Frame(pad_f, bg="#0F172A")
        head_f.pack(fill="x", pady=(0, 2))
        tk.Label(head_f, textvariable=title_var,
                 bg="#0F172A", fg="#F8FAFC", font=("Segoe UI", 8, "bold")).pack(side="left")
        btn_close = tk.Label(head_f, text="✕", bg="#0F172A", fg="#94A3B8", font=("Segoe UI", 8), cursor="hand2")
        btn_close.pack(side="right")
        btn_close.bind("<Button-1>", lambda _e: self._close_world_map_popup())

        tk.Label(pad_f, textvariable=mobs_var, bg="#0F172A", fg="#94A3B8",
                 font=("Segoe UI", 7), wraplength=220, justify="left").pack(anchor="w", pady=(0, 4))

        if candidates and len(candidates) > 1:
            cand_f = tk.Frame(pad_f, bg="#0F172A")
            cand_f.pack(fill="x", pady=(0, 4))
            for cand in candidates[:4]:
                lbl = cand.get("label") or cand["map_name"]
                short = lbl if len(lbl) <= 14 else cand["map_name"]
                tk.Button(
                    cand_f, text=short, bg="#1E293B", fg="#E2E8F0", font=("Segoe UI", 7),
                    relief="flat", cursor="hand2", padx=4, pady=1,
                    command=lambda c=cand: (
                        selected_entry.__setitem__(0, c),
                        title_var.set(c.get("label") or c.get("map_name", "")),
                        mobs_var.set(f"Monstros: {c['mobs']}" if c.get("mobs") else ""),
                    ),
                ).pack(side="left", padx=(0, 2))

        btns_f = tk.Frame(pad_f, bg="#0F172A")
        btns_f.pack(fill="x", pady=2)

        def _on_travel():
            self._start_world_map_travel(selected_entry[0]["map_name"], "travel")

        def _on_farm():
            self._start_world_map_travel(selected_entry[0]["map_name"], "farm")

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
        started = time.perf_counter()
        try:
            self._refresh_frame()
        except Exception:
            log_overlay_exception("REFRESH")
        finally:
            target_ms = 16 if self._hud_render_foreground else 40
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.window.after(max(1, target_ms - elapsed_ms), self.refresh)

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
                self._learn_accidental_warp(snapshot)
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
        elif self._hud_can_run() and (self.auto_patrol or self.nav_goal):
            self._click_debug("PATROL bloqueado: janela do Ragexe sem foco ou sem dados para projeção.")

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

    def _is_immediate_loot_threat(self, world, gid, mob, position, now):
        return (
            (gid == world.last_player_attacker_gid and now - world.last_player_aggro_ts <= 2.5)
            or (mob.is_aggro and now - mob.last_aggro_ts <= 2.5)
            or math.dist(position, mob.track.cell()) <= 2.5
        )

    def _reset_survival(self):
        self._survival_state = None
        self._sit_attempts = 0
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._retreat_started_at = 0.0
        self._retreat_safe_since = None
        self._survival_next_action = 0.0
        self._retreat_click = None
        self._retreat_retry_step = None
        self._last_retreat_target = None
        self._retreat_goal = None
        self._retreat_portal_attempts = {}

    def _clear_recovery_navigation(self):
        self._clear_patrol_route()
        self.manual_nav_goal = None
        self._city_travel_state = None
        self._travel_allowed_portal = None
        self._last_patrol_click_target = None
        self._finish_farm_lock("automação interrompida")
        self.locked_combat_mob_gid = None
        self._locked_drop_gid = None
        self._drop_lock_until = 0.0
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
        return self._visible_portal_cells(current_map) | self._registered_portal_cells(current_map)

    def _can_auto_sit(self):
        world = self.feed.world
        now = time.monotonic()
        if (not self.auto_farm or not self._hud_can_run() or self._has_hud_travel()
                or self.manual_nav_goal or self._kafra_cycle or self._kafra_manual
                or self._pending_hud_interaction or self._survival_state != SURVIVAL_RECOVER
                or world.is_map_loading or world.player_is_dead or world.player_is_sitting
                or world.npc_dialog_active or world.npc_dialog["open"]
                or world._is_player_walking() or self._automation_click_active()
                or (self._locked_drop_gid is not None and now < self._drop_lock_until)
                or (self._engage_state and self._engage_state["kind"] == "drop")):
            return False
        _, player, entities = world.snapshot()
        if player is None:
            return False
        position = player.cell()
        return not any(
            kind == "mob" and math.dist(position, mob.track.cell()) <= 8.0
            and (mob.is_hostile_now or self._is_mob_hostile(world, mob)
                 or self._is_active_threat(world, gid, mob, position, now))
            for (kind, gid), mob in entities
        )

    def _ensure_sitting(self, should_sit: bool):
        if self._automation_click_active() or self.feed.world.player_is_sitting == should_sit:
            return
        now = time.monotonic()
        if self._sit_confirmation or now - self._last_sit_attempt_at < (1.2 if should_sit else 2.0):
            return
        if should_sit and (self._sit_attempts >= 3 or not self._can_auto_sit()):
            return
        completion = Future()
        allowed = self._can_auto_sit if should_sit else None
        if self._send_game_key(0x2D, completion=completion, allowed=allowed):
            self._last_sit_attempt_at = now
            if should_sit:
                self._sit_attempts += 1
            self._sit_confirmation = (should_sit, completion)

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
                    or not self._has_gat_line_of_sight(*player_cell, *cell, gat)
                    or self._segment_hits_obstacle(*player_cell, *cell, blocked)):
                return None
            point = resolve(cell)
            if point and not any(abs(point[0] - mx) <= 55 and my - 75 <= point[1] <= my + 25
                                 for mx, my in monster_centers):
                return point
            return None

        stride = None
        retry = self._retreat_retry_step
        if retry and retry[0] == snapshot.map and (point := ground_point(retry[1])):
            stride = retry[1], point
        if (stride is None and portal and tuple(map(round, player_cell)) != portal
                and math.dist(player_cell, portal) <= 4 and (point := ground_point(portal))):
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
                    current_mobs = [ent.track.cell() for (kind, _), ent in world.entity_items() if kind == "mob"]
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

            hover_reset = retry == (snapshot.map, cell)
            if self._navigation_click(snapshot, gat, cell, point, target_point=safe_retreat_point,
                                      portal=portal, retry_key=(snapshot.map, "ground", cell),
                                      hover_reset=hover_reset):
                self._retreat_retry_step = None
                self._last_retreat_target = cell
                self._retreat_click = (now, player_cell, cell, point, portal == cell, hover_reset)
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
                if cycle["phase"] in ("dialog", "closing"):
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
            self._retreat_retry_step = None
            self._last_retreat_target = None

        if self._survival_state == SURVIVAL_RECOVER:
            self.active_nav_path = None
            if not walking:
                if hp_pct >= cfg["survival_resume_pct"]:
                    self._survival_state = SURVIVAL_RETURN
                    self._survival_next_action = 0.0
                elif not world.player_is_sitting:
                    self._ensure_sitting(True)
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
            clicked_at, previous, clicked_cell, point, is_portal, retried = self._retreat_click
            if is_portal:
                elapsed = now - self._touch_click_finished_at(point, clicked_at)
                if not self._touch_click_aborted(point, clicked_at) and (walking or elapsed < 1.6):
                    return True
                key = current_map, clicked_cell
                self._retreat_portal_attempts[key] = self._retreat_portal_attempts.get(key, 0) + 1
                if not retried:
                    self._retreat_retry_step = key
            else:
                result = self._walk_step_result(world, player_cell, previous, point, clicked_at, now)
                if result == "waiting":
                    return True
                if result == "stalled":
                    if retried:
                        self.temp_blocked_cells[clicked_cell] = now + 12.0
                    else:
                        self._retreat_retry_step = current_map, clicked_cell
            self._retreat_click = None
        if walking:
            self._retreat_safe_since = None
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
        blocked, penalties = self._navigation_obstacles(current_map, gat)
        blocked |= mob_cells
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
                barriers, portal_penalties = self._navigation_obstacles(current_map, gat, portal=goal)
                barriers |= mob_cells | (portals - {goal})
                if start == goal:
                    # Reentrar na célula é necessário se o personagem já chegou sobre o portal.
                    for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2), (1, 0), (-1, 0), (0, 1), (0, -1)):
                        step = start[0] + dx, start[1] + dy
                        if step not in barriers and gat.is_walkable(*step):
                            self._move_on_retreat_path(snapshot, gat, player_cell,
                                                       [start, step], mobs, portal=goal, blocked=barriers)
                            if self._retreat_click:
                                break
                    return True
                path = gat.find_path(start, goal, blocked_cells=barriers, penalty_cells=portal_penalties, cell_costs=costs)
                if path and path[-1] == goal:
                    self._move_on_retreat_path(snapshot, gat, player_cell, path, mobs, portal=goal, blocked=barriers)
                    return True
            return True

        def clearance(cell):
            return min((math.dist(cell, mob) for mob in mobs), default=math.inf)

        goal = self._retreat_goal
        if goal and goal not in blocked and clearance(goal) >= 11:
            path = gat.find_path(start, goal, blocked_cells=blocked, penalty_cells=penalties, cell_costs=costs)
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

        gat.find_path(start, None, blocked_cells=blocked, penalty_cells=penalties, cell_costs=costs, goal_test=consider)
        if best:
            self._retreat_goal = best[1]
            path = gat.find_path(start, best[1], blocked_cells=blocked, penalty_cells=penalties, cell_costs=costs)
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
                or self._is_pin_entry_active()
                or getattr(world, "storage_open", False)
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
                        if npc := world.entity("npc", closest[1]):
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
        pending = getattr(self, "_npc_dismissal_key", None)
        if pending is None or pending[0] != stamp:
            key = keys[0]
            if key == 0x1B and self._dismiss_settings_window(now, force_check=True):
                self._npc_dismissal = None
                return True
            completion = Future()

            def allowed():
                return (self._hud_can_run() and world.map == current_map
                        and world.npc_dialog.get("seq") == dialog.get("seq"))

            if self._send_game_key(key, completion=completion, allowed=allowed):
                self._npc_dismissal_key = stamp, completion
            else:
                self._npc_dismissal = stamp, keys, now + 0.25
            return True
        completion = pending[1]
        if not completion.done():
            return True
        self._npc_dismissal_key = None
        if completion.result() is None:
            self._npc_dismissal = stamp, keys, now + 0.5
            return True
        keys.pop(0)
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

    def _get_ui_vision(self):
        cycle = getattr(self, "_kafra_cycle", None)
        if cycle and cycle.get("vision"):
            return cycle["vision"]
        vision = getattr(self, "_vending_vision", None)
        if vision is None:
            try:
                vision = KafraVision()
            except (ImportError, OSError, ValueError):
                vision = False
            self._vending_vision = vision
        return vision or None

    def _is_pin_entry_active(self) -> bool:
        now = time.monotonic()
        cycle = getattr(self, "_kafra_cycle", None)
        if cycle is not None and cycle.get("phase") == "opening":
            if cycle.get("pin_seq") is not None or "pin_seen_at" in cycle:
                return True
            if (ok_at := cycle.get("pin_ok_sent_at")) and now - ok_at < 2.5:
                return True
        if (getattr(self, "_pin_dialog_screen_rect", None) is not None
                and now < getattr(self, "_pin_hud_suppress_until", 0.0)):
            return True
        return bool(getattr(self, "_pin_overlay_visible_cache", False))

    def _is_pin_dialog_visible(self) -> bool:
        if (getattr(self, "_pin_dialog_screen_rect", None) is not None
                and time.monotonic() < getattr(self, "_pin_hud_suppress_until", 0.0)):
            return True
        viewport = getattr(self, "current_viewport", None)
        if not viewport or not self._game_is_foreground():
            return False
        vision = self._get_ui_vision()
        if not vision:
            return False
        try:
            return vision.has_pin_dialog(vision.capture(viewport))
        except (OSError, ValueError):
            return False

    def _schedule_pin_overlay_check(self, viewport, now: float, *, debug=False) -> bool:
        job = getattr(self, "_pin_overlay_job", None)
        if job and not job["done"]:
            return getattr(self, "_pin_overlay_visible_cache", False)
        if job and job["done"]:
            self._pin_overlay_visible_cache = job["visible"]
            self._pin_debug_scan = job.get("debug_frame") if debug else None
            self._pin_overlay_job = None
        if now >= getattr(self, "_pin_overlay_check_at", 0.0) and viewport:
            self._pin_overlay_check_at = now + 0.35
            vision = self._get_ui_vision()
            if not vision:
                self._pin_overlay_visible_cache = False
                self._pin_debug_scan = None
                return False
            new_job = {"done": False, "visible": getattr(self, "_pin_overlay_visible_cache", False)}
            self._pin_overlay_job = new_job

            def _check():
                try:
                    if not debug:
                        new_job["visible"] = vision.has_pin_dialog(vision.capture(viewport))
                        return
                    image = vision._grab_viewport(viewport)
                    screen = vision.np.array(image.convert("L"))
                    rect = vision.pin_window_rect(screen)
                    new_job["visible"] = rect is not None
                    if rect is not None:
                        x, y, width, height = rect
                        panel = screen[y:y + height, x:x + width]
                        colors = vision.np.array(image.crop((x, y, x + width, y + height)).convert("RGB"))
                        highlighted, _ = vision.pin_highlight(colors)
                        buttons, ok, _ = vision.pin_controls(panel, highlighted, partial=True)
                        new_job["debug_frame"] = (viewport, rect, buttons, ok)
                except (OSError, ValueError):
                    new_job["visible"] = False
                finally:
                    new_job["done"] = True

            threading.Thread(target=_check, daemon=True, name="pin-overlay-check").start()
        return getattr(self, "_pin_overlay_visible_cache", False)

    def _is_storage_or_pin_escape_blocked(self, *, allow_storage_escape=False) -> bool:
        if self._is_pin_dialog_visible():
            return True
        if allow_storage_escape:
            return False
        world = getattr(getattr(self, "feed", None), "world", None)
        if world is not None and world.storage_open:
            return True
        cycle = getattr(self, "_kafra_cycle", None)
        if cycle and cycle.get("phase") in ("opening", "store"):
            return True
        return False

    def _dismiss_settings_window(self, now: float, *, force_check: bool = False) -> bool:
        if self._automation_click_active() or not self._game_is_foreground() or not self.current_viewport:
            return False
        if now < getattr(self, "_settings_close_cooldown_until", 0.0):
            return False
        vision = self._get_ui_vision()
        if not vision or vision.ocr is None:
            return False
        job = getattr(self, "_settings_check_job", None)
        if job and not job["done"]:
            return False
        if job and job["done"]:
            self._settings_check_job = None
            is_open, return_point = job["result"]
            if is_open and not self._is_pin_dialog_visible():
                if return_point and not self._is_point_in_hud(*return_point, margin=4):
                    hwnd = self.get_game_hwnd()
                    if self._schedule_game_click(*return_point, hwnd):
                        self._settings_close_cooldown_until = now + 2.0
                        self._settings_check_next_at = now + 2.0
                        print("[UI] Janela de Configurações detectada; clicando em 'Retornar ao jogo'.", flush=True)
                        return True
                if self._send_game_key(0x1B):
                    self._settings_close_cooldown_until = now + 2.5
                    self._settings_check_next_at = now + 2.5
                    print("[UI] Janela de Configurações detectada; enviando Esc único com cooldown.", flush=True)
                    return True
            return False
        if not force_check and now < getattr(self, "_settings_check_next_at", 0.0):
            return False
        if (getattr(self, "_pin_dialog_screen_rect", None) is not None
                and now < getattr(self, "_pin_hud_suppress_until", 0.0)):
            return False
        self._settings_check_next_at = now + 1.5
        viewport = self.current_viewport
        new_job = {"done": False, "result": (False, None)}
        self._settings_check_job = new_job

        def _check_settings():
            try:
                screen = vision.capture(viewport)
                if vision.has_pin_dialog(screen):
                    new_job["result"] = (False, None)
                    return
                new_job["result"] = vision.settings_window_return_button(viewport)
            except (OSError, ValueError):
                new_job["result"] = (False, None)
            finally:
                new_job["done"] = True

        threading.Thread(target=_check_settings, daemon=True, name="settings-ocr-check").start()
        return False

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
                    merchants = (ent for (_, eid), ent in world.entity_items()
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
        self._check_visual_walk()
        patrol_requested = self.auto_patrol or self.nav_goal is not None or self.patrol_target_route is not None
        if self._automation_click_active():
            if patrol_requested:
                self._click_debug("PATROL bloqueado: clique anterior ainda em andamento.")
            return
        touch_released = win32_input.release_pending_touch()
        if self._is_game_focused() and win32_input.recover_touch_mouse_release():
            log_click_file("[Clique] Enviada soltura de recuperação do toque automático.")
            return
        if not touch_released:
            return
        if self._tick_pin_probe(snapshot):
            return
        world = getattr(self.feed, "world", None)
        if (self._minimap_path_request is not None and world is not None and not self._hud_can_run()
                and not world.is_map_loading and not world.player_is_dead and not self._survival_state):
            map_id, player, _ = world.snapshot()
            if self._tick_minimap_route(map_id, player):
                return
        manual_kafra = self._kafra_manual and not self._hud_drawer_paused and self._hud_edit is None
        if not (self._hud_can_run() or manual_kafra):
            if patrol_requested:
                self._click_debug("PATROL bloqueado: automação pausada ou controle do HUD em edição.")
            return
        now = time.monotonic()
        if world is None or world.is_map_loading:
            if patrol_requested:
                self._click_debug("PATROL bloqueado: transição de mapa.")
            return
        curr_m, player, entities = world.snapshot()
        if snapshot.map != curr_m:
            if patrol_requested:
                self._click_debug("PATROL bloqueado: aguardando atualização do mapa.")
            return
        px, py = player.cell() if player else (None, None)
        if patrol_requested and px is None:
            self._click_debug("PATROL bloqueado: aguardando posição do personagem.")
        curr_m = curr_m or getattr(self, "current_map", "")
        self._update_map_stabilization(world, curr_m, now)
        if self._sit_confirmation:
            expected, completion = self._sit_confirmation
            if world.player_is_sitting == expected:
                self._sit_confirmation = None
                if expected:
                    self._sit_attempts = 0
            elif completion.done():
                sent_at = completion.result()
                if sent_at is None:
                    self._sit_confirmation = None
                    if expected:
                        self._sit_attempts = max(0, self._sit_attempts - 1)
                elif now >= sent_at + 1.1:
                    self._sit_confirmation = None
                    self._last_sit_attempt_at = sent_at
                    print("[Sentar] Cliente não confirmou a mudança de postura.", flush=True)
        if self._kafra_cycle and world.player_is_dead:
            self._finish_kafra_storage("ciclo interrompido pela morte do personagem.", curr_m)
        if self._handle_respawn(world, curr_m, now):
            if patrol_requested:
                self._click_debug("PATROL bloqueado: personagem morto ou aguardando respawn.")
            return
        self._update_post_kill(world, curr_m)
        travel_or_kafra = bool(self.travel_plan or self._kafra_cycle or self._city_travel_state)
        if not travel_or_kafra:
            if self.auto_farm or self.auto_loot or self.auto_patrol:
                self._sync_client_farm_target(world, curr_m)
            if (self.auto_farm or self.auto_patrol) and self.farm_map is None and curr_m:
                self.farm_map = curr_m
        hp_pct = 100.0 * world.player_hp / world.player_max_hp if world.player_max_hp > 0 else 100.0
        if self._handle_survival(snapshot, world, player, curr_m, now, hp_pct, entities):
            if patrol_requested:
                self._click_debug("PATROL bloqueado: rotina de sobrevivência em andamento.")
            return
        if self._minimap_path_request is not None and self._tick_minimap_route(curr_m, player):
            return
        if self.travel_plan:
            if self._kafra_cycle:
                self._tick_kafra_storage(snapshot, world, curr_m, px, py, now)
            else:
                self._handle_travel_plan(snapshot, px, py)
            return
        if self._is_pin_entry_active():
            if self._kafra_cycle:
                self._tick_kafra_storage(snapshot, world, curr_m, px, py, now)
            return
        if (self.auto_farm or self.auto_loot or patrol_requested
                or self._kafra_cycle or self._city_travel_state):
            if self._dismiss_settings_window(now):
                return
            if self._dismiss_accidental_npc_dialog(world, player, curr_m, entities, now):
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: diálogo de NPC aberto.")
                return
            if self._dismiss_accidental_vending_window(world, player, now):
                return
        if self._kafra_cycle is None:
            fighting = self._locked_farm_target_id is not None
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

        # Reinicia as rotas do mapa anterior após o carregamento.
        if curr_m and self.manual_nav_goal and curr_m != self.manual_nav_goal[0]:
            self.manual_nav_goal = None
            self._clear_patrol_route()
        if curr_m and self.farm_map and curr_m != self.farm_map and not travel_or_kafra:
            self._handle_map_change(curr_m)
            return

        # ── AUTO-RECUSA DE CONVITES MODAIS (Party / Troca / Amizade) ──
        invite_ts = getattr(getattr(self.feed, "world", None), "last_invite_popup_ts", 0.0)
        if invite_ts > getattr(self, "_last_handled_invite_ts", 0.0):
            self._last_handled_invite_ts = invite_ts
            if not travel_or_kafra and (self.auto_farm or self.auto_patrol):
                self.center_popup_until = now + 3.0
                self._click_debug("CONVITE MODAL DETECTADO (Party/Troca/Amizade): agendando ESC em 220ms para auto-recusa!")
                self._schedule_escape_key(220)

        if self._has_hud_travel():
            if self._city_travel_state:
                self._handle_city_travel(snapshot, px, py)
            if px is not None and (self.nav_goal or self.patrol_target_route):
                self._try_auto_patrol(snapshot, px, py)
            return


        if px is not None and self.auto_patrol:
            self._scan_patrol_area(world, curr_m, (px, py), now)

        if px is not None and self.auto_farm:
            if self._check_skill_cast_lock(world, (px, py), now):
                return
            if self._tick_auto_buff_skills(world, now):
                return

        if px is not None and (self.auto_farm or self.auto_loot or self.auto_patrol):
            position = (px, py)
            drops = self._eligible_drops(snapshot, position, entities, now) if self.auto_loot or self.auto_farm else []
            if self._locked_drop_gid is not None and not any(entry[2] == self._locked_drop_gid for entry in drops):
                self._locked_drop_gid = None
                self._drop_lock_until = 0.0
                if self._engage_state and self._engage_state["kind"] == "drop":
                    self._engage_state = self._engagement = None
                    self.active_nav_path = None
            loot_first = self.auto_loot and bool(drops)
            if loot_first:
                defense_range = max(2.5, getattr(world, "player_attack_range", 1.0))
                threats = [(key, mob) for key, mob in entities
                           if key[0] == "mob" and math.dist(position, mob.track.cell()) <= defense_range
                           and self._is_immediate_loot_threat(world, key[1], mob, position, now)]
                locked_gid = self._locked_farm_target_id
                if locked_gid is not None and all(key[1] != locked_gid for key, _ in threats):
                    self._finish_farm_lock("coleta prioritária")
                if threats and (self._continue_farm_lock(snapshot, position, now, direct_only=True)
                                or self._try_auto_farm(snapshot, position, threats, defense=True,
                                                       direct_only=True, loot_defense=True)):
                    self._reset_patrol(px, py, keep_waypoint=True)
                    return
            elif self._continue_farm_lock(snapshot, position, now):
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: combate em andamento.")
                self._reset_patrol(px, py, keep_waypoint=True)
                return
            if self._continue_loot_lock(snapshot, position, entities, drops, now):
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: coleta em andamento.")
                self._reset_patrol(px, py, keep_waypoint=True)
                return
            if now < self.post_kill_wait_until:
                started = self.post_kill_wait_until - 0.35
                if any(drop.created_at >= started for _, _, _, drop in drops):
                    self.post_kill_wait_until = 0.0
            waiting = now < self.post_kill_wait_until and not any(
                drop.ownership == "MINE" for _, _, _, drop in drops
            )
            walking = world._is_player_walking()
            if loot_first:
                if self._try_auto_loot(snapshot, position, drops, direct_only=walking):
                    if patrol_requested:
                        self._click_debug("PATROL bloqueado: coleta em andamento.")
                    self._reset_patrol(px, py, keep_waypoint=True)
                return

            tier1 = [entry for entry in drops if entry[0] == 1]
            if tier1 and self._try_auto_loot(snapshot, position, tier1, direct_only=walking):
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: coleta em andamento.")
                self._reset_patrol(px, py, keep_waypoint=True)
                return

            if self.auto_farm or self.auto_loot:
                if self._try_auto_farm(snapshot, position, entities, defense=True, direct_only=waiting):
                    if patrol_requested:
                        self._click_debug("PATROL bloqueado: combate em andamento.")
                    self._reset_patrol(px, py, keep_waypoint=True)
                    return
                if not waiting and self._try_auto_loot(snapshot, position, drops, direct_only=walking):
                    if patrol_requested:
                        self._click_debug("PATROL bloqueado: coleta em andamento.")
                    self._reset_patrol(px, py, keep_waypoint=True)
                    return

            if waiting:
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: aguardando itens do último abate.")
                return

        if px is not None and self.auto_farm:
            if self._try_auto_farm(snapshot, (px, py), entities):
                if patrol_requested:
                    self._click_debug("PATROL bloqueado: Auto-Farm se aproximando de um monstro.")
                self._reset_patrol(px, py, keep_waypoint=True)
                return

        # Preserva self._engage_state entre ticks se o alvo ainda existir e estiver acessível
        if self._engage_state:
            curr_target = world.entity(self._engage_state["kind"], self._engage_state["gid"])
            if not curr_target or world.map != snapshot.map:
                self._engage_state = None
                self._engagement = None

        if self.auto_patrol and px is not None:
            self._try_auto_patrol(snapshot, px, py)

    def _update_post_kill(self, world, current_map):
        with world.lock:
            deaths = tuple(world.recent_dead_mobs)
            zone = (current_map, world.last_map_warp_ts)
            gid = self._locked_farm_target_id
            removed_target = world.last_removed_farm_target
            target_missing = gid is not None and world.entity("mob", gid) is None
        if self._engagement_zone != zone:
            changed_map = self._engagement_zone is None or self._engagement_zone[0] != current_map
            self._engagement_zone = zone
            self._finish_farm_lock("troca de mapa")
            if changed_map:
                self.ignored_mobs.clear()
            self._farm_patrol_until = 0.0
            self._locked_drop_gid = None
            self._drop_lock_until = 0.0
            self._engagement = None
            self._engage_state = None
            self._loot_attempts.clear()
            self.post_kill_wait_until = 0.0
            self.last_kill_pos = None
        for dead in deaths:
            if dead["time"] <= self._last_kill_event:
                continue
            self._last_kill_event = dead["time"]
            d_gid = dead.get("gid")
            d_aid = dead.get("aid")
            was_locked = self._locked_farm_target_id is not None and self._locked_farm_target_id in (d_gid, d_aid)
            if was_locked:
                self._finish_farm_lock("morte confirmada (0x0080)", confirmed_death=True)
            if dead["near_player"] or dead["was_target"] or was_locked:
                self.post_kill_wait_until = dead["time"] + 0.35
                self.last_kill_pos = dead["cell"]
        if gid is not None and self._locked_farm_target_id == gid and target_missing:
            same_removal = (removed_target and removed_target[0] == gid
                            and removed_target[1] >= self._farm_lock_started_at)
            vanish_type = removed_target[4] if same_removal else None
            confirmed_death = vanish_type == 1
            reason = (removed_target[2] if same_removal else "alvo removido do feed (0x0080)")
            self._finish_farm_lock(reason, confirmed_death=confirmed_death, resume_patrol=not confirmed_death)
        else:
            self._farm_target_missing_since = None

    @staticmethod
    def _has_walkable_line(p0, p1, gat):
        """Verifica se há linha de caminhada direta sem colisão de paredes entre dois pontos."""
        if not gat:
            return True
        return gat.has_walkable_line(p0, p1)

    @staticmethod
    def _has_gat_line_of_sight(x0, y0, x1, y1, gat):
        return gat is not None and gat.has_walkable_line((x0, y0), (x1, y1))

    def _resolve_walkable_cell(self, gat, target_x, target_y, max_radius=3, forbidden_cells=None):
        """Valida (target_x, target_y) no .gat e resolve em espiral a célula caminhável válida mais próxima."""
        cx, cy = int(round(target_x)), int(round(target_y))
        forbidden = set(forbidden_cells) if forbidden_cells else set()
        if gat is None:
            return None if (cx, cy) in forbidden else (cx, cy)
        if gat.is_walkable(cx, cy) and (cx, cy) not in forbidden:
            return (cx, cy)
        best = None
        best_dist = float("inf")
        for r in range(1, max_radius + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in forbidden:
                        continue
                    if gat.is_walkable(nx, ny):
                        dist = math.hypot(dx, dy)
                        if dist < best_dist:
                            best_dist = dist
                            best = (nx, ny)
            if best is not None:
                return best
        return None

    def _set_locked_nav_path(self, path, goal_key, position, now):
        """Armazena a rota A* travada em self._active_nav_path sem recalcular a cada tick."""
        self._active_nav_path = list(path) if path else []
        self._active_nav_idx = 0
        self._active_nav_goal = goal_key
        if self._active_nav_path:
            self._active_nav_wp_dist = math.dist(position, self._active_nav_path[0])
            self._active_nav_wp_ts = now
            self.active_nav_path = list(self._active_nav_path)
        else:
            self._active_nav_wp_dist = float("inf")
            self._active_nav_wp_ts = 0.0
            self.active_nav_path = None

    def _clear_locked_nav_path(self):
        self._active_nav_path = []
        self._active_nav_idx = 0
        self._active_nav_goal = None
        self._active_nav_wp_dist = float("inf")
        self._active_nav_wp_ts = 0.0
        self.active_nav_path = None

    def _advance_locked_nav_waypoint(self, position, blocked, now, cur_cell):
        """Avança o índice do waypoint quando dist <= 2.5 e detecta bloqueio ou falta de progresso (> 2.2s)."""
        path = self._active_nav_path
        if not path:
            return False
        idx = min(self._active_nav_idx, len(path) - 1)
        end = min(len(path), idx + 16)
        if cur_cell in path[idx:end]:
            closest = path.index(cur_cell, idx, end)
            idx = min(closest + 1, len(path) - 1)
        if idx != self._active_nav_idx:
            self._active_nav_wp_dist = math.dist(position, path[idx])
            self._active_nav_wp_ts = now
        self._active_nav_idx = idx
        wp = path[idx]
        dist_wp = math.dist(position, wp)
        if dist_wp < self._active_nav_wp_dist - 0.1:
            self._active_nav_wp_dist = dist_wp
            self._active_nav_wp_ts = now
        self.active_nav_path = list(path[idx:])
        ahead = path[idx:min(len(path), idx + 8)]
        if any(cell in blocked for cell in ahead if cell != cur_cell):
            return False
        if dist_wp > 4.0 and now - self._active_nav_wp_ts > 4.0:
            return False
        return True

    def _safe_click_resolver(self, snapshot, project, *, allowed_portal=None, allowed_npc=None):
        left, top, width, height = snapshot.viewport
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        camera_ground = getattr(project, "camera_ground", None)
        screen_occluders = (
            gat.build_screen_occluders(snapshot.player_cell, snapshot.viewport, project, radius=14)
            if gat is not None and snapshot.player_cell is not None else ()
        )
        entities = self._navigation_entities(snapshot.map)
        portals, npcs = self._navigation_centers(snapshot.map, entities, allowed_npc)
        portal_keys = {f"{kind}:{gid}": tuple(map(round, ent.track.target))
                       for (kind, gid), ent in entities if ent.is_portal}
        points = []
        npc_boxes = []
        for item in snapshot.objects:
            if item.key in portal_keys or item.kind == "warp":
                if allowed_portal is None or portal_keys.get(item.key) != allowed_portal:
                    points.append((item.u, item.v))
            elif item.kind == "npc" and item.key != f"npc:{allowed_npc}":
                points.append((item.u, item.v))
                au, av = item.get_anchor()
                npc_boxes.append((left + au * width, top + av * height))
        allowed_npc_cells = set()
        for (kind, gid), ent in entities:
            if kind == "npc":
                if gid == allowed_npc:
                    allowed_npc_cells.add(tuple(map(round, ent.track.target)))
                else:
                    fu, fv = project(*ent.track.cell(), 0.0)
                    npc_boxes.append((left + fu * width, top + fv * height))
                    points.append(project(*ent.track.cell(), 5.5))
        ground_portals = []
        for cell in portals:
            if cell == allowed_portal:
                continue
            u, v = project(*cell, 1.2)
            if any(math.dist(cell, ac) <= 2.0 for ac in allowed_npc_cells):
                ground_portals.append((left + u * width, top + v * height))
            else:
                points.append((u, v))
        for cell in npcs:
            fu, fv = project(*cell, 0.0)
            npc_boxes.append((left + fu * width, top + fv * height))
            points.append(project(*cell, 5.5))
        protected = [(left + u * width, top + v * height) for u, v in points]
        npc_tiles = self._npc_zone_cells(npcs)
        sprite_boxes = [
            (left + u * width, top + v * height, kind)
            for (kind, _), ent in entities if kind in ("mob", "drop", "player")
            for u, v in (project(*ent.track.cell(), 0.0),)
            if -0.1 <= u <= 1.1 and -0.1 <= v <= 1.1
        ]
        npc_ground_boxes = []
        for x, y in npcs:
            corners = [project(x + dx, y + dy, 0.0)
                       for dx in (-1.5, 1.5) for dy in (-1.5, 1.5)]
            us, vs = zip(*corners)
            # Reserva 5 px de retentativa e 3 px de tolerância do cursor.
            npc_ground_boxes.append((left + min(us) * width - 8, top + min(vs) * height - 8,
                                     left + max(us) * width + 8, top + max(vs) * height + 8))

        def resolve(cell, uv=None):
            if uv is None:
                tile = tuple(map(round, cell))
                if self.visual_occluded_cells.get((snapshot.map, tile), 0.0) > time.monotonic():
                    return None
                if (gat is not None and camera_ground is not None and tile != allowed_portal
                        and gat.wall_faces_camera(tile, camera_ground)):
                    return None
            # Um clique intencional no sprite usa a proteção projetada dos outros NPCs.
            if (allowed_npc is None or uv is None) and tuple(map(round, cell)) in npc_tiles:
                return None
            u, v = project(*cell) if uv is None else uv
            if not (0 <= u < 1 and 0 <= v < 1):
                return None
            point = (left + int(u * width), top + int(v * height))
            if (self._is_point_in_game_ui(*point) or self._is_point_in_vending_shop(*point)
                    or self._is_point_in_hud(*point, margin=10)):
                return None
            if (uv is None and gat is not None and screen_occluders
                    and gat.is_screen_occluded(screen_occluders, cell, *point, project)):
                return None
            # Inclui 5 px de retentativa e 3 px de tolerância do cursor.
            if any(math.dist(point, center) < 36 for center in protected):
                return None
            if uv is None and any(math.dist(point, center) < 36 for center in ground_portals):
                return None
            px, py = point
            if any(x1 <= px <= x2 and y1 <= py <= y2 for x1, y1, x2, y2 in npc_ground_boxes):
                return None
            if any(abs(px - gx) <= 46 and (gy - 103) <= py <= (gy + 32) for gx, gy in npc_boxes):
                return None
            if uv is None and any(
                abs(px - gx) <= (10 if kind == "drop" else 20)
                and gy - (12 if kind == "drop" else 42) <= py <= gy + 7
                for gx, gy, kind in sprite_boxes
            ):
                return None
            return point

        return resolve

    def _entity_occupied_cells(self, map_name, *, allowed_portal=None, allowed_npc=None):
        """Retorna células ocupadas por monstros, entidades ou itens no mapa atual."""
        occupied = set()
        for (kind, gid), ent in self._navigation_entities(map_name):
            if kind == "npc" and gid == allowed_npc:
                continue
            if ent.is_portal and allowed_portal is not None and tuple(map(round, ent.track.target)) == allowed_portal:
                continue
            occupied.add(tuple(map(round, ent.track.cell())))
            occupied.add(tuple(map(round, ent.track.target)))
        return occupied

    def _is_empty_walkable_cell(self, map_name, gat, cell, blocked=None, *, allowed_portal=None, allowed_npc=None):
        """Confirma que a célula é caminhável no .gat, não está bloqueada e não coincide com entidade."""
        tile = (int(round(cell[0])), int(round(cell[1])))
        if gat is not None and not gat.is_walkable(*tile):
            return False
        if blocked and tile in blocked:
            return False
        return tile not in self._entity_occupied_cells(
            map_name, allowed_portal=allowed_portal, allowed_npc=allowed_npc)

    def _navigation_click(self, snapshot, gat, cell, point, *, hwnd=None, target_point=None,
                          retry_key=None, portal=None, npc=None, ground=True, hover_reset=False,
                          hover_reset_point=None, completion=None, direct_touch=False, kafra=False,
                          pickup_click=False, allow_offsets=None, hover_ms=None, hold_ms=None):
        if self._is_pin_entry_active():
            return False
        if ground and self._visual_walk_probe is not None:
            self._check_visual_walk()
            if self._visual_walk_probe is not None:
                return False
        world = self.feed.world
        cell = tuple(map(round, cell))

        def guarded_aim(lead):
            if self._is_pin_entry_active() or (ground and u32.GetAsyncKeyState(0x11) & 0x8000):
                return None
            with world.lock:
                if (world.map != snapshot.map or world.player is None or world.is_map_loading
                        or self.current_viewport != snapshot.viewport):
                    return None
                if not ground and npc is not None:
                    entity = world.entity("npc", npc)
                    if (entity is None or entity.is_portal
                            or tuple(map(round, entity.track.target)) != tuple(map(round, cell))):
                        return None
                position = world.player.cell()
            if kafra and not ground:
                # Interagir com o sprite só exige alcance e uma mira segura.
                if math.dist(position, cell) > KAFRA_INTERACTION_RANGE:
                    return None
            else:
                blocked, _ = self._navigation_obstacles(snapshot.map, gat, portal=portal, npc=npc)
                if not ground and npc is not None:
                    blocked.discard(cell)
                if self._segment_hits_obstacle(*position, *cell, blocked):
                    return None
                if ground and (gat is None
                               or not self._is_empty_walkable_cell(
                                   snapshot.map, gat, cell, blocked, allowed_portal=portal,
                                   allowed_npc=None if kafra else npc)
                               or not self._has_gat_line_of_sight(*position, *cell, gat)):
                    return None
            current_point = target_point(lead) if target_point else point
            if current_point is None:
                return None
            left, top, width, height = snapshot.viewport
            project = self.feed.camera.projector(snapshot.viewport, getattr(self.feed, "_cam", None) or position,
                                                 gat.get_elevation if gat else flat_height)
            resolve = self._safe_click_resolver(
                snapshot, project, allowed_portal=portal, allowed_npc=npc if kafra or not ground else None)
            if ground:
                return resolve(cell)
            uv = (current_point[0] - left) / width, (current_point[1] - top) / height
            return resolve(cell, uv)

        if guarded_aim(getattr(self, "_sprite_click_lead", 0.085)) is None:
            return False
        if ground:
            with world.lock:
                origin = world.player.cell() if world.player is not None else None
            if origin is None:
                return False
            queued_at = time.monotonic()
        sent = self.patrol_move_and_click(*point, hwnd=hwnd, target_point=guarded_aim,
                                          retry_key=retry_key, allow_offsets=not ground if allow_offsets is None else allow_offsets,
                                          hover_reset=hover_reset, hover_reset_point=hover_reset_point,
                                          completion=completion, direct_touch=direct_touch,
                                          pickup_click=pickup_click, hover_ms=hover_ms, hold_ms=hold_ms)
        if sent and ground:
            expected_path = (gat.find_path(tuple(map(round, origin)), cell, prefer_clearance=False)
                             if gat is not None else None)
            expected_path_len = (len(expected_path) if expected_path
                                 else max(1, int(round(math.dist(origin, cell))) + 1))
            self._visual_walk_probe = {
                "map": snapshot.map, "cell": cell, "point": tuple(map(int, point)),
                "origin": origin, "queued_at": queued_at, "finished_at": None,
                "expected_path_len": expected_path_len,
            }
        if sent and portal is not None:
            self._intentional_portal_click = snapshot.map, time.time() + 4.0
        return sent

    def _navigation_target_step(self, gat, position, target, uv, resolve, blocked, penalty_cells):
        """Retorna o clique no alvo ou um ponto alcançável do caminho até ele."""
        if target in blocked:
            return None
        if not (self._segment_hits_obstacle(*position, *target, blocked)
                or (gat and gat.is_walkable(*target) and not self._has_gat_line_of_sight(*position, *target, gat))):
            point = resolve(target, uv)
            return (target, point, True) if point else None
        if gat is None:
            return None
        path = gat.find_path(tuple(map(round, position)), target,
                             blocked_cells=blocked, penalty_cells=penalty_cells)
        stride = self._pick_walk_stride(
            path, position, blocked, lambda cell: self._walk_ground_point(gat, position, cell, blocked, resolve))
        return (*stride, False) if stride else None

    def _filter_forward_path(self, path, position):
        """Descarta nós iniciais que não aproximam do destino e rejeita células atrás do personagem em rota direta."""
        if not path or position is None:
            return list(path) if path else []
        goal = path[-1]
        player_goal_dist = math.dist(position, goal)
        trimmed = list(path)
        while len(trimmed) > 1 and math.dist(trimmed[0], goal) >= player_goal_dist:
            trimmed.pop(0)
        gx, gy = goal[0] - position[0], goal[1] - position[1]
        if gx == 0 and gy == 0:
            return trimmed
        terrain = getattr(self.feed, "terrain", None)
        world = getattr(self.feed, "world", None)
        gat = terrain.cached_gats.get(world.map) if (terrain and world and world.map) else None
        start = tuple(map(round, position))
        direct_astar = len(trimmed) >= 2 and (gat is None or gat.has_walkable_line(start, goal))
        if not direct_astar:
            return trimmed
        forward = [c for c in trimmed if (c[0] - position[0]) * gx + (c[1] - position[1]) * gy > 0]
        return forward or [goal]

    def _pick_long_stride_cell(self, path_cells, min_dist=9, max_dist=14, *, position=None, ground_point):
        if not path_cells:
            return None
        start = path_cells[0] if position is None else position
        forward_cells = self._filter_forward_path(path_cells, start)
        cells = sorted(forward_cells, key=lambda cell: math.dist(start, cell), reverse=True)
        preferred = [cell for cell in cells if min_dist <= math.dist(start, cell) <= max_dist]
        candidates = [cell for cell in cells if 0 < math.dist(start, cell) <= max_dist]
        return self._select_walk_point(preferred, candidates, start, ground_point)

    def _visual_walk_retry_active(self, now):
        failure = self._last_visual_failure
        return bool(failure and failure[0] == self.feed.world.map and now - failure[3] <= 1.5)

    def _select_walk_point(self, preferred, candidates, position, ground_point):
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(self.feed.world.map) if terrain else None
        if gat is None:
            return next(((cell, point) for cell in preferred if (point := ground_point(cell))), None)

        def clearance(cell):
            return gat.wall_clearance(*cell)

        now = time.monotonic()
        h_player = gat.cell_height_at(*position)
        short_only = self._plateau_short_step_until > now

        def same_plateau_short_cells():
            px, py = int(round(position[0])), int(round(position[1]))
            nearby = [
                c for c in self._patrol_cells_in_radius(px, py, 4)
                if 2.0 <= math.dist(position, c) <= 4.0
                and gat.is_walkable(*c)
                and abs(gat.cell_height_at(*c) - h_player) <= 8.5
            ]
            nearby.sort(key=lambda c: (-clearance(c), math.dist(position, c)))
            return nearby

        if short_only:
            for cell in same_plateau_short_cells():
                if point := ground_point(cell):
                    return cell, point

        retry = self._visual_walk_retry_active(now)
        short = []
        if retry:
            short = sorted((cell for cell in candidates if 2 <= math.dist(position, cell) <= 5),
                           key=clearance, reverse=True)
        ideal = preferred[:1] if preferred and clearance(preferred[0]) >= 2 else []
        alternatives = sorted((cell for cell in candidates if 3 <= math.dist(position, cell) <= 9),
                              key=clearance, reverse=True)
        for cell in dict.fromkeys([*short, *ideal, *alternatives, *preferred]):
            if point := ground_point(cell):
                return cell, point
        for cell in same_plateau_short_cells():
            if point := ground_point(cell):
                return cell, point
        return None

    def _check_visual_walk(self):
        probe = self._visual_walk_probe
        if probe is None:
            return
        map_id, cell, point = probe["map"], probe["cell"], probe["point"]
        origin, queued_at = probe["origin"], probe["queued_at"]
        world = self.feed.world
        moved = False
        last_walk_pkt = None
        with world.lock:
            invalid = world.map != map_id or world.is_map_loading or world.player is None
            if not invalid:
                last_walk_pkt = getattr(world, "last_walk_pkt", None)
                moved = (max(world.last_move_ts, world.last_player_move_pkt_time) >= queued_at
                         or math.dist(world.player.cell(), origin) >= 0.4)
        if not invalid and last_walk_pkt is not None:
            pkt_map, dest_cell, pkt_ts = last_walk_pkt
            if pkt_map == map_id and pkt_ts >= queued_at:
                terrain = getattr(self.feed, "terrain", None)
                gat = (terrain.cached_gats.get(map_id) or terrain.for_map(map_id)) if terrain else None
                if gat is not None:
                    px, py = int(round(origin[0])), int(round(origin[1]))
                    dx, dy = int(dest_cell[0]), int(dest_cell[1])
                    height_jump = abs(gat.cell_height_at(dx, dy) - gat.cell_height_at(px, py)) > 8.5
                    actual_path = gat.find_path((px, py), (dx, dy), prefer_clearance=False)
                    expected_len = probe.get("expected_path_len", max(1, int(round(math.dist(origin, cell))) + 1))
                    if height_jump or not actual_path or len(actual_path) > expected_len + 12:
                        now = time.monotonic()
                        self._visual_walk_probe = None
                        self.visual_occluded_cells = {
                            key: expiry for key, expiry in self.visual_occluded_cells.items()
                            if expiry > now
                        }
                        self.visual_occluded_cells[(map_id, cell)] = now + 12.0
                        self._last_visual_failure = (map_id, point, queued_at, now)
                        self._plateau_short_step_until = now + 2.0
                        self._click_debug(
                            f"[Movimento] Oclusão 3D via pacote 0x035F/0x0085: destino ({dx}, {dy}) "
                            f"para alvo {cell}; usando passo curto no platô."
                        )
                        return
        if invalid or moved or not self._is_game_focused():
            self._visual_walk_probe = None
            return
        now = time.monotonic()
        if probe["finished_at"] is None:
            result = getattr(self, "_last_touch_result", None)
            if result is None or result[0] != point or result[1] < queued_at:
                if now - queued_at > 2.0:
                    self._visual_walk_probe = None
                return
            if not result[2]:
                self._visual_walk_probe = None
                return
            probe["finished_at"] = result[1]
        if now - probe["finished_at"] >= 0.30:
            self._visual_walk_probe = None
            self.visual_occluded_cells = {key: expiry for key, expiry in self.visual_occluded_cells.items()
                                          if expiry > now}
            self.visual_occluded_cells[(map_id, cell)] = now + 12.0
            self._last_visual_failure = (map_id, point, queued_at, now)
            self._click_debug(f"[Movimento] Célula {cell} sem resposta ao clique por 300 ms.")

    def _visual_walk_failed(self, point, clicked_at):
        failure = self._last_visual_failure
        return bool(failure and failure[0] == self.feed.world.map and failure[1] == point
                    and 0.0 <= failure[2] - clicked_at <= 1.0)

    def _walk_step_result(self, world, position, previous, point, clicked_at, now):
        if self._visual_walk_failed(point, clicked_at):
            return "stalled"
        if world._is_player_walking():
            return "waiting"
        advanced = (tuple(map(round, position)) != tuple(map(round, previous))
                    and math.dist(position, previous) >= 0.8)
        if advanced:
            return "moved"
        if self._touch_click_aborted(point, clicked_at):
            return "stalled"
        if now - self._touch_click_finished_at(point, clicked_at) < 0.85:
            return "waiting"
        return "stalled"

    def _walk_ground_point(self, gat, position, cell, blocked, resolve, *, portal=None):
        world = getattr(self.feed, "world", None)
        map_name = world.map if world else None
        if (self.visual_occluded_cells.get((map_name, cell), 0.0) <= time.monotonic()
                and cell not in blocked and gat.is_walkable(*cell)
                and (not map_name or cell not in self._entity_occupied_cells(map_name, allowed_portal=portal))
                and not self._segment_hits_obstacle(*position, *cell, blocked)
                and self._has_gat_line_of_sight(*position, *cell, gat)
                and abs(gat.get_elevation(*position) - gat.get_elevation(*cell)) <= 16):
            return resolve(cell)
        return None

    def _pick_walk_stride(self, path, position, blocked, ground_point, *, min_dist=0.0):
        start = tuple(map(round, position))
        forward_path = self._filter_forward_path(path, position)
        candidates = [cell for cell in reversed(forward_path) if cell != start and math.dist(position, cell) <= 14
                      and not self._segment_hits_obstacle(*position, *cell, blocked)]
        preferred = [cell for cell in candidates if math.dist(position, cell) > min_dist]
        return self._select_walk_point(preferred, candidates, position, ground_point)


    def _prune_patrol_path(self, path, px, py, cur_cell):
        """Descarta apenas o prefixo até uma célula confirmada no corredor local."""
        if not path:
            return [], float("inf")
        local_path = path[:16]
        if cur_cell in local_path:
            return path[local_path.index(cur_cell) + 1:], math.dist((px, py), cur_cell)
        distance = min(math.dist((px, py), cell) for cell in local_path)
        return path, distance

    def _pick_walk_retry(self, path, position, target, ground_point, *, previous_point=None, radius=2,
                         min_dist=1.0):
        start = tuple(map(round, position))

        if self._visual_walk_retry_active(time.monotonic()):
            terrain = getattr(self.feed, "terrain", None)
            gat = terrain.cached_gats.get(self.feed.world.map) if terrain else None
            nearby = sorted(
                (cell for cell in self._patrol_cells_in_radius(*start, 5)
                 if cell != target and 2 <= math.dist(position, cell) <= 5),
                key=lambda cell: (-(gat.wall_clearance(*cell) if gat else 0), math.dist(cell, target)),
            )
            for cell in nearby:
                if point := ground_point(cell):
                    return cell, point

        def alternate(cell):
            if not min_dist <= math.dist(position, cell) <= 14:
                return None
            point = ground_point(cell)
            if point and (previous_point is None or math.dist(point, previous_point) >= 4):
                return point
            return None

        angle = math.atan2(position[1] - target[1], position[0] - target[0])
        nearby = dict.fromkeys(
            (target[0] + round(dist * math.cos(angle + i * math.pi / 4)),
             target[1] + round(dist * math.sin(angle + i * math.pi / 4)))
            for dist in (1, 2) for i in range(8)
        )
        for cell in sorted(nearby, key=lambda cell: (-math.dist(position, cell), math.dist(target, cell))):
            if (cell != target or previous_point is None) and (point := alternate(cell)):
                return cell, point
        if stride := self._pick_long_stride_cell(path, max(2.2, min_dist), 14,
                                                 position=position, ground_point=alternate):
            return stride
        neighbors = sorted(self._patrol_cells_in_radius(*start, radius), key=lambda cell: math.dist(cell, target))
        return next(((cell, point) for cell in neighbors if cell != target and (point := alternate(cell))), None)

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
            self._patrol_no_wp_cooldown = 0.0
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

    def _choose_patrol_waypoint(self, map_id, gat, position, blocked, penalties, now):
        center = tuple(map(round, position))
        unexplored = []
        revisits = []
        def available(cell):
            return (gat.is_walkable(*cell) and cell not in blocked
                    and now - self._patrol_finished_ts.get(cell, -math.inf) > 18
                    and now >= self._patrol_unreachable_until.get(cell, 0.0))

        for cell in self._patrol_cells_in_radius(*center, 49):
            x, y = cell
            if not (0 <= x < gat.width and 0 <= y < gat.height):
                continue
            clearance = gat.wall_clearance(*cell)
            distance = math.dist(position, cell)
            if not 10.0 <= distance <= 48 or not available(cell):
                continue
            sector = self._patrol_sector(cell)
            visited_at = self._patrol_sector_visit_ts.get(sector, -math.inf)
            turn = self._patrol_turn(center, cell)
            preferred_distance = not 18 <= distance <= 48, abs(distance - 32)
            if cell not in self._patrol_scanned_ts:
                score = (clearance < 2, turn, visited_at > now - 35, clearance < 3, *preferred_distance)
                unexplored.append((score, cell))
            else:
                scanned_at = self._patrol_scanned_ts[cell]
                score = (clearance < 2, max(visited_at, scanned_at) > now - 35, turn, visited_at,
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
                                     penalty_cells=penalties, max_iterations=2500)
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
                center, None, blocked_cells=blocked, penalty_cells=penalties, max_iterations=14000,
                goal_test=lambda cell: (cell not in self._patrol_scanned_ts
                                        and math.dist(center, cell) >= 6 and available(cell)),
            )
            if len(path) > 1:
                return path[-1], path
        return reachable(revisits)

    def _defer_directed_replan(self, now):
        self._directed_failures = min(4, self._directed_failures + 1)
        self._directed_replan_at = now + 0.5 * 2 ** (self._directed_failures - 1)

    def _try_auto_patrol(self, snapshot, px, py):
        """Auto-Patrol: patrulha mapa por A*, calculando sub-passos a 8-11 células e clicando via movimento relativo."""
        if not snapshot or not hasattr(self.feed, "world"):
            return
        if min(px, py) < 0:
            self._click_debug("PATROL bloqueado: coordenadas do personagem indisponíveis.")
            return

        if self._patrol_combat_or_loot_pending(snapshot):
            self._click_debug("PATROL bloqueado: combate ou coleta em andamento.")
            return
        world = self.feed.world
        interaction = self._patrol_interaction
        if (self.patrol_target_route and not self.nav_goal
                and travel_map_group(self.patrol_target_route[0]) != travel_map_group(snapshot.map)):
            self._clear_patrol_route()
            self._reset_patrol(px, py)
            return
        if interaction and interaction[0] != self.patrol_target_route:
            self._patrol_interaction = interaction = None
        if world.npc_dialog_active or world.npc_dialog.get("open"):
            if interaction:
                dialog = world.npc_dialog
                raw_gid = dialog.get("npc_gid")
                if world.aid_to_gid.get(raw_gid, raw_gid) == interaction[0][3]:
                    self._clear_patrol_route()
                    self._reset_patrol(px, py)
            self._click_debug("PATROL bloqueado: diálogo de NPC aberto.")
            return
        if self._automation_click_active():
            return
        if self._manual_mouse_button_pressed():
            self._click_debug("PATROL bloqueado: botão físico do mouse pressionado.")
            return

        vp_obj = getattr(self.feed, "viewport", None)
        target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        target_hwnd = self.get_game_hwnd(target_hwnd)
        if not self._game_input_ready(target_hwnd) or not self._game_is_foreground():
            self._click_debug("PATROL bloqueado: janela do Ragexe indisponível ou sem foco.")
            return

        now = time.monotonic()
        if now < getattr(self, "_patrol_no_wp_cooldown", 0.0):
            if self.nav_goal is None and not self.patrol_target_route:
                self._click_debug("PATROL bloqueado: aguardando nova busca por waypoint.")
                return
            self._patrol_no_wp_cooldown = 0.0
        if now < max(self._map_stabilize_until, self._directed_replan_at):
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

        curr_map = world.map
        gat = self._scan_patrol_area(world, curr_map, (px, py), now)
        if gat is None:
            self._click_debug(f"PATROL bloqueado: terreno .gat de {curr_map} indisponível.")
            return

        allowed_portal = self._patrol_portal_goal(curr_map)
        directed_route = self.patrol_target_route
        allowed_npc = (directed_route[3] if directed_route and allowed_portal is None
                       and isinstance(directed_route[3], int) else None)
        blocked, penalties = self._navigation_obstacles(curr_map, gat, portal=allowed_portal, npc=allowed_npc)

        def click_patrol_ground(cell, point, *, sprite=False, hover_reset=False):
            if self._patrol_combat_or_loot_pending(snapshot):
                return False
            goal = self.nav_goal
            route = self.patrol_target_route

            def aim(_lead):
                if (world.map != curr_map or not self._hud_can_run() or self.nav_goal != goal
                        or self.patrol_target_route != route or not (goal or route or self.auto_patrol)
                        or world.npc_dialog_active or world.npc_dialog.get("open")
                        or (not (goal or route) and self._client_combat_blocks_ground(world, curr_map))):
                    return None
                if not sprite:
                    return point
                with world.lock:
                    npc = world.entity("npc", allowed_npc)
                    if not npc:
                        return None
                    current = npc.track.cell()
                    player = world.player.cell() if world.player else (px, py)
                project_now = self.feed.camera.projector(
                    snapshot.viewport, getattr(self.feed, "_cam", None) or player, gat.get_elevation)
                u, v = project_now(*current, 5.5)
                return left + int(u * width), top + int(v * height)

            return self._navigation_click(snapshot, gat, cell, point, hwnd=target_hwnd, target_point=aim,
                                          portal=allowed_portal, npc=allowed_npc, ground=not sprite,
                                          retry_key=(curr_map, allowed_npc if sprite else "ground", cell),
                                          hover_reset=hover_reset)

        # ── Helper de projeção (definido antes de qualquer uso) ──
        viewport = snapshot.viewport
        left, top, width, height = viewport
        height_fn = getattr(gat, "get_elevation", gat.height_at)
        cam_pos = getattr(self.feed, "_cam", None) or (px, py)
        project = self.feed.camera.projector(viewport, cam_pos, height_fn)

        # ── Suporte a nav_goal: rota direcionada 100% independente do auto_patrol ──
        nav = getattr(self, "nav_goal", None)
        minimap = nav is not None and nav == self._minimap_goal
        if nav is not None:
            ng_map, ng_x, ng_y = nav
            norm_ng = ng_map.lower().removesuffix(".gat").removesuffix(".rsw")
            if curr_map == norm_ng:
                requested = ng_x, ng_y
                goal = requested if minimap else self._resolve_walkable_cell(
                    gat, *requested, max_radius=3, forbidden_cells=blocked)
                dist_ng = math.dist((px, py), goal) if goal else float("inf")
                is_manual = (getattr(self, "manual_nav_goal", None) is not None)
                reach_thresh = 0.75 if minimap else 1.5 if is_manual else 2.0
                if dist_ng <= reach_thresh and allowed_portal != (ng_x, ng_y):
                    self._click_debug(f"NAV_GOAL: Destino alcançado ({ng_x},{ng_y}) — encerrando.")
                    self.manual_nav_goal = None
                    self._minimap_goal = None
                    self.nav_goal = None
                    self._clear_locked_nav_path()
                    self._reset_patrol(px, py)
                    return
                # Valida destino no .gat e usa rota travada em self._active_nav_path sem recalcular a cada tick
                goal_key = (curr_map, goal) if goal else None
                need_recalc = (
                    not goal
                    or not self._active_nav_path
                    or self._active_nav_goal != goal_key
                    or (self._last_patrol_click_target is None
                        and not self._advance_locked_nav_waypoint((px, py), blocked, now, cur_cell))
                )
                if not goal:
                    ng_path = []
                elif need_recalc:
                    if minimap:
                        ng_path = self._find_minimap_path(gat, cur_cell, goal, blocked, penalties)
                    else:
                        ng_path = gat.find_path(cur_cell, goal, blocked_cells=blocked, penalty_cells=penalties)
                    if ng_path and ng_path[-1] == goal:
                        self._directed_replan_at = 0.0
                        self._set_locked_nav_path(ng_path, goal_key, (px, py), now)
                    else:
                        ng_path = []
                else:
                    ng_path = self._active_nav_path
                if ng_path:
                    self.current_patrol_path = list(ng_path[self._active_nav_idx:])
                    self.current_waypoint = goal
                    self.active_nav_path = list(ng_path[self._active_nav_idx:])
                    self.active_nav_color = "#A78BFA"  # Roxo = nav_goal
                else:
                    self._reset_patrol(px, py)
                    self._clear_locked_nav_path()
                    self._defer_directed_replan(now)
                    self._click_debug(f"PATROL bloqueado: A* sem rota até {(ng_x, ng_y)}.")
                    return
                # Depois deixa a lógica de execução do caminho abaixo tomar conta
                # (não retorna aqui, cai no código de sub-step abaixo)
            else:
                # Nav_goal em outro mapa: roteia via find_map_route
                walk_route = find_map_route(curr_map, norm_ng)
                if walk_route:
                    m_from, m_to, port_x, port_y = walk_route[0]
                    self.patrol_target_route = (m_from, port_x, port_y, "warp")
                    allowed_portal = (port_x, port_y)
                    blocked, penalties = self._navigation_obstacles(curr_map, gat, portal=allowed_portal)
                    self._click_debug(f"NAV_GOAL multi-mapa: Indo para portal {m_to} em ({port_x},{port_y})")
                else:
                    self._click_debug(f"NAV_GOAL: Sem rota de {curr_map} para {norm_ng}")
                    self.nav_goal = None
                    return

        resolve = self._safe_click_resolver(
            snapshot, project, allowed_portal=allowed_portal, allowed_npc=allowed_npc)

        def ground_point(cell):
            return self._walk_ground_point(gat, (px, py), cell, blocked, resolve, portal=allowed_portal)

        # ── Suporte a Rotas de Destino: (map_name, x, y, target_npc_or_warp) ──
        if self.patrol_target_route:
            r_map, r_x, r_y, r_target = self.patrol_target_route
            norm_r_map = r_map.lower().removesuffix(".gat").removesuffix(".rsw")
            if curr_map == norm_r_map:
                dist_to_route = math.hypot(px - r_x, py - r_y)
                interact_dist = 3.8 if r_target else 2.5
                if dist_to_route <= interact_dist and self._last_patrol_click_target is None:
                    if not r_target:
                        self._clear_patrol_route()
                        self._reset_patrol(px, py)
                        return
                    if world._is_player_walking():
                        return
                    attempts = 0
                    if interaction and interaction[0] == self.patrol_target_route:
                        _, clicked_at, last_point, attempts = interaction
                        if (not self._touch_click_aborted(last_point, clicked_at)
                                and now - self._touch_click_finished_at(last_point, clicked_at) < 1.5):
                            return
                        if attempts >= 3 and not self._city_travel_state:
                            self._click_debug(f"PATROL: Interação em {(r_x, r_y)} sem confirmação.")
                            self._clear_patrol_route()
                            self._reset_patrol(px, py)
                            return
                    target = r_x, r_y
                    npc_item = next((obj for obj in snapshot.objects if obj.key == f"npc:{r_target}"), None)
                    uv = (npc_item.u, npc_item.v) if npc_item else None
                    if allowed_portal is not None:
                        if cur_cell == target:
                            fallback = self._pick_walk_retry((), (px, py), target, ground_point)
                            if fallback and click_patrol_ground(*fallback):
                                cell, point = fallback
                                self._last_patrol_click_target = (now, px, py, *cell, *point, 0)
                            return
                        step = self._navigation_target_step(gat, (px, py), target, None, resolve, blocked, penalties)
                    elif uv is not None:
                        step = self._navigation_target_step(gat, (px, py), target, uv, resolve, blocked, penalties)
                    else:
                        step = None
                    if step:
                        cell, point, direct = step
                        if click_patrol_ground(cell, point, sprite=direct and npc_item is not None,
                                               hover_reset=direct and attempts == 1):
                            if direct:
                                if npc_item:
                                    self._hud_npc_line_target = (npc_item.key, curr_map, now + 2.0)
                                self._patrol_interaction = (self.patrol_target_route, now, point, attempts + 1)
                            else:
                                self._last_patrol_click_target = (now, px, py, *cell, *point, 0)
                        return
                    if npc_item:
                        self._side_step_to_reveal(target, px, py, from_patrol=True)
                        return
                    if allowed_npc is not None:
                        self._defer_directed_replan(now)
                        return
                    if allowed_portal is None:
                        self._clear_patrol_route()
                        self._reset_patrol(px, py)
                        return

        route_target = self.patrol_target_route and curr_map == self.patrol_target_route[0].lower().removesuffix(".gat")
        roaming = not route_target and self.nav_goal is None
        pending = self._last_patrol_click_target
        if pending:
            clicked_at, last_x, last_y, gx, gy, sx, sy, attempts = pending
            failed = (gx, gy)
            result = self._walk_step_result(world, (px, py), (last_x, last_y), (sx, sy), clicked_at, now)
            if result == "waiting":
                can_chain = (
                    world._is_player_walking()
                    and math.dist((px, py), (last_x, last_y)) >= 0.8
                    and math.dist((px, py), (gx, gy)) <= 3.5
                    and (not self.current_waypoint or math.dist((px, py), self.current_waypoint) > 5.0)
                )
                if can_chain:
                    result = "moved"
                elif now - clicked_at >= 0.8:
                    result = "moved" if math.dist((px, py), (last_x, last_y)) >= 0.8 else "stalled"
                else:
                    self._click_debug("PATROL bloqueado: aguardando confirmação do passo anterior.")
                    return
            if result == "moved":
                self._last_patrol_click_target = None
                self._directed_failures = 0
                dx, dy = px - last_x, py - last_y
                distance = math.hypot(dx, dy)
                if roaming and attempts == 0 and distance >= 2.5:
                    self._patrol_heading = dx / distance, dy / distance
            else:
                if attempts == 0 and (point := ground_point(failed)):
                    if click_patrol_ground(failed, point, hover_reset=True):
                        self._last_patrol_click_target = (now, px, py, *failed, *point, 1)
                    return
                if attempts < 2 and self.current_patrol_path:
                    retry = self._pick_walk_retry(
                        self.current_patrol_path[:16], (px, py), failed, ground_point, previous_point=(sx, sy),
                    )
                    if retry:
                        cell, point = retry
                        if click_patrol_ground(cell, point):
                            self._current_patrol_sub_step = cell
                            self._last_patrol_click_target = (now, px, py, *cell, *point, 2)
                            self._click_debug(f"PATROL: Sem avanço para {failed}. Tentando {cell}.")
                        return
                self.temp_blocked_cells[failed] = now + 12.0
                self._last_patrol_click_target = None
                self._current_patrol_sub_step = None
                self.current_patrol_path = []
                self._clear_locked_nav_path()
                self._directed_replan_at = now + 0.35
                return

        if world._is_player_walking():
            current_sub = getattr(self, "_current_patrol_sub_step", None)
            if current_sub is None or math.dist((px, py), current_sub) > 3.5:
                self._click_debug("PATROL bloqueado: personagem ainda caminhando.")
                return

        # ── Planejamento de Waypoint e Rota A* ──
        arrival_dist = 0.75 if minimap else 6.0 if roaming else 2.0
        stride = None
        max_plan_attempts = 2 if roaming else 1
        for plan_attempt in range(max_plan_attempts):
            if not self.current_waypoint or math.dist((px, py), self.current_waypoint) <= arrival_dist:
                if roaming and self.current_waypoint:
                    self._patrol_finished_ts[self.current_waypoint] = now
                    self._reset_patrol(px, py)
                    self.active_nav_path = None
                next_path = None
                if route_target:
                    next_wp = self._resolve_walkable_cell(
                        gat, self.patrol_target_route[1], self.patrol_target_route[2],
                        max_radius=3, forbidden_cells=blocked)
                    if next_wp is None:
                        next_wp = (int(self.patrol_target_route[1]), int(self.patrol_target_route[2]))
                elif getattr(self, "nav_goal", None) is not None:
                    # nav_goal ativo: nunca chama pick_next_waypoint, usa o destino direto
                    next_wp = self._active_nav_path[-1] if self._active_nav_path else self.nav_goal[1:3]
                    self.active_nav_color = "#A78BFA"  # Roxo = nav_goal
                else:
                    next_wp, next_path = self._choose_patrol_waypoint(
                        curr_map, gat, (px, py), blocked, penalties, now,
                    )
                    self.active_nav_color = "#38BDF8"  # Ciano = Auto-Patrol
                if next_wp:
                    self.current_waypoint = next_wp
                    self._patrol_no_wp_cooldown = 0.0
                    if next_path is None:
                        next_path = gat.find_path((px, py), next_wp, blocked_cells=blocked, penalty_cells=penalties)
                    self.current_patrol_path = next_path or []
                    self.active_nav_path = list(self.current_patrol_path)
                    # Preserva a cor já definida por nav_goal se aplicável
                    if not hasattr(self, "active_nav_color") or self.active_nav_color != "#A78BFA":
                        self.active_nav_color = "#38BDF8"  # Ciano = Auto-Patrol
                    self.stuck_count = 0
                else:
                    self.visited_cells.clear()
                    self._patrol_no_wp_cooldown = now + 0.35
                    self._click_debug("PATROL bloqueado: nenhum waypoint caminhável disponível.")
                    return

            if not (nav is not None and self._active_nav_path):
                if any(cell in blocked for cell in self.current_patrol_path[:12] if cell != cur_cell):
                    self.current_patrol_path = []
                if not self.current_patrol_path:
                    path = gat.find_path(cur_cell, self.current_waypoint, blocked_cells=blocked, penalty_cells=penalties)
                    if not path or (roaming and path[-1] != self.current_waypoint):
                        if roaming:
                            self._patrol_unreachable_until[self.current_waypoint] = now + 12.0
                            self._reset_patrol(px, py)
                            self.active_nav_path = None
                            if plan_attempt + 1 < max_plan_attempts:
                                continue
                            self._patrol_no_wp_cooldown = now + 0.35
                        else:
                            self._reset_patrol(px, py)
                            self._defer_directed_replan(now)
                        self._clear_locked_nav_path()
                        self._click_debug(f"PATROL bloqueado: A* sem rota até {self.current_waypoint}.")
                        return
                    self.current_patrol_path = path
                    self.active_nav_path = list(path)

                # ── Detecção e Poda Precisa de Avanço na Rota ──
                if self.current_patrol_path:
                    pruned, min_dist_to_path = self._prune_patrol_path(
                        self.current_patrol_path, px, py, cur_cell,
                    )
                    self.current_patrol_path = pruned

                    # Se o personagem se desviou muito da rota (> 4.5 células), recalcula a partir de onde ele está
                    if min_dist_to_path > 4.5 and self.current_waypoint:
                        recalc_p = gat.find_path(cur_cell, self.current_waypoint,
                                                 blocked_cells=blocked, penalty_cells=penalties)
                        if recalc_p and (not roaming or recalc_p[-1] == self.current_waypoint):
                            pruned_recalc, _ = self._prune_patrol_path(
                                recalc_p, px, py, cur_cell,
                            )
                            self.current_patrol_path = pruned_recalc
                            self.active_nav_path = list(pruned_recalc)
                        elif roaming:
                            self._patrol_unreachable_until[self.current_waypoint] = now + 12.0
                            self._reset_patrol(px, py)
                            self.active_nav_path = None
                            if plan_attempt + 1 < max_plan_attempts:
                                continue
                            self._patrol_no_wp_cooldown = now + 0.35
                            return

            if not self.current_patrol_path:
                if roaming:
                    if self.current_waypoint:
                        self._patrol_finished_ts[self.current_waypoint] = now
                    self._reset_patrol(px, py)
                    self.active_nav_path = None
                    if plan_attempt + 1 < max_plan_attempts:
                        continue
                    self._patrol_no_wp_cooldown = now + 0.35
                    return
                self.current_waypoint = None
                return

            if nav is not None and self._active_nav_path:
                stride = self._pick_walk_stride(
                    self._active_nav_path[self._active_nav_idx:self._active_nav_idx + 16],
                    (px, py), blocked, ground_point)
            else:
                stride = self._pick_walk_stride(
                    self.current_patrol_path[:15], (px, py), blocked, ground_point,
                    min_dist=2.5 if roaming else 0.0,
                )

            if roaming and stride and self.current_waypoint:
                if math.dist((px, py), stride[0]) < 6.0 and math.dist((px, py), self.current_waypoint) <= 8.0:
                    self._patrol_finished_ts[self.current_waypoint] = now
                    self._reset_patrol(px, py)
                    self.active_nav_path = None
                    stride = None
                    if plan_attempt + 1 < max_plan_attempts:
                        continue

            if stride is None:
                stride = self._pick_walk_stride(self.current_patrol_path[:16], (px, py), blocked, ground_point)
            if stride is None and not roaming and self.current_waypoint:
                step = self.current_patrol_path[min(4, len(self.current_patrol_path) - 1)]
                stride = self._pick_walk_retry(self.current_patrol_path[:16], (px, py), step, ground_point)
            if stride is None:
                if roaming:
                    if self.current_waypoint:
                        self._patrol_unreachable_until[self.current_waypoint] = now + 12.0
                    self._reset_patrol(px, py)
                    self.active_nav_path = None
                    if plan_attempt + 1 < max_plan_attempts:
                        continue
                    self._patrol_no_wp_cooldown = now + 0.35
                    self._click_debug("PATROL bloqueado: nenhum passo caminhável pode ser clicado no viewport.")
                    return
                self._click_debug("PATROL bloqueado: nenhum passo caminhável pode ser clicado no viewport.")
                self._clear_locked_nav_path()
                self._reset_patrol(px, py)
                self._defer_directed_replan(now)
                return
            break

        if stride is None:
            return

        current_sub = getattr(self, "_current_patrol_sub_step", None)
        cell, point = stride
        if world._is_player_walking() and current_sub and (cell == current_sub or math.dist(cell, current_sub) <= 2.5):
            self._click_debug("PATROL bloqueado: passo atual já em andamento.")
            return
        if click_patrol_ground(cell, point):
            self._current_patrol_sub_step = cell
            self._last_patrol_click_target = (now, px, py, *cell, *point, 0)
            self._click_debug(f"PATROL: Passo em {cell} -> tela {point}")
        else:
            self._click_debug("PATROL bloqueado: clique do passo recusado.")

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


    def _segment_hits_obstacle(self, x0, y0, x1, y1, blocked_cells):
        if not blocked_cells:
            return False
        x, y, end_x, end_y = map(round, (x0, y0, x1, y1))
        if (end_x, end_y) in blocked_cells:
            return True
        dx, dy = abs(end_x - x), abs(end_y - y)
        sx, sy = (1 if x < end_x else -1), (1 if y < end_y else -1)
        ix = iy = 0
        while ix < dx or iy < dy:
            cross_x = (2 * ix + 1) * dy
            cross_y = (2 * iy + 1) * dx
            if cross_x == cross_y and ((x + sx, y) in blocked_cells or (x, y + sy) in blocked_cells):
                return True
            if cross_x <= cross_y:
                x += sx
                ix += 1
            if cross_x >= cross_y:
                y += sy
                iy += 1
            if (x, y) in blocked_cells:
                return True
        return False

    def _registered_portal_cells(self, map_name):
        cells = {tuple(cell) for (source, _), cell in MAP_PORTALS.items() if source == map_name}
        cells.update(tuple(cell) for (source, _), cell in KAFRA_STORAGE_PORTALS.items() if source == map_name)
        cells.update((int(row["wx"]), int(row["wy"])) for row in self.world_routes_data.get("portals", ())
                     if row["from_map"].lower() == map_name)
        cells.update((int(x), int(y)) for source, _, x, y, _, _ in
                     self.world_routes_data.get("reference_portals", ()) if source.lower() == map_name)
        cells.update(tuple(cell) for link, cell in self._learned_warps.items()
                     if link.partition("->")[0] == map_name and "->" in link
                     and isinstance(cell, (list, tuple)) and len(cell) == 2
                     and all(type(coord) is int for coord in cell))
        triggers = self._learned_warps.get("portal_guard", {}).get(map_name, {})
        cells.update(tuple(portal) for portal in triggers.values()
                     if isinstance(portal, (list, tuple)) and len(portal) == 2
                     and all(type(coord) is int for coord in portal))
        return cells

    def _navigation_centers(self, map_name, entities, allowed_target_id=None):
        portals = self._registered_portal_cells(map_name)
        npcs = set()
        allowed_cells = set()
        for (kind, entity_id), entity in entities:
            if entity.is_portal:
                portals.add(tuple(map(round, entity.track.target)))
            elif kind == "npc" or entity.objecttype in (1, 6) or entity.is_kafra or entity.has_vending_shop:
                cell = tuple(map(round, entity.track.target))
                if entity_id == allowed_target_id:
                    allowed_cells.add(cell)
                else:
                    npcs.add(cell)
                    npcs.add(tuple(map(round, entity.track.cell())))

        world = getattr(self.feed, "world", None)
        locations = list(self._kafra_locations_for_map(map_name, self._navigation_kafras))
        if world and (discovered := world.kafras_discovered.get(map_name)):
            locations.append(discovered)
        for location in locations:
            cell = int(location["x"]), int(location["y"])
            if world and any(math.dist(cell, allowed) <= 2.0 for allowed in allowed_cells):
                with world.lock:
                    if world.map == map_name and self._registered_kafra(world, map_name, location) == allowed_target_id:
                        continue
            npcs.add(cell)
        return portals, npcs

    def _npc_exclusion_cells(self, map_name):
        """Retorna células ocupadas por NPCs, lojas e Kafras conhecidos no mapa."""
        cells = set()
        for (kind, _), entity in self._navigation_entities(map_name):
            if not entity.is_portal and (kind == "npc" or entity.objecttype in (1, 6) or entity.is_kafra or entity.has_vending_shop):
                cells.add(tuple(map(round, entity.track.target)))
        world = getattr(self.feed, "world", None)
        locations = list(self._kafra_locations_for_map(map_name, self._navigation_kafras))
        if world and (discovered := world.kafras_discovered.get(map_name)):
            locations.append(discovered)
        for location in locations:
            cells.add((int(location["x"]), int(location["y"])))
        return cells

    @staticmethod
    def _npc_zone_cells(centers):
        return {(x + dx, y + dy) for x, y in centers
                for dx in (-1, 0, 1) for dy in (-1, 0, 1)}

    @staticmethod
    def _clip_box_around_npcs(center, box, npc_cells):
        left, bottom, right, top = box
        cx, cy = center
        for nx, ny in npc_cells:
            if math.dist((cx, cy), (nx, ny)) <= 1.5:
                return None
            if left <= nx <= right and bottom <= ny <= top:
                if abs(nx - cx) >= abs(ny - cy):
                    if nx > cx:
                        right = min(right, nx - 1)
                    else:
                        left = max(left, nx + 1)
                else:
                    if ny > cy:
                        top = min(top, ny - 1)
                    else:
                        bottom = max(bottom, ny + 1)
                if left > right or bottom > top:
                    return None
        return (left, bottom, right, top)

    def _parsed_portal_guard_triggers(self, map_name):
        triggers = self._learned_warps.get("portal_guard", {}).get(map_name, {})
        key = (id(self._learned_warps), map_name, len(triggers))
        cache = getattr(self, "_portal_guard_parsed_cache", None)
        if cache and cache[0] == key:
            return cache[1]
        parsed = []
        for raw_key, portal in triggers.items():
            try:
                x, y = map(int, raw_key.split(","))
            except (AttributeError, ValueError):
                continue
            center = tuple(portal) if (isinstance(portal, (list, tuple)) and len(portal) == 2 and all(type(n) is int for n in portal)) else None
            parsed.append(((x, y), center))
        parsed_tuple = tuple(parsed)
        self._portal_guard_parsed_cache = (key, parsed_tuple)
        return parsed_tuple

    def _portal_boxes(self, map_name, gat, portals, allowed_portal_cell=None):
        npc_cells = self._npc_exclusion_cells(map_name)
        boxes = {}
        attached = []
        for (x, y), center in self._parsed_portal_guard_triggers(map_name):
            if center is not None and all(math.dist(center, nc) > 1.5 for nc in npc_cells):
                portals.add(center)
                attached.append((center, x, y))

        for x, y in portals:
            if (x, y) == allowed_portal_cell:
                continue
            radius = 3 if gat and (x <= 35 or x >= gat.width - 35 or y <= 35 or y >= gat.height - 35) else 2
            if clipped := self._clip_box_around_npcs((x, y), (x - radius, y - radius, x + radius, y + radius), npc_cells):
                boxes[(x, y)] = clipped
        for center, x, y in attached:
            if center != allowed_portal_cell and center in boxes:
                left, bottom, right, top = boxes[center]
                expanded = min(left, x), min(bottom, y), max(right, x), max(top, y)
                if clipped := self._clip_box_around_npcs(center, expanded, npc_cells):
                    boxes[center] = clipped
        return boxes

    def _build_navigation_obstacles(self, map_name, entities, gat, allowed_target_id=None, allowed_portal_cell=None,
                                    block_portals=True):
        portals, npcs = self._navigation_centers(map_name, entities, allowed_target_id)
        blocked_cells = set()
        penalty_cells = {}

        def add_box(box, ring, cost):
            left, bottom, right, top = box
            x_range = range(max(0, left - ring), min(gat.width, right + ring + 1) if gat else right + ring + 1)
            y_range = range(max(0, bottom - ring), min(gat.height, top + ring + 1) if gat else top + ring + 1)
            for x in x_range:
                for y in y_range:
                    cell = x, y
                    if left <= x <= right and bottom <= y <= top:
                        blocked_cells.add(cell)
                    else:
                        penalty_cells[cell] = max(cost, penalty_cells.get(cell, 0.0))

        world = getattr(self.feed, "world", None)
        player_cell = (tuple(map(round, world.player.cell()))
                       if world is not None and world.map == map_name and world.player else None)
        if block_portals:
            for center, box in self._portal_boxes(map_name, gat, portals, allowed_portal_cell).items():
                x1, y1, x2, y2 = box
                if player_cell and x1 <= player_cell[0] <= x2 and y1 <= player_cell[1] <= y2:
                    # A chegada pelo portal deve permitir sair da zona de entrada.
                    add_box((*center, *center), max(x2 - x1, y2 - y1), 8.0)
                else:
                    add_box(box, 2, 6.0)
        for x, y in npcs:
            # Se o personagem já estiver na zona, o A* pode sair dela.
            # O resolvedor de cliques continua protegendo os nove destinos.
            radius = 0 if player_cell and max(abs(player_cell[0] - x), abs(player_cell[1] - y)) <= 1 else 1
            add_box((x - radius, y - radius, x + radius, y + radius), 1, 3.0)
        for cell in blocked_cells:
            penalty_cells.pop(cell, None)
        if player_cell is not None:
            blocked_cells.discard(player_cell)

        return blocked_cells, penalty_cells

    def _navigation_entities(self, map_name):
        world = getattr(self.feed, "world", None)
        if world is None:
            return ()
        with world.lock:
            return tuple(world.entity_items()) if world.map == map_name else ()

    def _navigation_obstacles(self, map_name, gat, *, portal=None, npc=None, block_portals=True):
        now = time.monotonic()
            blocked = set(cache[2])
            penalties = dict(cache[3])
            world = getattr(self.feed, "world", None)
            player_cell = None
            if world is not None:
                with world.lock:
                    if world.player is not None:
                        player_cell = tuple(map(round, world.player.cell()))
            if player_cell is not None:
                blocked.discard(player_cell)
            return blocked, penalties

        entities = self._navigation_entities(map_name)
        allowed_target_id = npc
        allowed_portal_cell = portal if isinstance(portal, tuple) else None
        blocked, penalties = self._build_navigation_obstacles(
            map_name, entities, gat,
            allowed_target_id=allowed_target_id,
            allowed_portal_cell=allowed_portal_cell,
            block_portals=block_portals,
        )
        # Adiciona células temporariamente bloqueadas (colisões recentes)
        for cell, exp in list(self.temp_blocked_cells.items()):
            if now < exp:
                blocked.add(cell)
                penalties.pop(cell, None)
        self._nav_obstacles_cache = (cache_key, now + 0.04, frozenset(blocked), dict(penalties))
        # Remove célula do jogador para não bloquear o próprio movimento
        world = getattr(self.feed, "world", None)
        player_cell = None
        if world is not None:
            with world.lock:
                if world.player is not None:
                    player_cell = tuple(map(round, world.player.cell()))
        if player_cell is not None:
            blocked.discard(player_cell)
        return blocked, penalties

    def _is_intentional_warp(self, event, portal_ids):
        if self.travel_plan or self._city_travel_state or self._travel_allowed_portal or self._kafra_cycle:
            return True
        retreat = self._retreat_click
        if retreat and retreat[4]:
            return True
        map_name = event["map"]
        recent = self._intentional_portal_click
        if recent and map_name == recent[0] and event["ts"] <= recent[1]:
            return True
        route = self.patrol_target_route
        return bool(route and route[0].lower().removesuffix(".gat").removesuffix(".rsw") == map_name
                    and (route[3] == "warp" or route[3] in portal_ids))

    def _learn_accidental_warp(self, snapshot):
        event = snapshot.last_warp_from
        if not event or event["ts"] <= self._last_warp_seen:
            return
        self._last_warp_seen = event["ts"]
        portals, intentional = snapshot.warp_context
        if intentional:
            return
        map_name = event["map"]
        cell = event["x"], event["y"]
        portal = min(portals, key=lambda center: math.dist(cell, center), default=None)
        try:
            saved = save_portal_trigger(map_name, cell, portal)
        except OSError as exc:
            print(f"[PortalGuard] Falha ao gravar learned_warps.json: {exc}", flush=True)
            return
        if saved:
            self._learned_warps = load_learned_warps()
            print(f"[PortalGuard] Célula de teleporte aprendida em {map_name} ({cell[0]},{cell[1]})", flush=True)

    def _patrol_portal_goal(self, map_name):
        route = self.patrol_target_route
        if route and route[0].lower().removesuffix(".gat").removesuffix(".rsw") == map_name:
            if route[3] == "warp" or any(
                entity.is_portal and gid == route[3] for (_, gid), entity in self._navigation_entities(map_name)
            ):
                return tuple(route[1:3])
        return None

    def _visible_portal_cells(self, map_name):
        return {tuple(map(round, ent.track.target)) for _, ent in self._navigation_entities(map_name)
                if ent.is_portal}

    def _clear_patrol_route(self):
        """Limpa imediatamente todas as rotas e waypoints do Patrol e força redesenho do overlay."""
        self.patrol_path = []
        self.current_patrol_path = []
        self.patrol_waypoints = []
        self.current_waypoint = None
        self._patrol_no_wp_cooldown = 0.0
        self._clear_locked_nav_path()
        self.nav_path = None
        self.nav_goal = None
        self._minimap_goal = None
        self._minimap_path_request = None
        self._directed_replan_at = 0.0
        self._directed_failures = 0
        self._patrol_interaction = None
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
            world = getattr(getattr(self, "feed", None), "world", None)
            if (self._automation_click_active() or self._survival_state
                    or self._respawn_started or (world is not None and world.player_is_dead)
                    or self._kafra_cycle or self._city_travel_state
                    or (world is not None and world.storage_open)
                    or self._is_pin_dialog_visible()):
                return
            target_hwnd = self.get_game_hwnd(None)
            if target_hwnd and u32.IsWindow(target_hwnd):
                u32.SetForegroundWindow(target_hwnd)
            self._send_game_key(0x1B)
        self.root.after(delay_ms, _fire)

    def _update_map_stabilization(self, world, curr_m, now):
        """Aplica pausa breve de estabilização após transição de mapa ou map-server."""
        transition_ts = getattr(world, "last_map_server_transition_ts", 0.0)
        if transition_ts > self._last_map_server_transition_ts:
            self._last_map_server_transition_ts = transition_ts
            self._map_stabilize_until = max(self._map_stabilize_until, transition_ts + 0.75)
        arrival = getattr(world, "last_map_arrival", None)
        arrival_ts = arrival[2] if arrival and len(arrival) >= 3 else 0.0
        if arrival_ts > self._last_map_arrival_ts:
            self._last_map_arrival_ts = arrival_ts
            self._map_stabilize_until = max(self._map_stabilize_until, arrival_ts + 0.75)
        if curr_m:
            if self._last_stabilized_map is not None and curr_m != self._last_stabilized_map:
                if not world.last_map_ready_ts or now - world.last_map_ready_ts > 5.0:
                    self._map_stabilize_until = max(self._map_stabilize_until, now + 0.75)
                self.temp_blocked_cells.clear()
                self._nav_obstacles_cache = None
                self._last_patrol_click_target = None
                self._current_patrol_sub_step = None
            self._last_stabilized_map = curr_m

    def _handle_map_change(self, new_map):
        """Descarta rotas e alvos do mapa anterior, preservando os modos ativos."""
        self._click_debug(f"MAPA ALTERADO ({self.farm_map} -> {new_map}): reiniciando rotas e alvos.")
        self.farm_map = new_map if self.auto_farm or self.auto_patrol else None
        self.retreat_origin_map = None
        self.retreat_return_portal_pos = None
        self._finish_farm_lock("troca de mapa")
        self.locked_combat_mob_gid = None
        if hasattr(getattr(self.feed, "world", None), "locked_combat_mob_gid"):
            self.feed.world.locked_combat_mob_gid = None
        self._locked_drop_gid = None
        self._clear_patrol_route()

    def _toggle_auto_farm(self):
        """Alterna estado do Auto-Farm e gerencia farm_map."""
        if not self.auto_farm and (self.travel_plan or self._city_travel_state or self._kafra_cycle):
            self._click_debug("[Auto-Farm] Bloqueado durante viagem ou fluxo da Kafra.")
            return
        self.auto_farm = not self.auto_farm
        if self.auto_farm:
            self._resume_hud_automation()
            self.farm_map = getattr(getattr(self.feed, "world", None), "map", None) or getattr(self, "current_map", "")
        else:
            self._finish_farm_lock("Auto-Farm desativado")
            self.locked_combat_mob_gid = None
            if hasattr(getattr(self.feed, "world", None), "locked_combat_mob_gid"):
                self.feed.world.locked_combat_mob_gid = None
            if not self.auto_patrol:
                self.farm_map = None
        print(f"[Auto-Farm] {'ON' if self.auto_farm else 'OFF'}", flush=True)

    def _toggle_auto_patrol(self):
        """Alterna estado do Auto-Patrol e gerencia rotas e farm_map."""
        if not self.auto_patrol and (self.travel_plan or self._city_travel_state or self._kafra_cycle):
            self._click_debug("[Auto-Patrol / Roam] Bloqueado durante viagem ou fluxo da Kafra.")
            return
        self.auto_patrol = not self.auto_patrol
        if self.auto_patrol:
            self._resume_hud_automation()
            self.farm_map = getattr(getattr(self.feed, "world", None), "map", None) or getattr(self, "current_map", "")
        else:
            self._clear_patrol_route()
            if not self.auto_farm:
                self.farm_map = None
        print(f"[Auto-Patrol / Roam] {'ON' if self.auto_patrol else 'OFF'}", flush=True)

    def _is_point_in_hud(self, sx, sy, margin=12):
        if margin:
            margin = max(12, margin)
        for hud in (self._hud_panel, self._hud_drawer, self._hud_submenu):
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

    def _side_step_to_reveal(self, target_cell, px, py, *, from_patrol=False):
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
        if not snap or snap.map != curr_map:
            return False
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

        blocked, _ = self._navigation_obstacles(curr_map, gat)
        resolve = self._safe_click_resolver(snap, project)
        candidates = []
        for sign in (1, -1):
            for step_r in (2.3, 2.8, 3.2):
                sx_cell = int(round(px + perp_dx * step_r * sign))
                sy_cell = int(round(py + perp_dy * step_r * sign))
                if not (0 <= sx_cell < gat.width and 0 <= sy_cell < gat.height):
                    continue
                u_cand, v_cand = project(sx_cell, sy_cell)
                if 0.15 <= u_cand <= 0.85 and 0.15 <= v_cand <= 0.85:
                    cell = sx_cell, sy_cell
                    if point := self._walk_ground_point(gat, (px, py), cell, blocked, resolve):
                        candidates.append((cell, point))
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
                u_c, v_c = project(cx_c, cy_c)
                if 0.25 <= u_c <= 0.75 and 0.25 <= v_c <= 0.75:
                    cell = cx_c, cy_c
                    if point := self._walk_ground_point(gat, (px, py), cell, blocked, resolve):
                        candidates.append((cell, point))
                        break

        if candidates:
            cell, point = candidates[0]
            self._click_debug(f"PASSO DESLOCAMENTO CÂMERA: movendo char para {cell} para tirar alvo de trás da interface")

            def aim(_lead):
                if not self._client_combat_blocks_ground(self.feed.world, snap.map):
                    return point

            return self._navigation_click(snap, gat, cell, point, hwnd=target_hwnd,
                                          target_point=aim if from_patrol else None)
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
        preserve = keep_waypoint and self.auto_patrol and self.nav_goal is None and not self.patrol_target_route
        if not preserve:
            self.current_patrol_path = []
            self.patrol_path = []
            self.current_waypoint = None
            self._patrol_no_wp_cooldown = 0.0
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
        if self._loot_attempts:
            present = {(gid, ent.created_at) for (kind, gid), ent in entities if kind == "drop"}
            self._loot_attempts = {key: attempts for key, attempts in self._loot_attempts.items() if key in present}
        if self.feed.world.inventory_pickup_blocked:
            return []
        candidates = []
        wall_now = time.time()
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        blocked, _ = self._navigation_obstacles(snapshot.map, gat)
        for (kind, gid), drop in entities:
            if kind != "drop" or drop.ignore_until > now or tuple(map(round, drop.track.target)) in blocked:
                continue
            name = self.names.get("drop", {}).get(drop.catalog_id, "").strip().lower()
            tier = self.classify_item_tier(drop.catalog_id, name)
            if drop.retry_after > wall_now or drop.unreachable_until > now:
                continue
            loot_key = (gid, drop.created_at)
            _, exhausted_at = self._loot_attempts.get(loot_key, (0, None))
            if exhausted_at is not None:
                if math.dist(position, exhausted_at) < 1.5:
                    continue
                self._loot_attempts.pop(loot_key, None)
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

    def _client_combat_blocks_ground(self, world, map_id):
        with world.lock:
            gid, _, attack_ts = world.last_mob_attack
            return (world.map != map_id
                    or world.entity("mob", self._locked_farm_target_id) is not None
                    or (attack_ts > self._farm_seen_attack_ts and world.entity("mob", gid) is not None))

    def _patrol_combat_or_loot_pending(self, snapshot):
        if self.travel_plan or self._city_travel_state or self._kafra_cycle or self.nav_goal or self.patrol_target_route:
            return False
        return self._client_combat_blocks_ground(self.feed.world, snapshot.map)

    def _finish_farm_lock(self, reason, *, confirmed_death=False, resume_patrol=False):
        gid = self._locked_farm_target_id
        self._locked_farm_target_id = self._locked_mob_gid = None
        self._in_active_combat = False
        self._farm_target_missing_since = None
        self._farm_progress = None
        self._farm_route = self._farm_pursuit = None
        if gid is None:
            return
        if not confirmed_death:
            now = time.monotonic()
            self.ignored_mobs[gid] = now + FARM_IGNORE_DURATION
            if resume_patrol:
                self._farm_patrol_until = now + 1.0
        self._pending_skill_target = None
        self._skill_approach_step = None
        if self._engage_state and (self._engage_state["kind"], self._engage_state["gid"]) == ("mob", gid):
            self._engage_state = self._engagement = None
        if self.locked_combat_mob_gid == gid:
            self.locked_combat_mob_gid = None
        if self._active_splash_gid == gid:
            self._active_splash_gid = None
            self._active_splash_until = 0.0
        if self._skill_cast_target_gid == gid:
            self._skill_cast_until = 0.0
            self._skill_cast_target_gid = None
        # ── Parte 3: limpa estado pré-combate ao liberar farm lock ───────────
        if gid is not None:
            for k in [k for k in self._precombat_states if k[0] == gid]:
                self._precombat_states.pop(k, None)
                self._precombat_click_attempts.pop(k, None)
        self._precombat_cast_until = 0.0
        self._precombat_wait_until = 0.0
        self._precombat_active_slot = {}
        self._precombat_cursor_armed = False
        self._precombat_key_future = None
        self._precombat_need_hover_reset = False
        self.active_nav_path = None
        world = self.feed.world
        with world.lock:
            if world.farm_target_gid == gid:
                world.farm_target_gid = None
            if world.locked_combat_mob_gid == gid:
                world.locked_combat_mob_gid = None
        print(f"[FarmLock] Alvo #{gid} finalizado ({reason})", flush=True)

    def _farm_target_unreachable(self, snapshot, position, cell, attack_range):
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
        if gat is None:
            return False

        start, target = tuple(map(round, position)), tuple(map(round, cell))
        blocked, penalties = self._build_navigation_obstacles(
            snapshot.map, self._navigation_entities(snapshot.map), gat)
        check = snapshot.map, start, target, attack_range, frozenset(blocked)
        if self._farm_reachability_check == check:
            return False
        self._farm_reachability_check = check

        def within_attack_range(ground):
            return (max(abs(ground[0] - target[0]), abs(ground[1] - target[1])) <= attack_range
                    and not self._segment_hits_obstacle(*ground, *target, blocked)
                    and self._has_gat_line_of_sight(*ground, *target, gat))

        return not gat.find_path(start, target, goal_test=within_attack_range, goal_radius=attack_range,
                                 blocked_cells=blocked, penalty_cells=penalties,
                                 max_iterations=gat.width * gat.height)

    def _farm_path(self, gat, start, target, blocked, *, max_steps=FARM_MAX_PATH_STEPS):
        if gat is None:
            return []
        walkable_target = gat.is_walkable(*target)

        def reaches_target(cell):
            return (max(abs(cell[0] - target[0]), abs(cell[1] - target[1])) <= 1
                    and (not walkable_target or gat.has_walkable_line(cell, target))
                    and not self._segment_hits_obstacle(*cell, *target, blocked))

        path = gat.find_path(start, target, blocked_cells=blocked, goal_test=reaches_target, goal_radius=1,
                             max_iterations=2500, max_cost=max_steps * 10, diagonal_cost=10, prefer_clearance=False)
        return path if path and path[0] == start else []

    def _sync_client_farm_target(self, world, map_id):
        with world.lock:
            if world.map != map_id:
                return
            gid, action, attack_ts = world.last_mob_attack
            if attack_ts <= self._farm_seen_attack_ts:
                return
            self._farm_seen_attack_ts = attack_ts
            mob = world.entity("mob", gid)
            now = time.monotonic()
            if mob is None or mob.unreachable_until > now or self.ignored_mobs.get(gid, 0.0) > now:
                return

        if self._locked_farm_target_id != gid:
            if self._locked_farm_target_id is not None:
                self._finish_farm_lock("alvo substituído no cliente")
            self._locked_farm_target_id = self._locked_mob_gid = gid
            self._farm_lock_started_at = attack_ts
            self._farm_range_hit_ts = 0.0
            self._farm_range = 1.0
            self._farm_reachability_check = None
            self._farm_target_missing_since = None
        with world.lock:
            world.farm_target_gid = gid
        self._farm_attack_ts = attack_ts
        self._farm_attack_action = action
        self._farm_retry_at = 0.0
        self._in_active_combat = True
        self.locked_combat_mob_gid = gid
        if self.auto_loot and self._engage_state and self._engage_state["kind"] == "drop":
            return
        self._locked_drop_gid = None
        self._drop_lock_until = 0.0
        self._engage_state = self._engagement = None
        self.active_nav_path = None

    @staticmethod
    def _passes_skill_mob_filter(slot: dict, catalog_id: int, name: str) -> bool:
        bl = slot.get("blacklist", ())
        wl = slot.get("whitelist", ())
        id_token = f"#{catalog_id}"
        if any(tok == id_token or tok in name for tok in bl):
            return False
        if wl:
            return any(tok == id_token or tok in name for tok in wl)
        return True

    def _count_mobs_in_3x3(self, world, target_cell, locked_gid, now) -> int:
        tx, ty = int(round(target_cell[0])), int(round(target_cell[1]))
        count = 0
        with world.lock:
            entities = tuple(world.entity_items())
        for (kind, gid), ent in entities:
            if kind != "mob" or ent.unreachable_until > now:
                continue
            if ent.engaged_by_other_until > now and gid != locked_gid:
                continue
            mx, my = ent.track.cell()
            if max(abs(int(round(mx)) - tx), abs(int(round(my)) - ty)) <= 1:
                count += 1
        return count

    @staticmethod
    def _skill_has_sp(slot: dict, world) -> bool:
        if world.player_max_sp <= 0:
            return True
        sp_pct = 100.0 * world.player_sp / world.player_max_sp
        if sp_pct < slot.get("min_sp_pct", 0):
            return False
        skill_id = slot.get("skill_id", 0)
        if skill_id > 0:
            skill_snap = world.skill_snapshot()
            info_cost = skill_snap["skill_info"].get(skill_id, {}).get("sp_cost", 0)
            learned_cost = skill_snap["learned_skill_sp_cost"].get(skill_id, 0)
            sp_cost = info_cost or learned_cost
            if sp_cost > 0 and world.player_sp < sp_cost:
                return False
        return True

    def _check_skill_cast_lock(self, world, position, now) -> bool:
        if now >= self._skill_cast_until:
            return False
        fail_info = world.skill_snapshot().get("last_skill_fail")
        if fail_info and fail_info.get("ts", 0.0) > self._skill_cast_started_at:
            self._skill_cast_until = 0.0
            self._skill_cast_target_gid = None
            return False
        if self._skill_cast_target_gid is not None:
            with world.lock:
                mob = world.entity("mob", self._skill_cast_target_gid)
            if mob is None or math.dist(position, mob.track.cell()) > self._skill_cast_max_range + 0.5:
                self._skill_cast_until = 0.0
                self._skill_cast_target_gid = None
                return False
        return True

    def _sync_learned_skill_from_tcp(self, world, now) -> dict:
        """Aprende skill_id automaticamente via TCP e sincroniza cast_time/cooldowns do servidor."""
        snap = world.skill_snapshot()
        dispatched = self._last_dispatched_skill_slot
        if dispatched is not None:
            dispatch_ts, dispatch_idx = dispatched
            if 0 <= dispatch_idx < len(self.skills_config) and now - dispatch_ts <= 1.5:
                slot = self.skills_config[dispatch_idx]
                if slot.get("skill_id", 0) == 0:
                    cs_pkt = snap.get("last_cs_skill_packet")
                    cast_start = snap.get("last_skill_cast_start")
                    learned_id = 0
                    if cs_pkt and cs_pkt[0] >= dispatch_ts - 0.1 and cs_pkt[2] > 0:
                        learned_id = int(cs_pkt[2])
                    elif cast_start and cast_start[0] >= dispatch_ts - 0.1 and cast_start[1] > 0:
                        learned_id = int(cast_start[1])
                    if learned_id > 0:
                        slot["skill_id"] = learned_id
                        save_skills_config(self.skills_config)
                        print(f"[Skills] Skill ID #{learned_id} aprendido via TCP para '{slot['name'] or slot['hotkey']}'.", flush=True)
        cast_start = snap.get("last_skill_cast_start")
        if cast_start and now - cast_start[0] <= 0.5 and len(cast_start) > 2 and cast_start[2] > 0:
            self._skill_cast_until = max(self._skill_cast_until, cast_start[0] + cast_start[2] / 1000.0 + 0.1)
        fail_info = snap.get("last_skill_fail")
        if fail_info and time.time() - fail_info.get("ts", 0.0) <= 0.5:
            fail_id = fail_info.get("skill_id", 0)
            for idx, slot in enumerate(self.skills_config):
                if (fail_id and slot.get("skill_id") == fail_id) or (
                    dispatched is not None and dispatched[1] == idx and now - dispatched[0] <= 1.0
                ):
                    self._skill_cooldowns[idx] = max(self._skill_cooldowns.get(idx, 0.0), now + 0.8)
        return snap

    def _tick_auto_buff_skills(self, world, now) -> bool:
        snap = self._sync_learned_skill_from_tcp(world, now)
        if self._pending_buff_key is not None:
            idx, slot, completion = self._pending_buff_key
            if not completion.done():
                return True
            self._pending_buff_key = None
            if (sent_at := completion.result()) is not None:
                self._record_skill_use(idx, slot, sent_at)
                return True
            self._skill_cooldowns[idx] = now + 0.5
        if (not self.auto_farm or not self._hud_can_run() or not self.skills_config
                or self._pending_skill_target is not None or self._precombat_cursor_armed):
            return False
        if not self._game_is_foreground() or self._manual_mouse_button_pressed() or self._automation_click_active():
            return False
        active_statuses = snap.get("active_status_ids", set())
        server_cds = snap.get("skill_cooldowns_until", {})
        for idx, slot in enumerate(self.skills_config):
            if not slot["enabled"] or slot["mode"] != "Auto-Buff / Suporte":
                continue
            sk_id = slot.get("skill_id", 0)
            if now < max(self._skill_cooldowns.get(idx, 0.0), server_cds.get(sk_id, 0.0)):
                continue
            if slot["status_id"] > 0 and slot["status_id"] in active_statuses:
                continue
            if not self._skill_has_sp(slot, world):
                continue
            vk = SKILL_HOTKEY_TO_VK.get(slot["hotkey"])
            completion = Future()
            map_id = world.map
            if vk and self._send_game_key(
                vk, completion=completion,
                allowed=lambda: self.auto_farm and self._hud_can_run() and world.map == map_id,
            ):
                self._last_dispatched_skill_slot = (now, idx)
                self._pending_buff_key = (idx, slot, completion)
                return True
        return False

    def _record_skill_use(self, idx, slot, sent_at, gid=None):
        self._skill_cooldowns[idx] = sent_at + slot["cooldown_sec"]
        if gid is not None and slot["min_mobs_3x3"] > 1:
            self._active_splash_gid = gid
            self._active_splash_until = sent_at + max(1.0, slot["cooldown_sec"], slot["cast_time_sec"] + 0.5)
        if slot["cast_time_sec"] > 0:
            self._skill_cast_until = sent_at + slot["cast_time_sec"]
            self._skill_cast_started_at = time.time()
            self._skill_cast_target_gid = gid
            self._skill_cast_max_range = slot["max_range"]
            self._skill_cast_skill_id = slot["skill_id"]

    def _try_combat_skill(self, snapshot, position, gid, mob, now) -> str:
        if not self.skills_config:
            return "none"
        world = self.feed.world
        skill_snap = self._sync_learned_skill_from_tcp(world, now)
        if self._check_skill_cast_lock(world, position, now):
            return "used"
        if not self._game_is_foreground() or self._manual_mouse_button_pressed() or self._automation_click_active():
            return "none"

        pending = self._pending_skill_target
        if pending is not None:
            if (pending["map"], pending["gid"]) != (snapshot.map, gid):
                self._pending_skill_target = None
            else:
                return self._finish_combat_skill_input(snapshot, position, mob, pending, now)

        cell = mob.track.cell()
        distance = math.dist(position, cell)
        server_cds = skill_snap.get("skill_cooldowns_until", {})

        for idx, slot in enumerate(self.skills_config):
            if not slot["enabled"] or slot["mode"] != "Combate (Alvo / Área)":
                continue
            if not self._skill_has_sp(slot, world):
                continue
            sk_id = slot.get("skill_id", 0)
            if now < max(self._skill_cooldowns.get(idx, 0.0), server_cds.get(sk_id, 0.0)):
                continue
            if distance > slot["max_range"]:
                self._skill_approach_range = slot.get("skill_range", slot["max_range"])
                return "approach"

            hwnd = self.get_game_hwnd()
            if not hwnd:
                return "none"
            terrain = getattr(self.feed, "terrain", None)
            gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
            height = gat.get_elevation if gat else flat_height
            viewport = snapshot.viewport
            project = self.feed.camera.projector(viewport, getattr(self.feed, "_cam", None) or position, height)
            resolve = self._safe_click_resolver(snapshot, project)
            aimed = self._live_sprite_aim(("mob", gid), snapshot.map, viewport, height, mob, time.perf_counter() + 0.06)
            if aimed is None:
                continue
            aimed_cell, uv = aimed
            screen = resolve(aimed_cell, uv)
            if not screen or self._is_point_in_hud(*screen, margin=10):
                continue
            vk = SKILL_HOTKEY_TO_VK.get(slot["hotkey"])
            completion = Future()

            def allowed():
                return (self._hud_can_run() and self._locked_farm_target_id == gid
                        and world.map == snapshot.map and world.entity("mob", gid) is mob
                        and not world.is_map_loading and not world.npc_dialog_active)

            if vk and self._send_game_key(vk, completion=completion, allowed=allowed):
                self._last_dispatched_skill_slot = (now, idx)
                self._pending_skill_target = {
                    "map": snapshot.map, "gid": gid, "idx": idx, "slot": slot,
                    "key": completion, "allowed": allowed, "click": None,
                }
                return "used"
        return "none"

    def _finish_combat_skill_input(self, snapshot, position, mob, pending, now):
        key = pending["key"]
        if not key.done():
            return "used"
        sent_at = key.result()
        idx, slot, gid = pending["idx"], pending["slot"], pending["gid"]
        click = pending["click"]
        if click is not None and not click.done():
            return "used"
        if click is not None and click.result()[1]:
            self._pending_skill_target = None
            self._record_skill_use(idx, slot, click.result()[0], gid)
            return "used"
        if sent_at is None or now - sent_at > 0.85 or not pending["allowed"]():
            self._pending_skill_target = None
            self._skill_cooldowns[idx] = now + 0.5
            return "none"

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.cached_gats.get(snapshot.map) if terrain else None
        height = gat.get_elevation if gat else flat_height
        viewport = snapshot.viewport
        project = self.feed.camera.projector(viewport, getattr(self.feed, "_cam", None) or position, height)
        resolve = self._safe_click_resolver(snapshot, project)

        def aim(lead):
            if not pending["allowed"]():
                return None
            latest = self._live_sprite_aim(("mob", gid), snapshot.map, viewport, height, mob,
                                           time.perf_counter() + lead)
            if latest is None:
                return None
            world = self.feed.world
            with world.lock:
                current_position = world.player.cell() if world.player else position
            target_cell, uv = latest
            if math.dist(current_position, target_cell) > slot["max_range"]:
                return None
            if gat is not None and not self._has_gat_line_of_sight(*current_position, *target_cell, gat):
                return None
            return resolve(target_cell, uv)

        if screen := aim(0.075):
            click = Future()
            if self._navigation_click(
                snapshot, gat, mob.track.cell(), screen, target_point=aim, ground=False,
                retry_key=(snapshot.map, "skill", gid, idx), completion=click,
            ):
                pending["click"] = click
        return "used"

    # ── Parte 3: Máquina de estados Pré-Combate ──────────────────────────────
    _PRECOMBAT_SPRITE_OFFSETS = [(0, -22), (0, -30), (-12, -22), (12, -22)]
    _PRECOMBAT_MAX_CLICK_FAILURES = 3
    _PRECOMBAT_WAIT_S = 0.300       # aguarda sinal TCP até 300 ms
    _PRECOMBAT_KEY_DELAY_S = 0.080  # pausa entre hotkey e clique (80 ms)

    def _precombat_pending_slots(self, gid: int) -> list:
        """Retorna slots de modo 'Pré-Combate (Abertura / Furto)' ainda não DONE/FAILED para este mob."""
        result = []
        for idx, slot in enumerate(self.skills_config):
            if not slot["enabled"] or slot["mode"] != "Pré-Combate (Abertura / Furto)":
                continue
            state = self._precombat_states.get((gid, idx), "PENDING")
            if state not in ("DONE", "FAILED"):
                result.append((idx, slot))
        return result

    def _tick_precombat_skill(self, snapshot, position, gid, mob, now) -> str:
        """
        Executa a sequência pré-combate para um mob_gid.
        Retorna:
          "blocking"  — ainda em curso; não enviar ataque normal nem andar com cursor armado
          "casting"   — skill em conjuração; não mover nem atacar
          "done"      — todos os slots concluídos; libera ataque
          "approach"  — precisa se aproximar (antes de armar a skill)
          "none"      — nenhum slot pré-combate configurado
        """
        world = self.feed.world
        snap = self._sync_learned_skill_from_tcp(world, now)
        pending_slots = self._precombat_pending_slots(gid)
        if not pending_slots:
            self._precombat_cursor_armed = False
            return "none"

        # Bloqueia enquanto cast ativo (evita cancelar conjuração)
        if now < self._precombat_cast_until:
            return "casting"

        idx, slot = pending_slots[0]
        key = (gid, idx)
        state = self._precombat_states.get(key, "PENDING")
        attempts = self._precombat_click_attempts.get(key, 0)
        max_attempts = 3 if slot.get("repeat_until_success", True) else 1
        skill_range = slot.get("skill_range", 2.3)

        # Fase ARMING: aguarda hotkey ser enviada + 80 ms, então clica no sprite do monstro
        if state == "ARMING":
            key_future = self._precombat_key_future
            if key_future is not None and not key_future.done():
                return "blocking"
            sent_at = key_future.result() if key_future is not None else None
            if sent_at is None:
                self._precombat_cursor_armed = False
                new_att = attempts + 1
                self._precombat_click_attempts[key] = new_att
                self._precombat_states[key] = "FAILED" if new_att >= max_attempts else "PENDING"
                return "done" if self._precombat_states[key] == "FAILED" else "blocking"
            if time.monotonic() - sent_at < self._PRECOMBAT_KEY_DELAY_S:
                return "blocking"

            hwnd = self.get_game_hwnd()
            if not hwnd:
                self._precombat_cursor_armed = False
                return "none"
            terrain = getattr(self.feed, "terrain", None)
            gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
            height = gat.get_elevation if gat else flat_height
            viewport = snapshot.viewport
            project = self.feed.camera.projector(viewport, getattr(self.feed, "_cam", None) or position, height)
            resolve = self._safe_click_resolver(snapshot, project)

            offset_index = min(attempts, len(self._PRECOMBAT_SPRITE_OFFSETS) - 1)
            dx, dy = self._PRECOMBAT_SPRITE_OFFSETS[offset_index]
            aimed = self._live_sprite_aim(("mob", gid), snapshot.map, viewport, height, mob, now + 0.04)
            raw_screen = None
            if aimed is not None:
                aimed_cell, uv = aimed
                raw_screen = resolve(aimed_cell, uv)
            if raw_screen and self._is_point_in_hud(*raw_screen, margin=10):
                raw_screen = None

            if raw_screen is None:
                self._precombat_cursor_armed = False
                new_att = attempts + 1
                self._precombat_click_attempts[key] = new_att
                self._precombat_states[key] = "FAILED" if new_att >= max_attempts else "PENDING"
                return "done" if self._precombat_states[key] == "FAILED" else "blocking"

            screen = (raw_screen[0] + dx, raw_screen[1] + dy)
            # Garante que não está clicando na célula do próprio jogador
            player_screen = resolve(tuple(map(round, position)), (0.5, 0.5))
            if player_screen and math.dist(screen, player_screen) < 12:
                screen = (raw_screen[0], raw_screen[1] - 26)

            hover_reset_pt = None
            if self._precombat_need_hover_reset:
                vx, vy, vw, vh = viewport
                hx = max(vx + 10, min(vx + vw - 10, screen[0] + 30))
                hy = max(vy + 10, min(vy + vh - 10, screen[1] - 30))
                hover_reset_pt = (hx, hy)

            cell = mob.track.cell()
            self._navigation_click(
                snapshot, gat, cell, screen, ground=False,
                hover_reset=self._precombat_need_hover_reset,
                hover_reset_point=hover_reset_pt,
                retry_key=(snapshot.map, "precombat", gid, idx, attempts),
                allow_offsets=False,
            )
            self._precombat_need_hover_reset = False
            cs_ref = snap.get("last_cs_skill_packet")
            self._precombat_last_cs_ts = cs_ref[0] if cs_ref else now
            self._precombat_states[key] = "WAITING"
            self._precombat_wait_until = now + self._PRECOMBAT_WAIT_S
            self._precombat_active_slot = slot
            self._farm_attack_ts = now
            print(f"[PreCombate] Slot#{idx} skill#{slot.get('skill_id', 0)} → mob#{gid} tentativa #{attempts + 1}", flush=True)
            return "blocking"

        # Fase WAITING: aguarda sinal TCP por até 300 ms após hotkey+clique
        if state == "WAITING":
            cs_pkt = snap.get("last_cs_skill_packet")
            cast_start = snap.get("last_skill_cast_start")
            executed = snap.get("last_skill_executed")
            failed = snap.get("last_skill_failed")
            item_added = snap.get("last_item_added")
            skill_id = slot.get("skill_id", 0)
            ref_ts = self._precombat_last_cs_ts

            # Conjuração iniciada pelo servidor (0x07FB / 0x013E)
            if (cast_start and cast_start[0] >= ref_ts
                    and (skill_id == 0 or cast_start[1] == skill_id)):
                self._precombat_cursor_armed = False
                cast_ms = cast_start[2] if len(cast_start) > 2 else 0
                self._precombat_cast_until = now + max(0.0, cast_ms / 1000.0 + 0.15)
                self._precombat_states[key] = "CASTING"
                return "casting"

            # 0x09CB (result=1) ou 0x0B41 (item adicionado ao inventário): sucesso confirmado
            executed_ok = (
                executed and executed[0] >= ref_ts
                and (skill_id == 0 or executed[1] == skill_id)
                and (len(executed) < 4 or executed[3] != 0)
            )
            item_ok = item_added and item_added[0] >= ref_ts
            if executed_ok or item_ok:
                self._precombat_cursor_armed = False
                self._precombat_states[key] = "DONE"
                return "done" if not self._precombat_pending_slots(gid) else "blocking"

            # 0x0110 (ZC_SKILL_FAILED): acertou o monstro mas falhou na chance
            if (failed and failed[0] >= ref_ts
                    and (skill_id == 0 or failed[1] == skill_id)):
                self._precombat_cursor_armed = False
                if not slot.get("repeat_until_success", True):
                    self._precombat_states[key] = "DONE"
                else:
                    new_att = attempts + 1
                    self._precombat_click_attempts[key] = new_att
                    self._precombat_states[key] = "DONE" if new_att >= max_attempts else "PENDING"
                return "done" if not self._precombat_pending_slots(gid) else "blocking"

            # Cliente enviou apenas 0x035F/0x0085 (andou no chão porque errou o sprite)
            walked_instead = world.last_move_ts >= ref_ts and not (cs_pkt and cs_pkt[0] >= ref_ts)
            if walked_instead or now >= self._precombat_wait_until:
                self._precombat_cursor_armed = False
                if cs_pkt and cs_pkt[0] >= ref_ts and cs_pkt[1] in (0x0438, 0x0436):
                    self._precombat_states[key] = "DONE"
                    return "done" if not self._precombat_pending_slots(gid) else "blocking"
                self._precombat_need_hover_reset = True
                new_att = attempts + 1
                self._precombat_click_attempts[key] = new_att
                if new_att >= self._PRECOMBAT_MAX_CLICK_FAILURES:
                    self._precombat_states[key] = "FAILED"
                    print(f"[PreCombate] Slot#{idx} mob#{gid}: {new_att} falhas consecutivas; liberando combate.", flush=True)
                    return "done" if not self._precombat_pending_slots(gid) else "blocking"
                self._precombat_states[key] = "PENDING"
                return "blocking"

            return "blocking"

        # Fase CASTING: cast terminou
        if state == "CASTING":
            self._precombat_cursor_armed = False
            self._precombat_states[key] = "DONE"
            return "done" if not self._precombat_pending_slots(gid) else "blocking"

        if attempts >= max_attempts:
            self._precombat_cursor_armed = False
            self._precombat_states[key] = "DONE"
            return "done" if not self._precombat_pending_slots(gid) else "blocking"

        # Não arma a skill enquanto caminha de longe
        cell = mob.track.cell()
        distance = math.dist(position, cell)
        if distance > skill_range:
            self._skill_approach_range = skill_range
            return "approach"
        if world._is_player_walking():
            return "blocking"

        server_cds = snap.get("skill_cooldowns_until", {})
        sk_id = slot.get("skill_id", 0)
        if now < max(self._skill_cooldowns.get(idx, 0.0), server_cds.get(sk_id, 0.0)):
            return "blocking"

        if not self._skill_has_sp(slot, world):
            self._precombat_states[key] = "FAILED"
            return "done" if not self._precombat_pending_slots(gid) else "blocking"

        if now < self._skill_cast_until:
            return "blocking"

        vk = SKILL_HOTKEY_TO_VK.get(slot["hotkey"])
        if not vk:
            self._precombat_states[key] = "FAILED"
            return "done" if not self._precombat_pending_slots(gid) else "blocking"

        key_future = Future()

        def _allowed():
            return (self._hud_can_run() and self._locked_farm_target_id == gid
                    and world.map == snapshot.map and world.entity("mob", gid) is mob
                    and not world.is_map_loading and not world.npc_dialog_active)

        if not self._send_game_key(vk, completion=key_future, allowed=_allowed):
            new_att = attempts + 1
            self._precombat_click_attempts[key] = new_att
            if new_att >= max_attempts:
                self._precombat_states[key] = "FAILED"
            return "blocking"

        self._last_dispatched_skill_slot = (now, idx)
        self._precombat_key_future = key_future
        self._precombat_cursor_armed = True
        self._precombat_states[key] = "ARMING"
        self._precombat_active_slot = slot
        self._farm_attack_ts = now
        return "blocking"

    def _approach_skill_range(self, snapshot, position, cell, max_range):
        """Aproxima do alvo por célula do chão sem clicar no sprite (evita ataque normal antes da skill)."""
        world = self.feed.world
        if world._is_player_walking():
            return True
        hwnd = self.get_game_hwnd()
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
        if not hwnd or gat is None:
            return False
        start = tuple(map(round, position))
        target_cell = tuple(map(round, cell))
        now = time.monotonic()
        key = snapshot.map, self._locked_farm_target_id
        if self._skill_approach_step and self._skill_approach_step[0] == key:
            _, clicked_at, previous, ground, point, retried = self._skill_approach_step
            result = self._walk_step_result(world, position, previous, point, clicked_at, now)
            if result == "waiting":
                return True
        else:
            result = None
        self._skill_approach_step = None
        nav_blocked, penalties = self._navigation_obstacles(snapshot.map, gat)
        blocked = nav_blocked | {target_cell}
        project = self.feed.camera.projector(
            snapshot.viewport, getattr(self.feed, "_cam", None) or position, gat.get_elevation,
        )
        resolve = self._safe_click_resolver(snapshot, project)

        def ground_point(ground):
            return self._walk_ground_point(gat, position, ground, blocked, resolve)

        ring_max = max(1.5, min(max_range, 11.0))
        ring_min = 1.0

        def ring(ground):
            return (
                ground != start
                and ground not in blocked
                and ring_min <= math.dist(ground, cell) <= ring_max
                and not self._segment_hits_obstacle(*ground, *cell, nav_blocked)
                and self._has_gat_line_of_sight(*ground, *cell, gat)
            )

        if result == "stalled":
            if not retried and (screen := ground_point(ground)):
                if self._navigation_click(snapshot, gat, ground, screen, hwnd=hwnd, hover_reset=True):
                    self._skill_approach_step = (key, now, position, ground, screen, True)
                return True
            blocked.add(ground)
        path = self._approach_path(gat, start, target_cell, blocked, penalties, ring=ring, ring_radius=ring_max + 1)
        detour = (not gat.has_walkable_line(start, target_cell)
                  or self._segment_hits_obstacle(*start, *target_cell, nav_blocked))
        if not path or (detour and len(path) - 1 > FARM_MAX_PATH_STEPS):
            return False
        self.active_nav_path = path
        self.active_nav_color = "#A78BFA"
        stride = self._pick_walk_stride(path[:15], position, blocked, ground_point)
        if stride is None:
            return False
        ground_cell, screen = stride
        if self._navigation_click(snapshot, gat, ground_cell, screen, hwnd=hwnd,
                                  retry_key=(snapshot.map, "ground", ground_cell)):
            self._skill_approach_step = (key, now, position, ground_cell, screen, False)
            if detour:
                self._farm_route = {"cell": target_cell, "path": path, "index": 0, "started": True}
            return True
        return False

    def _continue_farm_pursuit(self, snapshot, position, now, gid):
        pursuit = self._farm_pursuit
        target = pursuit["cell"]
        world = self.feed.world
        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(snapshot.map) if terrain and snapshot.map else None
        if now >= pursuit["until"] or gat is None:
            self._finish_farm_lock("tempo limite ou terreno indisponível na perseguição", resume_patrol=True)
            return False
        if math.dist(position, target) <= 1.5 and gat.has_walkable_line(position, target):
            self._finish_farm_lock("última posição alcançada sem reencontrar o alvo", resume_patrol=True)
            return False
        if pending := pursuit["pending"]:
            clicked_at, previous, point = pending
            result = self._walk_step_result(world, position, previous, point, clicked_at, now)
            if result == "waiting":
                return True
            pursuit["pending"] = None
            if result == "stalled":
                self._finish_farm_lock("passo bloqueado na perseguição", resume_patrol=True)
                return False
        if world._is_player_walking():
            return True

        blocked, _ = self._navigation_obstacles(snapshot.map, gat)
        project = self.feed.camera.projector(snapshot.viewport, getattr(self.feed, "_cam", None) or position,
                                             gat.get_elevation)
        resolve = self._safe_click_resolver(snapshot, project)

        def ground_point(cell):
            return self._walk_ground_point(gat, position, cell, blocked, resolve)

        for _ in range(2):
            path = pursuit["path"]
            if path:
                index = min(range(pursuit["index"], len(path)), key=lambda i: math.dist(position, path[i]))
                pursuit["index"] = index
                if math.dist(position, path[index]) <= 2.0:
                    remaining = path[index + 1:]
                    self.active_nav_path = path[index:]
                    self.active_nav_color = "#F87171"
                    stride = self._pick_walk_stride(remaining[:11], position, blocked, ground_point)
                    if stride:
                        cell, point = stride

                        def aim(_lead):
                            with world.lock:
                                if (self._farm_pursuit is pursuit and self._locked_farm_target_id == gid
                                        and world.entity("mob", gid) is None
                                        and time.monotonic() < pursuit["until"]):
                                    return point
                            return None

                        if self._navigation_click(snapshot, gat, cell, point, target_point=aim,
                                                  retry_key=(snapshot.map, "farm-last-position", gid, cell)):
                            pursuit["pending"] = (now, position, point)
                        return True
            if pursuit["replanned"]:
                break
            pursuit["replanned"] = True
            start = tuple(map(round, position))
            path = self._farm_path(gat, start, target, blocked)
            if not path or len(path) - 1 > FARM_MAX_PATH_STEPS:
                break
            if len(path) == 1:
                self._finish_farm_lock("última posição alcançada sem reencontrar o alvo", resume_patrol=True)
                return False
            pursuit.update(path=path, index=0)
        self._finish_farm_lock("rota bloqueada até a última posição do alvo", resume_patrol=True)
        return False

    def _continue_farm_lock(self, snapshot, position, now, *, direct_only=False):
        gid = self._locked_farm_target_id
        if gid is None:
            return False
        world = self.feed.world
        with world.lock:
            move_ts = world.last_move_ts
            mob = world.entity("mob", gid)
            hit_ts = world.mobs_damaged_by_me.get(gid, 0.0)
            other_hit_ts = world.mobs_damaged_by_others.get(gid, 0.0)
            player = world.player
        if mob is None:
            return (self._continue_farm_pursuit(snapshot, position, now, gid)
                    if self._farm_pursuit is not None and not direct_only else True)
        if self._farm_pursuit is not None:
            self._farm_pursuit = None
            self._farm_progress = None
            print(f"[FarmLock] Alvo #{gid} reencontrado. Retomando combate.", flush=True)

        cell = mob.track.cell()
        distance = math.dist(position, cell)
        progress = self._farm_progress
        route = self._farm_route
        advanced = False
        if route and route["started"]:
            path = route["path"]
            index = min(range(route["index"], len(path)), key=lambda i: math.dist(position, path[i]))
            if index > route["index"] and math.dist(position, path[index]) <= 1.5:
                route["index"] = index
                advanced = True
        if progress is None or progress[0] != gid or advanced or distance <= progress[1] - 0.75:
            progress = self._farm_progress = (gid, distance, now)
        if now - max(progress[2], hit_ts) >= 8.0:
            with world.lock:
                if world.mobs_damaged_by_me.get(gid, 0.0) > hit_ts:
                    return True
                current = world.entity("mob", gid)
                if current is not None and current.created_at == mob.created_at:
                    current.unreachable_until = max(current.unreachable_until, now + 1.5)
            self._finish_farm_lock("8 s sem aproximação ou dano confirmado", resume_patrol=True)
            return False
        walking = world._is_player_walking()
        following = walking and player and math.dist(player.target, cell) < distance
        if hit_ts >= self._farm_lock_started_at and hit_ts > self._farm_range_hit_ts:
            self._farm_range_hit_ts = hit_ts
            self._farm_range = max(self._farm_range, distance)
        attack_range = max(self._farm_range, getattr(world, "player_attack_range", 1.0))

        last_event = max(self._farm_lock_started_at, self._farm_attack_ts, hit_ts, other_hit_ts, mob.last_aggro_ts)
        if (not walking and now - last_event > 10.0
                and self._farm_target_unreachable(snapshot, position, cell, attack_range)):
            with world.lock:
                current = world.entity("mob", gid)
                latest_target, _, latest_ts = world.last_mob_attack
                new_request = latest_target == gid and latest_ts > self._farm_attack_ts
                if (current is None or new_request
                        or world.mobs_damaged_by_me.get(gid, 0.0) > hit_ts
                        or world.mobs_damaged_by_others.get(gid, 0.0) > other_hit_ts
                        or current.last_aggro_ts > last_event or world._is_player_walking()
                        or tuple(map(round, current.track.cell())) != tuple(map(round, cell))):
                    self._farm_reachability_check = None
                    return True
            state = self._engage_state
            if state and (state["kind"], state["gid"]) == ("mob", gid):
                state["retry_at"] = now + 0.65
            return True

        if (self._in_active_combat and self._farm_attack_action == 0x07
                and move_ts <= self._farm_attack_ts and distance <= attack_range + 1.5
                and (now - max(self._farm_attack_ts, mob.last_combat_damage) < 1.8
                     or now < self._farm_retry_at)):
            self.active_nav_path = None
            return True
        if following and not direct_only:
            return True

        skill_state = self._try_combat_skill(snapshot, position, gid, mob, now)
        if skill_state == "used":
            self.active_nav_path = None
            return True
        if skill_state == "approach" and not direct_only:
            if self._approach_skill_range(snapshot, position, cell, getattr(self, "_skill_approach_range", 2.0)):
                return True

        # ── Parte 3: Pré-Combate — executa antes do ataque normal ────────────
        precombat = self._tick_precombat_skill(snapshot, position, gid, mob, now)
        if precombat == "casting":
            # Em conjuração: não mover, não atacar
            self.active_nav_path = None
            return True
        if precombat == "blocking":
            # Nunca clicar no chão enquanto o cursor estiver armado com habilidade
            return True
        if precombat == "approach" and not direct_only and not self._precombat_cursor_armed:
            if self._approach_skill_range(snapshot, position, cell,
                                          getattr(self, "_skill_approach_range", 2.3)):
                return True

        self._smart_engage_target((gid, mob), "mob", snapshot, position, direct_only=direct_only)
        return self._locked_farm_target_id == gid

    def _continue_loot_lock(self, snapshot, position, entities, drops, now):
        world = self.feed.world
        live = dict(entities)

        drop_gid = self._locked_drop_gid
        drop = live.get(("drop", drop_gid))
        ack = world.last_pickup_ack
        entry = next((entry for entry in drops if entry[2] == drop_gid), None)
        if (drop is None or entry is None or (drops and drops[0][0] == 1 and entry[0] != 1)
                or (ack and ack[0] == drop_gid and ack[2] == 0 and ack[1] >= self._drop_lock_until - 1.5)):
            self._locked_drop_gid = None
            if self._engage_state and self._engage_state["kind"] == "drop":
                self._engage_state = self._engagement = None
            return False
        state = self._engage_state
        has_attempt = bool(state and state["kind"] == "drop" and state["gid"] == drop_gid)
        if (self.auto_farm or self.auto_loot) and (entry[0] == 1 or now < self._drop_lock_until or has_attempt):
            if self._smart_engage_target((drop_gid, drop), "drop", snapshot, position, priority_loot=entry[0] == 1):
                return True
            self._locked_drop_gid = None
        return False

    def _try_auto_loot(self, snapshot, position, drops, *, direct_only=False):
        world = self.feed.world
        if not drops or self._client_combat_blocks_ground(world, snapshot.map):
            return False
        walking = world._is_player_walking()
        for tier, distance, gid, drop in drops[:3]:
            if walking and distance > (3.0 if tier == 1 else 2.0):
                continue
            if gid != self._locked_drop_gid:
                self._locked_drop_gid = gid
                self._drop_lock_until = time.monotonic() + 1.5
            if self._smart_engage_target((gid, drop), "drop", snapshot, position,
                                         direct_only=direct_only, priority_loot=tier == 1):
                return True
            self._locked_drop_gid = None
        return False

    @staticmethod
    def _path_cost(path):
        return sum(14 if a[0] != b[0] and a[1] != b[1] else 10 for a, b in zip(path, path[1:]))

    def _approach_path(self, gat, start, target_cell, blocked, penalties, *, ring=None, ring_radius=None):
        if gat is None:
            return []
        if ring is None:
            path = gat.find_path(start, target_cell, blocked_cells=blocked, penalty_cells=penalties)
            return path if path and path[-1] == target_cell else []
        return gat.find_path(start, target_cell, blocked_cells=blocked, penalty_cells=penalties,
                             max_iterations=3500, goal_test=ring, goal_radius=ring_radius)

    def _live_sprite_aim(self, key, map_id, viewport, height, expected, at):
        world = self.feed.world
        kind, _ = key
        with world.lock:
            target = world.entity(*key)
            player = world.player
            if not target or not player or world.map != map_id or self.current_viewport != viewport:
                return None
            if (target.catalog_id != expected.catalog_id
                    or (kind == "mob" and target.unreachable_until > time.monotonic()
                        and key != ("mob", self._locked_farm_target_id))):
                return None
            if kind == "drop" and target.created_at != expected.created_at:
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

    def _smart_engage_target(self, target, kind, snapshot, position, *, direct_only=False, priority_loot=False):
        gid, entity = target
        world = self.feed.world
        now = time.monotonic()
        key = (kind, gid)
        loot_key = (gid, entity.created_at) if kind == "drop" else None
        loot_attempts, _ = self._loot_attempts.get(loot_key, (0, None))
        with world.lock:
            live = world.entity(*key)
            committed = kind == "mob" and gid == self._locked_farm_target_id
            if (not live or world.map != snapshot.map
                    or (live.unreachable_until > now and not (committed or priority_loot))):
                return False
            if kind == "drop" and live.created_at != entity.created_at:
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
        if state is None or state["target"] != (*key, snapshot.map, live.created_at):
            state = self._engage_state = {
                "kind": kind, "gid": gid, "map": snapshot.map,
                "target": (*key, snapshot.map, live.created_at), "pending": None,
                "failed": set(), "direct_failures": 0,
            }
        self._engagement = {"key": key, "phase": state.get("phase", "direct")}

        def release():
            self._engage_state = None
            self._engagement = None
            self.active_nav_path = None
            return False

        def pause_target():
            if committed:
                state["pending"] = None
                state["direct_failures"] = 0
                state["failed"].clear()
                state.pop("path_key", None)
                state["retry_at"] = now + 0.65
                return True
            with world.lock:
                current = world.entity(*key)
                if current and world.map == snapshot.map:
                    if kind == "drop" and (current.suspected_other_owner or current.retry_after):
                        current.retry_after = max(current.retry_after, time.time() + 3.5)
                    else:
                        current.unreachable_until = now + (4.0 if kind == "drop" else 1.5)
                    if kind == "mob" and world.locked_combat_mob_gid == gid:
                        world.locked_combat_mob_gid = None
            if committed:
                self._finish_farm_lock("sem avanço após as retentativas")
            return release()

        if now < state.get("retry_at", 0.0):
            return True
        failed = None
        walking = world._is_player_walking()
        if state["pending"]:
            clicked_at, start_xy, last_cell, last_point, phase = state["pending"]
            aborted = self._touch_click_aborted(last_point, clicked_at)
            if phase != "direct":
                aborted = aborted or self._visual_walk_failed(last_point, clicked_at)
            elapsed = now - self._touch_click_finished_at(last_point, clicked_at)
            if kind == "drop":
                ack = (pickup_ack if phase == "direct" and pickup_ack and pickup_ack[0] == gid
                       and pickup_ack[1] >= clicked_at else None)
                if phase == "direct":
                    if ack and ack[2] == 0:
                        self._loot_attempts.pop(loot_key, None)
                        return release()
                    # O toque pode chegar mesmo quando o PostMessageW é recusado.
                    if ack is None and (elapsed < 0.75 or (walking and elapsed < 1.5)):
                        return True
                    if loot_attempts > len(LOOT_CLICK_OFFSETS) and not aborted:
                        state["loot_reset_done"] = True
                    if math.dist(position, start_xy) < 0.55:
                        state["loot_step_due"] = True
                distance = math.dist(position, live.track.cell())
                if not aborted:
                    if phase == "direct":
                        state["direct_failures"] += 1
                    elif phase == "approach":
                        if distance > 2.0 and (elapsed < 0.95 or (walking and elapsed < 2.25)):
                            return True
                        if distance > 2.0 and math.dist(position, start_xy) < 0.75:
                            state["failed"].add(last_cell)
                    elif phase == "loot-step":
                        if elapsed < 0.65 or (walking and elapsed < 1.5):
                            return True
                        if math.dist(position, last_cell) > 0.55:
                            state["failed"].add(last_cell)
                    elif walking:
                        return True
                    elif math.dist(position, last_cell) > 0.55:
                        if elapsed < 0.65:
                            return True
                        state["failed"].add(last_cell)
                        if len(state["failed"]) >= 3:
                            return pause_target()
                    else:
                        state["direct_failures"] = 0
                if phase == "direct":
                    if aborted:
                        state["direct_failures"] += 1
                    with world.lock:
                        if world.pending_pickup and world.pending_pickup[:2] == (gid, clicked_at):
                            world.pending_pickup = None
                elif aborted:
                    failed = last_cell
                    state["failed"].add(failed)
                state["pending"] = None
            else:
                if (phase == "direct" and not aborted and elapsed < 1.8
                        and max(attack_at, self._farm_attack_ts) < clicked_at):
                    return True
                started_walking = phase == "direct" and moved_at >= clicked_at
                moved = (phase != "direct" and not walking
                         and tuple(map(round, position)) != tuple(map(round, start_xy)))
                if max(attack_at, self._farm_attack_ts) >= clicked_at or started_walking or moved:
                    state["pending"] = None
                    state["failed"].clear()
                    if phase == "direct":
                        state["direct_failures"] = 0
                        if started_walking:
                            state["following"] = True
                    elif phase == "reposition":
                        state["reposition_goal"] = last_cell
                elif phase != "direct" and not aborted and (walking or elapsed < 0.85):
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

        if loot_attempts >= LOOT_MAX_ATTEMPTS:
            with world.lock:
                current = world.entity(*key)
                if current is not None and current.created_at == live.created_at:
                    current.unreachable_until = max(current.unreachable_until, now + 15.0)
            self._loot_attempts[loot_key] = (loot_attempts, position)
            self._click_debug(f"[Loot] Drop #{gid} adiado após {loot_attempts} tentativas. "
                              "A nova tentativa depende de mudar de posição.")
            return release()

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
        nav_blocked, penalties = self._navigation_obstacles(snapshot.map, gat)
        if target_cell in nav_blocked:
            return pause_target()
        aim_at = time.perf_counter() + getattr(self, "_sprite_click_lead", 0.085)

        def aim(at):
            return self._live_sprite_aim(key, snapshot.map, viewport, height, live, at)

        visible = any(obj.key == f"{kind}:{gid}" for obj in snapshot.objects)
        planned = aim(aim_at) if visible else None
        aimed_cell, uv = planned if planned is not None else (cell, None)
        aimed_tile = tuple(map(round, aimed_cell))
        if aimed_tile in nav_blocked:
            return pause_target()
        obstacle_between = self._segment_hits_obstacle(*position, *aimed_cell, nav_blocked)
        if loot_key is not None:
            dx, dy = LOOT_CLICK_OFFSETS[loot_attempts % len(LOOT_CLICK_OFFSETS)]
        elif kind == "mob" and state["direct_failures"]:
            dx, dy = CLICK_RETRY_OFFSETS[(state["direct_failures"] - 1) % len(CLICK_RETRY_OFFSETS)]
        else:
            dx, dy = 0, 0

        def sprite_point(cell_now, uv_now):
            _, _, width, height_px = viewport
            shifted = resolve(cell_now, (uv_now[0] + dx / width, uv_now[1] + dy / height_px))
            return shifted if loot_key is not None else shifted or resolve(cell_now, uv_now)

        point = sprite_point(aimed_cell, uv) if uv is not None else None
        protected_target = visible and point is None

        def attack_started():
            with world.lock:
                _, _, attack_ts = world.last_mob_attack
                return attack_ts > self._farm_seen_attack_ts

        def combat_blocks_click():
            if kind == "drop":
                return self._client_combat_blocks_ground(world, snapshot.map)
            return self._locked_farm_target_id != gid or attack_started()

        def current_target_point(lead):
            if not self._game_is_foreground() or self._manual_mouse_button_pressed():
                return None
            if combat_blocks_click():
                return None
            latest = aim(time.perf_counter() + lead)
            if latest is None:
                return None
            cell_now, uv_now = latest
            with world.lock:
                player_now = world.player.cell() if world.player else position
                if kind == "mob" and direct_only and math.dist(player_now, cell_now) > max(
                    2.5, getattr(world, "player_attack_range", 1.0),
                ):
                    return None
            blocked_now, _ = self._navigation_obstacles(snapshot.map, gat)
            if self._segment_hits_obstacle(*player_now, *cell_now, blocked_now):
                return None
            screen_now = sprite_point(cell_now, uv_now)
            if screen_now and not self._is_point_in_hud(*screen_now, margin=10):
                return screen_now
            return None

        def click(ground, screen, phase):
            if combat_blocks_click():
                return True
            clicked_at = time.monotonic()
            pickup = kind == "drop" and phase == "direct"
            if pickup:
                retry_delay = 1.2 if priority_loot else 3.5
                with world.lock:
                    current = world.entity(*key)
                    if current is None or current.created_at != live.created_at:
                        return False
                    if current.retry_after > time.time():
                        return True
                    world.pending_pickup = (gid, clicked_at, retry_delay)
            def ground_target_point(_lead):
                if combat_blocks_click():
                    return None
                if phase == "loot-step":
                    with world.lock:
                        current = world.entity(*key)
                        if current is None or current.created_at != live.created_at:
                            return None
                return screen

            target_point = current_target_point if phase == "direct" else ground_target_point
            retry_key = ((snapshot.map, kind, gid, live.created_at) if phase == "direct"
                         else (snapshot.map, "ground", ground))
            retry_hover = (loot_attempts >= len(LOOT_CLICK_OFFSETS) and not state.get("loot_reset_done") if pickup
                           else phase == "direct" and state["direct_failures"] == 1)
            if not self._navigation_click(snapshot, gat, ground, screen, hwnd=hwnd,
                                          target_point=target_point, retry_key=retry_key,
                                          ground=phase != "direct", hover_reset=retry_hover,
                                          pickup_click=pickup,
                                          allow_offsets=False,
                                          hover_ms=55 if pickup else None,
                                          hold_ms=85 if pickup else None):
                if pickup:
                    with world.lock:
                        if world.pending_pickup and world.pending_pickup[:2] == (gid, clicked_at):
                            world.pending_pickup = None
                    if now - state.setdefault("loot_refused_at", now) >= 1.0:
                        self._loot_attempts[loot_key] = (loot_attempts + 1, None)
                        state.pop("loot_refused_at")
                return True
            if pickup:
                self._loot_attempts[loot_key] = (loot_attempts + 1, None)
                state.pop("loot_refused_at", None)
            if kind == "mob" and phase == "direct":
                self._farm_retry_at = clicked_at + 1.8
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
                       if gat else distance <= (3.0 if priority_loot else 2.2))
        direct_line = direct_line and not obstacle_between
        if kind == "drop":
            distance = math.dist(position, cell)
            pickup_range = 3.0 if priority_loot else 2.0
            if state.get("loot_step_due") and not walking and not direct_only and gat is not None:
                state.pop("loot_step_due")
                blocked = nav_blocked | state["failed"] | {target_cell}
                nearby = sorted(self._patrol_cells_in_radius(*start, 2),
                                key=lambda ground: (math.dist(ground, cell), math.dist(ground, position)))
                for ground in nearby:
                    if ground != start and math.dist(ground, cell) <= min(pickup_range, max(1.5, distance)):
                        if screen := self._walk_ground_point(gat, position, ground, blocked, resolve):
                            click(ground, screen, "loot-step")
                            if state["pending"]:
                                return True
            if distance <= pickup_range and planned is not None and point is None and direct_line:
                self._loot_attempts[loot_key] = (loot_attempts + 1, None)
                return True
            if distance <= pickup_range and point and direct_line:
                last_attack = world.last_player_attack_ts
                if (live.ownership != "MINE" and now - last_attack < 0.28
                        and state.get("loot_attack_at") != last_attack):
                    state["loot_attack_at"] = last_attack
                    state["loot_click_after"] = now + 0.2
                if walking or live.retry_after > time.time() or now < state.get("loot_click_after", 0.0):
                    return True
                self.active_nav_path = None
                return click(aimed_tile, point, "direct")
            if walking:
                return state["pending"] is not None
            if direct_only and not obstacle_between:
                return release()
            if gat is None or len(state["failed"]) >= 3:
                return pause_target()

        reposition = state["direct_failures"] >= 3
        can_aim = kind == "mob" and point and direct_line and distance <= direct_range
        if can_aim and (not reposition or direct_only):
            self.active_nav_path = None
            handled = click(aimed_tile, point, "direct")
            if committed and state["pending"]:
                self._farm_route = None
            return handled
        if reposition and direct_only and gat is not None and not obstacle_between:
            return True
        if (direct_only and (kind == "mob" or not obstacle_between)) or gat is None or (walking and failed is None and not reposition):
            return pause_target() if failed or reposition else release()

        blocked = nav_blocked | state["failed"] | {target_cell}

        def ground_point(ground):
            return self._walk_ground_point(gat, position, ground, blocked, resolve)

        if reposition and not obstacle_between and kind == "mob":
            nearby = sorted(
                ((target_cell[0] + dx, target_cell[1] + dy)
                 for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                key=lambda ground: (math.dist(ground, cell), math.dist(ground, position)),
            )
            for ground in nearby:
                if ground != start and (screen := ground_point(ground)):
                    self.active_nav_path = [start, ground]
                    self.active_nav_color = "#F87171"
                    return click(ground, screen, "reposition")
            return pause_target()

        ring_min, ring_max = (2.5, 4.0) if protected_target and kind == "mob" else (1.0, 2.0)
        if priority_loot:
            ring_max = 3.0

        def ring(ground):
            return (ground != start and ground not in blocked and ring_min <= math.dist(ground, cell) <= ring_max
                    and (kind != "drop" or self._is_empty_walkable_cell(snapshot.map, gat, ground, blocked))
                    and not self._segment_hits_obstacle(*ground, *cell, nav_blocked)
                    and self._has_gat_line_of_sight(*ground, *cell, gat)
                    and (not protected_target or resolve(ground) is not None))

        path_key = target_cell, ring_min, ring_max, frozenset(state["failed"]), reposition
        path = state.get("path", []) if state.get("path_key") == path_key else []
        if start in path:
            path = path[path.index(start):]
            if len(path) <= 1 or any(ground in blocked for ground in path[1:16]):
                path = []
        else:
            path = []
        if not path and committed and not direct_line:
            route = self._farm_route
            if route and route["cell"] == target_cell:
                remaining = route["path"][route["index"]:]
                if start in remaining:
                    remaining = remaining[remaining.index(start):]
                    if len(remaining) > 1 and ring(remaining[-1]) and not any(
                        cell in blocked for cell in remaining[1:]
                    ):
                        path = remaining
        if not path:
            path = self._approach_path(gat, start, target_cell, blocked, penalties,
                                       ring=ring, ring_radius=ring_max + 1)
        state["path_key"], state["path"] = path_key, path
        if committed and not direct_line and (not path or len(path) - 1 > FARM_MAX_PATH_STEPS):
            self._finish_farm_lock("rota de aproximação bloqueada ou longa demais", resume_patrol=True)
            return False
        if not path:
            if kind == "mob" and gat is not None:
                if committed:
                    return pause_target()
                with world.lock:
                    current = world.entity(*key)
                    if current and world.map == snapshot.map:
                        current.unreachable_until = max(current.unreachable_until, now + 1.5)
                return release()
            return pause_target()
        self.active_nav_path = path or None
        self.active_nav_color = "#F87171" if kind == "mob" else "#FBBF24"
        if obstacle_between:
            stride = self._pick_walk_stride(path[:15], position, blocked, ground_point)
        elif failed:
            stride = self._pick_long_stride_cell(path, 6, 8, position=position, ground_point=ground_point)
            if stride is None:
                stride = next(((ground, screen) for ground in self._retry_lateral_cells(failed, position)
                               if (screen := ground_point(ground))), None)
        else:
            stride = self._pick_long_stride_cell(path, position=position, ground_point=ground_point)
        if stride is None:
            stride = self._pick_walk_stride(path[:15], position, blocked, ground_point)
        if stride:
            handled = click(*stride, "reposition" if reposition and kind == "drop" else "approach")
            if committed and not direct_line and state["pending"]:
                self._farm_route = {"cell": target_cell, "path": path, "index": 0, "started": True}
            return handled
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

    def _game_input_ready(self, hwnd):
        if self.feed.world.is_map_loading or not self.is_valid_ragexe_hwnd(hwnd):
            return False
        root = u32.GetAncestor(hwnd, 2) or hwnd
        if u32.IsIconic(root) or not u32.IsWindowVisible(hwnd):
            return False
        rect = wintypes.RECT()
        return bool(u32.GetClientRect(hwnd, ctypes.byref(rect))
                    and rect.right > rect.left and rect.bottom > rect.top)

    def _move_and_click_sync(self, sx, sy, hwnd, *, hover_reset=False):
        # Os controles da Kafra precisam esperar o clique anterior antes de avançar.
        deadline = time.perf_counter() + 2.0
        while (self._automation_click_active()
               or time.perf_counter() - getattr(self, "_last_click_ts", 0.0) < 0.35):
            if time.perf_counter() >= deadline:
                raise ValueError("tempo limite aguardando o clique anterior.")
            time.sleep(0.01)
        completed_at = getattr(self, "_last_click_end_ts", 0.0)
        if not self._schedule_game_click(sx, sy, hwnd, hover_reset=hover_reset):
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

    @staticmethod
    def _mouse_buttons_down():
        return any(u32.GetAsyncKeyState(key) & 0x8000 for key in (0x01, 0x02, 0x04, 0x05, 0x06))

    @staticmethod
    def _position_cursor(sx, sy):
        cursor = wintypes.POINT()
        def arrived():
            return bool(u32.GetCursorPos(ctypes.byref(cursor)) and (cursor.x, cursor.y) == (sx, sy))

        if arrived() or (u32.SetCursorPos(sx, sy) and arrived()):
            return True
        left, top = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
        width, height = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
        if width < 2 or height < 2 or not (left <= sx < left + width and top <= sy < top + height):
            return False
        move = INPUT(type=INPUT_MOUSE)
        move.mi.dx = round((sx - left) * 65535 / (width - 1))
        move.mi.dy = round((sy - top) * 65535 / (height - 1))
        move.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | 0x4000  # VIRTUALDESK
        return bool(u32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT)) and arrived())

    def _move_cursor_released(self, sx, sy):
        if not win32_input.release_pending_touch() or self._mouse_buttons_down():
            return False
        return win32_input.move_cursor_with_touch_hover(sx, sy) or self._position_cursor(sx, sy)

    def _hover_reset_away_point(self, sx, sy, viewport=None):
        """Retorna um ponto seguro ~3-4 células distante do alvo para reset de hover sem clicar nele."""
        vp = viewport or getattr(self, "current_viewport", None)
        if vp:
            vx, vy, vw, vh = vp
            min_x, max_x = vx + 12, vx + vw - 12
            min_y, max_y = vy + 12, vy + vh - 12
        else:
            min_x, max_x = 12, getattr(self, "_screen_w", 1920) - 12
            min_y, max_y = 12, getattr(self, "_screen_h", 1080) - 12
        for dx, dy in (
            (120, -75), (-120, -75), (120, 75), (-120, 75),
            (130, 0), (-130, 0), (0, -110), (0, 110),
            (85, -85), (-85, -85),
        ):
            ax, ay = int(sx + dx), int(sy + dy)
            if (min_x <= ax <= max_x and min_y <= ay <= max_y
                    and not self._is_point_in_hud(ax, ay, margin=10)
                    and not self._is_point_in_game_ui(ax, ay)
                    and not self._is_point_in_vending_shop(ax, ay)):
                return (ax, ay)
        return None

    def _schedule_game_click(self, sx, sy, game_hwnd=None, *, target_point=None, retry_key=None,
                             hover_reset=False, hover_reset_point=None, completion=None,
                             allow_offsets=True, direct_touch=False, trace_touch=False,
                             click_action=None, pickup_click=False, hover_ms=None, hold_ms=None):
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
        if not self._game_input_ready(hwnd):
            self._click_debug("move_and_click: janela do Ragexe indisponível")
            return False
        self.game_hwnd = hwnd
        game_window = self._remember_game_window(hwnd)
        root_hwnd = game_window[1]

        sx, sy = int(sx), int(sy)
        attempt = 0
        offset = (0, 0)
        if retry_key is not None:
            retry_key = (root_hwnd, retry_key)
            last_at, last_attempt = self._click_retry_history.get(retry_key, (float("-inf"), -1))
            if now - last_at <= 20.0:
                attempt = last_attempt + 1
            if allow_offsets and attempt >= 2:
                offset = CLICK_RETRY_OFFSETS[(attempt - 2) % len(CLICK_RETRY_OFFSETS)]
        reset_hover = hover_reset or (allow_offsets and attempt == 1)

        def can_click():
            return self._game_input_ready(hwnd) and self._is_game_focused(game_window=game_window)

        prepared = False

        def ready_aim(lead):
            nonlocal prepared
            if (not can_click() or not win32_input.release_pending_touch()
                    or self._mouse_buttons_down()):
                return None
            point = target_point(lead) if target_point is not None else (sx, sy)
            if point is None:
                return None
            x, y = map(int, point)
            if offset != (0, 0) and self.current_viewport:
                shifted = x + offset[0], y + offset[1]
                vx, vy, vw, vh = self.current_viewport
                if (vx <= shifted[0] < vx + vw and vy <= shifted[1] < vy + vh
                        and not self._is_point_in_hud(*shifted, margin=10)
                        and not self._is_point_in_game_ui(*shifted)
                        and not self._is_point_in_vending_shop(*shifted)):
                    x, y = shifted
            prepared = True
            return x, y

        def _do_smooth_click():
            sent = False
            try:
                if not can_click():
                    if trace_touch:
                        log_click_file("[PIN Debug] Envio cancelado pelo agendador: janela indisponível ou sem foco.")
                    return
                ensure_input_desktop()
                if not win32_input.release_pending_touch() or self._mouse_buttons_down():
                    if trace_touch:
                        log_click_file("[PIN Debug] Envio cancelado pelo agendador: botão ou toque ainda ativo.")
                    return
                if reset_hover:
                    away = (hover_reset_point if hover_reset_point is not None
                            else self._hover_reset_away_point(sx, sy))
                    moved = away is not None and self._move_cursor_released(*away)
                    if moved:
                        time.sleep(0.05)
                    if pickup_click:
                        cursor = wintypes.POINT()
                        if (not moved or not u32.GetCursorPos(ctypes.byref(cursor))
                                or math.dist((cursor.x, cursor.y), away) > 5):
                            return
                if click_action is not None:
                    point = ready_aim(0.075)
                    if point is not None:
                        sent = click_action(point)
                elif direct_touch:
                    point = ready_aim(0.075)
                    if point is None:
                        return
                    if not self._move_cursor_released(*point):
                        if trace_touch:
                            log_click_file("[PIN Debug] Toque direto adiado: posicionamento recusado.")
                        return
                    time.sleep(0.09)
                    latest = ready_aim(0.022)
                    if latest is None or math.dist(point, latest) > 4:
                        return
                    win32_input.begin_bot_click()
                    try:
                        down, down_error, up, up_error = win32_input.tap_touch(point)
                    finally:
                        win32_input.finish_bot_click()
                    sent = bool(down and up)
                    if trace_touch or not sent:
                        log_click_file(f"[Clique] Toque em {point}: pressionar={down} ({down_error}), "
                                       f"soltar={up} ({up_error}).")
                else:
                    sent = win32_input.move_and_click(
                        sx, sy, hwnd, root_hwnd,
                        is_focused=can_click, target_point=ready_aim,
                        cursor_tolerance=1 if offset != (0, 0) else 3,
                        debug=self._click_debug, log=log_click_file,
                        is_admin=getattr(self, "is_admin", False),
                        pickup_click=pickup_click,
                        hover_ms=hover_ms, hold_ms=hold_ms,
                    )
            except Exception as exc:
                print(f"[ClickDebug] move_and_click erro: {exc}", flush=True)
            finally:
                finished_at = time.perf_counter()
                self._last_click_ts = self.last_bot_click_ts = finished_at
                if sent:
                    self._last_click_end_ts = finished_at
                    if completion is None:
                        measured = max(0.04, min(0.16, (finished_at - now) * 0.7))
                        lead = getattr(self, "_sprite_click_lead", 0.085)
                        self._sprite_click_lead = lead * 0.75 + measured * 0.25
                completed_at = time.monotonic()
                self._last_touch_result = ((int(sx), int(sy)), completed_at, sent)
                if retry_key is not None and prepared:
                    self._click_retry_history[retry_key] = (finished_at, attempt)
                    self._click_retry_history.move_to_end(retry_key)
                    if len(self._click_retry_history) > 256:
                        self._click_retry_history.popitem(last=False)
                self._click_busy = False
                self._click_in_progress = False
                if completion is not None:
                    completion.set_result((completed_at, sent))

        started = False
        try:
            self._click_in_progress = True
            self._click_busy = True
            self._click_start_ts = now
            self._last_click_ts = self.last_bot_click_ts = now
            threading.Thread(target=_do_smooth_click, daemon=True).start()
            started = True
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

    def _mute_osk_audio(self, osk_hwnd):
        from contextlib import ExitStack
        from uuid import UUID

        pid = self._window_pid(osk_hwnd)
        if not pid:
            return
        try:
            if psutil.Process(pid).name().casefold() != "osk.exe":
                return
        except psutil.Error:
            return

        ptr = ctypes.c_void_p
        out_ptr = ctypes.POINTER(ptr)
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoCreateInstance.argtypes = (ptr, ptr, wintypes.DWORD, ptr, out_ptr)
        ole32.CoCreateInstance.restype = ctypes.c_long

        clsid_enumerator = ctypes.create_string_buffer(UUID("BCDE0395-E52F-467C-8E3D-C4579291692E").bytes_le)
        iid_enumerator = ctypes.create_string_buffer(UUID("A95664D2-9614-4F35-A746-DE8DB63617E6").bytes_le)
        iid_manager = ctypes.create_string_buffer(UUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F").bytes_le)
        iid_control = ctypes.create_string_buffer(UUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D").bytes_le)
        iid_volume = ctypes.create_string_buffer(UUID("87CE5498-68D6-44E5-9215-6DA47EF883D8").bytes_le)

        def call(interface, slot, arg_types=(), *args, result_type=ctypes.c_long):
            vtable = ctypes.cast(interface, ctypes.POINTER(out_ptr)).contents
            method = ctypes.WINFUNCTYPE(result_type, ptr, *arg_types)(vtable[slot])
            return method(interface, *args)

        def release(interface):
            if interface:
                call(interface, 2, result_type=wintypes.ULONG)  # IUnknown::Release

        def managed(stack):
            interface = ptr()
            stack.callback(release, interface)
            return interface

        with ExitStack() as endpoints:
            enumerator, devices = managed(endpoints), managed(endpoints)
            if ole32.CoCreateInstance(clsid_enumerator, None, 1, iid_enumerator,
                                      ctypes.byref(enumerator)) < 0:
                return
            # IMMDeviceEnumerator::EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE).
            if call(enumerator, 3, (ctypes.c_int, wintypes.DWORD, out_ptr), 0, 1, ctypes.byref(devices)) < 0:
                return
            count = wintypes.UINT()
            if call(devices, 3, (ctypes.POINTER(wintypes.UINT),), ctypes.byref(count)) < 0:
                return
            for index in range(count.value):
                with ExitStack() as endpoint:
                    device, manager, sessions = managed(endpoint), managed(endpoint), managed(endpoint)
                    if call(devices, 4, (wintypes.UINT, out_ptr), index, ctypes.byref(device)) < 0:
                        continue
                    # IMMDevice::Activate(IAudioSessionManager2) e GetSessionEnumerator.
                    if call(device, 3, (ptr, wintypes.DWORD, ptr, out_ptr),
                            iid_manager, 1, None, ctypes.byref(manager)) < 0:
                        continue
                    if call(manager, 5, (out_ptr,), ctypes.byref(sessions)) < 0:
                        continue
                    session_count = ctypes.c_int()
                    if call(sessions, 3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(session_count)) < 0:
                        continue
                    for session_index in range(session_count.value):
                        with ExitStack() as session:
                            control, control2, volume = managed(session), managed(session), managed(session)
                            if call(sessions, 4, (ctypes.c_int, out_ptr), session_index, ctypes.byref(control)) < 0:
                                continue
                            if call(control, 0, (ptr, out_ptr), iid_control, ctypes.byref(control2)) < 0:
                                continue
                            session_pid = wintypes.DWORD()
                            # GetProcessId pode indicar uma sessão compartilhada. Aceita apenas S_OK.
                            if (call(control2, 14, (ctypes.POINTER(wintypes.DWORD),), ctypes.byref(session_pid)) != 0
                                    or session_pid.value != pid):
                                continue
                            if call(control, 0, (ptr, out_ptr), iid_volume, ctypes.byref(volume)) >= 0:
                                call(volume, 5, (wintypes.BOOL, ptr), True, None)  # ISimpleAudioVolume::SetMute

    def _prepare_osk_startup(self):
        try:
            with self._osk_send_lock:
                self._ensure_stealth_osk()
        except Exception as exc:
            print(f"[OSK] Falha ao preparar OSK na inicialização: {exc}", flush=True)

    def _ensure_stealth_osk(self, *, require_numpad=False):
        import winreg

        sw = u32.GetSystemMetrics(0) or 1920
        sh = u32.GetSystemMetrics(1) or 1080
        numpad_configured = False
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Osk") as key:
                winreg.SetValueEx(key, "ShowNumPad", 0, winreg.REG_DWORD, 1)
                numpad_configured = True
                winreg.SetValueEx(key, "ClickSound", 0, winreg.REG_DWORD, 0)
                winreg.SetValueEx(key, "WindowLeft", 0, winreg.REG_DWORD, max(0, sw - 260))
                winreg.SetValueEx(key, "WindowTop", 0, winreg.REG_DWORD, max(0, sh - 80))
                winreg.SetValueEx(key, "WindowWidth", 0, winreg.REG_DWORD, 260)
                winreg.SetValueEx(key, "WindowHeight", 0, winreg.REG_DWORD, 80)
        except OSError as exc:
            if require_numpad and not numpad_configured:
                raise
            print(f"[OSK] Não foi possível configurar registro do OSK: {exc}", flush=True)

        osk_hwnd = u32.FindWindowW("OSKMainClass", None)
        was_missing = not osk_hwnd
        if was_missing:
            self._osk_hwnd = None
            os.startfile("osk.exe")
            deadline = time.monotonic() + 1.5
            while not osk_hwnd:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("janela OSKMainClass não encontrada em 1,5 s")
                time.sleep(min(0.025, remaining))
                osk_hwnd = u32.FindWindowW("OSKMainClass", None)

        self._mute_osk_audio(osk_hwnd)
        if was_missing:
            time.sleep(0.05)
        try:
            # Garante que o OSK fique recolhido no canto inferior direito fora da área do jogo
            u32.SetWindowPos(osk_hwnd, 0, max(0, sw - 260), max(0, sh - 80), 260, 80, 0x0010 | 0x0004)
        except Exception:
            pass
        self._osk_hwnd = osk_hwnd
        return osk_hwnd

    def _send_key_via_osk(self, vk: int, *, game_window=None, allowed=None, alt=False) -> bool:
        is_function_key = 0x70 <= vk <= 0x7B
        aliases = {
            0x2D: {"insert", "ins"},
            0x0D: {"enter", "return", "intro"},
            0x1B: {"esc", "escape"},
            0x09: {"tab"},
            0x20: {"espaço", "espaco", "space"},
            0x2E: {"delete", "del", "excluir"},
            0x26: {"seta para cima", "para cima", "cima", "up", "up arrow"},
            0x25: {"seta para a esquerda", "esquerda", "left", "left arrow"},
            0x28: {"seta para baixo", "para baixo", "baixo", "down", "down arrow"},
            0x27: {"seta para a direita", "direita", "right", "right arrow"},
            0x45: {"e"},
        }.get(vk, {f"f{vk - 0x6F}", f"fn+f{vk - 0x6F}"} if is_function_key else set())
        if not aliases:
            return False
        return self._invoke_osk_key(aliases, vk=vk, game_window=game_window, allowed=allowed, alt=alt)

    def _invoke_osk_key(self, aliases, *, vk=None, osk_hwnd=None, game_window=None, allowed=None,
                        numpad5=False, allow_numpad_fallback=False, alt=False) -> bool | None:
        from contextlib import ExitStack
        from uuid import UUID

        pin_click = vk is None
        is_function_key = not pin_click and 0x70 <= vk <= 0x7B
        prefix = "[OSK PIN]" if pin_click else f"[OSK VK=0x{vk:02X}]"
        if not osk_hwnd:
            osk_hwnd = self._ensure_stealth_osk()
        ptr = ctypes.c_void_p
        out_ptr = ctypes.POINTER(ptr)
        hresult = ctypes.c_long
        integer = ctypes.c_int

        def call(obj, slot, restype, argtypes=(), *args):
            vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            fn = ctypes.WINFUNCTYPE(restype, ptr, *argtypes)(vtable[slot])
            return fn(obj, *args)

        def release(obj):
            if obj:
                call(obj, 2, wintypes.ULONG)

        def invoke(pattern):
            return call(pattern, 3, hresult)

        def game_has_focus(_hwnd):
            if game_window and not self._is_game_focused(game_window=game_window):
                self._focus_hud_game()

        def can_invoke(_hwnd, restore_fn=False):
            if pin_click:
                return allowed is None or allowed()
            return self._is_game_focused(game_window=game_window) and (restore_fn or allowed is None or allowed())

        if (not pin_click and not alt and can_invoke(osk_hwnd)
                and vk not in self._osk_fn_keys and (pattern := self._osk_invoke_cache.get(vk))):
            if invoke(pattern) == 0:
                game_has_focus(osk_hwnd)
                return True
            release(self._osk_invoke_cache.pop(vk))

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
            ]

        clsid = GUID.from_buffer_copy(UUID("{FF48DBA4-60EF-4201-AA87-54103EEF594E}").bytes_le)
        automation_iid = GUID.from_buffer_copy(UUID("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}").bytes_le)
        invoke_iid = GUID.from_buffer_copy(UUID("{FB377FBE-8EA6-46D5-9C73-6499642D3059}").bytes_le)
        guid_ptr = ctypes.POINTER(GUID)
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoCreateInstance.argtypes = (guid_ptr, ptr, wintypes.DWORD, guid_ptr, out_ptr)
        ole32.CoCreateInstance.restype = hresult
        oleaut32 = ctypes.WinDLL("oleaut32")
        oleaut32.SysFreeString.argtypes = (ptr,)
        oleaut32.SysFreeString.restype = None

        def require_success(hr, operation):
            if hr < 0:
                raise OSError(f"{prefix} {operation} HRESULT=0x{hr & 0xFFFFFFFF:08X}")

        def read_bstr(element, slot):
            value = ptr()
            try:
                hr = call(element, slot, hresult, (out_ptr,), ctypes.byref(value))
                return ctypes.wstring_at(value.value) if hr >= 0 and value else ""
            finally:
                if value:
                    oleaut32.SysFreeString(value)

        with ExitStack() as session:
            if not pin_click:
                session.callback(game_has_focus, osk_hwnd)
            root = true_condition = None

            def find_and_invoke(names, cache_vk, *, restore_fn=False, matches=None):
                nonlocal root, true_condition
                if cache_vk is not None and (pattern := self._osk_invoke_cache.get(cache_vk)):
                    if not can_invoke(osk_hwnd, restore_fn):
                        return False, set()
                    if invoke(pattern) == 0:
                        return True, set()
                    release(self._osk_invoke_cache.pop(cache_vk))
                if root is None:
                    automation, root, true_condition = ptr(), ptr(), ptr()
                    session.callback(release, automation)
                    require_success(ole32.CoCreateInstance(
                        ctypes.byref(clsid), None, 1, ctypes.byref(automation_iid), ctypes.byref(automation)
                    ), "CoCreateInstance")
                    session.callback(release, root)
                    require_success(call(automation, 6, hresult, (wintypes.HWND, out_ptr),
                                         osk_hwnd, ctypes.byref(root)), "ElementFromHandle")
                    session.callback(release, true_condition)
                    require_success(call(automation, 21, hresult, (out_ptr,), ctypes.byref(true_condition)),
                                    "CreateTrueCondition")
                found = set()
                with ExitStack() as search:
                    elements = ptr()
                    search.callback(release, elements)
                    require_success(call(root, 6, hresult, (integer, ptr, out_ptr),
                                         4, true_condition, ctypes.byref(elements)), "FindAll")
                    length = integer()
                    require_success(call(elements, 3, hresult, (ctypes.POINTER(integer),),
                                         ctypes.byref(length)), "get_Length")
                    for index in range(length.value):
                        with ExitStack() as item:
                            element = ptr()
                            item.callback(release, element)
                            if call(elements, 4, hresult, (integer, out_ptr),
                                    index, ctypes.byref(element)) < 0 or not element:
                                continue
                            name = read_bstr(element, 23)
                            automation_id = read_bstr(element, 29)
                            if name or automation_id:
                                found.add((name, automation_id))
                            name_key, id_key = name.strip().casefold(), automation_id.strip().casefold()
                            if matches is not None:
                                if not matches(name_key, id_key):
                                    continue
                            elif names.isdisjoint((name_key, id_key)):
                                continue
                            state_type = (ctypes.POINTER(wintypes.BOOL),)
                            offscreen, enabled = wintypes.BOOL(), wintypes.BOOL()
                            hr = call(element, 38, hresult, state_type, ctypes.byref(offscreen))
                            if hr < 0 or offscreen.value:
                                continue
                            hr = call(element, 28, hresult, state_type, ctypes.byref(enabled))
                            if hr < 0 or not enabled.value:
                                continue
                            unknown, pattern = ptr(), ptr()
                            item.callback(release, unknown)
                            if call(element, 16, hresult, (integer, out_ptr),
                                    10000, ctypes.byref(unknown)) < 0 or not unknown:
                                continue
                            item.callback(release, pattern)
                            if call(unknown, 0, hresult, (guid_ptr, out_ptr),
                                    ctypes.byref(invoke_iid), ctypes.byref(pattern)) < 0 or not pattern:
                                continue
                            if not can_invoke(osk_hwnd, restore_fn):
                                print(f"{prefix} descartado: jogo sem foco.", flush=True)
                                return False, found
                            hr = invoke(pattern)
                            if numpad5:
                                log_click_file(f"[PIN] InvokePattern Numpad 5 Name={name!r}, "
                                               f"AutomationId={automation_id!r}, HRESULT=0x{hr & 0xFFFFFFFF:08X}.")
                            if hr == 0:
                                if cache_vk is not None:
                                    call(pattern, 1, wintypes.ULONG)  # IUnknown::AddRef
                                    self._osk_invoke_cache[cache_vk] = pattern
                            print(f"{prefix} name='{name or automation_id}' invoke_hr=0x{hr & 0xFFFFFFFF:08X}",
                                  flush=True)
                            return hr == 0, found
                return None, found

            def log_missing(names, found):
                print(f"{prefix} botão {sorted(names)} indisponível. "
                      f"Controles (nome, ID): {sorted(found)}", flush=True)

            if pin_click:
                if numpad5:
                    sent, found = find_and_invoke((), None, matches=lambda _name, aid: aid == "10664")
                    if sent is not None:
                        return sent
                    if any(aid.strip() == "10664" for _, aid in found):
                        log_click_file("[PIN] Controle 10664 encontrado, mas sem InvokePattern disponível.")
                        return False
                    if allow_numpad_fallback:
                        sent, _ = find_and_invoke(
                            (), None, matches=lambda name, aid: name == "5" and bool(aid) and aid != "6",
                        )
                        if sent is not None:
                            return sent
                    log_click_file("[PIN] Controle 10664 indisponível na árvore do OSK.")
                    return None
                sent, found = find_and_invoke(aliases, None)
                if sent is None:
                    log_missing(aliases, found)
                return bool(sent)

            titlebar_controls = {
                "fechar", "maximizar", "minimizar", "restaurar",
                "close", "maximize", "minimize", "restore", "",
            }
            restore_names = {"maximizar", "maximize", "restaurar", "restore"}

            def restore_collapsed_osk(found_controls):
                tokens = {s.strip().casefold() for pair in found_controls for s in pair}
                if not found_controls or not tokens.issubset(titlebar_controls):
                    return False
                print(f"{prefix} OSK recolhido (apenas barra de título). Restaurando antes de invocar.", flush=True)
                u32.ShowWindow(osk_hwnd, 9)  # SW_RESTORE
                if tokens & restore_names:
                    find_and_invoke(restore_names, None, restore_fn=True)
                time.sleep(0.12)
                game_has_focus(osk_hwnd)
                return True

            if alt:
                if u32.GetAsyncKeyState(0x12) & 0x8000:
                    return False
                alt_names = {"alt", "left alt", "alt (left)", "alt esquerdo", "alt esquerda", "alt (esquerdo)"}
                latched, found = find_and_invoke(alt_names, None)
                if latched is None and restore_collapsed_osk(found):
                    latched, found = find_and_invoke(alt_names, None)
                if not latched:
                    if latched is None:
                        log_missing(alt_names, found)
                    return False
                sent = False
                try:
                    time.sleep(0.04)
                    sent, found = find_and_invoke(aliases, vk)
                    if sent is None:
                        log_missing(aliases, found)
                    return bool(sent)
                finally:
                    if not sent:
                        game_has_focus(osk_hwnd)
                        find_and_invoke(alt_names, None, restore_fn=True)

            if vk not in self._osk_fn_keys:
                sent, found = find_and_invoke(aliases, vk)
                if sent is None and restore_collapsed_osk(found):
                    sent, found = find_and_invoke(aliases, vk)
                if sent is not None:
                    return sent
                log_missing(aliases, found)
                if not is_function_key:
                    return False

            fn_names = {"fn", "modo de função", "modo de funcao", "function", "function mode", "fnkey"}
            enabled_fn, found = find_and_invoke(fn_names, 0)
            if enabled_fn is None and restore_collapsed_osk(found):
                enabled_fn, found = find_and_invoke(fn_names, 0)
            if enabled_fn is None:
                log_missing(fn_names, found)
            if not enabled_fn:
                return False
            try:
                # A árvore da linha superior pode chegar logo após o Invoke de Fn.
                time.sleep(0.1)
                sent, found = find_and_invoke(aliases, vk)
                if sent:
                    self._osk_fn_keys.add(vk)
                elif sent is None:
                    log_missing(aliases, found)
            finally:
                restored_fn, found = find_and_invoke(fn_names, 0, restore_fn=True)
                if restored_fn is None:
                    log_missing(fn_names, found)
            return bool(sent and restored_fn)

    def _send_osk_with_retry(self, vk, *, game_window, allowed=None, alt=False):
        with self._osk_send_lock:
            if time.monotonic() < self._osk_retry_after:
                return False
            for attempt in range(3):
                if (self._game_key_closed or not self._is_game_focused(game_window=game_window)
                        or (allowed is not None and not allowed())):
                    return False
                try:
                    if self._send_key_via_osk(vk, game_window=game_window, allowed=allowed, alt=alt):
                        self._osk_retry_after = 0.0
                        return True
                except OSError as exc:
                    transient = (isinstance(exc, TimeoutError)
                                 or "ElementFromHandle HRESULT=0x80040201" in str(exc))
                    if transient and attempt < 2:
                        time.sleep((0.15, 0.35)[attempt])
                        continue
                    print(f"[OSK] Falha ao enviar VK=0x{vk:02X}: {exc}", flush=True)
                self._osk_retry_after = time.monotonic() + 1.25
                return False
        return False

    def _send_game_key(self, vk: int, *, restore_focus=False, completion=None, allowed=None,
                       allow_storage_escape=False, osk_alt=False) -> bool:
        if self._automation_click_active():
            return False
        if vk == 0x1B and self._is_storage_or_pin_escape_blocked(allow_storage_escape=allow_storage_escape):
            return False
        hwnd = self.get_game_hwnd()
        if not self._game_input_ready(hwnd):
            return False
        game_window = self._remember_game_window(hwnd)
        if restore_focus and not self._is_game_focused(game_window=game_window):
            self._focus_hud_game()
        if not self._is_game_focused(game_window=game_window):
            print(f"[Teclado] VK=0x{vk:02X} descartado: jogo sem foco.", flush=True)
            return False

        mapping = None
        if not osk_alt and vk not in OSK_ABNT2_KEYS and not 0x70 <= vk <= 0x7B:
            size = ctypes.sizeof(INPUT)
            if size != 40:
                print(f"[Teclado] INPUT inválido para Win32 x64: {size} bytes.", flush=True)
                return False
            mapping = VK_TO_SCANCODE.get(vk)
            if mapping is None:
                translated = u32.MapVirtualKeyW(vk, 4)  # MAPVK_VK_TO_VSC_EX
                mapping = (translated & 0xFF, translated >> 8 == 0xE0)
            if not mapping[0]:
                print(f"[Teclado] VK=0x{vk:02X} descartado: scan code indisponível.", flush=True)
                return False
        queued_at = time.monotonic()
        key = (game_window, vk, osk_alt)
        with self._game_key_ready:
            if (self._game_key_closed or self._menu_selection_active
                    or (mapping is None and (key in self._game_key_pending or queued_at < self._osk_retry_after))):
                return False
            if mapping is None:
                self._game_key_pending.add(key)
            self._game_key_queue.append((game_window, vk, mapping, completion, allowed, queued_at, osk_alt))
            if not self._game_key_worker_active:
                worker = threading.Thread(target=self._drain_game_keys, name="game-keyboard", daemon=True)
                self._game_key_worker_active = True
                try:
                    worker.start()
                except RuntimeError as exc:
                    self._game_key_worker_active = False
                    self._game_key_queue.pop()
                    if mapping is None:
                        self._game_key_pending.discard(key)
                    print(f"[Teclado] Falha ao iniciar envio: {exc}", flush=True)
                    return False
            self._game_key_ready.notify()
        return True

    def _drain_game_keys(self):
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = ()
        ole32.CoUninitialize.restype = None
        com_hr = ole32.CoInitializeEx(None, 2)
        try:
            while True:
                with self._game_key_ready:
                    while not self._game_key_queue and not self._game_key_closed:
                        self._game_key_ready.wait()
                    if self._game_key_closed and not self._game_key_queue:
                        return
                    game_window, vk, mapping, completion, allowed, queued_at, osk_alt = self._game_key_queue.popleft()
                sent_at = None
                try:
                    if (self._game_key_closed or self._menu_selection_active or time.monotonic() - queued_at > 2.0
                            or (allowed is not None and not allowed())):
                        continue
                    if not self._is_game_focused(game_window=game_window):
                        continue
                    if mapping is None:
                        if self._send_osk_with_retry(vk, game_window=game_window, allowed=allowed, alt=osk_alt):
                            sent_at = time.monotonic()
                    else:
                        scancode, extended = mapping
                        flags = 0x0008 | (0x0001 if extended else 0)
                        down = INPUT(type=1)
                        down.ki.wScan = scancode
                        down.ki.dwFlags = flags
                        up = INPUT(type=1)
                        up.ki.wScan = scancode
                        up.ki.dwFlags = flags | 0x0002
                        size = ctypes.sizeof(INPUT)
                        if _keyboard_api.SendInput(1, ctypes.byref(down), size):
                            time.sleep(0.03)
                            _keyboard_api.SendInput(1, ctypes.byref(up), size)
                            sent_at = time.monotonic()
                except Exception as exc:
                    print(f"[Teclado] Falha ao enviar VK=0x{vk:02X}: {exc}", flush=True)
                finally:
                    if mapping is None:
                        with self._game_key_ready:
                            self._game_key_pending.discard((game_window, vk, osk_alt))
                    if completion is not None and not completion.done():
                        completion.set_result(sent_at)
        finally:
            with self._game_key_ready:
                self._game_key_worker_active = False
            if com_hr >= 0:
                ole32.CoUninitialize()

    def _test_sit_key(self):
        if self._sit_test is not None:
            return
        world = getattr(self.feed, "world", None)
        if world is None:
            self._report_sit_test("Feed do jogo indisponível.")
            return
        was_sitting = bool(world.player_is_sitting)
        completion = Future()
        if not self._send_game_key(0x2D, restore_focus=True, completion=completion):
            self._report_sit_test("Não foi possível enviar Insert (verifique o foco do jogo).")
            return
        self._sit_test = completion
        self._hud_sit_test_status.configure(text="Enviando Insert via OSK...")
        sent_at_ref = [None]

        def check():
            if self._sit_test is not completion:
                return
            if not completion.done():
                self.root.after(30, check)
                return
            if sent_at_ref[0] is None:
                sent_at_ref[0] = completion.result()
            sent_at = sent_at_ref[0]
            if sent_at is None:
                self._report_sit_test("Falha ao enviar Insert pelo OSK.")
            elif world.player_is_sitting != was_sitting:
                posture = "sentou" if world.player_is_sitting else "levantou"
                self._report_sit_test(f"Confirmado pelo jogo: personagem {posture}.")
            elif time.monotonic() < sent_at + 1.5:
                self.root.after(30, check)
            else:
                self._report_sit_test("Insert enviado. O jogo não confirmou mudança de postura.")

        check()

    def _report_sit_test(self, status):
        self._sit_test = None
        self._hud_sit_test_status.configure(text=status)
        print(f"[Teste Sentar] {status}", flush=True)

    def _send_menu_selection(self, index: int, *, allowed=None):
        if self._menu_selection_active or index < 0 or time.monotonic() < self._osk_retry_after:
            return None
        dialog = dict(self.feed.world.npc_dialog)
        allowed = allowed or self._dialog_action_guard(dialog, lambda: True)
        result = Future()
        self._menu_selection_active = True
        hwnd = self.get_game_hwnd()
        game_window = self._remember_game_window(hwnd)
        menu_key = (game_window, self.feed.world.map, dialog.get("npc_id"), dialog.get("seq"), index)
        if self._menu_osk_progress is None or self._menu_osk_progress[0] != menu_key:
            self._menu_osk_progress = (menu_key, 0)

        def _worker():
            sent_at = None
            ole32 = ctypes.WinDLL("ole32")
            ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
            ole32.CoInitializeEx.restype = ctypes.c_long
            ole32.CoUninitialize.argtypes = ()
            ole32.CoUninitialize.restype = None
            com_hr = ole32.CoInitializeEx(None, 2)
            try:
                delay = dialog.get("last_ts", 0.0) + 0.15 - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                with self._osk_send_lock:
                    for step_down in range(self._menu_osk_progress[1], index):
                        if not self._send_osk_with_retry(0x28, game_window=game_window, allowed=allowed):
                            return
                        self._menu_osk_progress = (menu_key, step_down + 1)
                        if step_down < index - 1:
                            time.sleep(0.120)
                    time.sleep(0.150)
                    if not self._send_osk_with_retry(0x0D, game_window=game_window, allowed=allowed):
                        return
                    self._menu_osk_progress = None
                    sent_at = time.monotonic()
            except Exception as exc:
                print(f"[Kafra] Falha ao enviar teclas via OSK: {exc}", flush=True)
            finally:
                if com_hr >= 0:
                    ole32.CoUninitialize()
                self._menu_selection_active = False
                if not result.done():
                    result.set_result(sent_at)

        try:
            threading.Thread(target=_worker, name="kafra-menu-osk", daemon=True).start()
        except RuntimeError as exc:
            self._menu_selection_active = False
            print(f"[Kafra] Falha ao iniciar thread de menu OSK: {exc}", flush=True)
            return None
        return result
    def _navigate_menu_keyboard(self, option_index: int):
        """Compatibilidade: navega pelo menu delegando para _send_menu_selection."""
        self._send_menu_selection(option_index)

    def touch_postmsg(self, sx, sy, game_hwnd=None, *, test_mode=False):
        """Dispara clique via touch_postmsg (pipeline funcional de 9e1ac16)."""
        return self._dispatch_game_click(game_hwnd, sx, sy, method="touch_postmsg", test_mode=test_mode)

    def patrol_move_and_click(self, sx, sy, *, hwnd=None, target_point=None, retry_key=None,
                              hover_reset=False, hover_reset_point=None, allow_offsets=True,
                              completion=None, direct_touch=False, pickup_click=False,
                              hover_ms=None, hold_ms=None):
        return self._dispatch_game_click(
            hwnd, sx, sy, method="touch_postmsg", target_point=target_point,
            retry_key=retry_key, hover_reset=hover_reset, hover_reset_point=hover_reset_point,
            allow_offsets=allow_offsets, completion=completion, direct_touch=direct_touch,
            pickup_click=pickup_click, hover_ms=hover_ms, hold_ms=hold_ms,
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
            for (_, aid), ent in world.entity_items():
                if ent.kind == "player" and (ent.has_vending_shop or aid in world.vending_aids
                                             or ent.gid in world.vending_aids):
                    vending_cells.append(ent.track.cell())
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
                             target_point=None, retry_key=None, hover_reset=False,
                             hover_reset_point=None, allow_offsets=True,
                             completion=None, direct_touch=False, pickup_click=False,
                             hover_ms=None, hold_ms=None):
        """Envia clique na posição física (screen_x, screen_y)."""
        hwnd = self.get_game_hwnd(hwnd)
        if not self._game_input_ready(hwnd):
            return False
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

        if self._mouse_buttons_down():
            return False
        owns_click_lock = method != "touch_postmsg"
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
                    sx, sy, hwnd, target_point=target_point, retry_key=retry_key,
                    hover_reset=hover_reset, hover_reset_point=hover_reset_point,
                    allow_offsets=allow_offsets, completion=completion,
                    direct_touch=direct_touch, pickup_click=pickup_click,
                    hover_ms=hover_ms, hold_ms=hold_ms,
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
            if not self._game_input_ready(hwnd):
                return False

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
                if not self._game_input_ready(hwnd):
                    return False
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
                inp_up.mi.dwFlags = MOUSEEVENTF_LEFTUP
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
                if not self._game_input_ready(hwnd):
                    return False
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
                inp_up.mi.dwFlags = MOUSEEVENTF_LEFTUP
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

            if not self._game_input_ready(hwnd):
                return False
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
            inp_up.mi.dwFlags = MOUSEEVENTF_LEFTUP
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
            if not self._game_input_ready(hwnd):
                return False
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

    def _try_auto_farm(self, snapshot, position, entities, *, defense=False, direct_only=False,
                       loot_defense=False):
        if self._locked_farm_target_id is not None:
            return True
        world = self.feed.world
        now = time.monotonic()
        if not defense and self.auto_patrol and now < self._farm_patrol_until:
            return False
        self.ignored_mobs = {gid: until for gid, until in self.ignored_mobs.items() if until > now}
        combat_gid = world.locked_combat_mob_gid
        self.locked_combat_mob_gid = combat_gid
        candidates = []
        for (kind, gid), mob in entities:
            if kind != "mob" or mob.unreachable_until > now or gid in self.ignored_mobs:
                continue
            cell = mob.track.cell()
            distance = math.dist(position, cell)
            if distance > 20.0:
                continue
            active = (gid == combat_gid and distance <= 2.5) or self._is_active_threat(world, gid, mob, position, now)
            if defense:
                if not loot_defense and not active and not (distance <= 2.5 and self._is_mob_hostile(world, mob)):
                    continue
                if direct_only and not loot_defense and distance > 2.5:
                    continue
            if mob.engaged_by_other_until > now and gid != combat_gid and not active:
                continue
            name = self.names.get("mob", {}).get(mob.catalog_id, "").strip().lower()
            if not defense and gid != combat_gid and not active:
                if not self._passes_target_filter("mob", mob.catalog_id, name):
                    continue
                if getattr(self, "_monster_filter", "").lower() not in name:
                    continue
            attacking_player = mob.last_aggro_ts > 0 and now - mob.last_aggro_ts < 2.0
            candidates.append((gid, mob, cell, distance, active, attacking_player))

        if not candidates:
            return False

        terrain = getattr(self.feed, "terrain", None)
        gat = terrain.for_map(world.map) if terrain and world.map else None
        start = tuple(round(coord) for coord in position)
        blocked, _ = self._navigation_obstacles(snapshot.map, gat)
        ranked = []
        for gid, mob, cell, distance, active, attacking_player in candidates:
            target_cell = tuple(round(coord) for coord in cell)
            if target_cell in blocked:
                continue
            has_los = (self._has_gat_line_of_sight(*start, *target_cell, gat)
                       and not self._segment_hits_obstacle(*start, *target_cell, blocked))
            if direct_only and gat is not None and not has_los:
                continue
            immediate = self._is_immediate_loot_threat(world, gid, mob, position, now)
            path = self._farm_path(gat, start, target_cell, blocked, max_steps=FARM_MAX_PATH_STEPS) if not immediate else []
            steps = len(path) - 1 if path else math.ceil(distance)
            if not immediate and (not path or steps > FARM_MAX_PATH_STEPS):
                continue
            ranked.append((not attacking_player, defense and not active, steps, gid, distance, target_cell, path))

        if not ranked:
            return False
        _, _, steps, gid, distance, cell, path = min(ranked)
        self._locked_farm_target_id = self._locked_mob_gid = gid
        with world.lock:
            world.farm_target_gid = gid
        self._farm_route = {"cell": cell, "path": path, "index": 0, "started": False} if path else None
        self._farm_lock_started_at = now
        self._farm_attack_ts = self._farm_range_hit_ts = 0.0
        self._farm_attack_action = None
        self._farm_retry_at = 0.0
        self._farm_range = 1.0
        self._farm_reachability_check = None
        self._farm_target_missing_since = None
        self._in_active_combat = False
        print(f"[FarmLock] Alvo travado: #{gid} (dist={distance:.1f}, A*={steps if path else 'defesa imediata'})",
              flush=True)
        return self._continue_farm_lock(snapshot, position, now, direct_only=direct_only)

    def _position_render_window(self, viewport):
        if viewport is not None and getattr(self, "_pin_render_suspended", False):
            return False
        previous = self._render_layout
        if viewport is None:
            self._pin_debug_drawn = None
            if previous[4]:
                # SWP_HIDEWINDOW | SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOMOVE | SWP_NOSIZE
                if not self.user32.SetWindowPos(self.handle, None, 0, 0, 0, 0, 0x0097):
                    return False
                self._render_layout = (*previous[:4], False)
                self.canvas_pool.hide_all()
            return True

        layout = (*viewport, True)
        now = time.monotonic()
        check_topmost = now >= getattr(self, "_render_topmost_check_at", 0.0)
        if check_topmost:
            self._render_topmost_check_at = now + 0.4
        restack = (self._hud_restack_render or not previous[4]
                   or (check_topmost and not u32.GetWindowLongW(self.handle, -20) & 0x0008))  # WS_EX_TOPMOST
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
                    entities = tuple(world.entity_items())
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

        moment = snapshot.frame_time or time.perf_counter()
        player_cell = snapshot.player_cell or player.cell(moment)
        if not all(value is not None and math.isfinite(value) for value in player_cell):
            return

        height_fn = flat_height
        gat = None
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

        if self._show_warps.get():
            allowed = self._travel_allowed_portal or self._patrol_portal_goal(snapshot.map)
            now_mono = time.monotonic()
            cache = getattr(self, "_portal_boxes_draw_cache", None)
            if cache and cache[0] == (snapshot.map, allowed) and now_mono < cache[1]:
                boxes_iter = cache[2]
            else:
                portals = self._visible_portal_cells(snapshot.map) | self._registered_portal_cells(snapshot.map)
                boxes_iter = tuple(self._portal_boxes(snapshot.map, gat, portals, allowed).values())
                self._portal_boxes_draw_cache = ((snapshot.map, allowed), now_mono + 0.25, boxes_iter)
            for left, bottom, right, top in boxes_iter:
                if not (left - 28 <= player_cell[0] <= right + 28 and bottom - 28 <= player_cell[1] <= top + 28):
                    continue
                if gat:
                    left, bottom = max(left, 0), max(bottom, 0)
                    right, top = min(right, gat.width - 1), min(top, gat.height - 1)
                if left > right or bottom > top:
                    continue
                corners = [(left - .5, bottom - .5), (right + .5, bottom - .5),
                           (right + .5, top + .5), (left - .5, top + .5)]
                outline = [pixels(cell, 0.5) for cell in corners + corners[:1]]
                if all(outline):
                    self.canvas_pool.get_line(*(value for point in outline for value in point),
                                              fill="#ff6347", width=2, dash=(5, 4))

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

        if (self._locked_farm_target_id is not None
                and self._active_splash_gid == self._locked_farm_target_id
                and time.monotonic() < self._active_splash_until):
            splash_mob = getattr(self.feed, "world", None)
            splash_mob = splash_mob.entity("mob", self._locked_farm_target_id) if splash_mob else None
            if splash_mob is not None:
                tx, ty = map(round, splash_mob.track.cell(moment))
                corners = [(tx - 1.5, ty - 1.5), (tx + 1.5, ty - 1.5),
                           (tx + 1.5, ty + 1.5), (tx - 1.5, ty + 1.5)]
                outline = [pixels(cell, 0.5) for cell in corners + corners[:1]]
                if all(outline):
                    self.canvas_pool.get_line(*(value for point in outline for value in point),
                                              fill="#f59e0b", width=2, dash=(4, 3))

        if status := self._player_overlay_status(player):
            label, color = status
            point = pixels(player_cell, 15.0)
            if point and 0 <= point[0] < width and 0 <= point[1] < height:
                return round(point[0]), max(12, round(point[1]) - 10), label, color

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
                self.canvas_pool.get_line(*coords, fill=color, width=2,
                                          dash=(6, 4) if self._minimap_goal else ())
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

    def _draw_overlay_label(self, width, height, occupied, x, y, label, color, *,
                            font_size=8, padding=(4, 2), outline=None, fixed=False):
        margin, gap = 2, 3
        pad_x, pad_y = padding
        text = self.canvas_pool.get_text(
            x, y, text=label, fill=color, font=("Tahoma", font_size, "bold"), anchor="center",
            width=max(1, width - 2 * (margin + pad_x + 2)),
        )
        bounds = self.canvas.bbox(text)
        if not bounds:
            return
        x1, y1, x2, y2 = bounds
        box_width, box_height = x2 - x1 + 2 * pad_x, y2 - y1 + 2 * pad_y
        max_left, max_top = width - margin - box_width, height - margin - box_height
        if max_left < margin or max_top < margin:
            self.canvas.itemconfigure(text, state="hidden")
            return
        wanted_left = max(margin, min(x1 - pad_x, max_left))
        wanted_top = max(margin, min(y1 - pad_y, max_top))

        def nearest_free_top(left):
            first = margin
            candidates = []
            for top, bottom, other_left, other_right in occupied:
                if left + box_width + gap <= other_left or other_right + gap <= left:
                    continue
                last = top - gap - box_height
                if first <= last:
                    candidates.append(max(first, min(wanted_top, last)))
                first = max(first, bottom + gap)
            if first <= max_top:
                candidates.append(max(first, wanted_top))
            return min(candidates, key=lambda top: abs(top - wanted_top), default=None)

        if fixed:
            left, top = x1 - pad_x, y1 - pad_y
        else:
            left = wanted_left
            top = nearest_free_top(left)
            if top is None:
                # Se a coluna estiver cheia, procura espaço ao lado das caixas já desenhadas.
                side_positions = {margin, max_left}
                for _, _, other_left, other_right in occupied:
                    side_positions.add(other_left - gap - box_width)
                    side_positions.add(other_right + gap)
                for left in sorted(side_positions, key=lambda pos: (abs(pos - wanted_left), pos)):
                    if not margin <= left <= max_left:
                        continue
                    top = nearest_free_top(left)
                    if top is not None:
                        break
            if top is None:
                self.canvas.itemconfigure(text, state="hidden")
                return

        right, bottom = left + box_width, top + box_height
        insort(occupied, (top, bottom, left, right))
        dx, dy = left + pad_x - x1, top + pad_y - y1
        if dx or dy:
            self.canvas.move(text, dx, dy)
        background = self.canvas_pool.get_rect(
            left, top, right, bottom, fill="#0B1120", outline=outline or color, width=1, dash=(),
        )
        self.canvas.tag_raise(text, background)

    def _pin_debug_frame(self, viewport, now):
        if not viewport:
            return None
        cycle = self._kafra_cycle
        probe = getattr(self, "_pin_click_probe", None)
        opening = cycle and cycle["phase"] == "opening"
        probe_status = getattr(self, "_pin_probe_status", None)
        if probe and (seq := probe.get("pin_seq")) and seq["viewport"] == viewport:
            status = f"Teste de senha · {seq['step']}/{len(seq['pin'])}"
            return seq["rect"], seq["buttons"], seq["ok"], probe.get("pin_debug_aim"), status
        status = probe_status or ("Kafra aguardando a senha" if opening else "Só leitura")
        rect = None
        if opening and (seen := cycle.get("pin_debug_rect")):
            seen_viewport, seen_rect, seen_at = seen
            if seen_viewport == viewport and now - seen_at < 0.6:
                rect = seen_rect
                if seq := cycle.get("pin_seq"):
                    status = probe_status or f"Kafra · Campo Senha {seq['step']}/{len(seq['pin'])}"
                    active = getattr(self, "_pin_click_probe", None) or cycle
                    return rect, seq["buttons"], seq["ok"], active.get("pin_debug_aim"), status

        self._schedule_pin_overlay_check(viewport, now, debug=True)
        if scanned := getattr(self, "_pin_debug_scan", None):
            seen_viewport, seen_rect, buttons, ok = scanned
            if seen_viewport == viewport and (rect is None or rect == seen_rect):
                probe = getattr(self, "_pin_click_probe", None)
                return seen_rect, buttons, ok, probe.get("pin_debug_aim") if probe else None, status
        if rect is not None:
            return rect, {}, None, None, status
        return None

    def _draw_pin_debug(self, viewport, rect, buttons, ok, target, status):
        if not self._position_render_window(viewport):
            self._pin_debug_drawn = None
            self.canvas_pool.hide_all()
            return False

        vx, vy, _, _ = viewport
        x, y, width, height = rect
        ordered_buttons = tuple(sorted(buttons.items()))
        reading = (viewport, rect, ordered_buttons, ok)
        now = time.monotonic()
        stale_reading = False
        if len(buttons) == 10:
            self._pin_debug_last_complete = (viewport, rect, buttons.copy(), ok, now)
        elif previous := getattr(self, "_pin_debug_last_complete", None):
            last_viewport, last_rect, last_buttons, last_ok, last_seen_at = previous
            if (last_viewport == viewport and last_rect == rect and now - last_seen_at < 1.25
                    and all(digit in last_buttons and math.dist(last_buttons[digit], point) <= 2
                            for digit, point in buttons.items())):
                # Mantém as caixas no desenho durante uma perda breve da leitura.
                # O fluxo de cliques recebe somente a leitura atual.
                reading = (viewport, rect, tuple(sorted(last_buttons.items())), last_ok)
                stale_reading = True
            else:
                self._pin_debug_last_complete = None
        frame = (reading, target, status)
        if frame == getattr(self, "_pin_debug_drawn", None):
            return True
        if not stale_reading and reading != getattr(self, "_pin_debug_logged", None):
            screen_rect = (vx + x, vy + y, width, height)
            screen_ok = (vx + x + ok[0], vy + y + ok[1]) if ok is not None else None
            log_click_file(f"[PIN Debug] Janela (x, y, largura, altura) {screen_rect}, "
                           f"{len(ordered_buttons)} teclas reconhecidas, OK {screen_ok} (tela).")
            self._pin_debug_logged = reading

        pool = self.canvas_pool
        pool.begin_frame()
        outline = "#14b8a6"
        pool.get_rect(x - 2, y - 2, x + width + 2, y + height + 2,
                      fill="", outline=outline, width=1, dash=())
        info = f"Grade: {len(reading[2])}/10 teclas"
        summary = pool.get_text(x + 6, y - 6, text=f"{info} · {status}",
                                anchor="sw", font=("Segoe UI", 9, "bold"), fill="#e0f2fe")
        left, top, right, bottom = self.canvas.bbox(summary)
        background = pool.get_rect(left - 4, top - 2, right + 4, bottom + 2,
                                   fill="#0f172a", outline=outline, width=1, dash=())
        self.canvas.tag_raise(summary, background)
        controls = [(digit, bx, by, 10) for digit, (bx, by) in reading[2]]
        if shown_ok := reading[3]:
            controls.append(("OK", shown_ok[0], shown_ok[1], 16))
        for label, bx, by, half_width in controls:
            cx, cy = x + bx, y + by
            # As anotações ficam fora dos pixels usados pelos modelos de botão.
            pool.get_rect(cx - half_width - 2, cy - 12, cx + half_width + 2, cy + 12,
                          fill="", outline=outline, width=1, dash=())
            text = pool.get_text(cx, cy - 12, text=label, anchor="s",
                                 font=("Segoe UI", -7, "bold"), fill="#0f172a")
            self.canvas.tag_raise(text)
        if target is not None:
            cx, cy = target[0] - vx, target[1] - vy
            # As quatro pontas da mira deixam o glifo central livre.
            # A mira de OK também precisa ficar fora da parte interna desse botão.
            dx = 17 if shown_ok and (cx, cy) == (x + shown_ok[0], y + shown_ok[1]) else 11
            for coords in ((cx - dx - 2, cy, cx - dx, cy), (cx + dx, cy, cx + dx + 2, cy),
                           (cx, cy - 13, cx, cy - 11), (cx, cy + 11, cx, cy + 13)):
                marker = pool.get_line(*coords, fill="#fbbf24", width=1)
                self.canvas.tag_raise(marker)
        pool.end_frame()
        self._pin_debug_drawn = frame
        return True

    def draw(self, snapshot, stats=None):
        world = getattr(self.feed, "world", None)
        if not self._hud_render_foreground:
            self._position_render_window(None)
            return False
        live_dialog = world.npc_dialog if world is not None else None
        dialog = snapshot.npc_dialog or live_dialog
        now = time.monotonic()
        dialog_active = any(
            d and (d.get("open") or d.get("opcode") in (0x00B4, 0x00B5, 0x00B7))
            for d in (dialog, live_dialog)
        )
        pin_active = (
            getattr(self, "_pin_dialog_screen_rect", None) is not None
            and now < getattr(self, "_pin_hud_suppress_until", 0.0)
        )
        if self.filters_config["kafra_pin_debug"] or getattr(self, "_pin_click_probe", None) is not None:
            if frame := self._pin_debug_frame(snapshot.viewport, now):
                return self._draw_pin_debug(snapshot.viewport, *frame)
            pin_active = pin_active or bool(getattr(self, "_pin_overlay_visible_cache", False))
        elif not dialog_active and not pin_active and not (world is not None and world.storage_open):
            pin_active = self._schedule_pin_overlay_check(snapshot.viewport, now)
        self._pin_debug_drawn = None
        self._pin_debug_logged = None
        self._pin_debug_last_complete = None
        if getattr(self, "_pin_click_probe", None) is None:
            self._pin_probe_status = None
        in_npc_dialog = bool(
            dialog_active
            or (world is not None and world.storage_open)
            or (self._kafra_cycle is not None and self._kafra_cycle.get("phase") in ("opening", "store"))
            or pin_active
        )
        if not self._overlay_visible or in_npc_dialog:
            self._position_render_window(None)
            return True
        if not self._position_render_window(snapshot.viewport):
            self.canvas_pool.hide_all()
            return False

        _, _, width, height = snapshot.viewport
        self.canvas_pool.begin_frame()
        player_status = self._draw_overlay_routes(snapshot, width, height)
        labels = []
        groups = collections.defaultdict(list)
        wall_now = time.time()

        for item in snapshot.objects:
            if item.kind == "mob" and not (self._show_mobs.get() if hasattr(self, "_show_mobs") else True):
                continue
            if item.kind == "drop" and not (self._show_drops.get() if hasattr(self, "_show_drops") else True):
                continue
            if item.kind == "npc" and not (self._show_npcs.get() if hasattr(self, "_show_npcs") else True):
                continue
            if item.kind == "warp" and not (self._show_warps.get() if hasattr(self, "_show_warps") else True):
                continue
            if item.kind == "player" and not self._show_players.get():
                continue

            ax, ay = self._projection_pixels(*item.get_anchor(), width, height)
            cx, cy = round(item.u * width), round(item.v * height)

            label = item.name or self.names.get(item.kind, {}).get(item.catalog_id)
            if item.kind == "drop" and label and (wrapped := ITEM_LABEL_DELIMITERS.fullmatch(label)):
                label = wrapped["name"].strip()
            if not label:
                if item.kind == "drop":
                    prefix = "Item"
                elif item.kind == "warp":
                    prefix = "Portal"
                elif item.kind == "npc":
                    prefix = "NPC"
                elif item.kind == "player":
                    prefix = "Player"
                elif item.kind == "mob":
                    prefix = "Monstro"
                else:
                    prefix = {"merc": "Mercenário", "homun": "Homúnculo", "elem": "Elemental"}.get(item.kind, "Entidade")
                label_id = item.key.partition(":")[2] if item.kind in ("npc", "player") else item.catalog_id
                label = f"{prefix} #{label_id or item.catalog_id}"
            if item.kind == "drop":
                is_locked_drop = (self._locked_drop_gid is not None and item.key == f"drop:{self._locked_drop_gid}")
                tier = self.classify_item_tier(item.catalog_id, label)
                retrying = item.retry_after > wall_now
                if tier == 1:
                    color = "#FFD700"  # Dourado vibrante para Tier 1
                    label = f"★ [T1] {label}"
                elif tier == 3:
                    label = f"[T3] {label}"
                    if item.ownership == "MINE":
                        color = "#4ADE80"
                    elif retrying:
                        color = "#F87171"
                    else:
                        color = "#94A3B8"  # Cinza discreto para lixo
                else:
                    if item.ownership == "MINE":
                        color = "#4ADE80"  # Verde (Seu)
                    elif retrying:
                        color = "#F87171"
                    else:
                        color = "#FBBF24"  # Amarelo (Livre)

                if is_locked_drop:
                    label += " [VIGÍLIA]" if tier == 1 and retrying else " [COLETANDO]"
                    radius = LOOT_CLICK_RADIUS
                    self.canvas_pool.get_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                                              outline="#4ADE80", width=3)
                else:
                    self.canvas_pool.get_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=2)
                self.canvas_pool.get_oval(ax - 3, ay - 3, ax + 3, ay + 3, outline=color, width=1)
            elif item.kind == "warp":
                color = "#22D3EE"  # Ciano para portal
                self.canvas_pool.get_oval(cx - 7, cy - 7, cx + 7, cy + 7, outline="#06B6D4", width=2)
                self.canvas_pool.get_oval(ax - 9, ay - 9, ax + 9, ay + 9, outline="#22D3EE", width=1, dash=(3, 3))
            elif item.kind == "npc":
                color, _ = npc_colors(item.catalog_id, item.name)
                if color != "#94a3b8":
                    self.canvas_pool.get_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=1)
            elif item.kind == "player":
                color = "#2DD4BF"  # Verde-água / ciano discreto para outros players
                self.canvas_pool.get_rect(cx - 5, cy - 5, cx + 5, cy + 5, outline=color, width=1, fill="", dash=())
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline="#0D9488", width=1)
            else:
                is_locked_mob = (self._locked_mob_gid is not None and item.key == f"mob:{self._locked_mob_gid}")
                if item.destination_uv is not None:
                    dx, dy = self._projection_pixels(*item.destination_uv, width, height)
                    trail_color = "#D99441" if is_locked_mob else "#D28B7E"
                    self.canvas_pool.get_line(ax, ay, dx, dy, fill=trail_color, width=1)
                    if 0 <= dx < width and 0 <= dy < height:
                        self.canvas_pool.get_oval(dx - 2, dy - 2, dx + 2, dy + 2,
                                                  fill=trail_color, outline="", width=0, dash=())
                if is_locked_mob:
                    color = "#FFA500"  # Laranja vibrante para o mob em foco
                    self.canvas_pool.get_rect(cx - 7, cy - 7, cx + 7, cy + 7, outline=color, width=2, fill="", dash=())
                else:
                    color = "#FFA294"
                    self.canvas_pool.get_rect(cx - 6, cy - 6, cx + 6, cy + 6, outline=color, width=2, fill="", dash=())
                if item.kind != "mob":
                    self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline=color, width=1)

            if item.tag_uv is None:
                # Snapshots legados fornecem só o ponto de clique em tela.
                text_x, text_y = cx, cy - (24 if item.kind == "npc" else 16)
            else:
                text_x, text_y = self._projection_pixels(*item.tag_uv, width, height)
            if not (0 <= text_x < width and 0 <= text_y < height):
                continue
            entry = EntityOverlayLabel(text_x, text_y, label, color, item.amount, fixed=item.kind == "mob")
            if item.kind == "drop":
                nearby = groups[item.catalog_id, label, item.ownership]
                for group_x, group_y, group in nearby:
                    if (ax - group_x) ** 2 + (ay - group_y) ** 2 <= OVERLAY_LABEL_GROUP_RADIUS ** 2:
                        group.amount += item.amount
                        break
                else:
                    nearby.append((ax, ay, entry))
                    labels.append(entry)
            else:
                labels.append(entry)

        occupied = []
        camera = getattr(self.feed, "camera", None)
        if isinstance(camera, CameraTracker):
            message, point = camera.alignment_overlay(snapshot)
            if message:
                self._draw_overlay_label(width, height, occupied, width / 2, 85, message, "#E0F2FE",
                                         font_size=10, padding=(8, 5), outline="#22D3EE")
            if point is not None:
                x, y = point
                self.canvas_pool.get_oval(x - 8, y - 8, x + 8, y + 8, outline="#22D3EE", width=2)
                self.canvas_pool.get_line(x - 11, y, x + 11, y, fill="#22D3EE")
                self.canvas_pool.get_line(x, y - 11, x, y + 11, fill="#22D3EE")
        if player_status:
            self._draw_overlay_label(width, height, occupied, *player_status,
                                     font_size=9, padding=(5, 2), outline="#334155")
        for entry in sorted(labels, key=lambda entry: not entry.fixed):
            label = f"{entry.amount}× {entry.text}" if entry.amount > 1 else entry.text
            self._draw_overlay_label(width, height, occupied, entry.x, entry.y, label, entry.color,
                                     fixed=entry.fixed)
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
                    if (w > 50 and h > 50) or u32.IsIconic(hwnd):
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
    if not hwnd:
        hwnd, _, title = find_ragnarok_window(target_proc.pid)
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
    traffic_log = TcpCaptureLog(_get_base_dir() / "logs") if args.interface else None
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
        start(lambda: capture(world, lengths, args.interface, server, port, args.local_port, stop, ready, ragexe_pid,
                              traffic_log=traffic_log))
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
    overlay = Overlay(root, LiveFeed(world, terrain, camera, viewport, traffic_log), names)
    world.on_pickup_ack_callback = overlay._on_pickup_ack
    overlay._click_method = args.click_method
    overlay._ragexe_pid = getattr(args, "ragexe_pid", None)
    overlay._hardware_cursor = getattr(args, "hardware_cursor", False)
    if getattr(args, "auto_patrol", False):
        overlay.auto_patrol = True
    if getattr(args, "route", None):
        r_map, r_x, r_y, r_target = args.route
        overlay.patrol_target_route = (r_map, int(r_x), int(r_y), int(r_target) if r_target.isdecimal() else r_target)
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
        if traffic_log is not None:
            traffic_log.close()
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
