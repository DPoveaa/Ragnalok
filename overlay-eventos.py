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
import random
import re
from subprocess import list2cmdline
import tempfile
import threading
from threading import Event, Thread
import time
import tkinter as tk
from tkinter import ttk
import traceback
import unicodedata
import collections
from dataclasses import dataclass

import psutil
from scapy.all import conf

from rathena_feed import DEFAULT_GRF, DEFAULT_LENGTHS, World, capture, list_interfaces, packet_lengths
from rathena_mock import Loopback
from rathena_terrain import (
    CELL_SIZE, Camera, Gat, RagnarokProjector3D, Terrain, get_map_camera_profile, MapCameraProfile,
    CITY_NAMES, CITY_KAFRA, MAP_PORTALS, find_map_route, get_city_travel_plan,
    get_mob_catalog, load_item_catalog, get_item_tier, search_mob_catalog,
    KNOWN_CITIES, get_adjacent_city_portal, load_heatmap,
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


BG_KEY = "#010101"
DEFAULT_CLICK_METHOD = "touch_postmsg"
MAX_SNAPSHOT_BYTES = 256_000
MAX_OBJECTS = 200
SNAPSHOT_TTL = .75
DEFAULT_CATALOG = Path("D:/Jogos/Ragnarok/System/itemInfo.lua")


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_asset_file(filename: str) -> Path:
    candidates = [
        _get_base_dir() / filename,
        getattr(sys, "_MEIPASS", None) and (Path(sys._MEIPASS) / filename),
        Path(__file__).parent / filename,
        Path(filename),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return Path(__file__).with_name(filename)


MOB_CATALOG = _resolve_asset_file("monstros-pt-br.json")


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
    is_dead: bool = False
    death_ts: float = 0.0


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
                    objects.append(ScreenObject(
                        f"{kind}:{aid}", kind, entity.catalog_id,
                        click_u, click_v, entity.amount, ownership,
                        anchor_u, anchor_v
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
            map=name,
            is_dead=getattr(player, "is_dead", False) if player else False,
            death_ts=getattr(player, "death_ts", 0.0) if player else 0.0,
        )


def flat_height(x, y):
    return 0.0


# ============================================================================
# Win32 API Isolada (u32 = ctypes.WinDLL("user32", use_last_error=True))
# Evita conflitos de argtypes de outros módulos e garante captura de GetLastError
# ============================================================================
u32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE        = 0x0001
MOUSEEVENTF_LEFTDOWN    = 0x0002
MOUSEEVENTF_LEFTUP      = 0x0004
MOUSEEVENTF_RIGHTDOWN   = 0x0008
MOUSEEVENTF_RIGHTUP     = 0x0010
MOUSEEVENTF_MIDDLEDOWN  = 0x0020
MOUSEEVENTF_MIDDLEUP    = 0x0040
MOUSEEVENTF_ABSOLUTE    = 0x8000

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_u", INPUT_UNION),
    ]


assert ctypes.sizeof(INPUT) == 40, f"INPUT size={ctypes.sizeof(INPUT)}, expected 40"


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]

assert ctypes.sizeof(_INPUT) == 40, f"_INPUT size={ctypes.sizeof(_INPUT)}, expected 40"


# --- Windows Native Touch Injection Structures ---
PT_TOUCH = 2
POINTER_FLAG_NONE = 0x00000000
POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_PRIMARY = 0x00002000
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
TOUCH_MASK_CONTACTAREA = 0x00000001
TOUCH_MASK_ORIENTATION = 0x00000002
TOUCH_MASK_PRESSURE = 0x00000004
TOUCH_FLAG_NONE = 0x00000000
TOUCH_FEEDBACK_DEFAULT = 1


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", wintypes.DWORD),
        ("pointerId", wintypes.UINT),
        ("frameId", wintypes.UINT),
        ("pointerFlags", wintypes.INT),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", wintypes.UINT),
        ("inputData", ctypes.c_int32),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_int),
        ("touchMask", ctypes.c_int),
        ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT),
        ("orientation", wintypes.UINT),
        ("pressure", wintypes.UINT),
    ]


# Assinaturas estritas na instância dedicada u32
u32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
u32.SetCursorPos.restype = wintypes.BOOL

u32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
u32.GetCursorPos.restype = wintypes.BOOL

u32.SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
u32.SendInput.restype = wintypes.UINT

u32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
u32.MapVirtualKeyW.restype = wintypes.UINT

u32.mouse_event.argtypes = (wintypes.DWORD, ctypes.c_long, ctypes.c_long, wintypes.DWORD, ULONG_PTR)
u32.mouse_event.restype = None

u32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u32.PostMessageW.restype = wintypes.BOOL

u32.IsWindow.argtypes = (wintypes.HWND,)
u32.IsWindow.restype = wintypes.BOOL

u32.InitializeTouchInjection.argtypes = (wintypes.UINT, wintypes.DWORD)
u32.InitializeTouchInjection.restype = wintypes.BOOL

u32.InjectTouchInput.argtypes = (wintypes.UINT, ctypes.POINTER(POINTER_TOUCH_INFO))
u32.InjectTouchInput.restype = wintypes.BOOL

u32.GetForegroundWindow.argtypes = ()
u32.GetForegroundWindow.restype = wintypes.HWND

u32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
u32.GetClientRect.restype = wintypes.BOOL

u32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
u32.ClientToScreen.restype = wintypes.BOOL

u32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
u32.ScreenToClient.restype = wintypes.BOOL

u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
u32.GetWindowThreadProcessId.restype = wintypes.DWORD

u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
u32.GetWindowTextW.restype = ctypes.c_int

u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
u32.GetWindowTextLengthW.restype = ctypes.c_int

u32.IsIconic.argtypes = (wintypes.HWND,)
u32.IsIconic.restype = wintypes.BOOL

u32.IsWindowVisible.argtypes = (wintypes.HWND,)
u32.IsWindowVisible.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
u32.EnumWindows.restype = wintypes.BOOL
u32.EnumChildWindows.argtypes = (wintypes.HWND, WNDENUMPROC, wintypes.LPARAM)
u32.EnumChildWindows.restype = wintypes.BOOL

u32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
u32.GetAncestor.restype = wintypes.HWND

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

u32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
u32.GetAsyncKeyState.restype = ctypes.c_short

u32.GetSystemMetrics.argtypes = (ctypes.c_int,)
u32.GetSystemMetrics.restype = ctypes.c_int

try:
    u32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
except AttributeError:
    pass

LRESULT = ctypes.c_ssize_t

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

_touch_initialized = False


def ensure_touch_injection():
    """Inicializa Touch Injection uma única vez por processo."""
    global _touch_initialized
    if not _touch_initialized:
        ret = u32.InitializeTouchInjection(1, TOUCH_FEEDBACK_DEFAULT)
        err = ctypes.get_last_error()
        if ret or err in (0, 1208):  # 1208 = ERROR_ALREADY_INITIALIZED
            _touch_initialized = True
            return True, err
        return False, err
    return True, 0


def log_click_file(msg: str):
    """Grava log de diagnóstico imediato em click_debug.log e no terminal."""
    try:
        with open("click_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    print(f"[ClickDebug] {msg}", flush=True)


try:
    u32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    u32.OpenInputDesktop.restype = wintypes.HANDLE
    u32.SetThreadDesktop.argtypes = (wintypes.HANDLE,)
    u32.SetThreadDesktop.restype = wintypes.BOOL
    u32.CloseDesktop.argtypes = (wintypes.HANDLE,)
    u32.CloseDesktop.restype = wintypes.BOOL
except AttributeError:
    pass


def ensure_input_desktop():
    """Conecta a thread atual ao desktop de input ativo do Windows."""
    try:
        hdesk = u32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            u32.SetThreadDesktop(hdesk)
            u32.CloseDesktop(hdesk)
    except Exception:
        pass


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
        lmb = bool(u32.GetAsyncKeyState(0x01) & 0x8000)
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
            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t),
                ]

            def _wheel_proc(nCode, wParam, lParam):
                if nCode >= 0 and wParam == WM_MOUSEWHEEL and lParam:
                    try:
                        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        fg = u32.GetForegroundWindow()
                        is_game = True
                        if self.target_hwnd and fg != self.target_hwnd:
                            is_game = False
                        if is_game:
                            delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                            # Scroll para frente aproxima câmera (diminui distance)
                            step = (delta / 120.0) * 25.0
                            self.distance = max(self.profile.distance_min, min(self.profile.distance_max, self.distance - step))
                            self._last_camera_input_time = time.perf_counter()
                            self.sync_projector()
                    except Exception:
                        pass
                return u32.CallNextHookEx(self._wheel_hook, nCode, wParam, lParam)

            self._wheel_hook_proc = HOOKPROC(_wheel_proc)
            h_mod = u32.GetModuleHandleW(None)
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
        self.feed = feed
        self.names = names
        self.window = tk.Toplevel(root, bg=BG_KEY, highlightthickness=0, bd=0)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.canvas = tk.Canvas(self.window, bg=BG_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas_pool = CanvasPool(self.canvas)
        self.window.update_idletasks()
        self.user32 = screen_api()
        overlay_hwnd = u32.GetAncestor(self.window.winfo_id(), 2)  # GA_ROOT
        if not overlay_hwnd:
            raise RuntimeError("A janela do overlay não foi criada.")
        self.handle = overlay_hwnd
        # Overlay 100% click-through: apenas WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE em GWL_EXSTYLE.
        # WS_DISABLED (0x08000000) REMOVIDO de GWL_STYLE — afeta janelas filhas (HUD) via UIPI.
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
        self.window.deiconify()
        self.last = None
        self.last_stats = None
        self.active_nav_path = None
        self.active_nav_color = "#38BDF8"
        self.auto_loot = False
        self.auto_farm = False
        self.auto_patrol = False
        self.auto_sit = True
        self.is_sitting = False
        self._sit_retreat_state = None
        self._last_player_hp = None
        self.flee_enabled = tk.BooleanVar(value=True)
        self.flee_pct_var = tk.StringVar(value="35")
        self.potion_key_enabled = tk.BooleanVar(value=False)
        self.potion_key_var = tk.StringVar(value="F1")
        self.auto_resurrect = tk.BooleanVar(value=False)
        self.last_farm_spot = None
        self.last_farm_modes = None
        self._death_click_toggle = 0
        self._death_start_ts = 0.0
        self._respawn_state = None
        self._last_healer_click_ts = 0.0
        self.fleeing_mode = False
        self._last_potion_press = 0.0
        self._last_resurrect_press = 0.0
        self._ignore_tier3_always = tk.BooleanVar(value=False)
        self._last_decision_ts = 0.0
        self._cached_hud_rect = None
        self._cached_hud_fg = False
        self._last_hud_geom_ts = 0.0
        self._last_viewport_pos = None
        self._last_fg_hwnd = None
        self._last_fg_result = False
        self.last_left = False
        self.last_right = False
        self.last_insert = False
        self.last_f6 = False
        self.last_f9 = False
        self.last_loot_click = 0.0
        self.last_farm_click = 0.0
        self.last_patrol_click_time = 0.0
        self.last_patrol_click_player_pos = None
        self.locked_mob_gid = None
        self.locked_mob_clicks = 0
        self.locked_mob_first_time = 0.0
        self.locked_drop_gid = None
        self.locked_drop_clicks = 0
        self.locked_drop_first_time = 0.0
        self.current_patrol_path = []
        self.patrol_path = []
        self.current_waypoint = None
        self.patrol_waypoint = None
        self.visited_cells = []
        self.temp_blocked_cells = {}
        self.stuck_count = 0
        self.patrol_target_route = None  # (map_name, x, y, target_npc_or_warp)
        self.nav_goal = None  # (map_name, x, y) — rota direcionada INDEPENDENTE do auto_patrol
        self._farm_blacklist = {}    # gid -> expire_timestamp
        self._drop_blacklist = {}    # gid -> expire_timestamp
        self._loot_retry_state = {}       # drop_gid -> {"attempt": int, "last_time": float}
        self._picked_items_history = []  # [(hora, nome, cnt, is_fail), ...]
        self._picked_list_data = []
        self._last_rendered_picked_items = None
        if hasattr(self.feed, "world") and self.feed.world:
            self.feed.world.on_pickup_ack_callback = self._on_pickup_ack
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
        # Painel HUD dark-mode (F8 para alternar)
        self._overlay_visible = True
        self._hud_panel = None
        self._hud_panel_visible = True
        self._last_f8 = False
        self._monster_filter = ""
        self._filter_var = None
        self._route_x_var = None
        self._route_y_var = None
        self._target_city_var = None
        self._hud_btns = {}
        self._npc_list_data = []
        self._npc_listbox = None
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
        self._last_rendered_npc_items = []
        self._last_rendered_mobs_items = []
        self._last_rendered_drops_items = []
        self._click_in_progress = False
        self._last_patrol_click_target = None
        self._npc_click_state = {}
        self._kill_history = []
        self._loot_history = []
        self._session_kill_count = 0
        self._kill_history_last_check = 0.0
        self._load_filters_config()
        self._create_hud_panel()
        self._install_navi_hook()

    def _get_filters_config_path(self):
        return str(_get_base_dir() / "filters_config.json")

    def _load_filters_config(self):
        """Carrega filtros persistentes de filters_config.json."""
        config_path = self._get_filters_config_path()
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                m_mode = cfg.get("mob_filter_mode", "Todos")
                if str(m_mode).lower() in ("all", "todos"):
                    self._mob_filter_mode = "Todos"
                elif str(m_mode).lower() in ("whitelist",):
                    self._mob_filter_mode = "Whitelist"
                elif str(m_mode).lower() in ("blacklist",):
                    self._mob_filter_mode = "Blacklist"
                else:
                    self._mob_filter_mode = m_mode

                d_mode = cfg.get("drop_filter_mode", "Todos")
                if str(d_mode).lower() in ("all", "todos"):
                    self._drop_filter_mode = "Todos"
                elif str(d_mode).lower() in ("whitelist",):
                    self._drop_filter_mode = "Whitelist"
                elif str(d_mode).lower() in ("blacklist",):
                    self._drop_filter_mode = "Blacklist"
                else:
                    self._drop_filter_mode = d_mode

                self.mob_whitelist = set(cfg.get("mob_whitelist", []))
                self.mob_whitelist_names = set(str(x).lower() for x in cfg.get("mob_whitelist_names", []))
                self.mob_blacklist = set(cfg.get("mob_blacklist", []))
                self.mob_blacklist_names = set(str(x).lower() for x in cfg.get("mob_blacklist_names", []))
                self.drop_whitelist = set(cfg.get("drop_whitelist", []))
                self.drop_whitelist_names = set(str(x).lower() for x in cfg.get("drop_whitelist_names", []))
                self.drop_blacklist = set(cfg.get("drop_blacklist", []))
                self.drop_blacklist_names = set(str(x).lower() for x in cfg.get("drop_blacklist_names", []))
        except Exception as exc:
            print(f"[HUD] Erro ao carregar filters_config.json: {exc}", flush=True)

    def _save_filters_config(self):
        """Salva filtros persistentes em filters_config.json."""
        config_path = self._get_filters_config_path()
        try:
            cfg = {
                "mob_filter_mode": self._mob_filter_mode,
                "drop_filter_mode": self._drop_filter_mode,
                "mob_whitelist": sorted(list(self.mob_whitelist)),
                "mob_whitelist_names": sorted(list(self.mob_whitelist_names)),
                "mob_blacklist": sorted(list(self.mob_blacklist)),
                "mob_blacklist_names": sorted(list(self.mob_blacklist_names)),
                "drop_whitelist": sorted(list(self.drop_whitelist)),
                "drop_whitelist_names": sorted(list(self.drop_whitelist_names)),
                "drop_blacklist": sorted(list(self.drop_blacklist)),
                "drop_blacklist_names": sorted(list(self.drop_blacklist_names)),
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[HUD] Erro ao salvar filters_config.json: {exc}", flush=True)

    def _create_hud_panel(self):
        """Cria painel de controle HUD dark-mode flutuante em 4 abas (F8 para alternar)."""
        try:
            panel = tk.Toplevel(bg="#0F172A")
            panel.title("Ragnalok HUD")
            panel.overrideredirect(True)
            panel.resizable(False, False)
            panel.geometry("390x520+10+50")
            panel.attributes("-alpha", 0.88)
            try:
                panel.attributes("-toolwindow", True)
            except Exception:
                pass
            self._hud_panel_ref = panel

            self._drag_x = 0
            self._drag_y = 0
            self._hud_compact = False

            # Contêiner raiz com borda customizada dark #334155
            main_box = tk.Frame(panel, bg="#0F172A", highlightthickness=1,
                                highlightbackground="#334155", highlightcolor="#334155")
            main_box.pack(fill="both", expand=True)

            # Barra superior customizada arrastável com acento #3B82F6
            title_bar = tk.Frame(main_box, bg="#1E293B", height=28)
            title_bar.pack(fill="x")

            lbl_title = tk.Label(title_bar, text="● Ragnalok HUD (F8)", bg="#1E293B",
                                 fg="#3B82F6", font=("Segoe UI", 9, "bold"))
            lbl_title.pack(side="left", padx=8, pady=4)

            def _toggle_compact():
                self._hud_compact = not self._hud_compact
                if self._hud_compact:
                    nb_frame.pack_forget()
                    btn_min.config(text="▼")
                    panel.geometry("390x135")
                else:
                    nb_frame.pack(fill="both", expand=True, padx=4, pady=2)
                    btn_min.config(text="▲")
                    panel.geometry("390x520")

            def _hide_hud():
                panel.withdraw()
                self._hud_panel_visible = False

            btn_close = tk.Button(title_bar, text="✕", command=_hide_hud, bg="#1E293B",
                                  fg="#94A3B8", activebackground="#EF4444", activeforeground="#FFFFFF",
                                  relief="flat", bd=0, font=("Segoe UI", 8, "bold"), cursor="hand2")
            btn_close.pack(side="right", padx=6, pady=2)

            btn_min = tk.Button(title_bar, text="▲", command=_toggle_compact, bg="#1E293B",
                                fg="#94A3B8", activebackground="#334155", activeforeground="#FFFFFF",
                                relief="flat", bd=0, font=("Segoe UI", 8), cursor="hand2")
            btn_min.pack(side="right", padx=2, pady=2)

            # Controle de opacidade compacto (60%–100%) na barra de título
            self._hud_alpha_var = tk.DoubleVar(value=0.88)
            def _set_alpha(val):
                try:
                    panel.attributes("-alpha", float(val))
                except Exception:
                    pass
            alpha_scale = tk.Scale(
                title_bar, from_=0.60, to=1.0, resolution=0.01,
                orient="horizontal", variable=self._hud_alpha_var, command=_set_alpha,
                bg="#1E293B", fg="#94A3B8", troughcolor="#0F172A",
                highlightthickness=0, length=55, showvalue=False, bd=0,
            )
            alpha_scale.pack(side="right", padx=4, pady=2)

            self._last_drag_time = 0.0
            def _on_drag_start(e):
                self._drag_x = e.x_root - panel.winfo_x()
                self._drag_y = e.y_root - panel.winfo_y()

            def _on_drag(e):
                now = time.monotonic()
                if now - self._last_drag_time < 0.016:
                    return
                self._last_drag_time = now
                panel.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

            title_bar.bind("<ButtonPress-1>", _on_drag_start)
            title_bar.bind("<B1-Motion>", _on_drag)
            lbl_title.bind("<ButtonPress-1>", _on_drag_start)
            lbl_title.bind("<B1-Motion>", _on_drag)

            # Estilos consistentes dark mode
            style_btn = {
                "bg": "#1E293B", "fg": "#E2E8F0",
                "activebackground": "#334155", "activeforeground": "#F8FAFC",
                "relief": "flat", "bd": 0, "font": ("Segoe UI", 8, "bold"),
                "cursor": "hand2", "highlightthickness": 1,
                "highlightbackground": "#334155",
            }
            style_lbl = {"bg": "#0F172A", "fg": "#94A3B8", "font": ("Segoe UI", 8)}
            style_entry = {
                "bg": "#1E293B", "fg": "#E2E8F0", "insertbackground": "#E2E8F0",
                "relief": "flat", "bd": 1, "font": ("Segoe UI", 8),
                "highlightthickness": 1, "highlightbackground": "#334155",
            }

            # ── Telemetria Integrada no Cabeçalho ──
            telem_frame = tk.Frame(main_box, bg="#0F172A")
            telem_frame.pack(fill="x", padx=6, pady=(3, 1))

            telem_map_lbl = tk.Label(telem_frame, text="Mapa: --- (X, Y) | Nv. 1 | Peso: 0/1 (0%)",
                                     bg="#0F172A", fg="#38BDF8", font=("Segoe UI", 8, "bold"), anchor="w")
            telem_map_lbl.pack(fill="x")

            telem_hp_sp_lbl = tk.Label(telem_frame, text="HP: 0/0 (0%) | SP: 0/0 (0%)",
                                       bg="#0F172A", fg="#4ADE80", font=("Segoe UI", 8, "bold"), anchor="w")
            telem_hp_sp_lbl.pack(fill="x")

            telem_cam_lbl = tk.Label(telem_frame, text="Câmera: 0° 50.0° d=399",
                                     bg="#0F172A", fg="#94A3B8", font=("Segoe UI", 7), anchor="w")
            telem_cam_lbl.pack(fill="x")

            telem_status_lbl = tk.Label(telem_frame, text="[Farm: OFF] [Loot: OFF] [Patrol: OFF] [Sentar: OFF]",
                                        bg="#0F172A", fg="#CBD5E1", font=("Segoe UI", 7, "bold"), anchor="w")
            telem_status_lbl.pack(fill="x")

            tk.Frame(main_box, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            # ── Contêiner do Notebook com 4 Abas ──
            nb_frame = tk.Frame(main_box, bg="#0F172A")
            nb_frame.pack(fill="both", expand=True, padx=4, pady=2)

            nb_style = ttk.Style()
            try:
                nb_style.theme_use("default")
            except Exception:
                pass
            nb_style.configure("Ragnalok.TNotebook", background="#0F172A", borderwidth=0)
            nb_style.configure("Ragnalok.TNotebook.Tab", background="#1E293B", foreground="#94A3B8",
                               padding=[8, 3], font=("Segoe UI", 8, "bold"))
            nb_style.map("Ragnalok.TNotebook.Tab",
                         background=[("selected", "#2563EB"), ("active", "#334155")],
                         foreground=[("selected", "#FFFFFF"), ("active", "#F8FAFC")])

            notebook = ttk.Notebook(nb_frame, style="Ragnalok.TNotebook")
            notebook.pack(fill="both", expand=True)

            tab_bot = tk.Frame(notebook, bg="#0F172A")
            tab_filtros = tk.Frame(notebook, bg="#0F172A")
            tab_npcs = tk.Frame(notebook, bg="#0F172A")
            tab_historico = tk.Frame(notebook, bg="#0F172A")

            notebook.add(tab_bot, text="Bot & Rotas")
            notebook.add(tab_filtros, text="Filtros & Busca")
            notebook.add(tab_npcs, text="NPCs & Portais")
            notebook.add(tab_historico, text="Histórico")

            # ══════════════════════════════════════════════════════════════
            # ABA 1: BOT & ROTAS
            # ══════════════════════════════════════════════════════════════
            ctrl_frame = tk.Frame(tab_bot, bg="#0F172A")
            ctrl_frame.pack(fill="x", padx=4, pady=2)
            self._hud_btns = {}

            def make_toggle_btn(parent, label_on, label_off, get_state, toggle_fn):
                btn = tk.Button(parent, **style_btn)
                def update(*_):
                    state = get_state()
                    btn.config(
                        text=label_on if state else label_off,
                        bg="#1D4ED8" if state else "#1E293B",
                        highlightbackground="#3B82F6" if state else "#334155",
                    )
                def click():
                    toggle_fn()
                    update()
                btn.config(command=click)
                update()
                return btn, update

            row_btn1 = tk.Frame(ctrl_frame, bg="#0F172A")
            row_btn1.pack(fill="x", pady=1)
            btn_farm, upd_farm = make_toggle_btn(row_btn1, "Auto-Farm: ON", "Auto-Farm: OFF",
                                                 lambda: self.auto_farm,
                                                 lambda: setattr(self, "auto_farm", not self.auto_farm))
            btn_farm.pack(side="left", fill="x", expand=True, padx=1)
            self._hud_btns["farm"] = (btn_farm, upd_farm)

            btn_loot, upd_loot = make_toggle_btn(row_btn1, "Auto-Loot: ON", "Auto-Loot: OFF",
                                                 lambda: self.auto_loot,
                                                 lambda: setattr(self, "auto_loot", not self.auto_loot))
            btn_loot.pack(side="left", fill="x", expand=True, padx=1)
            self._hud_btns["loot"] = (btn_loot, upd_loot)

            row_btn2 = tk.Frame(ctrl_frame, bg="#0F172A")
            row_btn2.pack(fill="x", pady=1)
            btn_patrol, upd_patrol = make_toggle_btn(row_btn2, "Auto-Patrol [F6]: ON", "Auto-Patrol [F6]: OFF",
                                                     lambda: self.auto_patrol,
                                                     lambda: setattr(self, "auto_patrol", not self.auto_patrol))
            btn_patrol.pack(side="left", fill="x", expand=True, padx=1)
            self._hud_btns["patrol"] = (btn_patrol, upd_patrol)

            btn_overlay, upd_overlay = make_toggle_btn(row_btn2, "Overlay: ON", "Overlay: OFF",
                                                       lambda: getattr(self, "_overlay_visible", True),
                                                       lambda: setattr(self, "_overlay_visible", not getattr(self, "_overlay_visible", True)))
            btn_overlay.pack(side="left", fill="x", expand=True, padx=1)
            self._hud_btns["overlay"] = (btn_overlay, upd_overlay)

            # Linha 3 de botões: Auto-Sentar
            row_btn3 = tk.Frame(ctrl_frame, bg="#0F172A")
            row_btn3.pack(fill="x", pady=1)
            btn_sit, upd_sit = make_toggle_btn(
                row_btn3, "Auto-Sentar <30%: ON", "Auto-Sentar <30%: OFF",
                lambda: getattr(self, "auto_sit", False),
                lambda: setattr(self, "auto_sit", not getattr(self, "auto_sit", False)),
            )
            btn_sit.pack(side="left", fill="x", expand=True, padx=1)
            self._hud_btns["sit"] = (btn_sit, upd_sit)

            # ══════════════════════════════════════════════════════════════
            # ABA 1: BOT & ROTAS (Toggles, Checkboxes, Rotas)
            # ══════════════════════════════════════════════════════════════
            tk.Frame(tab_bot, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            # Checkboxes Visuais
            toggles_frame1 = tk.Frame(tab_bot, bg="#0F172A")
            toggles_frame1.pack(fill="x", padx=4, pady=1)
            chk_opts = {"bg": "#0F172A", "fg": "#CBD5E1", "activebackground": "#0F172A",
                        "activeforeground": "#F8FAFC", "selectcolor": "#1E293B",
                        "font": ("Segoe UI", 7), "bd": 0, "highlightthickness": 0}
            tk.Checkbutton(toggles_frame1, text="Mobs", variable=self._show_mobs, **chk_opts).pack(side="left", padx=2)
            tk.Checkbutton(toggles_frame1, text="Drops", variable=self._show_drops, **chk_opts).pack(side="left", padx=2)
            tk.Checkbutton(toggles_frame1, text="NPCs", variable=self._show_npcs, **chk_opts).pack(side="left", padx=2)
            toggles_frame2 = tk.Frame(tab_bot, bg="#0F172A")
            toggles_frame2.pack(fill="x", padx=4, pady=1)
            tk.Checkbutton(toggles_frame2, text="Portais", variable=self._show_warps, **chk_opts).pack(side="left", padx=2)
            tk.Checkbutton(toggles_frame2, text="Players", variable=self._show_players, **chk_opts).pack(side="left", padx=2)
            tk.Checkbutton(toggles_frame2, text="Ignorar Lixo >60%", variable=self._ignore_tier3_always, **chk_opts).pack(side="left", padx=2)

            toggles_frame3 = tk.Frame(tab_bot, bg="#0F172A")
            toggles_frame3.pack(fill="x", padx=4, pady=1)
            tk.Checkbutton(toggles_frame3, text="Fugir <", variable=self.flee_enabled, **chk_opts).pack(side="left", padx=(2, 0))
            tk.Entry(toggles_frame3, textvariable=self.flee_pct_var, width=3, **style_entry).pack(side="left", padx=1)
            tk.Label(toggles_frame3, text="% HP", **style_lbl).pack(side="left", padx=(0, 4))
            tk.Checkbutton(toggles_frame3, text="Auto-Ressuscitar", variable=self.auto_resurrect, **chk_opts).pack(side="left", padx=2)

            toggles_frame4 = tk.Frame(tab_bot, bg="#0F172A")
            toggles_frame4.pack(fill="x", padx=4, pady=1)
            tk.Checkbutton(toggles_frame4, text="Asa/Poção <20% HP:", variable=self.potion_key_enabled, **chk_opts).pack(side="left", padx=(2, 1))
            f_keys = [f"F{i}" for i in range(1, 10)]
            potion_menu = tk.OptionMenu(toggles_frame4, self.potion_key_var, *f_keys)
            potion_menu.config(bg="#1E293B", fg="#E2E8F0", activebackground="#334155",
                               activeforeground="#F8FAFC", relief="flat", bd=0,
                               highlightthickness=1, highlightbackground="#334155",
                               font=("Segoe UI", 7), cursor="hand2")
            potion_menu["menu"].config(bg="#1E293B", fg="#E2E8F0", activebackground="#3B82F6")
            potion_menu.pack(side="left", padx=2)

            tk.Frame(tab_bot, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            # Controles de Rota e Destino
            city_frame = tk.Frame(tab_bot, bg="#0F172A")
            city_frame.pack(fill="x", padx=4, pady=1)
            tk.Label(city_frame, text="Cidade:", **style_lbl).pack(side="left", padx=(0, 2))
            cities = ["Prontera", "Izlude", "Geffen", "Payon", "Morocc", "Alberta",
                      "Aldebaran", "Rachel", "Veins", "Juno", "Comodo", "Einbroch", "Lighthalzen", "Hugel"]
            self._target_city_var = tk.StringVar(value="Izlude")
            city_menu = tk.OptionMenu(city_frame, self._target_city_var, *cities)
            city_menu.config(bg="#1E293B", fg="#E2E8F0", activebackground="#334155",
                             activeforeground="#F8FAFC", relief="flat", bd=0,
                             highlightthickness=1, highlightbackground="#334155",
                             font=("Segoe UI", 7), cursor="hand2")
            city_menu["menu"].config(bg="#1E293B", fg="#E2E8F0", activebackground="#3B82F6")
            city_menu.pack(side="left", fill="x", expand=True, padx=2)

            def _travel_to_city():
                tgt = self._target_city_var.get().strip()
                curr = getattr(getattr(self.feed, "world", None), "map", "") or ""
                kafra_found = None
                snap = self.feed.current()
                if snap:
                    for obj in snap.objects:
                        if obj.kind == "npc":
                            c_id = obj.catalog_id
                            n = getattr(self.feed.world, "entity_names", {}).get(c_id, "").lower() if hasattr(self.feed, "world") else ""
                            if c_id in (112, 113, 114, 115, 116, 117) or any(k in n for k in ("kafra", "funcionaria", "funcionária")):
                                kafra_found = obj
                                break
                if kafra_found is not None:
                    self._click_debug(f"CIDADE: Kafra visível detectada na tela (#{kafra_found.catalog_id})!")
                    k_cell = self.feed.world.get_entity_cell("npc", kafra_found.key) if hasattr(self.feed, "world") else None
                    kx, ky = k_cell if k_cell else (150, 150)
                    self._city_travel_state = {
                        "mode": "kafra",
                        "target_city": tgt,
                        "kafra_pos": (kx, ky),
                        "state": "walking_to_kafra",
                        "last_action_time": 0.0,
                    }
                    self.nav_goal = (curr, int(kx), int(ky))
                else:
                    plan = get_city_travel_plan(curr, tgt)
                    mode = plan.get("mode")
                    if mode == "already_there":
                        self._click_debug(f"CIDADE: {plan.get('message')}")
                        return
                    if mode == "walk":
                        self._click_debug(f"CIDADE: {plan.get('message')}")
                        self._city_travel_state = {
                            "mode": "walk",
                            "target_city": tgt,
                            "route": plan.get("route", []),
                            "step_idx": 0,
                        }
                        self.auto_patrol = True
                    elif mode == "kafra":
                        self._click_debug(f"CIDADE: {plan.get('message')}")
                        kafra_coords = plan.get("kafra_coords", (150, 150))
                        self._city_travel_state = {
                            "mode": "kafra",
                            "target_city": tgt,
                            "kafra_pos": kafra_coords,
                            "state": "walking_to_kafra",
                            "last_action_time": 0.0,
                        }
                        self.nav_goal = (curr, kafra_coords[0], kafra_coords[1])
                    else:
                        self._click_debug(f"CIDADE: Não foi possível planejar rota para {tgt}")

            tk.Button(city_frame, text="Viajar (Cidade)", command=_travel_to_city,
                      bg="#1D4ED8", fg="#E2E8F0", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="right")

            # Ir Andando (X, Y)
            walk_frame = tk.Frame(tab_bot, bg="#0F172A")
            walk_frame.pack(fill="x", padx=4, pady=1)
            tk.Label(walk_frame, text="Ir Andando:", **style_lbl).pack(side="left")
            self._walk_x_var = tk.StringVar(value="150")
            self._walk_y_var = tk.StringVar(value="150")
            tk.Label(walk_frame, text="X:", **style_lbl).pack(side="left", padx=(4, 1))
            tk.Entry(walk_frame, textvariable=self._walk_x_var, width=5, **style_entry).pack(side="left", padx=1)
            tk.Label(walk_frame, text="Y:", **style_lbl).pack(side="left", padx=(4, 1))
            tk.Entry(walk_frame, textvariable=self._walk_y_var, width=5, **style_entry).pack(side="left", padx=1)

            def _walk_to_coords():
                try:
                    wx = int(self._walk_x_var.get().strip())
                    wy = int(self._walk_y_var.get().strip())
                    curr = getattr(getattr(self.feed, "world", None), "map", "") or ""
                    self.nav_goal = (curr, wx, wy)
                    self.patrol_target_route = (curr, wx, wy, None)
                    self.active_nav_color = "#38BDF8"
                    self.auto_patrol = True
                    self._click_debug(f"ROTA: Andando até ({wx}, {wy}) no mapa {curr}")
                except Exception as exc:
                    self._click_debug(f"ROTA: Coordenadas inválidas: {exc}")

            tk.Button(walk_frame, text="Ir", command=_walk_to_coords,
                      bg="#1E293B", fg="#38BDF8", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=3)

            # Entrada /navi
            navi_frame = tk.Frame(tab_bot, bg="#0F172A")
            navi_frame.pack(fill="x", padx=4, pady=1)
            tk.Label(navi_frame, text="/navi:", **style_lbl).pack(side="left", padx=(0, 2))
            self._navi_entry_var = tk.StringVar(value="/navi morocc 150 150")
            tk.Entry(navi_frame, textvariable=self._navi_entry_var, **style_entry).pack(side="left", fill="x", expand=True, padx=2)

            def _run_navi_cmd():
                raw = self._navi_entry_var.get().strip()
                if not raw:
                    return
                parts = raw.replace("/", " ").replace(",", " ").split()
                if parts and parts[0].lower() == "navi":
                    parts = parts[1:]
                if len(parts) >= 3:
                    tgt_map = parts[0]
                    try:
                        tx = int(parts[1])
                        ty = int(parts[2])
                        curr = getattr(getattr(self.feed, "world", None), "map", "") or ""
                        plan = get_city_travel_plan(curr, tgt_map)
                        if plan.get("mode") == "walk":
                            self._city_travel_state = {"mode": "walk", "target_city": tgt_map, "route": plan.get("route", []), "step_idx": 0}
                        else:
                            self.patrol_target_route = (tgt_map, tx, ty, None)
                        self.nav_goal = (tgt_map, tx, ty)
                        self.auto_patrol = True
                        self._click_debug(f"NAVI: Navegando para {tgt_map} ({tx}, {ty})")
                    except Exception as exc:
                        self._click_debug(f"NAVI: Erro ao interpretar: {exc}")

            tk.Button(navi_frame, text="Navegar", command=_run_navi_cmd,
                      bg="#1E293B", fg="#C084FC", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="right")

            def _stop_all_routes():
                self.nav_goal = None
                self.patrol_target_route = None
                self._city_travel_state = None
                self.auto_patrol = False
                self._click_debug("ROTA: Todas as rotas e A* cancelados.")

            tk.Button(tab_bot, text="⛔ Parar Rota / A*", command=_stop_all_routes,
                      bg="#DC2626", fg="#FFFFFF", activebackground="#B91C1C", relief="flat",
                      font=("Segoe UI", 8, "bold"), cursor="hand2").pack(fill="x", padx=4, pady=3)

            # ══════════════════════════════════════════════════════════════
            # ABA 2: FILTROS & BUSCA
            # ══════════════════════════════════════════════════════════════
            tk.Label(tab_filtros, text="Monstros Vivos Agora (Mais Próximos):", **style_lbl).pack(anchor="w", padx=4, pady=(2, 0))
            alive_frame = tk.Frame(tab_filtros, bg="#0F172A")
            alive_frame.pack(fill="x", padx=4, pady=1)
            alive_scroll = tk.Scrollbar(alive_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._alive_mobs_listbox = tk.Listbox(alive_frame, bg="#1E293B", fg="#FFA294",
                                                  selectbackground="#3B82F6", font=("Segoe UI", 7),
                                                  relief="flat", bd=0, height=3, yscrollcommand=alive_scroll.set)
            alive_scroll.config(command=self._alive_mobs_listbox.yview)
            self._alive_mobs_listbox.pack(side="left", fill="x", expand=True)
            alive_scroll.pack(side="right", fill="y")
            self._alive_mobs_data = []

            alive_btn_frame = tk.Frame(tab_filtros, bg="#0F172A")
            alive_btn_frame.pack(fill="x", padx=4, pady=1)

            def _refresh_filters_list():
                if not hasattr(self, "_active_filters_listbox") or not self._active_filters_listbox:
                    return
                self._active_filters_listbox.delete(0, tk.END)
                self._active_filters_data = []
                for cid in sorted(self.mob_whitelist):
                    name = self.names.get("mob", {}).get(cid, "")
                    label = f"[MOB WL] #{cid} {name}".strip()
                    self._active_filters_data.append(("mob_wl_id", cid))
                    self._active_filters_listbox.insert(tk.END, label)
                for name in sorted(self.mob_whitelist_names):
                    label = f"[MOB WL] {name}"
                    self._active_filters_data.append(("mob_wl_name", name))
                    self._active_filters_listbox.insert(tk.END, label)
                for cid in sorted(self.mob_blacklist):
                    name = self.names.get("mob", {}).get(cid, "")
                    label = f"[MOB BL] #{cid} {name}".strip()
                    self._active_filters_data.append(("mob_bl_id", cid))
                    self._active_filters_listbox.insert(tk.END, label)
                for name in sorted(self.mob_blacklist_names):
                    label = f"[MOB BL] {name}"
                    self._active_filters_data.append(("mob_bl_name", name))
                    self._active_filters_listbox.insert(tk.END, label)
                for iid in sorted(self.drop_whitelist):
                    name = self.names.get("drop", {}).get(iid, "")
                    label = f"[DROP WL] #{iid} {name}".strip()
                    self._active_filters_data.append(("drop_wl_id", iid))
                    self._active_filters_listbox.insert(tk.END, label)
                for name in sorted(self.drop_whitelist_names):
                    label = f"[DROP WL] {name}"
                    self._active_filters_data.append(("drop_wl_name", name))
                    self._active_filters_listbox.insert(tk.END, label)
                for iid in sorted(self.drop_blacklist):
                    name = self.names.get("drop", {}).get(iid, "")
                    label = f"[DROP BL] #{iid} {name}".strip()
                    self._active_filters_data.append(("drop_bl_id", iid))
                    self._active_filters_listbox.insert(tk.END, label)
                for name in sorted(self.drop_blacklist_names):
                    label = f"[DROP BL] {name}"
                    self._active_filters_data.append(("drop_bl_name", name))
                    self._active_filters_listbox.insert(tk.END, label)

            def _add_alive_mob_wl():
                sel = self._alive_mobs_listbox.curselection()
                if sel and sel[0] < len(self._alive_mobs_data):
                    cid, mname = self._alive_mobs_data[sel[0]]
                    self.mob_whitelist.add(cid)
                    if mname: self.mob_whitelist_names.add(mname.lower())
                    self._save_filters_config()
                    _refresh_filters_list()
                    self._click_debug(f"FILTRO: Adicionado à Whitelist: {mname} (#{cid})")

            def _add_alive_mob_bl():
                sel = self._alive_mobs_listbox.curselection()
                if sel and sel[0] < len(self._alive_mobs_data):
                    cid, mname = self._alive_mobs_data[sel[0]]
                    self.mob_blacklist.add(cid)
                    if mname: self.mob_blacklist_names.add(mname.lower())
                    self._save_filters_config()
                    _refresh_filters_list()
                    self._click_debug(f"FILTRO: Adicionado à Blacklist: {mname} (#{cid})")

            tk.Button(alive_btn_frame, text="+ Whitelist Selecionado", command=_add_alive_mob_wl,
                      bg="#1E293B", fg="#4ADE80", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)
            tk.Button(alive_btn_frame, text="+ Blacklist Selecionado", command=_add_alive_mob_bl,
                      bg="#1E293B", fg="#F87171", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)

            tk.Frame(tab_filtros, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            # Busca Inteligente de Monstro / Carta / Item
            search_hdr = tk.Frame(tab_filtros, bg="#0F172A")
            search_hdr.pack(fill="x", padx=4, pady=(1, 0))
            tk.Label(search_hdr, text="Buscar Monstro / Carta / Item:", **style_lbl).pack(side="left")

            search_input_frame = tk.Frame(tab_filtros, bg="#0F172A")
            search_input_frame.pack(fill="x", padx=4, pady=1)
            self._search_entry_var = tk.StringVar()
            search_entry = tk.Entry(search_input_frame, textvariable=self._search_entry_var, **style_entry)
            search_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))

            self._search_results_data = []
            search_list_frame = tk.Frame(tab_filtros, bg="#0F172A")
            search_list_frame.pack(fill="x", padx=4, pady=1)
            search_scroll = tk.Scrollbar(search_list_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._search_listbox = tk.Listbox(search_list_frame, bg="#1E293B", fg="#38BDF8",
                                              selectbackground="#3B82F6", font=("Segoe UI", 7),
                                              relief="flat", bd=0, height=3, yscrollcommand=search_scroll.set)
            search_scroll.config(command=self._search_listbox.yview)
            self._search_listbox.pack(side="left", fill="x", expand=True)
            search_scroll.pack(side="right", fill="y")

            def _do_search(*_args):
                q = self._search_entry_var.get().strip()
                if not q:
                    self._search_results_data.clear()
                    self._search_listbox.delete(0, tk.END)
                    return
                b_lvl = 1
                if hasattr(self.feed, "world") and self.feed.world and self.feed.world.player:
                    b_lvl = getattr(self.feed.world.player, "base_level", 1) or 1
                res = search_mob_catalog(q, base_level=b_lvl)
                self._search_results_data = res[:30]
                self._search_listbox.delete(0, tk.END)
                for mob in self._search_results_data:
                    h_tag = "Hostil" if mob["hostile"] else ("Imóvel" if mob["immobile"] else "Passivo")
                    md = mob.get("matched_drop")
                    drop_str = f"{md['item_name']} ({md['rate']:.2f}%)" if md else "Sem drop"
                    self._search_listbox.insert(tk.END, f"{mob['name']} | Nv. {mob['level']} | HP {mob['hp']} | {h_tag} | {drop_str}")

            search_entry.bind("<Return>", lambda _e: _do_search())
            tk.Button(search_input_frame, text="Buscar", command=_do_search,
                      bg="#1E293B", fg="#38BDF8", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="right")

            def _focus_searched_mob():
                sel = self._search_listbox.curselection()
                if sel and sel[0] < len(self._search_results_data):
                    mob = self._search_results_data[sel[0]]
                    jid = mob["job_id"]
                    mname = mob["name"]
                    self.mob_whitelist.add(jid)
                    self.mob_whitelist_names.add(mname.lower())
                    self._mob_filter_mode = "Whitelist"
                    if hasattr(self, "_mob_mode_var"):
                        self._mob_mode_var.set("Whitelist")
                    self._save_filters_config()
                    _refresh_filters_list()
                    self._click_debug(f"BUSCA: Monstro focado na Whitelist: {mname} (#{jid})")

            def _go_to_searched_mob_map():
                sel = self._search_listbox.curselection()
                if sel and sel[0] < len(self._search_results_data):
                    mob = self._search_results_data[sel[0]]
                    spawns = mob.get("spawns", [])
                    if not spawns:
                        self._click_debug(f"BUSCA: Nenhum mapa de spawn conhecido para {mob['name']}")
                        return
                    tgt_map = spawns[0]
                    curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
                    if curr_map.lower().removesuffix(".gat") == tgt_map.lower().removesuffix(".gat"):
                        self._click_debug(f"BUSCA: Você já está no mapa de spawn {tgt_map}!")
                        return
                    plan = get_city_travel_plan(curr_map, tgt_map)
                    mode = plan.get("mode")
                    if mode == "walk":
                        self._click_debug(f"BUSCA: Iniciando rota a pé para mapa de spawn {tgt_map}")
                        self._city_travel_state = {"mode": "walk", "target_city": tgt_map, "route": plan.get("route", []), "step_idx": 0}
                        self.auto_patrol = True
                    else:
                        self._click_debug(f"BUSCA: Rota direta definida para {tgt_map}")
                        self.patrol_target_route = (tgt_map, 150, 150, None)
                        self.auto_patrol = True

            search_btn_frame = tk.Frame(tab_filtros, bg="#0F172A")
            search_btn_frame.pack(fill="x", padx=4, pady=1)
            tk.Button(search_btn_frame, text="Focar Monstro", command=_focus_searched_mob,
                      bg="#1E293B", fg="#4ADE80", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)
            tk.Button(search_btn_frame, text="Ir para Mapa", command=_go_to_searched_mob_map,
                      bg="#1E293B", fg="#38BDF8", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)

            tk.Frame(tab_filtros, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            # Modos e Caixas Visíveis de Whitelist e Blacklist
            mode_frame = tk.Frame(tab_filtros, bg="#0F172A")
            mode_frame.pack(fill="x", padx=4, pady=1)

            tk.Label(mode_frame, text="Mobs:", **style_lbl).pack(side="left")
            self._mob_mode_var = tk.StringVar(value=self._mob_filter_mode)
            def _on_mob_mode_change(val):
                self._mob_filter_mode = val
                self._save_filters_config()
            mob_menu = tk.OptionMenu(mode_frame, self._mob_mode_var, "Todos", "Whitelist", "Blacklist", command=_on_mob_mode_change)
            mob_menu.config(bg="#1E293B", fg="#E2E8F0", relief="flat", bd=0, font=("Segoe UI", 7), cursor="hand2")
            mob_menu["menu"].config(bg="#1E293B", fg="#E2E8F0")
            mob_menu.pack(side="left", padx=(1, 6))

            tk.Label(mode_frame, text="Drops:", **style_lbl).pack(side="left")
            self._drop_mode_var = tk.StringVar(value=self._drop_filter_mode)
            def _on_drop_mode_change(val):
                self._drop_filter_mode = val
                self._save_filters_config()
            drop_menu = tk.OptionMenu(mode_frame, self._drop_mode_var, "Todos", "Whitelist", "Blacklist", command=_on_drop_mode_change)
            drop_menu.config(bg="#1E293B", fg="#E2E8F0", relief="flat", bd=0, font=("Segoe UI", 7), cursor="hand2")
            drop_menu["menu"].config(bg="#1E293B", fg="#E2E8F0")
            drop_menu.pack(side="left", padx=1)

            # Entrada manual rápida
            manual_frame = tk.Frame(tab_filtros, bg="#0F172A")
            manual_frame.pack(fill="x", padx=4, pady=1)
            self._filter_manual_var = tk.StringVar()
            tk.Entry(manual_frame, textvariable=self._filter_manual_var, **style_entry).pack(side="left", fill="x", expand=True, padx=(0, 2))

            def _add_manual_filter(target_kind):
                txt = self._filter_manual_var.get().strip()
                if not txt:
                    return
                is_id = txt.isdigit() or (txt.startswith("#") and txt[1:].isdigit())
                val_id = int(txt.replace("#", "")) if is_id else None
                val_name = txt.lower() if not is_id else None
                if target_kind == "mob_wl":
                    if val_id: self.mob_whitelist.add(val_id)
                    if val_name: self.mob_whitelist_names.add(val_name)
                elif target_kind == "mob_bl":
                    if val_id: self.mob_blacklist.add(val_id)
                    if val_name: self.mob_blacklist_names.add(val_name)
                elif target_kind == "drop_wl":
                    if val_id: self.drop_whitelist.add(val_id)
                    if val_name: self.drop_whitelist_names.add(val_name)
                elif target_kind == "drop_bl":
                    if val_id: self.drop_blacklist.add(val_id)
                    if val_name: self.drop_blacklist_names.add(val_name)
                self._save_filters_config()
                _refresh_filters_list()
                self._filter_manual_var.set("")

            tk.Button(manual_frame, text="+ Mob WL", command=lambda: _add_manual_filter("mob_wl"),
                      bg="#1E293B", fg="#4ADE80", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)
            tk.Button(manual_frame, text="+ Mob BL", command=lambda: _add_manual_filter("mob_bl"),
                      bg="#1E293B", fg="#F87171", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)
            tk.Button(manual_frame, text="+ Item WL", command=lambda: _add_manual_filter("drop_wl"),
                      bg="#1E293B", fg="#38BDF8", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)
            tk.Button(manual_frame, text="+ Item BL", command=lambda: _add_manual_filter("drop_bl"),
                      bg="#1E293B", fg="#FB923C", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)

            filt_list_frame = tk.Frame(tab_filtros, bg="#0F172A")
            filt_list_frame.pack(fill="x", padx=4, pady=1)
            filt_scroll = tk.Scrollbar(filt_list_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._active_filters_listbox = tk.Listbox(filt_list_frame, bg="#1E293B", fg="#E2E8F0",
                                                      selectbackground="#3B82F6", font=("Segoe UI", 7),
                                                      relief="flat", bd=0, height=3, yscrollcommand=filt_scroll.set)
            filt_scroll.config(command=self._active_filters_listbox.yview)
            self._active_filters_listbox.pack(side="left", fill="x", expand=True)
            filt_scroll.pack(side="right", fill="y")
            self._active_filters_data = []

            def _remove_selected_filter():
                sel = self._active_filters_listbox.curselection()
                if sel and sel[0] < len(self._active_filters_data):
                    f_kind, val = self._active_filters_data[sel[0]]
                    if f_kind == "mob_wl_id": self.mob_whitelist.discard(val)
                    elif f_kind == "mob_wl_name": self.mob_whitelist_names.discard(val)
                    elif f_kind == "mob_bl_id": self.mob_blacklist.discard(val)
                    elif f_kind == "mob_bl_name": self.mob_blacklist_names.discard(val)
                    elif f_kind == "drop_wl_id": self.drop_whitelist.discard(val)
                    elif f_kind == "drop_wl_name": self.drop_whitelist_names.discard(val)
                    elif f_kind == "drop_bl_id": self.drop_blacklist.discard(val)
                    elif f_kind == "drop_bl_name": self.drop_blacklist_names.discard(val)
                    self._save_filters_config()
                    _refresh_filters_list()

            def _clear_all_filters():
                self.mob_whitelist.clear()
                self.mob_whitelist_names.clear()
                self.mob_blacklist.clear()
                self.mob_blacklist_names.clear()
                self.drop_whitelist.clear()
                self.drop_whitelist_names.clear()
                self.drop_blacklist.clear()
                self.drop_blacklist_names.clear()
                self._save_filters_config()
                _refresh_filters_list()

            filt_btn_frame = tk.Frame(tab_filtros, bg="#0F172A")
            filt_btn_frame.pack(fill="x", padx=4, pady=1)
            tk.Button(filt_btn_frame, text="Remover Selecionado", command=_remove_selected_filter,
                      bg="#1E293B", fg="#F87171", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)
            tk.Button(filt_btn_frame, text="Limpar Todos", command=_clear_all_filters,
                      bg="#1E293B", fg="#94A3B8", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)

            _refresh_filters_list()

            # ══════════════════════════════════════════════════════════════
            # ABA 3: NPCS & PORTAIS
            # ══════════════════════════════════════════════════════════════
            tk.Label(tab_npcs, text="NPCs e Portais Próximos (<= 30 células):", **style_lbl).pack(anchor="w", padx=4, pady=(2, 0))
            npc_frame = tk.Frame(tab_npcs, bg="#0F172A")
            npc_frame.pack(fill="both", expand=True, padx=4, pady=1)
            npc_scroll = tk.Scrollbar(npc_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._npc_listbox = tk.Listbox(
                npc_frame, bg="#1E293B", fg="#C084FC",
                selectbackground="#3B82F6", font=("Segoe UI", 8),
                relief="flat", bd=0, height=10, yscrollcommand=npc_scroll.set,
            )
            npc_scroll.config(command=self._npc_listbox.yview)
            self._npc_listbox.pack(side="left", fill="both", expand=True)
            npc_scroll.pack(side="right", fill="y")
            self._npc_list_data = []

            def _go_interact():
                sel = self._npc_listbox.curselection()
                if not sel:
                    return
                idx = sel[0]
                if idx < len(self._npc_list_data):
                    _aid, _kind, tx, ty, _name = self._npc_list_data[idx]
                    curr_map = getattr(getattr(self.feed, "world", None), "map", "") or ""
                    now_i = time.monotonic()
                    px_i, py_i = None, None
                    if hasattr(self.feed, "world") and self.feed.world.player:
                        px_i, py_i = self.feed.world.player.cell(now_i)
                    dist_i = math.hypot(px_i - tx, py_i - ty) if px_i is not None else 999.0

                    # Se diálogo já estiver aberto, encerra navegação imediatamente
                    dialog_obj = getattr(getattr(self.feed, "world", None), "npc_dialog", {})
                    if dialog_obj.get("open"):
                        self.nav_goal = None
                        self.patrol_target_route = None
                        self._npc_interact_state = None
                        return

                    # Nunca clica a > 3.8 células: navega via A* até <= 3.5 células
                    if dist_i > 3.8:
                        self.nav_goal = (curr_map, tx, ty)
                        self.patrol_target_route = (curr_map, tx, ty, _aid)
                        self.active_nav_color = "#A78BFA"
                        self._npc_interact_state = {"clicks": 0, "last_click": 0.0}
                        self.auto_patrol = True
                        self._click_debug(f"INTERAGIR: Longe ({dist_i:.1f}c > 3.8c), aproximando via A* até {_name}...")
                        return

                    # Já está a <= 3.8 células: clica no centro do sprite (max 2 tentativas, cadence 950ms)
                    st_i = getattr(self, "_npc_interact_state", None) or {"clicks": 0, "last_click": 0.0}
                    if st_i["clicks"] >= 2:
                        self._click_debug("INTERAGIR: Limite de 2 tentativas atingido sem abrir diálogo. Encerrando.")
                        self.nav_goal = None
                        self.patrol_target_route = None
                        self._npc_interact_state = None
                        return

                    if now_i - st_i["last_click"] < 0.950:
                        return

                    snap_i = self.feed.current()
                    if snap_i:
                        gat_i = getattr(self.feed, "terrain", None).for_map(curr_map) if getattr(self.feed, "terrain", None) else None
                        h_fn_i = getattr(gat_i, "get_elevation", None) if gat_i else None
                        cam_i = getattr(self.feed, "_cam", None) or (px_i, py_i)
                        proj_i = self.feed.camera.projector(snap_i.viewport, cam_i, h_fn_i)
                        sprite_z_i = 5.5 if _kind == "npc" else 1.2
                        u_i, v_i = proj_i(tx, ty, sprite_z_i)
                        left_i, top_i, w_i, h_i = snap_i.viewport
                        sx_i = left_i + int(u_i * w_i)
                        sy_i = top_i + int(v_i * h_i)

                        if self._is_point_in_game_ui(sx_i, sy_i):
                            self._side_step_to_reveal((tx, ty), px_i, py_i)
                            return

                        g_hwnd_i = self.get_game_hwnd(None)
                        if g_hwnd_i:
                            u32.SetForegroundWindow(g_hwnd_i)

                        st_i["clicks"] += 1
                        st_i["last_click"] = now_i
                        self._npc_interact_state = st_i
                        self._click_debug(f"INTERAGIR: clique {st_i['clicks']}/2 em ({sx_i},{sy_i}) para {_name} a {dist_i:.1f}c")
                        self._send_click(g_hwnd_i, sx_i, sy_i)

                        if st_i["clicks"] >= 2:
                            self.nav_goal = None
                            self.patrol_target_route = None
                            self._npc_interact_state = None

            self._npc_listbox.bind("<Double-Button-1>", lambda _e: _go_interact())
            tk.Button(tab_npcs, text="Ir / Interagir (ou Duplo-Clique)", command=_go_interact,
                      bg="#1D4ED8", fg="#E2E8F0", relief="flat",
                      font=("Segoe UI", 8, "bold"), cursor="hand2").pack(fill="x", padx=4, pady=3)

            # ══════════════════════════════════════════════════════════════
            # ABA 4: HISTÓRICO
            # ══════════════════════════════════════════════════════════════
            self._kills_lbl = tk.Label(tab_historico, text=f"Kills Recentes (Total Abatidos: {self._session_kill_count}):", **style_lbl)
            self._kills_lbl.pack(anchor="w", padx=4, pady=(2, 0))
            kills_frame = tk.Frame(tab_historico, bg="#0F172A")
            kills_frame.pack(fill="both", expand=True, padx=4, pady=1)
            kills_scroll = tk.Scrollbar(kills_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._kills_listbox = tk.Listbox(kills_frame, bg="#1E293B", fg="#F87171",
                                             selectbackground="#3B82F6", font=("Segoe UI", 7),
                                             relief="flat", bd=0, height=5, yscrollcommand=kills_scroll.set)
            kills_scroll.config(command=self._kills_listbox.yview)
            self._kills_listbox.pack(side="left", fill="both", expand=True)
            kills_scroll.pack(side="right", fill="y")

            tk.Frame(tab_historico, bg="#334155", height=1).pack(fill="x", padx=4, pady=2)

            tk.Label(tab_historico, text="Loot Coletado Recentemente (0x00A0):", **style_lbl).pack(anchor="w", padx=4, pady=(1, 0))
            loot_frame = tk.Frame(tab_historico, bg="#0F172A")
            loot_frame.pack(fill="both", expand=True, padx=4, pady=1)
            loot_scroll = tk.Scrollbar(loot_frame, orient="vertical", bg="#1E293B", troughcolor="#0F172A")
            self._picked_listbox = tk.Listbox(loot_frame, bg="#1E293B", fg="#4ADE80",
                                              selectbackground="#3B82F6", font=("Segoe UI", 7),
                                              relief="flat", bd=0, height=5, yscrollcommand=loot_scroll.set)
            loot_scroll.config(command=self._picked_listbox.yview)
            self._picked_listbox.pack(side="left", fill="both", expand=True)
            loot_scroll.pack(side="right", fill="y")

            loot_btn_frame = tk.Frame(tab_historico, bg="#0F172A")
            loot_btn_frame.pack(fill="x", padx=4, pady=2)

            def _add_picked_whitelist():
                sel = self._picked_listbox.curselection()
                if sel and sel[0] < len(getattr(self, "_loot_history", [])):
                    _h, iname, _cnt, iid = self._loot_history[sel[0]]
                    if iid: self.drop_whitelist.add(iid)
                    if iname: self.drop_whitelist_names.add(iname.lower())
                    self._save_filters_config()
                    _refresh_filters_list()
                    self._click_debug(f"COLETADO: Adicionado à Whitelist: {iname} (#{iid})")

            def _add_picked_blacklist():
                sel = self._picked_listbox.curselection()
                if sel and sel[0] < len(getattr(self, "_loot_history", [])):
                    _h, iname, _cnt, iid = self._loot_history[sel[0]]
                    if iid: self.drop_blacklist.add(iid)
                    if iname: self.drop_blacklist_names.add(iname.lower())
                    self._save_filters_config()
                    _refresh_filters_list()
                    self._click_debug(f"COLETADO: Adicionado à Blacklist: {iname} (#{iid})")

            def _clear_loot_history():
                self._loot_history.clear()
                self._picked_listbox.delete(0, tk.END)

            tk.Button(loot_btn_frame, text="+ Whitelist Selecionado", command=_add_picked_whitelist,
                      bg="#1E293B", fg="#4ADE80", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)
            tk.Button(loot_btn_frame, text="+ Blacklist Selecionado", command=_add_picked_blacklist,
                      bg="#1E293B", fg="#F87171", relief="flat", font=("Segoe UI", 7, "bold"), cursor="hand2").pack(side="left", padx=1)
            tk.Button(loot_btn_frame, text="Limpar", command=_clear_loot_history,
                      bg="#1E293B", fg="#94A3B8", relief="flat", font=("Segoe UI", 7), cursor="hand2").pack(side="left", padx=1)

            self._hud_panel = panel

            # ── Estilização Win32 (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) + keepontop loop ──
            def _apply_hud_win32_styles():
                try:
                    panel.update_idletasks()
                    hwnd = panel.winfo_id()
                    p_hwnd = ctypes.windll.user32.GetParent(hwnd)
                    hud_hwnd = p_hwnd if p_hwnd else hwnd
                    # Apenas WS_EX_TOOLWINDOW (0x80) | WS_EX_NOACTIVATE (0x08000000) —
                    # sem WS_DISABLED e sem GWLP_HWNDPARENT cruzado (causaria err=5 no InjectTouchInput).
                    ex_style = ctypes.windll.user32.GetWindowLongW(hud_hwnd, -20)
                    ex_style = (ex_style | 0x00000080 | 0x08000000) & ~0x08000000  # TOOLWINDOW | NOACTIVATE; NOACTIVATE = 0x08000000
                    # Reescrever: WS_EX_TOOLWINDOW=0x80, WS_EX_NOACTIVATE=0x08000000
                    ex_style_new = (ctypes.windll.user32.GetWindowLongW(hud_hwnd, -20) | 0x00000080 | 0x08000000)
                    ctypes.windll.user32.SetWindowLongW(hud_hwnd, -20, ex_style_new)
                    # Visibilidade: garante HUD na tela e no topo
                    panel.deiconify()
                    panel.lift()
                    panel.attributes("-topmost", True)
                    panel.attributes("-alpha", 0.90)
                    # Valida coordenadas: reposiciona se fora do monitor visível
                    px, py = panel.winfo_x(), panel.winfo_y()
                    sw = panel.winfo_screenwidth()
                    sh = panel.winfo_screenheight()
                    if px < 0 or px > sw - 50 or py < 0 or py > sh - 50:
                        panel.geometry(f"+{max(0, sw - 420)}+50")
                    # HWND_TOPMOST | SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                    ctypes.windll.user32.SetWindowPos(hud_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                except Exception as exc:
                    print(f"[HUD] Win32 style hook: {exc}", flush=True)

            def _hud_keepontop_loop():
                try:
                    if not panel.winfo_exists():
                        return
                    if getattr(self, "_hud_panel_visible", True):
                        hwnd = panel.winfo_id()
                        p_hwnd = ctypes.windll.user32.GetParent(hwnd)
                        hud_hwnd = p_hwnd if p_hwnd else hwnd
                        ctypes.windll.user32.SetWindowPos(hud_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                except Exception:
                    pass
                panel.after(500, _hud_keepontop_loop)

            panel.after(100, _apply_hud_win32_styles)
            panel.after(600, _hud_keepontop_loop)

            def _update_hud():
                try:
                    if not panel.winfo_exists():
                        return
                    for _, upd in self._hud_btns.values():
                        try: upd()
                        except Exception: pass

                    # Atualiza telemetrias no topo do HUD
                    if hasattr(self.feed, "stats") and hasattr(self.feed, "world"):
                        st = self.feed.stats() or {}
                        m_name = st.get("current_map") or st.get("map") or "---"
                        pos = st.get("pos")
                        pos_str = f"({int(pos[0])}, {int(pos[1])})" if pos else "---"
                        b_lvl = st.get("base_level", 1)
                        w = st.get("weight", 0)
                        mw = st.get("max_weight", 1)
                        w_ratio = st.get("weight_ratio", 0.0)
                        telem_map_lbl.config(text=f"Mapa: {m_name} {pos_str} | Nv. {b_lvl} | Peso: {w}/{mw} ({w_ratio*100:.0f}%)")

                        hp = st.get("hp", 0)
                        max_hp = max(1, st.get("max_hp", 1))
                        hp_pct = (hp / max_hp) * 100
                        sp = st.get("sp", 0)
                        max_sp = max(1, st.get("max_sp", 1))
                        sp_pct = (sp / max_sp) * 100
                        telem_hp_sp_lbl.config(text=f"HP: {hp}/{max_hp} ({hp_pct:.0f}%) | SP: {sp}/{max_sp} ({sp_pct:.0f}%)")

                        if hasattr(self.feed, "camera"):
                            cam = self.feed.camera
                            telem_cam_lbl.config(text=f"Câmera: Yaw {cam.yaw_deg:.0f}° | Pitch {cam.pitch_deg:.1f}° | Dist {cam.distance:.0f}")

                        status_str = f"[Farm: {'ON' if self.auto_farm else 'OFF'}] [Loot: {'ON' if self.auto_loot else 'OFF'}] [Patrol: {'ON' if self.auto_patrol else 'OFF'}] [Sentar: {'ON' if getattr(self, 'auto_sit', False) else 'OFF'}]"
                        if getattr(self, "last_farm_spot", None):
                            status_str += f" | Spot: {self.last_farm_spot[0]}"
                        if getattr(self, "patrol_target_route", None):
                            status_str += f" | Rota: {self.patrol_target_route[0]}"
                        telem_status_lbl.config(text=status_str)

                    # 1. Atualização Estável de NPCs e Portais (Aba 3)
                    if hasattr(self.feed, "world") and self._npc_listbox:
                        w = self.feed.world
                        now_t = time.monotonic()
                        px_h, py_h = None, None
                        with w.lock:
                            if w.player: px_h, py_h = w.player.cell(now_t)
                            names_h = dict(getattr(w, "entity_names", {}))
                            ents_h = list(w.entities.items())
                        new_npc_data = []
                        if px_h is not None:
                            for (ekind, aid_h), ent_h in ents_h:
                                if ekind in ("npc", "warp"):
                                    c_id = getattr(ent_h, "catalog_id", 0)
                                    name_h = names_h.get(aid_h, "")
                                    # Filtra NPCs invisíveis ou gatilhos de script sem nome real
                                    if ekind == "npc" and c_id in (111, 139, 32767) and not name_h:
                                        continue
                                    tx_h = int(ent_h.track.target[0])
                                    ty_h = int(ent_h.track.target[1])
                                    dist_h = math.hypot(tx_h - px_h, ty_h - py_h)
                                    if dist_h <= 30.0:
                                        display_name = name_h if name_h else (f"NPC #{c_id}" if ekind == "npc" else f"Portal #{aid_h}")
                                        new_npc_data.append((aid_h, ekind, tx_h, ty_h, display_name, dist_h))
                            new_npc_data.sort(key=lambda x: x[5])

                        curr_sig = [(item[0], item[4]) for item in new_npc_data[:25]]
                        if curr_sig != getattr(self, "_last_rendered_npc_items", None):
                            cur_sel_aid = None
                            cur_sel = self._npc_listbox.curselection()
                            if cur_sel and cur_sel[0] < len(self._npc_list_data):
                                cur_sel_aid = self._npc_list_data[cur_sel[0]][0]
                            y_scroll = self._npc_listbox.yview()

                            self._npc_list_data = [(x[0], x[1], x[2], x[3], x[4]) for x in new_npc_data[:25]]
                            self._npc_listbox.delete(0, tk.END)
                            restore_idx = None
                            for i, (aid_h, ekind, tx_h, ty_h, name_h, dist_h) in enumerate(new_npc_data[:25]):
                                icon = "⬟" if ekind == "warp" else "◈"
                                self._npc_listbox.insert(tk.END, f"{icon} {name_h} ({tx_h},{ty_h}) {dist_h:.1f}c")
                                if cur_sel_aid is not None and aid_h == cur_sel_aid:
                                    restore_idx = i
                            if restore_idx is not None:
                                self._npc_listbox.selection_set(restore_idx)
                            try:
                                self._npc_listbox.yview_moveto(y_scroll[0])
                            except Exception:
                                pass
                            self._last_rendered_npc_items = curr_sig

                    # 2. Atualização dos Monstros Vivos Agora (Aba 2 - estritamente vivos, ordenados por distância)
                    if hasattr(self.feed, "world") and hasattr(self, "_alive_mobs_listbox") and self._alive_mobs_listbox:
                        w = self.feed.world
                        now_m = time.monotonic()
                        px_m, py_m = (None, None)
                        alive_ents = []
                        with w.lock:
                            if w.player: px_m, py_m = w.player.cell(now_m)
                            for (ekind, gid_k), ent in w.entities.items():
                                if ekind == "mob":
                                    if getattr(ent, "hp", 1) is None or getattr(ent, "hp", 1) > 0:
                                        alive_ents.append((gid_k, ent.catalog_id, ent.track.target))
                        alive_formatted = []
                        for gid_k, cid, target_pos in alive_ents:
                            mname = self.names.get("mob", {}).get(cid, f"Mob #{cid}")
                            dist_c = math.hypot(px_m - target_pos[0], py_m - target_pos[1]) if px_m is not None else 99.0
                            alive_formatted.append((dist_c, cid, mname, f"⚔ {mname} (#{cid}) {dist_c:.1f}c"))
                        alive_formatted.sort(key=lambda x: x[0])
                        sig = [x[1] for x in alive_formatted[:15]]
                        if sig != getattr(self, "_last_alive_mobs_sig", None):
                            self._alive_mobs_data = [(x[1], x[2]) for x in alive_formatted[:15]]
                            self._alive_mobs_listbox.delete(0, tk.END)
                            for item in alive_formatted[:15]:
                                self._alive_mobs_listbox.insert(tk.END, item[3])
                            self._last_alive_mobs_sig = sig

                    # 3. Atualização do Histórico de Kills (Aba 4 - via 0x0080 death e abates da sessão)
                    if hasattr(self.feed, "world") and self.feed.world and hasattr(self, "_kills_listbox") and self._kills_listbox:
                        w = self.feed.world
                        with w.lock:
                            dead_list = list(w.recent_dead_mobs)
                        new_kills = False
                        for d in dead_list:
                            d_time = d.get("time", 0.0)
                            if d_time > self._kill_history_last_check:
                                if d.get("by_me", False):
                                    cid = d.get("catalog_id", 0)
                                    mname = self.names.get("mob", {}).get(cid, f"Mob #{cid}")
                                    k_ts = d.get("kill_ts", time.strftime("%H:%M:%S"))
                                    self._kill_history.append((k_ts, mname, cid))
                                    self._session_kill_count += 1
                                    new_kills = True
                        self._kill_history_last_check = time.monotonic()
                        if new_kills:
                            self._kills_lbl.config(text=f"Kills Recentes (Total Abatidos: {self._session_kill_count}):")
                            self._kills_listbox.delete(0, tk.END)
                            for k_ts, mname, cid in reversed(self._kill_history[-50:]):
                                self._kills_listbox.insert(tk.END, f"[{k_ts}] ⚔ {mname} (#{cid})")

                    # 4. Atualização do Histórico de Loot Coletado (Aba 4 - via 0x00A0 + 0x00A1)
                    if hasattr(self, "_picked_listbox") and self._picked_listbox:
                        loot_sig = [(item[0], item[3]) for item in self._loot_history[:15]]
                        if loot_sig != getattr(self, "_last_rendered_loot_sig", None):
                            self._picked_listbox.delete(0, tk.END)
                            for hora, iname, cnt, iid in self._loot_history[:50]:
                                self._picked_listbox.insert(tk.END, f"[{hora}] ✓ {iname} ×{cnt} (#{iid})")
                            self._last_rendered_loot_sig = loot_sig

                except Exception:
                    pass
                try:
                    panel.after(350, _update_hud)
                except Exception:
                    pass

            _update_hud()
        except Exception as exc:
            print(f"[HUD] Erro ao criar painel de controle: {exc}", flush=True)
            self._hud_panel = None

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
                        c_id = item.catalog_id
                        name = getattr(self.feed.world, "entity_names", {}).get(c_id, "").lower() if hasattr(self.feed, "world") else ""
                        if c_id in (112, 113, 114, 115, 116, 117) or any(k in name for k in ("kafra", "funcionária", "funcionaria")):
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
                        self._send_click(target_hwnd, sx, sy)
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
                        self._send_game_key(0x0D, hold_ms=40)
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
                            self._send_game_key(0x1B, hold_ms=35)


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
                    if nCode >= 0 and wParam == WM_KEYDOWN:
                        kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = kbd.vkCode
                        # Só acumula se Ragexe.exe estiver em foco
                        fg = u32.GetForegroundWindow()
                        if fg:
                            pid_buf = wintypes.DWORD(0)
                            u32.GetWindowThreadProcessId(fg, ctypes.byref(pid_buf))
                            if pid_buf.value == getattr(self, "_ragexe_pid", 0) or True:
                                if vk == VK_RETURN:
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
            handle = u32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, None, 0)
            self._navi_hook_handle = handle
        except Exception as exc:
            print(f"[Navi] Falha ao instalar hook de teclado: {exc}", flush=True)

    def close(self):
        """Libera conexões seriais e contextos de driver HID."""
        if hasattr(self, "_navi_hook_handle") and self._navi_hook_handle:
            try:
                u32.UnhookWindowsHookEx(self._navi_hook_handle)
            except Exception:
                pass
            self._navi_hook_handle = None
        if hasattr(self, "serial_hid") and self.serial_hid:
            self.serial_hid.close()
        if hasattr(self, "interception") and self.interception:
            self.interception.close()

    def refresh(self):
        if not self._game_is_foreground():
            # Oculta todos os itens do pool sem destruí-los (preserva IDs válidos do canvas)
            self.canvas_pool.hide_all()
            self.last = None
            if self._hud_panel and self._hud_panel.winfo_exists() and self._hud_panel.winfo_viewable():
                self._hud_panel.withdraw()
            self.window.after(40, self.refresh)
            return
        else:
            if self._hud_panel and self._hud_panel.winfo_exists() and self._hud_panel_visible and not self._hud_panel.winfo_viewable():
                self._hud_panel.deiconify()

        # Hotkeys: ← = Auto-Loot toggle, → = Auto-Farm toggle, F6 = Auto-Patrol toggle, F9 = Teste imediato
        left_now   = bool(self.user32.GetAsyncKeyState(0x25) & 0x8000)
        right_now  = bool(self.user32.GetAsyncKeyState(0x27) & 0x8000)
        insert_now = bool(self.user32.GetAsyncKeyState(0x2D) & 0x8000)
        f6_now     = bool(self.user32.GetAsyncKeyState(0x75) & 0x8000)
        f9_now     = bool(self.user32.GetAsyncKeyState(0x78) & 0x8000)
        f8_now     = bool(self.user32.GetAsyncKeyState(0x77) & 0x8000)

        if f8_now and not self._last_f8:
            if self._hud_panel and self._hud_panel.winfo_exists():
                if self._hud_panel_visible:
                    self._hud_panel.withdraw()
                    self._hud_panel_visible = False
                else:
                    self._hud_panel.deiconify()
                    self._hud_panel_visible = True

        if (left_now and not self.last_left) or (insert_now and not self.last_insert):
            self.auto_loot = not self.auto_loot
            if not self.auto_loot:
                self.locked_drop_gid = None
                self.locked_drop_clicks = 0
                self.locked_drop_first_time = 0.0
            print(f"[Auto-Loot] {'ON' if self.auto_loot else 'OFF'}", flush=True)
        if right_now and not self.last_right:
            self.auto_farm = not self.auto_farm
            if not self.auto_farm:
                self.locked_mob_gid = None
                self.locked_mob_clicks = 0
                self.locked_mob_first_time = 0.0
            print(f"[Auto-Farm] {'ON' if self.auto_farm else 'OFF'}", flush=True)
        if f6_now and not self.last_f6:
            self.auto_patrol = not self.auto_patrol
            if not self.auto_patrol:
                self.current_patrol_path = []
                self.current_waypoint = None
            print(f"[Auto-Patrol / Roam] {'ON' if self.auto_patrol else 'OFF'}", flush=True)
        if f9_now and not self.last_f9:
            self.test_click_f9()

        self.last_left   = left_now
        self.last_right  = right_now
        self.last_insert = insert_now
        self.last_f6     = f6_now
        self.last_f9     = f9_now
        self._last_f8    = f8_now

        snapshot = self.feed.current()
        stats = self.feed.stats() if hasattr(self.feed, "stats") else None

        if snapshot is None:
            self.canvas_pool.hide_all()
            self.last = None
        else:
            self.current_viewport = snapshot.viewport
            if self.draw(snapshot, stats):
                self.last = snapshot
                self.last_stats = stats
            self._handle_actions(snapshot)
        self.window.after(16, self.refresh)

    def _entity_distance(self, kind, key, px, py):
        """Retorna a distância Chebyshev (8 direções) em células."""
        if px is None or py is None or not hasattr(self.feed, "world"):
            return 0.0
        cell = self.feed.world.get_entity_cell(kind, key)
        if not cell:
            return 999.0
        return max(abs(cell[0] - px), abs(cell[1] - py))

    def _is_mob_hostile(self, item_key, cid):
        """Classificação de Hostilidade (3 Camadas):
        1. Por espécie (get_mob_catalog): Imóvel = False, Passivo = False, Demais = True.
        2. Por aggro em rede: mob.is_aggro == True -> Hostil.
        3. Por vetor de aproximação: mob.is_hostile_now == True -> Hostil.
        """
        if hasattr(self.feed, "world"):
            try:
                aid = int(item_key.split(":", 1)[1])
                ent = self.feed.world.entities.get(("mob", aid))
                if ent:
                    if getattr(ent, "is_aggro", False) or getattr(ent, "is_hostile_now", False):
                        return True
            except Exception:
                pass
        cat = get_mob_catalog().get("by_id", {})
        entry = cat.get(cid)
        if entry:
            if entry.get("immobile", False) or not entry.get("hostile", True):
                return False
            return True
        return True

    def _handle_actions(self, snapshot):
        """Coordena o loop entre Farm, Loot, Auto-Sentar e Patrol/Roam com prioridades estritas:
        - 1. Jogador Sentado / Recuperação de HP e autodefesa de emergência.
        - 2. Recuo Estratégico (Cidade ou Célula Segura se HP < 30%).
        - 3. Autodefesa: se mob agressor ou <= 3.0 células com Farm/Loot ativo, reage imediatamente.
        - 4. Auto-Loot Tier 1: Cartas, Equipamentos e Valiosos interrompem farm e têm prioridade máxima.
        - 5. Auto-Loot: coleta drops elegíveis <= 22 células antes de farm distante.
        - 6. Auto-Farm: ataca monstro mais próximo (dist crescente).
        - 7. Auto-Patrol: só quando 1-6 inativos.
        """
        if not self.auto_loot and not self.auto_farm and not self.auto_patrol and not getattr(self, "auto_sit", False):
            return

        # Pausa automática se o HUD (ControlPanel) estiver em foreground
        if self._is_hud_foreground():
            return

        now = time.monotonic()
        px, py = (None, None)
        player = None
        if hasattr(self.feed, "world"):
            _, player, _ = self.feed.world.snapshot()
            if player:
                px, py = player.cell(now)

        # Limpa blacklists expiradas
        self._drop_blacklist = {gid: exp for gid, exp in self._drop_blacklist.items() if now < exp}
        self._farm_blacklist = {gid: exp for gid, exp in self._farm_blacklist.items() if now < exp}

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

        # Gerenciamento de Viagem entre Cidades (Kafra / Portais)
        if getattr(self, "_city_travel_state", None):
            self._handle_city_travel(snapshot, px, py)

        w = getattr(self.feed, "world", None)
        hp = getattr(w, "player_hp", 0) if w else 0
        max_hp = getattr(w, "player_max_hp", 0) if w else 0
        hp_ratio = (hp / max_hp) if max_hp > 0 else 1.0

        c_map = (getattr(w, "map", "") or "").lower().removesuffix(".gat").removesuffix(".rsw").strip()
        is_city = c_map in KNOWN_CITIES or any(matches_city(c_map, city) for city in ("prontera", "izlude", "geffen", "payon", "morocc", "alberta", "aldebaran", "rachel", "veins", "yuno", "juno", "comodo", "einbroch", "lighthalzen", "hugel"))

        is_dead = (
            (player.get("is_dead", False) if isinstance(player, dict) else getattr(player, "is_dead", False))
            or getattr(snapshot, "is_dead", False)
            or (w and getattr(w, "player_is_dead", False))
            or (hp <= 1 and max_hp > 1)
        )

        # 1. Salva spot de farm e modos ativos quando em mapa de caça e vivo
        if self.auto_farm and not is_dead and not is_city and px is not None and py is not None:
            self.last_farm_spot = (c_map, int(round(px)), int(round(py)))
            self.last_farm_modes = {
                "farm": self.auto_farm,
                "loot": self.auto_loot,
                "patrol": self.auto_patrol,
            }

        # 2. Verificação de Morte e Auto-Ressuscitar
        if is_dead:
            self.fleeing_mode = False
            self.is_sitting = False
            self._sit_retreat_state = None
            if getattr(self, "auto_resurrect", None) and self.auto_resurrect.get():
                # Pause Farm/Loot/Patrol
                self.auto_farm = False
                self.auto_loot = False
                self.auto_patrol = False
                for btn_key in ("farm", "loot", "patrol"):
                    if hasattr(self, "_hud_btns") and btn_key in self._hud_btns:
                        self._hud_btns[btn_key][1]()

                self._respawn_state = "healing"

                # Aguarda 900ms após death_ts
                p_dts = player.get("death_ts", 0.0) if isinstance(player, dict) else getattr(player, "death_ts", 0.0)
                death_ts = p_dts or getattr(snapshot, "death_ts", 0.0) or (w and getattr(w, "player_death_ts", 0.0)) or 0.0
                if death_ts <= 0.0:
                    if not getattr(self, "_death_start_ts", 0.0):
                        self._death_start_ts = now
                    death_elapsed = now - self._death_start_ts
                else:
                    death_elapsed = time.time() - death_ts

                if death_elapsed >= 0.9:
                    if now - getattr(self, "_last_resurrect_press", 0.0) >= 1.8:
                        self._last_resurrect_press = now
                        vp_obj = getattr(self.feed, "viewport", None)
                        hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
                        game_hwnd = self.get_game_hwnd(hwnd)
                        if snapshot.viewport:
                            vx, vy, vw, vh = snapshot.viewport
                            target_x = vx + int(vw * 0.50)
                            toggle = getattr(self, "_death_click_toggle", 0)
                            target_y = vy + int(vh * (0.665 if toggle % 2 == 1 else 0.638))
                            self._death_click_toggle = toggle + 1
                            self._click_debug(f"AUTO-RESSUSCITAR: Clicando retorno modal em ({target_x}, {target_y})")
                            self.touch_postmsg(target_x, target_y, game_hwnd)
            return
        else:
            self._death_start_ts = 0.0
            self._death_click_toggle = 0

        # 3. Gerenciamento pós-ressurreição na cidade (cura e retorno /navi)
        if getattr(self, "_respawn_state", None) and not is_dead and px is not None and py is not None:
            if self._respawn_state == "healing":
                vp_obj = getattr(self.feed, "viewport", None)
                hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
                game_hwnd = self.get_game_hwnd(hwnd)

                if hp_ratio < 0.85:
                    healer_obj = None
                    healer_cell = None
                    min_h_dist = 999.0
                    for item in snapshot.objects:
                        if item.kind == "npc":
                            c_id = item.catalog_id
                            name = getattr(self.feed.world, "entity_names", {}).get(c_id, "").lower() if hasattr(self.feed, "world") else ""
                            if any(k in name for k in ("curandeira", "healer", "cura", "recuperadora", "curador", "enfermeira")):
                                d = self._entity_distance("npc", item.key, px, py)
                                if d <= 14.0 and d < min_h_dist:
                                    min_h_dist = d
                                    healer_obj = item
                                    healer_cell = self.feed.world.get_entity_cell("npc", item.key) if hasattr(self.feed, "world") else None

                    if healer_obj is not None:
                        if getattr(self, "is_sitting", False):
                            self._send_vk_key(0x2D, hwnd)
                            self.is_sitting = False

                        hx, hy = healer_cell if healer_cell else (px, py)
                        if min_h_dist <= 2.5:
                            if now - getattr(self, "_last_healer_click_ts", 0.0) >= 1.5:
                                self._last_healer_click_ts = now
                                left, top, width, height = snapshot.viewport
                                sx = left + int(healer_obj.u * width)
                                sy = top + int(healer_obj.v * height)
                                self._click_debug(f"RESPAWN: Interagindo com curandeira em ({sx}, {sy})")
                                self.touch_postmsg(sx, sy, game_hwnd)
                        else:
                            self.nav_goal = (c_map, int(hx), int(hy))
                            self.patrol_target_route = (c_map, int(hx), int(hy), None)
                            self.auto_patrol = True
                            self._try_auto_patrol(snapshot, px, py)
                        return
                    else:
                        if not getattr(self, "is_sitting", False):
                            self._click_debug("RESPAWN: Sem curandeira <= 14 cels, sentando via VK_INSERT")
                            self._send_vk_key(0x2D, hwnd)
                            self.is_sitting = True
                        return
                else:
                    if getattr(self, "is_sitting", False):
                        self._click_debug("RESPAWN: HP >= 85%, levantando")
                        self._send_vk_key(0x2D, hwnd)
                        self.is_sitting = False

                    if getattr(self, "last_farm_spot", None):
                        tgt_m, tgt_x, tgt_y = self.last_farm_spot
                        curr = getattr(getattr(self.feed, "world", None), "map", "") or ""
                        curr = curr.lower().removesuffix(".gat").removesuffix(".rsw").strip()
                        tgt_clean = str(tgt_m).lower().removesuffix(".gat").removesuffix(".rsw").strip()
                        plan = get_city_travel_plan(curr, tgt_clean)
                        if plan.get("mode") == "walk":
                            self._city_travel_state = {
                                "mode": "walk",
                                "target_city": tgt_clean,
                                "route": plan.get("route", []),
                                "step_idx": 0,
                            }
                        else:
                            self.patrol_target_route = (tgt_clean, int(tgt_x), int(tgt_y), None)
                        self.nav_goal = (tgt_clean, int(tgt_x), int(tgt_y))
                        self.auto_patrol = True
                        self._respawn_state = "returning"
                        self._click_debug(f"RESPAWN: Iniciando rota /navi para {tgt_clean} ({tgt_x}, {tgt_y})")
                        return
                    else:
                        self._respawn_state = None

            elif self._respawn_state == "returning":
                if getattr(self, "last_farm_spot", None):
                    tgt_m, tgt_x, tgt_y = self.last_farm_spot
                    curr = getattr(getattr(self.feed, "world", None), "map", "") or ""
                    curr = curr.lower().removesuffix(".gat").removesuffix(".rsw").strip()
                    tgt_clean = str(tgt_m).lower().removesuffix(".gat").removesuffix(".rsw").strip()
                    if curr == tgt_clean and math.hypot(px - tgt_x, py - tgt_y) <= 4.0:
                        self._click_debug(f"RESPAWN: Chegamos ao spot de farm {tgt_clean} ({tgt_x}, {tgt_y})! Reativando modos.")
                        self._respawn_state = None
                        self.nav_goal = None
                        self.patrol_target_route = None
                        self._city_travel_state = None
                        if getattr(self, "last_farm_modes", None):
                            self.auto_farm = self.last_farm_modes.get("farm", True)
                            self.auto_loot = self.last_farm_modes.get("loot", False)
                            self.auto_patrol = self.last_farm_modes.get("patrol", False)
                            for btn_key in ("farm", "loot", "patrol"):
                                if hasattr(self, "_hud_btns") and btn_key in self._hud_btns:
                                    self._hud_btns[btn_key][1]()

        # Ativação/Desativação de Modo Fuga
        try:
            flee_pct = float(self.flee_pct_var.get()) / 100.0 if getattr(self, "flee_pct_var", None) else 0.35
        except (ValueError, TypeError):
            flee_pct = 0.35

        flee_enabled = getattr(self, "flee_enabled", None) and self.flee_enabled.get()
        if flee_enabled and 0 < hp_ratio < flee_pct:
            self.fleeing_mode = True
        elif hp_ratio >= 0.85:
            self.fleeing_mode = False

        # Disparo de Poção/Asa opcional se < 20% HP
        if self.fleeing_mode and hp_ratio < 0.20 and getattr(self, "potion_key_enabled", None) and self.potion_key_enabled.get():
            key_name = self.potion_key_var.get().upper().strip()
            if key_name.startswith("F") and key_name[1:].isdigit():
                f_idx = int(key_name[1:])
                if 1 <= f_idx <= 9:
                    vk = 0x70 + (f_idx - 1)  # VK_F1..VK_F9
                    if now - getattr(self, "_last_potion_press", 0.0) >= 0.4:
                        self._last_potion_press = now
                        vp_obj = getattr(self.feed, "viewport", None)
                        hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
                        self._send_vk_key(vk, hwnd)
                        self._click_debug(f"FUGA: HP {hp_ratio:.0%} < 20% – acionando tecla {key_name}")

        # ── 1. GERENCIAMENTO DE JOGADOR SENTADO ──
        if getattr(self, "is_sitting", False) and px is not None:
            vp_obj = getattr(self.feed, "viewport", None)
            hwnd = getattr(vp_obj, "handle", None) if vp_obj else None

            # Dano sofrido enquanto sentado
            damage_taken = (self._last_player_hp is not None and hp < self._last_player_hp)

            # Monstro hostil a <= 4.5 células enquanto sentado
            too_close_hostile = False
            for item in snapshot.objects:
                if item.kind == "mob" and self._is_mob_hostile(item.key, item.catalog_id):
                    d = self._entity_distance("mob", item.key, px, py)
                    if d <= 4.5:
                        too_close_hostile = True
                        break

            if damage_taken or too_close_hostile:
                self._click_debug(f"AUTO-SENTAR: {'Dano sofrido' if damage_taken else 'Hostil a <= 4.5 cels'}, levantando imediatamente!")
                self._send_vk_key(0x2D, hwnd)
                self.is_sitting = False
                self._sit_retreat_state = None
                self._last_player_hp = hp
            elif hp_ratio >= 0.85:
                self._click_debug("AUTO-SENTAR: HP >= 85%, levantando")
                self._send_vk_key(0x2D, hwnd)
                self.is_sitting = False
                self.fleeing_mode = False
                self._last_player_hp = hp
                if self._sit_retreat_state and self._sit_retreat_state.get("mode") == "city":
                    orig_map = self._sit_retreat_state.get("origin_map")
                    ret_portal = self._sit_retreat_state.get("return_portal")
                    if ret_portal and orig_map:
                        self._click_debug(f"AUTO-SENTAR: Retornando ao mapa de origem {orig_map} pelo portal {ret_portal}")
                        curr_m = getattr(w, "map", "") or ""
                        self.patrol_target_route = (curr_m, ret_portal[0], ret_portal[1], "warp")
                        self.auto_patrol = True
                        self._sit_retreat_state = {"mode": "returning", "origin_map": orig_map}
                        return
                self._sit_retreat_state = None
                return
            else:
                self._last_player_hp = hp
                return  # Mantém sentado recuperando

        self._last_player_hp = hp

        # ── 2. CONTINUAÇÃO DO RECUO PARA CIDADE (EM ANDAMENTO) ──
        if getattr(self, "_sit_retreat_state", None) and px is not None and w:
            mode = self._sit_retreat_state.get("mode")
            c_map = (getattr(w, "map", "") or "").lower().removesuffix(".gat")
            if mode == "city":
                stage = self._sit_retreat_state.get("stage")
                tgt_city = (self._sit_retreat_state.get("city_map", "") or "").lower().removesuffix(".gat")
                if stage == "walking_to_portal":
                    if c_map == tgt_city:
                        # Atravessou o portal e entrou na cidade!
                        self._sit_retreat_state["stage"] = "stepping_away"
                        gat_city = self.feed.terrain.for_map(c_map) if getattr(self.feed, "terrain", None) else None
                        step_target = None
                        if gat_city:
                            for r in (5, 4, 6):
                                for dx, dy in ((r, 0), (0, r), (-r, 0), (0, -r), (r, r), (-r, -r)):
                                    cand = (int(round(px)) + dx, int(round(py)) + dy)
                                    if gat_city.is_walkable(*cand):
                                        step_target = cand
                                        break
                                if step_target:
                                    break
                        if not step_target:
                            step_target = (int(round(px)) + 4, int(round(py)) + 4)
                        self._sit_retreat_state["step_target"] = step_target
                        self.patrol_target_route = (c_map, step_target[0], step_target[1], None)
                        self.auto_patrol = True
                        self._reset_patrol(px, py)
                        self._try_auto_patrol(snapshot, px, py)
                        return
                    else:
                        self._try_auto_patrol(snapshot, px, py)
                        return
                elif stage == "stepping_away":
                    tgt = self._sit_retreat_state.get("step_target", (px, py))
                    if math.hypot(px - tgt[0], py - tgt[1]) <= 2.0:
                        self._click_debug("AUTO-SENTAR: Na cidade a 4-5 passos do portal, sentando via VK_INSERT")
                        vp_obj = getattr(self.feed, "viewport", None)
                        hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
                        self._send_vk_key(0x2D, hwnd)
                        self.is_sitting = True
                        self.patrol_target_route = None
                        self._sit_retreat_state["stage"] = "sitting"
                        return
                    else:
                        self._try_auto_patrol(snapshot, px, py)
                        return
            elif mode == "returning":
                orig_map = (self._sit_retreat_state.get("origin_map", "") or "").lower().removesuffix(".gat")
                if c_map == orig_map:
                    self._click_debug(f"AUTO-SENTAR: Retorno a {orig_map} concluído!")
                    self._sit_retreat_state = None
                    self.patrol_target_route = None
                else:
                    self._try_auto_patrol(snapshot, px, py)
                    return

        # ── 3. FUGA E AUTO-SENTAR (< flee_pct HP ou <30% HP) ──
        flee_trigger = self.fleeing_mode or (getattr(self, "auto_sit", False) and hp_ratio < 0.30)
        if flee_trigger and px is not None and w:
            c_map = (getattr(w, "map", "") or "").lower().removesuffix(".gat")
            gat = self.feed.terrain.for_map(c_map) if getattr(self.feed, "terrain", None) else None

            # Prioridade 1 — Recuo para Cidade Adjacente (Portal <= 32 células)
            adj_city = get_adjacent_city_portal(c_map, (px, py), max_dist=32.0, gat=gat)
            if adj_city is not None:
                city_name, (wx, wy) = adj_city
                ret_portal = None
                for (m_f, m_t), (r_x, r_y) in MAP_PORTALS.items():
                    if m_f.lower().removesuffix(".gat") == city_name.lower().removesuffix(".gat") and m_t.lower().removesuffix(".gat") == c_map:
                        ret_portal = (r_x, r_y)
                        break
                self._sit_retreat_state = {
                    "mode": "city",
                    "origin_map": c_map,
                    "city_map": city_name,
                    "portal_pos": (wx, wy),
                    "return_portal": ret_portal,
                    "stage": "walking_to_portal",
                }
                self._click_debug(f"FUGA: HP {hp_ratio:.0%} < {flee_pct:.0%} – Recuando para cidade adjacente {city_name} via portal ({wx},{wy})")
                self.patrol_target_route = (c_map, wx, wy, "warp")
                self.auto_patrol = True
                self._reset_patrol(px, py)
                self._try_auto_patrol(snapshot, px, py)
                return

            # Prioridade 2 — Fuja via find_safe_path contornando monstros vivos até > 9 células de hostis para sentar
            danger_cells = []
            hostile_positions = []
            for item in snapshot.objects:
                if item.kind == "mob":
                    pos_m = self.feed.world.get_entity_cell("mob", item.key) if hasattr(self.feed, "world") else None
                    if pos_m:
                        danger_cells.append(pos_m)
                        if self._is_mob_hostile(item.key, item.catalog_id):
                            hostile_positions.append(pos_m)

            close_hostiles = [h for h in hostile_positions if math.hypot(px - h[0], py - h[1]) <= 9.0]
            if not close_hostiles:
                # Já está a > 9 células de todos os hostis: senta via VK_INSERT
                self._click_debug("FUGA: Seguro (> 9 cels de hostis), sentando via VK_INSERT")
                vp_obj = getattr(self.feed, "viewport", None)
                hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
                self._send_vk_key(0x2D, hwnd)
                self.is_sitting = True
                self.patrol_target_route = None
                self._sit_retreat_state = {"mode": "field", "stage": "sitting"}
                return
            else:
                safe_cell = None
                if gat:
                    best_score = -999.0
                    for r_dist in (12, 14, 16, 10, 18, 20):
                        for ang_i in range(16):
                            ang = ang_i * (2 * math.pi / 16)
                            cand_x = int(round(px + r_dist * math.cos(ang)))
                            cand_y = int(round(py + r_dist * math.sin(ang)))
                            if not (0 <= cand_x < gat.width and 0 <= cand_y < gat.height):
                                continue
                            if not gat.is_walkable(cand_x, cand_y):
                                continue
                            min_h = min(math.hypot(cand_x - hx, cand_y - hy) for hx, hy in hostile_positions)
                            if min_h > 9.0:
                                path = gat.find_safe_path(
                                    (int(round(px)), int(round(py))),
                                    (cand_x, cand_y),
                                    danger_cells=danger_cells,
                                    danger_radius=4,
                                    danger_penalty=85,
                                    blocked_cells=self.temp_blocked_cells
                                )
                                if path:
                                    if min_h > best_score:
                                        best_score = min_h
                                        safe_cell = (cand_x, cand_y)
                if safe_cell:
                    self._click_debug(f"FUGA: HP {hp_ratio:.0%}, {len(close_hostiles)} hostil(is) <= 9 cels – rota segura via find_safe_path para {safe_cell}")
                    self.patrol_target_route = (c_map, safe_cell[0], safe_cell[1], None)
                    self.auto_patrol = True
                    self._reset_patrol(px, py)
                    self._try_auto_patrol(snapshot, px, py)
                    return
                else:
                    self._try_auto_patrol(snapshot, px, py)
                    return

        if self.fleeing_mode:
            return

        farm_busy = False
        loot_busy = False

        # ── PRIORIDADE 1: Autodefesa – mob agressor ou <= 3.0 células ──
        if (self.auto_farm or self.auto_loot) and px is not None:
            has_threat = False
            farm_target_available = False
            for item in snapshot.objects:
                if item.kind == "mob":
                    try:
                        gid = int(item.key.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    if self._farm_blacklist.get(gid, 0.0) > now:
                        continue
                    dist = self._entity_distance("mob", item.key, px, py)
                    is_aggro = False
                    if hasattr(self.feed, "world"):
                        ent = self.feed.world.entities.get(("mob", gid))
                        if ent and getattr(ent, "is_aggro", False) and (now - ent.last_aggro_ts) < 6.0:
                            is_aggro = True
                    if dist <= 3.0 or is_aggro:
                        has_threat = True
                    if dist <= 22.0:
                        farm_target_available = True

            if has_threat:
                self._reset_patrol(px, py)
                self._try_auto_farm(snapshot, px, py, max_search_dist=3.0)
                return

        # ── PRIORIDADE TIER 1 LOOT (Interrompe farm, prioridade máxima, coleta independente de peso) ──
        if (self.auto_loot or self.auto_farm) and px is not None:
            has_tier1_drop = False
            for item in snapshot.objects:
                if item.kind == "drop" and item.ownership in ("MINE", "FREE"):
                    try:
                        gid = int(item.key.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    if self._drop_blacklist.get(gid, 0.0) > now:
                        continue
                    d_name = self.names.get("drop", {}).get(item.catalog_id, "")
                    if get_item_tier(item.catalog_id, d_name) == 1:
                        d = self._entity_distance("drop", item.key, px, py)
                        if d <= 22.0:
                            has_tier1_drop = True
                            break
            if has_tier1_drop:
                self._click_debug("AUTO-LOOT: Item Tier 1 detectado! Interrompendo farm para coletar com prioridade máxima!")
                self._reset_patrol(px, py)
                self._try_auto_loot(snapshot, px, py)
                return

        # ── PRIORIDADE 2: Auto-Loot antes de farm distante ──
        if self.auto_loot and px is not None:
            has_valid_drop = False
            w_ratio = getattr(player, "weight_ratio", 0.0) if player else 0.0
            for item in snapshot.objects:
                if item.kind == "drop" and item.ownership in ("MINE", "FREE"):
                    try:
                        gid = int(item.key.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    if self._drop_blacklist.get(gid, 0.0) > now:
                        continue
                    d_cid = item.catalog_id
                    d_name = self.names.get("drop", {}).get(d_cid, "").strip().lower()
                    tier = get_item_tier(d_cid, d_name)
                    if tier == 3 and (self._ignore_tier3_always.get() or w_ratio >= 0.60):
                        continue
                    d_mode = getattr(self, "_drop_filter_mode", "Todos")
                    if d_mode == "Whitelist":
                        if d_cid not in getattr(self, "drop_whitelist", set()) and not any(w in d_name for w in getattr(self, "drop_whitelist_names", set())):
                            continue
                    elif d_mode == "Blacklist":
                        if d_cid in getattr(self, "drop_blacklist", set()) or any(w in d_name for w in getattr(self, "drop_blacklist_names", set())):
                            continue
                    dist = self._entity_distance("drop", item.key, px, py)
                    if dist <= 22.0:
                        has_valid_drop = True
                        break

            if has_valid_drop or self.locked_drop_gid is not None:
                self._reset_patrol(px, py)
                self._try_auto_loot(snapshot, px, py)
                return

        # ── PRIORIDADE 3: Auto-Farm monstro mais próximo ──
        if self.auto_farm and px is not None:
            farm_target_available_local = False
            for item in snapshot.objects:
                if item.kind == "mob":
                    try:
                        gid = int(item.key.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    if self._farm_blacklist.get(gid, 0.0) > now:
                        continue
                    dist = self._entity_distance("mob", item.key, px, py)
                    if dist <= 22.0:
                        farm_target_available_local = True
                        break
            if farm_target_available_local or self.locked_mob_gid is not None:
                self._reset_patrol(px, py)
                self._try_auto_farm(snapshot, px, py, max_search_dist=22.0)
                return

        # ── PRIORIDADE 5: nav_goal (direcionado) ou Auto-Patrol (aleatório) ──
        # nav_goal tem PRIORIDADE sobre auto_patrol e NUNCA chama pick_next_waypoint
        if getattr(self, "nav_goal", None) is not None and px is not None and py is not None:
            self._try_auto_patrol(snapshot, px, py)
        elif self.auto_patrol and px is not None and py is not None:
            self._try_auto_patrol(snapshot, px, py)

    def _try_auto_patrol(self, snapshot, px, py):
        """Auto-Patrol: patrulha mapa por A*, calculando sub-passos a 8-11 células e clicando via touch_postmsg."""
        if not snapshot or not hasattr(self.feed, "world"):
            return

        # Pausa se qualquer botão físico do mouse estiver pressionado
        if any((self.user32.GetAsyncKeyState(k) & 0x8000) != 0 for k in (2, 4)) or self._is_physical_mouse_down():
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
                if not self._is_point_in_game_ui(sx_ng, sy_ng):
                    self._send_click(target_hwnd, sx_ng, sy_ng)
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
                if dist_ng <= 2.0:
                    self._click_debug(f"NAV_GOAL: Destino alcançado ({ng_x},{ng_y}) — encerrando.")
                    self.nav_goal = None
                    self._reset_patrol(px, py)
                    return
                # Usa apenas find_path, nunca pick_next_waypoint
                ng_path = gat.find_path((int(round(px)), int(round(py))), (ng_x, ng_y),
                                        blocked_cells=self.temp_blocked_cells)
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

        # ── Suporte a Rotas de Destino / Interação com NPC e Portal ──
        dialog = getattr(getattr(self.feed, "world", None), "npc_dialog", {})
        if dialog.get("open"):
            if getattr(self, "patrol_target_route", None) or getattr(self, "nav_goal", None) or getattr(self, "_npc_interact_state", None):
                self.patrol_target_route = None
                self.nav_goal = None
                self._npc_interact_state = None
                self._click_debug("NPC DIALOG ABERTO: Rota/navegação de interação concluída.")

        if self.patrol_target_route:
            r_map, r_x, r_y, r_target = self.patrol_target_route
            norm_r_map = r_map.lower().removesuffix(".gat").removesuffix(".rsw")
            if curr_map == norm_r_map:
                dist_to_route = math.hypot(px - r_x, py - r_y)
                # Nunca clica a > 3.8 células: aproxima até ficar a <= 3.5 células
                if r_target:
                    if dist_to_route <= 3.5:
                        left, top, width, height = snapshot.viewport
                        st_interact = getattr(self, "_npc_interact_state", None) or {"clicks": 0, "last_click": 0.0}
                        # Máximo de 2 tentativas
                        if st_interact["clicks"] >= 2:
                            self._click_debug(f"INTERAGIR: 2 tentativas sem abrir diálogo para alvo {r_target}. Encerrando navegação.")
                            self.patrol_target_route = None
                            self.nav_goal = None
                            self._npc_interact_state = None
                            self._reset_patrol(px, py)
                            return
                        # Intervalo mínimo de 950ms
                        if now - st_interact["last_click"] >= 0.950:
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
                                        st_interact["clicks"] += 1
                                        st_interact["last_click"] = now
                                        self._npc_interact_state = st_interact
                                        self._click_debug(f"INTERAGIR: clique {st_interact['clicks']}/2 no alvo {item.key} em ({sx},{sy})")
                                        self._send_click(target_hwnd, sx, sy)
                                        if st_interact["clicks"] >= 2:
                                            self.patrol_target_route = None
                                            self.nav_goal = None
                                            self._npc_interact_state = None
                                            self._reset_patrol(px, py)
                                        return
                else:
                    if dist_to_route <= 2.5:
                        self._click_debug(f"PATROL: Destino da rota alcançado ({r_x}, {r_y})")
                        self.patrol_target_route = None
                        self.nav_goal = None
                        self._reset_patrol(px, py)

        # ── Detecção de Falha de Movimento (>= 950ms sem mover e < 0.5 células) ──
        if getattr(self, "last_patrol_click", None):
            lp_time, lp_px, lp_py, lp_gx, lp_gy = self.last_patrol_click
            p_obj = getattr(getattr(self.feed, "world", None), "player", None)
            p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
            dist_moved = math.hypot(px - lp_px, py - lp_py) if px is not None else 0.0
            if p_moving or dist_moved >= 0.8:
                self.last_patrol_click = None
            elif (now - lp_time >= 0.950) and dist_moved < 0.5 and not p_moving:
                self._click_debug(f"PATROL PRESO: não moveu após >=950ms em ({lp_gx},{lp_gy}). Bloqueando raio=2 por 25s e fazendo varredura radial.")
                # 1. Bloqueia (lp_gx, lp_gy) e raio de 2 células por 25s
                for ddx in range(-2, 3):
                    for ddy in range(-2, 3):
                        if math.hypot(ddx, ddy) <= 2.2:
                            self.temp_blocked_cells[(lp_gx + ddx, lp_gy + ddy)] = now + 25.0

                # 2. Varredura radial imediata ao redor do jogador (raio = 3, 4, 5, 6, 16 ângulos)
                blocked_angle = math.atan2(lp_gy - py, lp_gx - px) if (px is not None and py is not None) else 0.0
                escape_found = False
                if gat and px is not None and py is not None:
                    for r_esc in (3, 4, 5, 6):
                        if escape_found:
                            break
                        for k in range(16):
                            ang = k * (2.0 * math.pi / 16.0)
                            diff_ang = abs((ang - blocked_angle + math.pi) % (2.0 * math.pi) - math.pi)
                            if diff_ang < math.pi / 3.0:  # menos de 60 graus da direção bloqueada
                                continue
                            cx = int(round(px + r_esc * math.cos(ang)))
                            cy = int(round(py + r_esc * math.sin(ang)))
                            if (cx, cy) in self.temp_blocked_cells:
                                continue
                            if not (0 <= cx < gat.width and 0 <= cy < gat.height):
                                continue
                            if not gat.is_walkable(cx, cy):
                                continue
                            w_nb = sum(1 for ddx2 in (-1, 0, 1) for ddy2 in (-1, 0, 1)
                                       if (ddx2 != 0 or ddy2 != 0) and gat.is_walkable(cx + ddx2, cy + ddy2))
                            if w_nb < 6:
                                continue
                            u_esc, v_esc = project(cx, cy)
                            if 0.15 <= u_esc <= 0.85 and 0.15 <= v_esc <= 0.85:
                                esc_sx = left + int(u_esc * width)
                                esc_sy = top + int(v_esc * height)
                                if not self._is_point_in_game_ui(esc_sx, esc_sy):
                                    self._click_debug(f"PATROL DESTRAVE: Varredura radial encontrou ({cx},{cy}) -> tela ({esc_sx},{esc_sy})")
                                    self._send_click(target_hwnd, esc_sx, esc_sy)
                                    self.last_patrol_click = (now, px, py, cx, cy)
                                    self.last_patrol_click_time = now
                                    self.last_patrol_click_player_pos = (px, py)
                                    self.current_patrol_path = []
                                    self.current_waypoint = None
                                    escape_found = True
                                    return
                self.current_patrol_path = []
                self.current_waypoint = None
                self.last_patrol_click = None
                return

        # ── Watchdog de Destravamento (>= 900ms sem mover e < 0.5 células) ──
        if getattr(self, "_walk_watchdog", None):
            p_obj = getattr(getattr(self.feed, "world", None), "player", None)
            p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
            w_px = self._walk_watchdog.get("px", px)
            w_py = self._walk_watchdog.get("py", py)
            dist_from_start = math.hypot(px - w_px, py - w_py) if (px is not None and w_px is not None) else 0.0
            last_pkt = getattr(getattr(self.feed, "world", None), "last_player_move_pkt_time", 0.0)

            # Nunca considera travamento se estiver em movimento ou já andou >= 0.8 células
            if p_moving or dist_from_start >= 0.8 or last_pkt > self._walk_watchdog["time"]:
                self._walk_watchdog = None
            elif (now - self._walk_watchdog["time"] >= 0.900) and dist_from_start < 0.5 and not p_moving:
                w_sx = self._walk_watchdog["sx"]
                w_sy = self._walk_watchdog["sy"]
                stuck_cell = self._walk_watchdog["cell"]
                self._walk_watchdog = None
                self._click_debug(f"WATCHDOG: Travamento em {stuck_cell} (>=900ms sem mover). Desvio suave do cursor e bloqueio por 15s.")
                self._natural_dodge_cursor(w_sx, w_sy)
                self.temp_blocked_cells[stuck_cell] = now + 15.0
                alt_cell = gat.find_safe_alternative_step(stuck_cell, (px, py), blocked_cells=self.temp_blocked_cells, min_neighbors=7)
                if alt_cell:
                    self._click_debug(f"WATCHDOG: Sub-passo alternativo livre no .gat: {alt_cell}")
                    u_alt, v_alt = project(*alt_cell)
                    if 0.05 <= u_alt <= 0.95 and 0.05 <= v_alt <= 0.95:
                        alt_sx = left + int(u_alt * width)
                        alt_sy = top + int(v_alt * height)
                        if math.hypot(alt_cell[0] - px, alt_cell[1] - py) >= 2.2:
                            self._send_click(target_hwnd, alt_sx, alt_sy)
                            self._walk_watchdog = {"time": now, "cell": alt_cell, "sx": alt_sx, "sy": alt_sy, "px": px, "py": py}
                            return
                self.current_patrol_path = []
                self.patrol_path = []
                self.current_waypoint = None
                return

        # Enquanto estiver em deslocamento ativo, aguarda sem disparar cliques no meio do trajeto
        p_obj = getattr(getattr(self.feed, "world", None), "player", None)
        p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
        if p_moving and getattr(self, "last_patrol_click", None) and (px is not None and py is not None):
            lp_time, _, _, lp_gx, lp_gy = self.last_patrol_click
            last_ts = getattr(self, "last_patrol_click_time", lp_time)
            if math.hypot(px - lp_gx, py - lp_gy) > 2.5 and (now - last_ts < 1.6):
                return

        # ── Detecção de Travamento (> 1.8s parado mesmo após clicar) ──
        if self.last_patrol_click_time > 0 and (now - self.last_patrol_click_time > 1.8):
            if self.last_patrol_click_player_pos:
                dist_moved = math.hypot(px - self.last_patrol_click_player_pos[0], py - self.last_patrol_click_player_pos[1])
                if not p_moving and dist_moved < 0.5:
                    stuck_cell = self.current_patrol_path[0] if self.current_patrol_path else cur_cell
                    self.temp_blocked_cells[stuck_cell] = now + 12.0
                    self._click_debug(f"PATROL: Travamento detectado (>1.8s sem mover)! Bloqueando {stuck_cell} e recalculando rota.")
                    self.current_patrol_path = []
                    self.patrol_path = []
                    self.stuck_count += 1
                    if self.stuck_count >= 2:
                        self.current_waypoint = None
                        self.stuck_count = 0
                    self.last_patrol_click_time = now
                    return

        # ── Planejamento de Waypoint e Rota A* ──
        need_new_waypoint = False
        if not self.current_waypoint:
            need_new_waypoint = True
        elif math.hypot(px - self.current_waypoint[0], py - self.current_waypoint[1]) <= 2.0:
            need_new_waypoint = True

        if need_new_waypoint:
            if self.patrol_target_route and curr_map == self.patrol_target_route[0].lower().removesuffix(".gat"):
                next_wp = (int(self.patrol_target_route[1]), int(self.patrol_target_route[2]))
            elif getattr(self, "nav_goal", None) is not None:
                # nav_goal ativo: nunca chama pick_next_waypoint, usa o destino direto
                ng_map, ng_x, ng_y = self.nav_goal
                next_wp = (ng_x, ng_y)
                self.active_nav_color = "#A78BFA"  # Roxo = nav_goal
            else:
                hmap = self.feed.world.get_heatmap_sectors() if (hasattr(self.feed, "world") and hasattr(self.feed.world, "get_heatmap_sectors")) else None
                next_wp = gat.pick_next_waypoint(
                    (px, py), visited_cells=self.visited_cells,
                    min_dist=12, max_dist=28, blocked_cells=self.temp_blocked_cells,
                    heatmap=hmap
                )
                self.active_nav_color = "#38BDF8"  # Ciano = Auto-Patrol
            if next_wp:
                self.current_waypoint = next_wp
                self.current_patrol_path = gat.find_path(
                    (px, py), next_wp, blocked_cells=self.temp_blocked_cells
                )
                self.patrol_path = self.current_patrol_path
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
                    (px, py), self.current_waypoint, blocked_cells=self.temp_blocked_cells
                )
                self.patrol_path = self.current_patrol_path
                if self.current_patrol_path:
                    self.active_nav_path = list(self.current_patrol_path)
                    self.active_nav_color = "#38BDF8"
            if not self.current_patrol_path:
                self.current_waypoint = None
                return

        # Descarta células da rota já ultrapassadas (<= 1.5 células ou direção oposta ao patrol_goal)
        patrol_goal = self.current_waypoint
        while len(self.current_patrol_path) > 1:
            node = self.current_patrol_path[0]
            d_node = math.hypot(px - node[0], py - node[1])
            if d_node <= 1.5:
                self.current_patrol_path.pop(0)
            elif patrol_goal is not None and d_node <= 3.5:
                vgx = patrol_goal[0] - px
                vgy = patrol_goal[1] - py
                vnx = node[0] - px
                vny = node[1] - py
                if (vnx * vgx + vny * vgy) < 0:
                    self.current_patrol_path.pop(0)
                else:
                    break
            else:
                break
        self.patrol_path = self.current_patrol_path

        if not self.current_patrol_path:
            self.current_waypoint = None
            return

        # ── Projeta sub-passo a 6–9 células à frente na área segura da tela ──
        sub_step_cell = None
        sub_step_u = None
        sub_step_v = None

        cand_list = []
        for cand in self.current_patrol_path:
            d_cand = math.hypot(cand[0] - px, cand[1] - py)
            if 6.0 <= d_cand <= 9.0:
                cand_list.append((d_cand, cand))

        cand_list.sort(key=lambda x: x[0], reverse=True)
        for _, cand in cand_list:
            u, v = project(*cand)
            if 0.15 <= u <= 0.85 and 0.15 <= v <= 0.85:
                gx_c, gy_c = cand
                walkable_neighbors = (
                    gat.count_walkable_neighbors(gx_c, gy_c)
                    if hasattr(gat, "count_walkable_neighbors")
                    else sum(1 for ddx in (-1, 0, 1) for ddy in (-1, 0, 1) if (ddx != 0 or ddy != 0) and gat.is_walkable(gx_c + ddx, gy_c + ddy))
                )
                if walkable_neighbors >= 6:
                    sub_step_cell = cand
                    sub_step_u = u
                    sub_step_v = v
                    break

        # Fallback para ponto visível mais distante na área segura (mínimo 2.2 células)
        if sub_step_cell is None:
            max_idx = min(len(self.current_patrol_path) - 1, 11)
            for idx in range(max_idx, -1, -1):
                cand = self.current_patrol_path[idx]
                if math.hypot(cand[0] - px, cand[1] - py) < 2.2:
                    continue
                u, v = project(*cand)
                if 0.15 <= u <= 0.85 and 0.15 <= v <= 0.85:
                    gx_c, gy_c = cand
                    walkable_neighbors = (
                        gat.count_walkable_neighbors(gx_c, gy_c)
                        if hasattr(gat, "count_walkable_neighbors")
                        else sum(1 for ddx in (-1, 0, 1) for ddy in (-1, 0, 1) if (ddx != 0 or ddy != 0) and gat.is_walkable(gx_c + ddx, gy_c + ddy))
                    )
                    if walkable_neighbors >= 5:
                        sub_step_cell = cand
                        sub_step_u = u
                        sub_step_v = v
                        break

        if sub_step_cell is None:
            for cand in reversed(self.current_patrol_path):
                if math.hypot(cand[0] - px, cand[1] - py) < 2.0:
                    continue
                u, v = project(*cand)
                if 0.05 <= u <= 0.95 and 0.05 <= v <= 0.95:
                    sub_step_cell = cand
                    sub_step_u = u
                    sub_step_v = v
                    break

        if sub_step_cell is None:
            return

        # Zona de exclusão: nunca clica se NPC, portal, player ou loja estiver a <= 2.5 células
        gx, gy = sub_step_cell
        skip_cell = False
        if hasattr(self.feed, "world"):
            w = self.feed.world
            with w.lock:
                for (ekind, _), ent in w.entities.items():
                    if ekind in ("npc", "warp"):
                        ec = ent.track.target
                        if max(abs(ec[0] - gx), abs(ec[1] - gy)) <= 2.5:
                            skip_cell = True
                            break
                if not skip_cell:
                    for _pid, (pc, _) in w.other_players.items():
                        if max(abs(pc[0] - gx), abs(pc[1] - gy)) <= 2.5:
                            skip_cell = True
                            break
                if not skip_cell:
                    for vaid in w.vending_aids:
                        ent = w.entities.get(("npc", vaid))
                        if ent:
                            ec = ent.track.target
                            if max(abs(ec[0] - gx), abs(ec[1] - gy)) <= 2.5:
                                skip_cell = True
                                break
        if skip_cell:
            self._click_debug(f"PATROL: célula {sub_step_cell} bloqueada por entidade próxima")
            return

        # Verifica distância em px de entidades na tela (55px)
        sx_cand = left + int(sub_step_u * width)
        sy_cand = top + int(sub_step_v * height)
        for item in snapshot.objects:
            if item.kind in ("npc", "warp"):
                ix = left + int(item.u * width)
                iy = top + int(item.v * height)
                if math.hypot(sx_cand - ix, sy_cand - iy) < 55:
                    self._click_debug(f"PATROL: célula {sub_step_cell} a < 55px de entidade na tela")
                    return

        # Cadência de cliques: não clica no meio do caminho enquanto o jogador estiver andando
        p_obj = getattr(getattr(self.feed, "world", None), "player", None)
        p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
        if p_moving and getattr(self, "last_patrol_click", None):
            lp_time, _, _, lp_gx, lp_gy = self.last_patrol_click
            last_ts = getattr(self, "last_patrol_click_time", lp_time)
            if math.hypot(px - lp_gx, py - lp_gy) > 2.5 and (now - last_ts < 1.6):
                return
        if p_moving and (now - self.last_patrol_click_time < 0.85):
            return

        dist_player = math.hypot(sub_step_cell[0] - px, sub_step_cell[1] - py)
        if dist_player < 2.2:
            self._click_debug(f"PATROL: clique descartado por zona de exclusão nos pés ({dist_player:.2f} < 2.2)")
            return

        sx = left + int(sub_step_u * width)
        sy = top + int(sub_step_v * height)

        if self._is_point_in_game_ui(sx, sy):
            self._click_debug(f"PATROL: clique descartado por cair dentro da UI do jogo ({sx},{sy})")
            return

        self._click_debug(f"PATROL: Passo em ({sub_step_cell[0]}, {sub_step_cell[1]}) -> tela ({sx}, {sy})")
        self._send_click(target_hwnd, sx, sy)
        self.last_patrol_click_time = now
        self.last_patrol_click_player_pos = (px, py)
        self.last_patrol_click = (now, px, py, sub_step_cell[0], sub_step_cell[1])
        self._walk_watchdog = {
            "time": now,
            "cell": sub_step_cell,
            "sx": sx,
            "sy": sy,
            "px": px,
            "py": py,
        }

    def _game_is_foreground(self):
        """Verifica se o Ragexe.exe ou o processo do overlay está em primeiro plano."""
        fg = self.user32.GetForegroundWindow()
        if not fg:
            return False
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if not pid.value:
            return False
        if pid.value in (self._own_pid, self._ragexe_pid):
            return True
        try:
            if psutil.Process(pid.value).name().casefold() == "ragexe.exe":
                self._ragexe_pid = pid.value
                return True
        except psutil.Error:
            pass
        return False

    def _is_hud_foreground(self):
        """Retorna True se o painel HUD estiver em primeiro plano (usuário interagindo com ele)."""
        hud = getattr(self, "_hud_panel", None)
        if not hud or not hud.winfo_exists():
            return False
        try:
            fg = self.user32.GetForegroundWindow()
            if not fg:
                return False
            # Obtém HWND raiz do HUD tkinter
            hud_hwnd = hud.winfo_id()
            root_hud = u32.GetAncestor(hud_hwnd, 2)  # GA_ROOT
            fg_int = int(fg)
            if root_hud and fg_int == int(root_hud):
                return True
            # Também checa o hwnd direto do panel
            if fg_int == int(hud_hwnd):
                return True
        except Exception:
            pass
        return False

    def _is_point_in_hud(self, sx, sy, margin=10):
        """Verifica se (sx, sy) cai dentro do retângulo da janela do HUD (com margem de segurança)."""
        hud = getattr(self, "_hud_panel", None)
        if hud and hud.winfo_exists() and hud.winfo_viewable():
            try:
                hx = hud.winfo_rootx() - margin
                hy = hud.winfo_rooty() - margin
                hw = hud.winfo_width() + (margin * 2)
                hh = hud.winfo_height() + (margin * 2)
                if hx <= sx <= hx + hw and hy <= sy <= hy + hh:
                    return True
            except Exception:
                pass
        return False

    def _is_point_in_game_ui(self, sx: int, sy: int) -> bool:
        """Verifica se (sx, sy) cai dentro das zonas nativas de UI do Ragnarok Online ou HUD."""
        # Janela do Ragnalok HUD com margem de 12px
        if self._is_point_in_hud(sx, sy, margin=12):
            return True

        # Obtém área cliente atual do jogo (vx, vy, vw, vh)
        snap = self.feed.current() if hasattr(self, "feed") else None
        if snap and hasattr(snap, "viewport") and snap.viewport:
            vx, vy, vw, vh = snap.viewport
        else:
            vp = getattr(self, "current_viewport", None)
            if vp and len(vp) == 4 and vp[2] > 0 and vp[3] > 0:
                vx, vy, vw, vh = vp
            else:
                return False

        # Caixa de Chat (canto inferior esquerdo)
        if sx <= vx + 470 and sy >= vy + vh - 235:
            return True
        # Janela de Status/Skills (canto superior esquerdo)
        if sx <= vx + 435 and sy <= vy + 165:
            return True
        # Minimapa e Lista de Quests (canto superior direito)
        if sx >= vx + vw - 265 and sy <= vy + 340:
            return True

        return False

    def _side_step_to_reveal(self, target_cell, px, py):
        """Desloca o personagem para uma célula segura na direção do alvo ou lateralmente para tirar o alvo de trás da UI/HUD."""
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
            dir_x, dir_y = 1.0, 0.0
            perp_dx, perp_dy = 0.0, 1.0
        else:
            dir_x, dir_y = dx / dist, dy / dist
            perp_dx, perp_dy = -dy / dist, dx / dist

        candidates = []
        # 1. Tenta passos na direção do alvo (mais próximos do centro da tela) para deslocar a câmera
        for step_r in (2.0, 2.5, 3.0, 3.5):
            sx_cell = int(round(px + dir_x * step_r))
            sy_cell = int(round(py + dir_y * step_r))
            if not (0 <= sx_cell < gat.width and 0 <= sy_cell < gat.height):
                continue
            if not gat.is_walkable(sx_cell, sy_cell):
                continue
            if (sx_cell, sy_cell) in getattr(self, "temp_blocked_cells", {}):
                continue
            u_cand, v_cand = project(sx_cell, sy_cell)
            if 0.18 <= u_cand <= 0.82 and 0.18 <= v_cand <= 0.82:
                screen_x = left + int(u_cand * width)
                screen_y = top + int(v_cand * height)
                if not self._is_point_in_game_ui(screen_x, screen_y):
                    candidates.append((sx_cell, sy_cell, screen_x, screen_y))
                    break

        # 2. Tenta passos laterais perpendiculares
        if not candidates:
            for sign in (1, -1):
                for step_r in (2.3, 2.8, 3.2):
                    sx_cell = int(round(px + perp_dx * step_r * sign))
                    sy_cell = int(round(py + perp_dy * step_r * sign))
                    if not (0 <= sx_cell < gat.width and 0 <= sy_cell < gat.height):
                        continue
                    if not gat.is_walkable(sx_cell, sy_cell):
                        continue
                    if (sx_cell, sy_cell) in getattr(self, "temp_blocked_cells", {}):
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

        if candidates:
            sx_c, sy_c, sc_x, sc_y = candidates[0]
            self._click_debug(f"PASSO SEGURO: desviando char para ({sx_c},{sy_c}) para tirar alvo de trás da UI/Chat")
            self._send_click(target_hwnd, sc_x, sc_y)
            return True
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
        if px is not None and py is not None:
            cur = (int(round(px)), int(round(py)))
            if not self.visited_cells or self.visited_cells[-1] != cur:
                self.visited_cells.append(cur)
                if len(self.visited_cells) > 80:
                    self.visited_cells.pop(0)

    def _on_pickup_ack(self, item_id, cnt, fail):
        """Processa pacote 0x00A0 ZC_ITEM_PICKUP_ACK para detecção de posse e histórico."""
        item_name = self.names.get("drop", {}).get(item_id, f"Item #{item_id}")
        hora = time.strftime("%H:%M:%S")
        is_fail = (fail != 0)
        self._picked_items_history.insert(0, (hora, item_name, max(1, cnt), is_fail))
        if len(self._picked_items_history) > 40:
            self._picked_items_history.pop()
        if not is_fail:
            # Coleta bem-sucedida: zera contador de tentativas (item em pilha não bloqueia)
            self.locked_drop_clicks = 0
            self._loot_history.insert(0, (hora, item_name, max(1, cnt), item_id))
            if len(self._loot_history) > 50:
                self._loot_history.pop()
        if is_fail:
            target_gid = self.locked_drop_gid
            self._click_debug(f"LOOT FALHOU (0x00A0 fail={fail}): item #{item_id} (gid={target_gid}) bloqueado por 8s")
            if target_gid is not None:
                self._drop_blacklist[target_gid] = time.monotonic() + 8.0
                self._loot_retry_state.pop(target_gid, None)
                if hasattr(self.feed.world, "mark_drop_locked"):
                    self.feed.world.mark_drop_locked(target_gid)
            elif hasattr(self.feed.world, "mark_nearest_drop_locked"):
                self.feed.world.mark_nearest_drop_locked()
            self._reset_patrol(None, None)
            self.locked_drop_gid = None
            self.locked_drop_clicks = 0
            self.locked_drop_first_time = 0.0

    def _click_debug(self, msg):
        """Imprime log de diagnóstico no terminal, limitado a 1 linha por segundo."""
        now = time.monotonic()
        if now - self._last_debug_log >= 1.0:
            print(f"[ClickDebug] {msg}", flush=True)
            self._last_debug_log = now

    def _try_auto_loot(self, snapshot, px=None, py=None):
        """Auto-Loot com A* em tela inteira, Target Lock, detecção de posse e retry em 3 etapas."""
        if not snapshot or not hasattr(self.feed, "world"):
            return

        # Pausa se qualquer botão físico do mouse estiver pressionado (1=L, 2=R, 4=M)
        if any((self.user32.GetAsyncKeyState(k) & 0x8000) != 0 for k in (2, 4)) or self._is_physical_mouse_down():
            self._click_debug("LOOT BLOQUEADO: botão físico do mouse pressionado")
            return

        # Atua se o jogo ou o overlay estiver em foco
        vp_obj = getattr(self.feed, "viewport", None)
        target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        target_hwnd = self.get_game_hwnd(target_hwnd)
        if not self._game_is_foreground():
            self._click_debug(f"LOOT BLOQUEADO: foco não está no jogo/overlay (game_hwnd={target_hwnd})")
            return

        now = time.monotonic()
        _, player, _ = self.feed.world.snapshot()
        if px is None or py is None:
            px, py = player.cell(now) if player else (None, None)
        left, top, width, height = snapshot.viewport

        curr_map = getattr(getattr(self.feed, "world", None), "map", None)
        gat = None
        if curr_map and hasattr(self.feed, "terrain") and self.feed.terrain:
            try:
                gat = self.feed.terrain.for_map(curr_map)
            except Exception:
                pass

        # 1. Se já temos um drop travado, verifica se ainda é válido no snapshot e no raio <= 22 células
        target_item = None
        if self.locked_drop_gid is not None:
            if self._drop_blacklist.get(self.locked_drop_gid, 0.0) > now:
                self._loot_retry_state.pop(self.locked_drop_gid, None)
                self.locked_drop_gid = None
                self.locked_drop_clicks = 0
                self.locked_drop_first_time = 0.0
            else:
                for item in snapshot.objects:
                    if item.kind == "drop" and item.key == f"drop:{self.locked_drop_gid}":
                        if item.ownership in ("MINE", "FREE"):
                            dist = self._entity_distance("drop", item.key, px, py)
                            if px is None or dist <= 22.0:
                                target_item = item
                        break
                # Se o item sumiu (coletado 0x00A1 ou desapareceu do snapshot)
                if target_item is None:
                    self._loot_retry_state.pop(self.locked_drop_gid, None)
                    self._reset_patrol(px, py)
                    self.locked_drop_gid = None
                    self.locked_drop_clicks = 0
                    self.locked_drop_first_time = 0.0

        # 2. Se não tem drop travado, escolhe o melhor elegível (raio <= 22 células com checagem A*)
        if self.locked_drop_gid is None:
            w_ratio = 0.0
            if hasattr(self.feed, "world") and self.feed.world and self.feed.world.player:
                w_ratio = getattr(self.feed.world.player, "weight_ratio", 0.0)

            candidates = []
            for item in snapshot.objects:
                if item.kind != "drop" or item.ownership not in ("MINE", "FREE"):
                    continue
                try:
                    gid = int(item.key.split(":", 1)[1])
                except (IndexError, ValueError):
                    continue
                if self._drop_blacklist.get(gid, 0.0) > now:
                    continue
                # Filtro Whitelist / Blacklist de itens e 3 Tiers
                d_mode = getattr(self, "_drop_filter_mode", "Todos")
                d_cid = item.catalog_id
                d_name = self.names.get("drop", {}).get(d_cid, "").strip().lower()
                d_id_str = f"#{d_cid}"

                tier = get_item_tier(d_cid, d_name)
                # Tier 3 (Lixo): ignora se peso >= 60% ou se toggle de ignorar lixo estiver ativo
                if tier == 3:
                    if (hasattr(self, "_ignore_tier3_always") and self._ignore_tier3_always.get()) or w_ratio >= 0.60:
                        continue

                if d_mode == "Whitelist":
                    # Tier 1 sempre passa na whitelist
                    if tier != 1:
                        if d_cid not in getattr(self, "drop_whitelist", set()) \
                                and d_id_str not in getattr(self, "drop_whitelist_names", set()) \
                                and not any(w in d_name for w in getattr(self, "drop_whitelist_names", set())):
                            continue
                elif d_mode == "Blacklist":
                    if d_cid in getattr(self, "drop_blacklist", set()) \
                            or d_id_str in getattr(self, "drop_blacklist_names", set()) \
                            or any(w in d_name for w in getattr(self, "drop_blacklist_names", set())):
                        continue
                dist = self._entity_distance("drop", item.key, px, py)
                if px is None or dist <= 22.0:
                    tier_rank = 0 if tier == 1 else (1 if tier == 2 else 2)
                    candidates.append((tier_rank, dist, gid, item))

            candidates.sort(key=lambda c: (c[0], c[1]))

            best_item = None
            best_gid = None
            for _tr, dist, gid, item in candidates:
                drop_cell = self.feed.world.get_entity_cell("drop", item.key)
                if gat and px is not None and drop_cell:
                    start_cell = (int(round(px)), int(round(py)))
                    path = gat.find_path(start_cell, (int(round(drop_cell[0])), int(round(drop_cell[1]))), blocked_cells=self.temp_blocked_cells)
                    if not path:
                        # Inacessível por rio/penhasco: bloqueia por 8s
                        self._drop_blacklist[gid] = now + 8.0
                        continue
                best_item = item
                best_gid = gid
                break

            if best_gid is not None and best_item is not None:
                self.locked_drop_gid = best_gid
                self.locked_drop_clicks = 0
                self.locked_drop_first_time = now
                self._loot_retry_state[best_gid] = {"attempt": 0, "last_time": 0.0}
                target_item = best_item

        # 3. Executa aproximação A* ou coleta no drop travado
        if target_item is not None and self.locked_drop_gid is not None:
            drop_cell = self.feed.world.get_entity_cell("drop", target_item.key)
            dist_drop = self._entity_distance("drop", target_item.key, px, py) if (px is not None and py is not None) else 0.0

            # Projeta sprite do drop com sprite_z = 0.5
            if drop_cell:
                u_drop, v_drop, sx_drop, sy_drop = self._project_ground_cell(snapshot, drop_cell[0], drop_cell[1], sprite_z=0.5)
            else:
                u_drop, v_drop = target_item.u, target_item.v
                sx_drop = left + int(u_drop * width)
                sy_drop = top  + int(v_drop * height)

            is_in_hud = self._is_point_in_game_ui(sx_drop, sy_drop)
            is_safe_screen = (0.05 <= u_drop <= 0.95 and 0.05 <= v_drop <= 0.95) and not is_in_hud

            p_obj = getattr(getattr(self.feed, "world", None), "player", None)
            p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
            if player and player.duration and now < player.started + min(player.duration, 2.5):
                p_moving = True

            # Se já clicou no drop e o personagem está andando até ele, aguarda sem repetir cliques
            if getattr(self, "locked_drop_clicks", 0) >= 1 and p_moving:
                self._click_debug(f"LOOT pausado: personagem andando até drop:{self.locked_drop_gid}")
                return

            # Se drop a <= 11.5 e na área segura da tela: clica diretamente no item (sprite_z = 0.5, intervalo de 850ms)
            if dist_drop <= 11.5 and is_safe_screen:
                retry = self._loot_retry_state.get(self.locked_drop_gid, {"attempt": 0, "last_time": 0.0})
                attempt = retry["attempt"]

                if attempt >= 4:
                    self._click_debug(f"LOOT: falha após {attempt} tentativas em drop #{self.locked_drop_gid}, bloqueando por 10s")
                    self._drop_blacklist[self.locked_drop_gid] = now + 10.0
                    if hasattr(self.feed.world, "mark_drop_locked"):
                        self.feed.world.mark_drop_locked(self.locked_drop_gid)
                    self._loot_retry_state.pop(self.locked_drop_gid, None)
                    self._reset_patrol(px, py)
                    self.locked_drop_gid = None
                    self.locked_drop_clicks = 0
                    self.locked_drop_first_time = 0.0
                    return

                # Intervalo de 850ms entre tentativas
                if now - getattr(self, "last_loot_click", 0.0) < 0.850:
                    return

                self._click_debug(f"LOOT clique direto (z=0.5) tentativa {attempt+1} em ({sx_drop},{sy_drop}) drop:{self.locked_drop_gid}")
                self._send_click(target_hwnd, sx_drop, sy_drop)
                self.last_loot_click = now
                self.locked_drop_clicks += 1
                self._loot_retry_state[self.locked_drop_gid] = {"attempt": attempt + 1, "last_time": now}
                return

            # Pausa se o personagem estiver andando em deslocamento ativo
            if p_moving:
                self._click_debug(f"LOOT pausado: personagem andando (drop:{self.locked_drop_gid})")
                return

            # A* para aproximação: se estiver a > 11.5 células, ou na borda, ou atrás do HUD/UI
            if dist_drop <= 3.5 and is_in_hud and drop_cell:
                if self._side_step_to_reveal(drop_cell, px, py):
                    return

            # Caminhada via A* em direção ao drop
            if gat and px is not None and drop_cell:
                start_cell = (int(round(px)), int(round(py)))
                path = gat.find_path(start_cell, (int(round(drop_cell[0])), int(round(drop_cell[1]))), blocked_cells=self.temp_blocked_cells)
                if not path:
                    self._drop_blacklist[self.locked_drop_gid] = now + 8.0
                    self._loot_retry_state.pop(self.locked_drop_gid, None)
                    self._reset_patrol(px, py)
                    self.locked_drop_gid = None
                    return

                self.active_nav_path = list(path)
                self.active_nav_color = "#FBBF24"  # Dourado = Auto-Loot

                # Seleciona sub-passo a 8–10 células de distância
                cam_pos = getattr(self.feed, "_cam", None) or (px, py)
                height_fn = getattr(gat, "get_elevation", gat.height_at)
                project = self.feed.camera.projector(snapshot.viewport, cam_pos, height_fn)

                step_cand = None
                max_idx = min(len(path) - 1, 10)
                for idx in range(max_idx, 1, -1):
                    cand = path[idx]
                    if math.hypot(cand[0] - px, cand[1] - py) < 2.2:
                        continue
                    u_c, v_c = project(*cand)
                    if 0.15 <= u_c <= 0.85 and 0.15 <= v_c <= 0.85:
                        cand_sx = left + int(u_c * width)
                        cand_sy = top + int(v_c * height)
                        if not self._is_point_in_game_ui(cand_sx, cand_sy):
                            step_cand = (cand, cand_sx, cand_sy)
                            break

                if step_cand:
                    c_cell, c_sx, c_sy = step_cand
                    self._click_debug(f"LOOT A*: andando para drop #{self.locked_drop_gid} via ({c_cell[0]},{c_cell[1]})")
                    self._send_click(target_hwnd, c_sx, c_sy)
                    self.last_loot_click = now
                    return

    def get_game_hwnd(self, hwnd=None):
        """Retorna o handle válido do jogo ou redetecta automaticamente se IsWindow retornar 0."""
        if hwnd and self.user32.IsWindow(hwnd):
            return hwnd
        vp_obj = getattr(self.feed, "viewport", None)
        cached_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        if cached_hwnd and self.user32.IsWindow(cached_hwnd):
            return cached_hwnd

        # IsWindow(game_hwnd) retornou 0 (ou hwnd inexistente): re-detecta automaticamente a janela 'Ragnarok' (Ragexe.exe) em tempo real
        new_hwnd, new_pid, new_title = find_ragnarok_window(getattr(self, "_ragexe_pid", None))
        if new_hwnd and self.user32.IsWindow(new_hwnd):
            if vp_obj:
                vp_obj.handle = new_hwnd
            print(f"[Janela] Ragnarok redetectado em tempo real: hwnd=0x{new_hwnd:X} (PID={new_pid}, título='{new_title}')", flush=True)
            return new_hwnd
        return None

    def test_click_f9(self):
        """Dispara 1 clique de teste imediato no centro da janela do Ragnarok e imprime diagnóstico completo."""
        print("\n" + "=" * 70, flush=True)
        print(f"[F9 Test Click] Disparando clique de teste imediato (Método ativo: '{self._click_method}')...", flush=True)

        hwnd = self.get_game_hwnd()
        sx, sy = None, None
        if hwnd and self.user32.IsWindow(hwnd):
            rect = wintypes.RECT()
            origin = wintypes.POINT(0, 0)
            if self.user32.GetClientRect(hwnd, ctypes.byref(rect)) and self.user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    sx = origin.x + (w // 2)
                    sy = origin.y + (h // 2)

        if sx is None or sy is None:
            vp_obj = getattr(self.feed, "viewport", None)
            vp = vp_obj() if callable(vp_obj) else None
            if vp:
                sx = vp[0] + (vp[2] // 2)
                sy = vp[1] + (vp[3] // 2)
            else:
                sx = self._screen_w // 2
                sy = self._screen_h // 2

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetLastError(0)
        pt_before = wintypes.POINT()
        r_pos_before = bool(self.user32.GetCursorPos(ctypes.byref(pt_before)))
        err_pos_before = ctypes.get_last_error()
        is_admin_diag = bool(ctypes.windll.shell32.IsUserAnAdmin())
        fg_diag = u32.GetForegroundWindow()
        fg_pid_diag = wintypes.DWORD()
        u32.GetWindowThreadProcessId(fg_diag, ctypes.byref(fg_pid_diag))
        print(f"[F9 Diag] is_admin={is_admin_diag} fg_hwnd={fg_diag} fg_pid={fg_pid_diag.value}", flush=True)
        print(f"[F9 Test Click] Cursor ANTES: ({pt_before.x}, {pt_before.y}) (GetCursorPos={r_pos_before}, LastError={err_pos_before})", flush=True)
        print(f"[F9 Test Click] Alvo do clique: ({sx}, {sy}) [hwnd: {f'0x{hwnd:X}' if hwnd else 'None'}] método={self._click_method}", flush=True)
        log_click_file(
            f"F9_DIAG: is_admin={is_admin_diag} fg_pid={fg_pid_diag.value} "
            f"cursor_antes=({pt_before.x},{pt_before.y}) alvo=({sx},{sy}) "
            f"hwnd={'0x{:X}'.format(hwnd) if hwnd else 'None'} metodo={self._click_method}"
        )

        self._send_click(hwnd, sx, sy, test_mode=True)

        k32.SetLastError(0)
        pt_after = wintypes.POINT()
        r_pos_after = bool(self.user32.GetCursorPos(ctypes.byref(pt_after)))
        err_pos_after = ctypes.get_last_error()
        print(f"[F9 Test Click] Cursor DEPOIS: ({pt_after.x}, {pt_after.y}) (GetCursorPos={r_pos_after}, LastError={err_pos_after})", flush=True)
        print("=" * 70 + "\n", flush=True)

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

    def _is_physical_mouse_down(self) -> bool:
        """Retorna True somente se o botão esquerdo FÍSICO estiver pressionado.
        Ignora VK_LBUTTON por 0.55s após o último clique sintético do bot."""
        if getattr(self, "_click_in_progress", False) or getattr(self, "_click_busy", False):
            return False
        if (time.perf_counter() - getattr(self, "last_bot_click_ts", 0.0)) < 0.55:
            return False
        return bool((self.user32.GetAsyncKeyState(0x01) & 0x8000) != 0)

    def _smooth_move_to(self, sx: int, sy: int, game_hwnd: int = None) -> None:
        """Deslizamento interpolado ease-out do cursor até (sx, sy) para o DirectX reconhecer hover."""
        ensure_touch_injection()
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        x0, y0 = pt.x, pt.y
        dist = math.hypot(sx - x0, sy - y0)
        if game_hwnd is None:
            game_hwnd = self.get_game_hwnd(None)

        if dist > 8.0:
            steps = max(5, min(12, int(dist / 55)))
            for i in range(1, steps + 1):
                t = i / steps
                eased = 1.0 - (1.0 - t) ** 2
                if i == steps:
                    ix = int(sx)
                    iy = int(sy)
                else:
                    ix = int(round(x0 + (sx - x0) * eased)) + random.choice([-2, -1, 1, 2])
                    iy = int(round(y0 + (sy - y0) * eased)) + random.choice([-2, -1, 1, 2])

                u32.SetCursorPos(ix, iy)

                contact = POINTER_TOUCH_INFO()
                contact.pointerInfo.pointerType = PT_TOUCH
                contact.pointerInfo.pointerId = 0
                contact.pointerInfo.ptPixelLocation.x = ix
                contact.pointerInfo.ptPixelLocation.y = iy
                contact.pointerInfo.pointerFlags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE
                contact.touchFlags = TOUCH_FLAG_NONE
                contact.touchMask = 0
                contact.rcContact.left = ix - 2
                contact.rcContact.top = iy - 2
                contact.rcContact.right = ix + 2
                contact.rcContact.bottom = iy + 2
                u32.InjectTouchInput(1, ctypes.byref(contact))

                if game_hwnd and u32.IsWindow(game_hwnd):
                    pt_cl = wintypes.POINT(ix, iy)
                    if u32.ScreenToClient(game_hwnd, ctypes.byref(pt_cl)):
                        lparam_m = ((pt_cl.y & 0xFFFF) << 16) | (pt_cl.x & 0xFFFF)
                        u32.PostMessageW(game_hwnd, 0x0200, 0, lparam_m)

                time.sleep(0.006)

        # Pouso exato em (sx, sy)
        u32.SetCursorPos(int(sx), int(sy))
        contact_final = POINTER_TOUCH_INFO()
        contact_final.pointerInfo.pointerType = PT_TOUCH
        contact_final.pointerInfo.pointerId = 0
        contact_final.pointerInfo.ptPixelLocation.x = int(sx)
        contact_final.pointerInfo.ptPixelLocation.y = int(sy)
        contact_final.pointerInfo.pointerFlags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE
        contact_final.touchFlags = TOUCH_FLAG_NONE
        contact_final.touchMask = 0
        contact_final.rcContact.left = int(sx) - 2
        contact_final.rcContact.top = int(sy) - 2
        contact_final.rcContact.right = int(sx) + 2
        contact_final.rcContact.bottom = int(sy) + 2
        u32.InjectTouchInput(1, ctypes.byref(contact_final))
        if game_hwnd and u32.IsWindow(game_hwnd):
            pt_cl = wintypes.POINT(int(sx), int(sy))
            if u32.ScreenToClient(game_hwnd, ctypes.byref(pt_cl)):
                lparam_m = ((pt_cl.y & 0xFFFF) << 16) | (pt_cl.x & 0xFFFF)
                u32.PostMessageW(game_hwnd, 0x0200, 0, lparam_m)

    def touch_postmsg(self, sx, sy, game_hwnd=None, *, test_mode=False):
        now = time.perf_counter()
        if getattr(self, "_click_in_progress", False):
            return False
        last_ts = getattr(self, "_last_click_ts", 0.0)
        if getattr(self, "_click_busy", False) and (now - getattr(self, "_click_start_ts", 0.0) < 0.25):
            return False
        if now - last_ts < 0.35:
            return False

        hwnd = int(getattr(self, "game_hwnd", 0) or game_hwnd or 0)
        if not hwnd or not u32.IsWindow(hwnd):
            hwnd = int(u32.FindWindowW(None, "Ragnarok") or 0)
        if not hwnd or not u32.IsWindow(hwnd):
            new_h, _, _ = find_ragnarok_window(getattr(self, "_ragexe_pid", None))
            if new_h and u32.IsWindow(new_h):
                hwnd = int(new_h)
        if not hwnd or not u32.IsWindow(hwnd):
            self._click_debug("touch_postmsg: janela do jogo indisponível")
            return False
        self.game_hwnd = hwnd

        self._click_in_progress = True
        self._click_busy = True
        self._click_start_ts = now
        self._last_click_ts = self.last_bot_click_ts = now

        def _do_smooth_click():
            try:
                ensure_input_desktop()
                ensure_touch_injection()

                # Foca janela do jogo se necessário
                try:
                    fg = u32.GetForegroundWindow()
                    if not fg or int(fg) != int(hwnd):
                        u32.BringWindowToTop(hwnd)
                        u32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

                # 1 e 2. Deslizamento interpolado suave (anti-teleporte para hover no Ragexe.exe)
                self._smooth_move_to(sx, sy, hwnd)

                # 3. Ao chegar exatamente em (sx, sy), aguarda 45ms para o cliente atualizar o cursor de hover
                time.sleep(0.045)

                pt_cl = wintypes.POINT(int(sx), int(sy))
                u32.ScreenToClient(hwnd, ctypes.byref(pt_cl))
                lparam = ((pt_cl.y & 0xFFFF) << 16) | (pt_cl.x & 0xFFFF)

                SM_XVIRTUALSCREEN = 76
                SM_YVIRTUALSCREEN = 77
                SM_CXVIRTUALSCREEN = 78
                SM_CYVIRTUALSCREEN = 79
                v_left = u32.GetSystemMetrics(SM_XVIRTUALSCREEN)
                v_top  = u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
                v_w    = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or getattr(self, "_screen_w", 1920)
                v_h    = u32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or getattr(self, "_screen_h", 1080)
                norm_x = max(0, min(65535, int(((sx - v_left) * 65535) / max(1, v_w - 1))))
                norm_y = max(0, min(65535, int(((sy - v_top)  * 65535) / max(1, v_h - 1))))
                abs_flags = 0x0001 | 0x8000 | 0x4000

                contact = POINTER_TOUCH_INFO()
                contact.pointerInfo.pointerType = PT_TOUCH
                contact.pointerInfo.pointerId = 0
                contact.pointerInfo.hwndTarget = 0
                contact.pointerInfo.ptPixelLocation.x = int(sx)
                contact.pointerInfo.ptPixelLocation.y = int(sy)
                contact.touchFlags = 0
                contact.touchMask = 0x0004
                contact.rcContact.left = int(sx) - 2
                contact.rcContact.top = int(sy) - 2
                contact.rcContact.right = int(sx) + 2
                contact.rcContact.bottom = int(sy) + 2

                # Envia WM_LBUTTONDOWN (0x0201)
                ctypes.set_last_error(0)
                contact.pointerInfo.pointerFlags = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                d = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
                ed = ctypes.get_last_error()

                try:
                    u32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
                    u32.mouse_event(0x0002, 0, 0, 0, 0)
                except Exception:
                    pass

                # 50ms de retenção física antes de liberar o botão
                time.sleep(0.050)

                # Envia WM_LBUTTONUP (0x0202)
                ctypes.set_last_error(0)
                contact.pointerInfo.pointerFlags = POINTER_FLAG_UP
                u = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
                eu = ctypes.get_last_error()

                try:
                    u32.PostMessageW(hwnd, 0x0202, 0x0000, lparam)
                    u32.mouse_event(0x0004, 0, 0, 0, 0)
                except Exception:
                    pass

                # Fallback SendInput se InjectTouchInput falhou
                if d == 0 or u == 0:
                    inp_d = INPUT()
                    inp_d.type = 0
                    inp_d.mi.dx = norm_x
                    inp_d.mi.dy = norm_y
                    inp_d.mi.dwFlags = abs_flags | 0x0002
                    u32.SendInput(1, ctypes.byref(inp_d), ctypes.sizeof(INPUT))
                    time.sleep(0.050)
                    inp_u = INPUT()
                    inp_u.type = 0
                    inp_u.mi.dx = norm_x
                    inp_u.mi.dy = norm_y
                    inp_u.mi.dwFlags = abs_flags | 0x0004
                    u32.SendInput(1, ctypes.byref(inp_u), ctypes.sizeof(INPUT))

                now_end = time.perf_counter()
                self._last_click_ts = self.last_bot_click_ts = self._last_click_end_ts = now_end

                is_admin = getattr(self, "is_admin", False)
                msg = (
                    f"touch_postmsg ({sx},{sy}) hwnd={hex(hwnd)}: "
                    f"is_admin={is_admin} HOVER=1 "
                    f"INJ_DOWN={d}(err={ed}) INJ_UP={u}(err={eu})"
                )
                if 5 in (ed, eu):
                    msg += " | Acesso negado (UIPI)."
                log_click_file(msg)
            except Exception as exc:
                self._last_click_ts = self.last_bot_click_ts = self._last_click_end_ts = time.perf_counter()
                if test_mode:
                    print(f"[ClickDebug] touch_postmsg erro _do_smooth_click: {exc}", flush=True)
            finally:
                self._click_in_progress = False
                self._click_busy = False

        threading.Thread(target=_do_smooth_click, daemon=True).start()
        return True

    def _natural_dodge_cursor(self, sx, sy):
        """Executa movimento natural de desvio no cursor (arco suave de 20..35px para o lado via InjectTouchInput + micro-move)."""
        try:
            dx = random.choice([22, 28, 33, -22, -28, -33])
            dy = random.choice([8, -8, 12, -12])
            steps = 4
            for i in range(1, steps + 1):
                t = i / steps
                # Arco suave em curva senoidal
                arc_y = int(math.sin(t * math.pi) * 8.0)
                cx = int(sx + dx * t)
                cy = int(sy + dy * t - arc_y)
                self._move_pointer_with_touch(cx, cy)
                try:
                    self.user32.mouse_event(0x0001, 1, 0, 0, 0)
                    self.user32.mouse_event(0x0001, -1, 0, 0, 0)
                except Exception:
                    pass
                time.sleep(0.012)
        except Exception as exc:
            self._click_debug(f"Erro no desvio suave do cursor: {exc}")

    def _send_game_key(self, vk_code: int, hold_ms: int = 35):
        """Envia tecla de jogo usando Win32 SendInput com Hardware ScanCode."""
        scan = u32.MapVirtualKeyW(vk_code, 0)
        flags = 0x0008  # KEYEVENTF_SCANCODE
        if vk_code in (0x25, 0x26, 0x27, 0x28, 0x2D):  # Left, Up, Right, Down, Insert
            flags |= 0x0001  # KEYEVENTF_EXTENDEDKEY

        inp_down = INPUT(type=1)  # INPUT_KEYBOARD = 1
        inp_down.ki.wVk = 0
        inp_down.ki.wScan = scan
        inp_down.ki.dwFlags = flags
        inp_down.ki.time = 0
        inp_down.ki.dwExtraInfo = 0
        u32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))

        time.sleep(max(0.005, hold_ms / 1000.0))

        inp_up = INPUT(type=1)
        inp_up.ki.wVk = 0
        inp_up.ki.wScan = scan
        inp_up.ki.dwFlags = flags | 0x0002  # KEYEVENTF_KEYUP
        inp_up.ki.time = 0
        inp_up.ki.dwExtraInfo = 0
        u32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))

        # Fallback PostMessageW para o Ragexe
        target_hwnd = getattr(getattr(self.feed, "viewport", None), "handle", None)
        target_hwnd = self.get_game_hwnd(target_hwnd)
        if target_hwnd:
            lparam_down = 1 | (scan << 16)
            if flags & 0x0001:
                lparam_down |= (1 << 24)
            lparam_up = lparam_down | (3 << 30)
            u32.PostMessageW(target_hwnd, 0x0100, vk_code, lparam_down)
            time.sleep(0.005)
            u32.PostMessageW(target_hwnd, 0x0101, vk_code, lparam_up)

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
                self._send_game_key(VK_DOWN, hold_ms=35)
                time.sleep(0.075)

            time.sleep(0.075)
            self._click_debug(f"[ClickDebug] Enviando tecla VK_RETURN (Enter) para confirmar opção #{index}")
            self._send_game_key(VK_RETURN, hold_ms=45)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _send_vk_key(self, vk_code: int, hwnd=None):
        """Compatibilidade: envia tecla virtual delegando para _send_game_key."""
        self._send_game_key(vk_code, hold_ms=35)

    def _navigate_menu_keyboard(self, option_index: int):
        """Compatibilidade: navega pelo menu delegando para _send_menu_selection."""
        self._send_menu_selection(option_index)

    def _send_click(self, hwnd, screen_x, screen_y, test_mode=False, **_kwargs):
        """Envia clique na posição física (screen_x, screen_y)."""
        sx = int(screen_x)
        sy = int(screen_y)

        # Zona Proibida do Retângulo do HUD (com margem de 12px) e da UI do jogo (chat, status, minimapa)
        if not test_mode and self._is_point_in_game_ui(sx, sy):
            self._click_debug(f"CLIQUE BLOQUEADO: ({sx},{sy}) está dentro da interface do jogo/HUD (zona proibida)")
            return

        # Registra clique no CameraTracker para auto-calibração PnP
        if not test_mode and hasattr(self, "feed") and hasattr(self.feed, "camera"):
            vp = getattr(self, "current_viewport", None) or (self.feed.viewport() if callable(getattr(self.feed, "viewport", None)) else None)
            if vp and hasattr(self.feed.camera, "record_click"):
                self.feed.camera.record_click(sx, sy, vp)

        method = self._click_method

        if method == "touch_postmsg":
            ensure_input_desktop()
            now = time.perf_counter()
            if getattr(self, "_click_in_progress", False) or getattr(self, "_click_busy", False) or (now - getattr(self, "last_bot_click_ts", 0.0)) < 0.28:
                return
            self.touch_postmsg(sx, sy, self.get_game_hwnd(hwnd), test_mode=test_mode)
            return

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

    def _try_auto_farm(self, snapshot, px=None, py=None, max_search_dist=22.0):
        """Auto-Farm: pathfinding A* tela inteira, trava de alvo (locked_mob_gid) e ataque inteligente."""
        if not snapshot or not hasattr(self.feed, "world"):
            return

        # Pausa se qualquer botão físico do mouse estiver pressionado (1=L, 2=R, 4=M)
        if any((self.user32.GetAsyncKeyState(k) & 0x8000) != 0 for k in (2, 4)) or self._is_physical_mouse_down():
            self._click_debug("FARM BLOQUEADO: botão físico do mouse pressionado")
            return

        # Atua se o jogo ou o overlay estiver em foco
        vp_obj = getattr(self.feed, "viewport", None)
        target_hwnd = getattr(vp_obj, "handle", None) if vp_obj else None
        target_hwnd = self.get_game_hwnd(target_hwnd)
        if not self._game_is_foreground():
            self._click_debug(f"FARM BLOQUEADO: foco não está no jogo/overlay (game_hwnd={target_hwnd})")
            return

        now = time.monotonic()
        _, player, entities = self.feed.world.snapshot()
        if px is None or py is None:
            px, py = player.cell(now) if player else (None, None)
        left, top, width, height = snapshot.viewport

        curr_map = getattr(getattr(self.feed, "world", None), "map", None)
        gat = None
        if curr_map and hasattr(self.feed, "terrain") and self.feed.terrain:
            try:
                gat = self.feed.terrain.for_map(curr_map)
            except Exception:
                pass

        # Limpa blacklist expirada
        self._farm_blacklist = {
            gid: exp for gid, exp in self._farm_blacklist.items()
            if now < exp
        }

        # 1. Se já temos um mob travado, verifica se ainda existe no snapshot ativo
        target_item = None
        search_limit = 2.5 if max_search_dist <= 2.5 else 22.0
        if self.locked_mob_gid is not None:
            if self._farm_blacklist.get(self.locked_mob_gid, 0.0) > now:
                self.locked_mob_gid = None
                self.locked_mob_clicks = 0
                self.locked_mob_first_time = 0.0
            else:
                for item in snapshot.objects:
                    if item.kind == "mob" and item.key == f"mob:{self.locked_mob_gid}":
                        dist = self._entity_distance("mob", item.key, px, py)
                        if px is None or dist <= search_limit:
                            target_item = item
                        break
                if target_item is None:
                    # Mob morreu ou sumiu do snapshot (0x0080 / 0x00A1) ou saiu do raio
                    self._reset_patrol(px, py)
                    self.locked_mob_gid = None
                    self.locked_mob_clicks = 0
                    self.locked_mob_first_time = 0.0

        # 2. Se não tem mob travado, escolhe o melhor elegível (raio <= 22 células com checagem A*)
        if self.locked_mob_gid is None:
            candidates = []
            for item in snapshot.objects:
                if item.kind != "mob":
                    continue
                try:
                    gid = int(item.key.split(":", 1)[1])
                except (IndexError, ValueError):
                    continue
                if gid <= 0 or self._farm_blacklist.get(gid, 0.0) > now:
                    continue
                # Filtro Whitelist / Blacklist de monstros
                m_mode = getattr(self, "_mob_filter_mode", "Todos")
                m_cid = item.catalog_id
                m_name = self.names.get("mob", {}).get(m_cid, "").strip().lower()
                m_id_str = f"#{m_cid}"
                if m_mode == "Whitelist":
                    if m_cid not in getattr(self, "mob_whitelist", set()) \
                            and m_id_str not in getattr(self, "mob_whitelist_names", set()) \
                            and not any(w in m_name for w in getattr(self, "mob_whitelist_names", set())):
                        continue
                elif m_mode == "Blacklist":
                    if m_cid in getattr(self, "mob_blacklist", set()) \
                            or m_id_str in getattr(self, "mob_blacklist_names", set()) \
                            or any(w in m_name for w in getattr(self, "mob_blacklist_names", set())):
                        continue
                # Filtro de monstro por nome digitado
                mf = getattr(self, '_monster_filter', '')
                if mf:
                    mob_name = self.names.get("mob", {}).get(item.catalog_id, "")
                    if mf.lower() not in mob_name.lower():
                        continue

                # ── Filtro Anti-KS 1: Monstro já engajado em combate com outro jogador ──
                engaged_until = 0.0
                if hasattr(self.feed, "world"):
                    w = self.feed.world
                    with w.lock:
                        mob_ent = w.entities.get(("mob", gid))
                        if mob_ent:
                            engaged_until = getattr(mob_ent, "engaged_other_until", 0.0)
                        if not engaged_until and hasattr(w, "mobs_engaged_other"):
                            engaged_until = w.mobs_engaged_other.get(gid, 0.0)
                if engaged_until > now:
                    continue

                # ── Filtro Anti-KS 2: Proteção contra Mob Train por proximidade ──
                # Ignora monstros a <= 4.5 células de outro jogador e mais perto dele do que de você
                # (exceto se você já estiver batendo naquele monstro antes)
                already_hit_by_me = False
                if hasattr(self.feed, "world") and hasattr(self.feed.world, "mobs_damaged_by_me"):
                    already_hit_by_me = (now - self.feed.world.mobs_damaged_by_me.get(gid, 0.0)) < 15.0

                if not already_hit_by_me and hasattr(self.feed, "world"):
                    mob_c = self.feed.world.get_entity_cell("mob", item.key)
                    if mob_c:
                        is_mob_train = False
                        with self.feed.world.lock:
                            for p_gid, (p_c, _) in self.feed.world.other_players.items():
                                if self.feed.world.player_id and p_gid == self.feed.world.player_id:
                                    continue
                                d_other = math.hypot(mob_c[0] - p_c[0], mob_c[1] - p_c[1])
                                if d_other <= 4.5:
                                    d_me = math.hypot(mob_c[0] - px, mob_c[1] - py) if (px is not None and py is not None) else 999.0
                                    if d_other < d_me:
                                        is_mob_train = True
                                        break
                        if is_mob_train:
                            continue

                dist = self._entity_distance("mob", item.key, px, py)
                if px is None or dist <= search_limit:
                    candidates.append((dist, gid, item))

            candidates.sort(key=lambda c: c[0])

            best_item = None
            best_gid = None
            for dist, gid, item in candidates:
                mob_cell = self.feed.world.get_entity_cell("mob", item.key)
                if gat and px is not None and mob_cell:
                    start_cell = (int(round(px)), int(round(py)))
                    path = gat.find_path(start_cell, (int(round(mob_cell[0])), int(round(mob_cell[1]))), blocked_cells=self.temp_blocked_cells)
                    if not path:
                        # Inacessível por rio/penhasco: bloqueia por 6s
                        self._farm_blacklist[gid] = now + 6.0
                        continue
                best_item = item
                best_gid = gid
                break

            if best_gid is not None and best_item is not None:
                self.locked_mob_gid = best_gid
                self.locked_mob_clicks = 0
                self.locked_mob_first_time = now
                target_item = best_item

        # 3. Executa aproximação A* ou ataque no mob travado
        if target_item is not None and self.locked_mob_gid is not None:
            last_damaged = 0.0
            if hasattr(self.feed.world, "mobs_damaged_by_me"):
                last_damaged = self.feed.world.mobs_damaged_by_me.get(self.locked_mob_gid, 0.0)

            p_obj = getattr(getattr(self.feed, "world", None), "player", None)
            p_moving = getattr(p_obj, "is_moving", False) or getattr(self.feed.world, "_is_player_walking", lambda: False)()
            if player and player.duration and now < player.started + min(player.duration, 2.5):
                p_moving = True

            # Mantém vivo se o personagem estiver ativamente se deslocando em direção ao mob
            if p_moving:
                self.locked_mob_first_time = now

            # Se após 6.0s parado o mob não sofrer dano (inacessível/bugado), blacklist de 6s e troca de alvo
            if (now - self.locked_mob_first_time > 6.0) and (last_damaged < self.locked_mob_first_time) and not p_moving:
                self._farm_blacklist[self.locked_mob_gid] = now + 6.0
                self._reset_patrol(px, py)
                self.locked_mob_gid = None
                self.locked_mob_clicks = 0
                self.locked_mob_first_time = 0.0
                return

            # Enquanto o personagem já estiver batendo nesse mesmo alvo (ZC_NOTIFY_ACT recente < 1.2s), não repete cliques
            if (now - last_damaged) < 1.2:
                self._click_debug(f"FARM pausado: já atacando mob:{self.locked_mob_gid} (last_hit={now - last_damaged:.2f}s atrás)")
                return

            # Se já clicou no monstro e o personagem está se deslocando até ele, aguarda a aproximação
            if self.locked_mob_clicks >= 1 and p_moving:
                self._click_debug(f"FARM pausado: personagem andando até mob:{self.locked_mob_gid}")
                return

            mob_cell = self.feed.world.get_entity_cell("mob", target_item.key)
            dist_mob = self._entity_distance("mob", target_item.key, px, py) if (px is not None and py is not None) else 0.0

            # Projeta sprite do monstro com sprite_z = 3.2
            if mob_cell:
                u_mob, v_mob, sx_mob, sy_mob = self._project_ground_cell(snapshot, mob_cell[0], mob_cell[1], sprite_z=3.2)
            else:
                u_mob, v_mob = target_item.u, target_item.v
                sx_mob = left + int(u_mob * width)
                sy_mob = top  + int(v_mob * height)

            is_in_hud = self._is_point_in_game_ui(sx_mob, sy_mob)
            is_visible_screen = (0.05 <= u_mob <= 0.95 and 0.05 <= v_mob <= 0.95) and not is_in_hud

            is_attacking = (now - last_damaged) < 1.2
            started_action = p_moving or is_attacking

            # Se monstro vivo a <= 11.5 células e visível fora do HUD: clica direto no sprite (sprite_z = 3.2)
            # Só usa A* no chão se dist > 11.5 ou após 2 cliques diretos sem iniciar ataque/movimento
            if dist_mob <= 11.5 and is_visible_screen and (self.locked_mob_clicks < 2 or started_action):
                if self.locked_mob_clicks == 0:
                    if now - self.locked_mob_first_time < 0.10:
                        return
                else:
                    if now - self.last_farm_click < 0.850:
                        return

                self._click_debug(f"FARM clique #{self.locked_mob_clicks+1} no sprite (z=3.2) em ({sx_mob},{sy_mob}) mob:{self.locked_mob_gid}")
                self._send_click(target_hwnd, sx_mob, sy_mob)
                self.last_farm_click = now
                self.locked_mob_clicks += 1
                return

            # Caso contrário (dist > 11.5 ou após 2 cliques diretos sem ataque/movimento ou fora do HUD): aproximação via A*
            if p_moving:
                self._click_debug(f"FARM pausado: personagem andando (mob:{self.locked_mob_gid})")
                return

            # Se estiver a <= 4.5 células e apenas atrás da UI, tenta passo seguro para desobstruir visão
            if dist_mob <= 4.5 and is_in_hud and mob_cell:
                if self._side_step_to_reveal(mob_cell, px, py):
                    return

            # Caminhada via A* em direção ao monstro
            if gat and px is not None and mob_cell:
                start_cell = (int(round(px)), int(round(py)))
                path = gat.find_path(start_cell, (int(round(mob_cell[0])), int(round(mob_cell[1]))), blocked_cells=self.temp_blocked_cells)
                if not path:
                    self._farm_blacklist[self.locked_mob_gid] = now + 6.0
                    self._reset_patrol(px, py)
                    self.locked_mob_gid = None
                    return

                self.active_nav_path = list(path)
                self.active_nav_color = "#F87171"  # Vermelho = Auto-Farm

                # Seleciona sub-passo a 8–10 células de distância
                cam_pos = getattr(self.feed, "_cam", None) or (px, py)
                height_fn = getattr(gat, "get_elevation", gat.height_at)
                project = self.feed.camera.projector(snapshot.viewport, cam_pos, height_fn)

                step_cand = None
                max_idx = min(len(path) - 1, 10)
                for idx in range(max_idx, 1, -1):
                    cand = path[idx]
                    if math.hypot(cand[0] - px, cand[1] - py) < 2.2:
                        continue
                    u_c, v_c = project(*cand)
                    if 0.15 <= u_c <= 0.85 and 0.15 <= v_c <= 0.85:
                        cand_sx = left + int(u_c * width)
                        cand_sy = top + int(v_c * height)
                        if not self._is_point_in_game_ui(cand_sx, cand_sy):
                            step_cand = (cand, cand_sx, cand_sy)
                            break

                if step_cand:
                    c_cell, c_sx, c_sy = step_cand
                    self._click_debug(f"FARM A*: andando para mob #{self.locked_mob_gid} (dist={dist_mob:.1f}) via ({c_cell[0]},{c_cell[1]})")
                    self._send_click(target_hwnd, c_sx, c_sy)
                    self.last_farm_click = now
                    return

    def draw(self, snapshot, stats=None):
        if not self._game_is_foreground():
            self.canvas_pool.hide_all()
            return False

        if not getattr(self, "_overlay_visible", True):
            self.canvas_pool.hide_all()
            return True

        left, top, width, height = snapshot.viewport
        # SWP_NOZORDER | SWP_NOACTIVATE
        if not self.user32.SetWindowPos(self.handle, None, left, top, width, height, 0x0014):
            self.canvas_pool.hide_all()
            return False

        self.canvas_pool.begin_frame()

        # Desenha linha tracejada do caminho A* planejado (Farm, Loot, Patrol, Rota)
        nav_path  = getattr(self, "active_nav_path", None)
        nav_color = getattr(self, "active_nav_color", "#38BDF8")
        # Mantém compatibilidade: patrol legacy usa current_patrol_path
        if nav_path is None and self.auto_patrol and self.current_patrol_path and len(self.current_patrol_path) >= 2:
            nav_path  = self.current_patrol_path
            nav_color = "#38BDF8"
        if nav_path and len(nav_path) >= 2:
            if hasattr(self.feed, "camera") and hasattr(self.feed, "terrain"):
                curr_map = self.feed.world.map if hasattr(self.feed, "world") else None
                height_fn = flat_height
                if curr_map and self.feed.terrain:
                    try:
                        g = self.feed.terrain.for_map(curr_map)
                        if g:
                            height_fn = getattr(g, "get_elevation", g.height_at)
                    except Exception:
                        pass
                cam_pos = getattr(self.feed, "_cam", None) or (0, 0)
                project = self.feed.camera.projector(snapshot.viewport, cam_pos, height_fn)
                pts = []
                for cell in nav_path:
                    u, v = project(*cell)
                    if -0.2 <= u <= 1.2 and -0.2 <= v <= 1.2:
                        pts.append((u * width, v * height))
                if len(pts) >= 2:
                    for i in range(len(pts) - 1):
                        self.canvas_pool.get_line(
                            pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1],
                            fill=nav_color, width=2, dash=(6, 4)
                        )
        if self.current_waypoint and hasattr(self.feed, "camera"):
            try:
                curr_map2 = self.feed.world.map if hasattr(self.feed, "world") else None
                height_fn2 = flat_height
                if curr_map2 and self.feed.terrain:
                    g2 = self.feed.terrain.for_map(curr_map2)
                    if g2:
                        height_fn2 = getattr(g2, "get_elevation", g2.height_at)
                cam_pos2 = getattr(self.feed, "_cam", None) or (0, 0)
                project2 = self.feed.camera.projector(snapshot.viewport, cam_pos2, height_fn2)
                wu, wv = project2(*self.current_waypoint)
                if 0 <= wu <= 1 and 0 <= wv <= 1:
                    wx, wy = self.canvas_pool.apply_hysteresis("waypoint", wu * width, wv * height, threshold=0.65)
                    self.canvas_pool.get_oval(wx - 6, wy - 6, wx + 6, wy + 6, outline=nav_color, width=2)
                    wp_text = self.canvas_pool.get_text(wx, wy - 14, text="[Waypoint]", fill=nav_color, font=("Tahoma", 8, "bold"))
                    wp_bbox = self.canvas.bbox(wp_text)
                    if wp_bbox:
                        bx1, by1, bx2, by2 = wp_bbox
                        wp_bg = self.canvas_pool.get_rect(bx1 - 3, by1 - 1, bx2 + 3, by2 + 1, fill="#0B1120", outline=nav_color, width=1)
                        self.canvas.tag_lower(wp_bg, wp_text)
            except Exception:
                pass

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
            snap_val = 2 if is_moving else None
            cx, cy = self.canvas_pool.apply_hysteresis(f"lbl:{item.key}", item.u * width, item.v * height, threshold=0.65, snap_px=snap_val)

            label = self.names.get(item.kind, {}).get(item.catalog_id)
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
                label = f"{prefix} #{item.catalog_id}"
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
                color = "#C084FC"  # Lilás para NPC
                border_outline = "#9333EA"
                border_width = 2
                self.canvas_pool.get_rect(cx - 7, cy - 7, cx + 7, cy + 7, outline="#A855F7", width=2)
                self.canvas_pool.get_oval(ax - 4, ay - 4, ax + 4, ay + 4, outline="#9333EA", width=1)
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
                        help="Método de clique. Padrão: %(default)s (InjectTouchInput + PostMessageW).")
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

    try:
        u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
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
