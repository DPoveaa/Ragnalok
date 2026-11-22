"""Server-to-client rAthena packets, TCP reassembly and world snapshots."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
from threading import Event, Lock, Thread
import time


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_LENGTHS = Path(__file__).with_name("rathena-lengths.txt")
DEFAULT_GRF = Path("D:/Jogos/Ragnarok/data.grf")
MAX_BUFFER = 1 << 20
MASK = 0xFFFFFFFF
ACCEPT = {0x0073, 0x02EB, 0x0A18}
MAP_TRANSFER = {0x0092, 0x0AC7}
FLOOR = {0x009D, 0x009E, 0x084A, 0x084B, 0x0ADD, 0x0B14, 0x0B15}
UNIT = {
    0x0078: "idle", 0x007B: "walk",
    0x02EC: "walk", 0x02ED: "spawn", 0x02EE: "idle",
    0x07F7: "walk", 0x07F8: "spawn", 0x07F9: "idle",
    0x0856: "walk", 0x0858: "spawn", 0x0857: "idle",
    0x0914: "walk", 0x090F: "spawn", 0x0915: "idle",
    0x09DB: "walk", 0x09DC: "spawn", 0x09DD: "idle",
    0x09FD: "walk", 0x09FE: "spawn", 0x09FF: "idle",
    0x0AFD: "walk", 0x0AFE: "spawn", 0x0AFF: "idle",
}
KNOWN_PACKET_SIZES = {
    0x0088: 10,
    0x00B0: 8,
    0x0BC9: 10,
    0x0B14: 21,
    0x0B15: 21,
    0x0ADD: 24,  # ZC_ITEM_FALL_ENTRY (24 bytes no PACKETVER atual)
    0x0AFE: -1,  # ZC_NOTIFY_NEWENTRY (comprimento dinâmico)
    0x0AFD: -1,  # ZC_NOTIFY_MOVE
    0x0AFF: -1,  # ZC_NOTIFY_STANDENTRY
    0x09FD: -1,
    0x09FE: -1,
    0x09FF: -1,
    0x084A: 19,
    0x0078: 55,
    0x007B: 60,
    0x00B4: -1,  # ZC_SAY_DIALOG (tamanho variável)
    0x00B5: 6,   # ZC_WAIT_DIALOG
    0x00B6: 6,   # ZC_CLOSE_DIALOG
    0x00B7: -1,  # ZC_MENU_LIST (tamanho variável)
    0x0095: 30,
    0x0195: 102,
    0x0136: -1,  # variable
    0x0137: 6,
    0x00D7: -1,  # variable
    0x00D8: 6,
    0x00A0: 33,  # ZC_ITEM_PICKUP_ACK
    0x011C: 68,  # ZC_WARPLIST
    0x01DE: 15,  # ZC_NOTIFY_SKILL
    0x0114: -1,  # ZC_USESKILL_ACK
}
KNOWN_SYNC_OPCODES = (
    ACCEPT
    | MAP_TRANSFER
    | FLOOR
    | set(UNIT.keys())
    | set(KNOWN_PACKET_SIZES.keys())
    | {
        0x0080, 0x0086, 0x0087, 0x0088, 0x008A, 0x008E, 0x0091, 0x00A0, 0x00A1,
        0x00B4, 0x00B5, 0x00B6, 0x00B7, 0x011C, 0x01DE, 0x0114,
        0x01FF, 0x0283, 0x0295, 0x02E1, 0x08C8, 0x09DF, 0x0B18,
        0x0095, 0x0195, 0x0136, 0x0137, 0x00D7, 0x00D8,
    }
)


def u16(data, offset=0):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def position(data, offset):
    a, b, c = data[offset:offset + 3]
    return (a << 2 | b >> 6, (b & 63) << 4 | c >> 4)


def movement(data, offset):
    c, d, e = data[offset + 2:offset + 5]
    return position(data, offset), ((c & 15) << 6 | d >> 2, (d & 3) << 8 | e)


def map_name(value):
    return value.lower().removesuffix(".gat").removesuffix(".rsw").removesuffix(".gnd")


def extract_map_from_packet(data, offset=2):
    """Extrai nome do mapa a partir de pacotes de transição (0x0091, 0x0AC7, 0x0092)."""
    if len(data) >= offset + 16:
        chunk = data[offset:offset + 16].split(b"\0", 1)[0]
        name_str = chunk.decode("ascii", errors="replace").strip()
        if name_str and any(c.isalnum() for c in name_str):
            return name_str
    if len(data) >= 20:
        chunk4 = data[4:20].split(b"\0", 1)[0]
        name_str4 = chunk4.decode("ascii", errors="replace").strip()
        if name_str4 and any(c.isalnum() for c in name_str4):
            return name_str4
    return ""


def packet_lengths(path):
    lengths = dict(KNOWN_PACKET_SIZES)
    path = Path(path) if path is not None else DEFAULT_LENGTHS
    if not path.is_file():
        candidates = [
            _get_base_dir() / path.name,
            getattr(sys, "_MEIPASS", None) and (Path(sys._MEIPASS) / path.name),
            Path(__file__).parent / path.name,
            Path(".") / path.name,
        ]
        for c in candidates:
            if c and Path(c).is_file():
                path = Path(c)
                break
    if not path.is_file():
        return lengths
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        try:
            opcode, size = int(fields[0], 16), int(fields[1])
            if not (0 <= opcode <= 0xFFFF and (size == -1 or 2 <= size <= 0xFFFF)):
                raise ValueError
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Tamanho de pacote inválido em {path}:{number}.") from exc
        lengths[opcode] = size
    lengths.update(KNOWN_PACKET_SIZES)
    return lengths


class PacketStream:
    def __init__(self, lengths, consume, world=None):
        self.lengths = lengths
        self.consume = consume
        self.world = world
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        start = 0
        while len(self.buffer) - start >= 2:
            opcode = u16(self.buffer, start)
            size = self.lengths.get(opcode)

            # Se o pacote não está na tabela ou tem tamanho incoerente, faz resync
            is_invalid_size = (
                size == -1
                and len(self.buffer) - start >= 4
                and (u16(self.buffer, start + 2) < 4 or u16(self.buffer, start + 2) > 16384)
            )
            if size is None or is_invalid_size:
                # Procura o próximo opcode conhecido no buffer
                found = -1
                for candidate in range(start + 1, len(self.buffer) - 1):
                    cand_op = u16(self.buffer, candidate)
                    if cand_op in KNOWN_SYNC_OPCODES or cand_op in self.lengths:
                        cand_size = self.lengths.get(cand_op)
                        if cand_size == -1 and len(self.buffer) - candidate >= 4:
                            var_len = u16(self.buffer, candidate + 2)
                            if var_len < 4 or var_len > 16384:
                                continue
                        found = candidate
                        break

                if found != -1:
                    skipped = found - start
                    if self.world:
                        cand_data = bytes(self.buffer[start:found])
                        self.world._log_unknown_packet(opcode, cand_data)
                    op_desc = f"0x{opcode:04X}" if size is None else "tamanho variável inválido"
                    print(
                        f"[Resync] Descartados {skipped} bytes no buffer ({op_desc}). "
                        f"Ressincronizado no opcode 0x{u16(self.buffer, found):04X}.",
                        flush=True,
                    )
                    start = found
                    continue
                else:
                    if self.world:
                        cand_data = bytes(self.buffer[start:min(len(self.buffer), start + 32)])
                        self.world._log_unknown_packet(opcode, cand_data)
                    # Nenhum opcode conhecido no buffer atual; mantém últimos bytes e aguarda
                    if len(self.buffer) - start > 1024:
                        start = len(self.buffer) - 2
                    break

            if size == -1:
                if len(self.buffer) - start < 4:
                    break
                size = u16(self.buffer, start + 2)

            end = start + size
            if end > len(self.buffer):
                break

            try:
                self.consume(opcode, self.buffer[start:end])
                if self.world:
                    self.world.inc_packets_ok()
            except Exception as exc:
                print(f"[PacketStream] Erro ao processar 0x{opcode:04X}: {exc}", flush=True)

            start = end
        del self.buffer[:start]


class TcpStream:
    """Reassemble one direction, including gaps, overlaps and 32-bit wraparound."""

    def __init__(self, first_sequence, consume):
        self.sequence = first_sequence & MASK
        self.consume = consume
        self.pending = {}

    def feed(self, sequence, data):
        delta = ((sequence - self.sequence + (1 << 31)) & MASK) - (1 << 31)
        if delta + len(data) <= 0:
            return
        if delta > 0:
            existing = self.pending.get(sequence, b"")
            if len(data) > len(existing):
                self.pending[sequence] = data
            if delta > MAX_BUFFER or len(self.pending) > 1024:
                raise ValueError("Lacuna TCP grande. Reconecte para reiniciar a captura.")
            return
        while data:
            fresh = data[-delta:]
            self.consume(fresh)
            self.sequence = (self.sequence + len(fresh)) & MASK
            data = b""
            for next_sequence, pending in tuple(self.pending.items()):
                relative = ((next_sequence - self.sequence + (1 << 31)) & MASK) - (1 << 31)
                if relative <= 0:
                    del self.pending[next_sequence]
                    if relative + len(pending) > 0:
                        delta, data = relative, pending
                        break


def diagonal_first_path(x0, y0, x1, y1):
    """Gera passos diagonais primeiro (min(|dx|, |dy|)) seguidos dos passos ortogonais restantes."""
    x0, y0 = int(round(x0)), int(round(y0))
    x1, y1 = int(round(x1)), int(round(y1))
    dx = x1 - x0
    dy = y1 - y0
    sx = 1 if dx > 0 else (-1 if dx < 0 else 0)
    sy = 1 if dy > 0 else (-1 if dy < 0 else 0)

    n_diag = min(abs(dx), abs(dy))
    cx, cy = x0, y0
    path = [(cx, cy)]

    # 1. Passos diagonais primeiro
    for _ in range(n_diag):
        cx += sx
        cy += sy
        path.append((cx, cy))

    # 2. Passos ortogonais restantes em X
    for _ in range(abs(dx) - n_diag):
        cx += sx
        path.append((cx, cy))

    # 3. Passos ortogonais restantes em Y
    for _ in range(abs(dy) - n_diag):
        cy += sy
        path.append((cx, cy))

    return path


@dataclass
class Track:
    origin: tuple
    target: tuple
    started: float = 0.0
    duration: float = 0.0
    segments: tuple = ()  # ((t_start, t_end, p_from, p_to), ...)
    _speed_ms: float = 150.0
    base_level: int = 1
    job_level: int = 1
    weight: int = 0
    max_weight: int = 1

    def __init__(self, origin, target, started=0.0, duration=0.0, segments=(), speed_ms=150.0, base_level=1, job_level=1, weight=0, max_weight=1):
        self.origin = origin
        self.target = target
        self.started = started
        self.duration = duration
        self.segments = segments
        self._speed_ms = float(speed_ms)
        self.base_level = int(base_level)
        self.job_level = int(job_level)
        self.weight = int(weight)
        self.max_weight = max(1, int(max_weight))

    @property
    def weight_ratio(self) -> float:
        mw = max(1, getattr(self, "max_weight", 1))
        return float(getattr(self, "weight", 0)) / float(mw)

    @property
    def speed_ms(self):
        return self._speed_ms

    @speed_ms.setter
    def speed_ms(self, val):
        new_speed = float(val)
        old_speed = getattr(self, "_speed_ms", 150.0)
        self._speed_ms = new_speed
        now = time.perf_counter()
        # Se houver um Track em andamento do jogador, recalcula o tempo restante proporcionalmente à nova velocidade
        if self.duration and now < (self.started + self.duration) and self.segments and old_speed > 0:
            old_sec = max(0.001, old_speed / 1000.0)
            new_sec = max(0.001, new_speed / 1000.0)
            ratio = new_sec / old_sec

            new_segs = []
            cur_t = self.started
            for t0, t1, p0, p1 in self.segments:
                if now >= t1:
                    new_segs.append((t0, t1, p0, p1))
                    cur_t = t1
                elif now <= t0:
                    dur = (t1 - t0) * ratio
                    new_segs.append((cur_t, cur_t + dur, p0, p1))
                    cur_t += dur
                else:
                    remaining = (t1 - now) * ratio
                    new_segs.append((t0, now + remaining, p0, p1))
                    cur_t = now + remaining
            self.segments = tuple(new_segs)
            self.duration = cur_t - self.started

    @classmethod
    def stationary(cls, cell, speed_ms=150.0):
        c = (float(cell[0]), float(cell[1]))
        return cls(c, c, 0.0, 0.0, (), speed_ms=float(speed_ms))

    @classmethod
    def walking(cls, cells, speed, start_time=None, gat=None):
        origin = (float(cells[0][0]), float(cells[0][1]))
        target = (float(cells[1][0]), float(cells[1][1]))
        # Inicia a interpolação 35ms adiantada para compensar a latência da animação no cliente
        started = start_time if start_time is not None else (time.perf_counter() - 0.035)

        if origin == target:
            return cls.stationary(origin, speed_ms=speed)

        path = []
        if gat is not None:
            try:
                path = gat.find_path(origin, target)
            except Exception:
                path = []
        if not path or len(path) < 2:
            path = diagonal_first_path(origin[0], origin[1], target[0], target[1])

        if len(path) < 2:
            return cls.stationary(target, speed_ms=speed)

        segments = []
        cur_t = started
        speed_sec = max(0.001, speed / 1000.0)

        for i in range(len(path) - 1):
            p0 = (float(path[i][0]), float(path[i][1]))
            p1 = (float(path[i + 1][0]), float(path[i + 1][1]))
            dx = abs(p1[0] - p0[0])
            dy = abs(p1[1] - p0[1])
            is_diag = (dx > 0 and dy > 0)
            step_duration = speed_sec * (1.4142135 if is_diag else 1.0)
            segments.append((cur_t, cur_t + step_duration, p0, p1))
            cur_t += step_duration

        total_duration = cur_t - started
        return cls(origin, target, started, total_duration, tuple(segments), speed_ms=float(speed))

    def cell(self, now=None):
        if now is None:
            now = time.perf_counter()
        if not self.duration or now >= self.started + self.duration:
            return self.target
        if now <= self.started:
            return self.origin

        for t0, t1, p0, p1 in self.segments:
            if now < t1:
                prog = (now - t0) / (t1 - t0) if t1 > t0 else 1.0
                return (
                    p0[0] + (p1[0] - p0[0]) * prog,
                    p0[1] + (p1[1] - p0[1]) * prog,
                )
        return self.target

    @property
    def end_time(self):
        return self.started + self.duration

    @property
    def is_moving(self) -> bool:
        """Indica se a entidade está atualmente em movimento."""
        now = time.perf_counter()
        return bool(self.duration > 0 and now < (self.started + self.duration))


@dataclass
class Entity:
    kind: str
    catalog_id: int
    track: Track
    amount: int = 1
    speed: int = 150
    ownership: str = "FREE"
    created_at: float = 0.0
    is_aggro: bool = False
    last_aggro_ts: float = 0.0
    is_hostile_now: bool = False
    consecutive_approaches: int = 0
    last_player_dist: float = 999.0
    engaged_other_until: float = 0.0


class World:
    def __init__(self, initial_map=None, legacy_fields=False, player_speed=150):
        self.lock = Lock()
        self.initial_map = map_name(initial_map) if initial_map else None
        self.legacy_fields = legacy_fields
        self.player_speed = player_speed
        self.next_map = None
        self.tcp_bytes = 0
        self.packets_ok = 0
        self.active_server = None
        self.active_port = None
        self.unknown_log_count = 0
        self.aid_to_gid = {}
        self.mobs_damaged_by_me = {}
        self.mobs_damaged_by_others = {}
        self.mobs_engaged_other = {}
        self.other_players = {}
        self.players = {}
        self.entity_names = {}   # aid -> str (nome real do NPC/player)
        self.vending_aids = set()  # aids com loja de venda ativa
        self.patrol_npc_blocked_cells = {}
        self.recent_dead_mobs = []
        self.recent_drop_gids = {}
        self.recent_picked_items = []
        self.recent_seen_drops = []
        self.last_player_move_pkt_time = 0.0
        self.current_map = f"{self.initial_map}.gat" if self.initial_map else None
        self.npc_dialog = {
            "open": False,
            "type": "closed",
            "seq": 0,
            "npc_gid": 0,
            "npc_id": 0,
            "text": "",
            "options": [],
            "last_ts": 0.0,
            "last_update": 0.0,
        }
        self.on_player_move_callback = None
        self.on_map_change_callback = None
        self.on_pickup_ack_callback = None
        self.player_hp = 0
        self.player_max_hp = 0
        self.player_sp = 0
        self.player_max_sp = 0
        self.player_base_level = 1
        self.player_job_level = 1
        self.player_weight = 0
        self.player_max_weight = 1
        self.heatmap_data = {}
        self.heatmap_dirty = False
        self.last_heatmap_save_time = time.monotonic()
        self.current_heatmap_map = None
        self.terrain = None
        # Detector automático de offset PosDir para pacotes 0x09FF/0x09FD deste servidor
        self._posdir_offset = None
        self._mob_debug_count = 0
        try:
            with open("packets_debug.log", "w", encoding="utf-8") as f:
                pass
        except OSError:
            pass
        self.reset()

    def _set_player_track(self, new_track):
        """Preserva atributos de level e peso ao recriar o Track do jogador."""
        if self.player is not None:
            new_track.base_level = getattr(self.player, "base_level", self.player_base_level)
            new_track.job_level = getattr(self.player, "job_level", self.player_job_level)
            new_track.weight = getattr(self.player, "weight", self.player_weight)
            new_track.max_weight = getattr(self.player, "max_weight", self.player_max_weight)
        else:
            new_track.base_level = self.player_base_level
            new_track.job_level = self.player_job_level
            new_track.weight = self.player_weight
            new_track.max_weight = self.player_max_weight
        self.player = new_track

    def _sync_heatmap_map(self):
        clean_map = str(self.map or "").lower().removesuffix(".gat").removesuffix(".rsw").strip()
        if clean_map and clean_map != self.current_heatmap_map:
            if self.heatmap_dirty and self.current_heatmap_map:
                self._save_heatmap(self.current_heatmap_map)
            self.current_heatmap_map = clean_map
            self.heatmap_data = self._load_heatmap(clean_map)
            self.heatmap_dirty = False
            self.last_heatmap_save_time = time.monotonic()

    def _load_heatmap(self, map_name):
        if not map_name:
            return {}
        m = str(map_name).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        candidates = [
            _get_base_dir() / f"heatmap_{m}.json",
            Path(f"heatmap_{m}.json"),
            Path(__file__).parent / f"heatmap_{m}.json",
        ]
        for p in candidates:
            if p.is_file():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    sectors = raw.get("sectors", raw)
                    return {str(k): int(v) for k, v in sectors.items()}
                except Exception:
                    pass
        return {}

    def _save_heatmap(self, map_name):
        if not map_name:
            return
        m = str(map_name).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        p = _get_base_dir() / f"heatmap_{m}.json"
        try:
            payload = {
                "map": m,
                "sectors": dict(self.heatmap_data),
                "total_spawns": sum(self.heatmap_data.values()),
                "updated_at": time.time(),
            }
            p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _maybe_save_heatmap(self, now=None):
        if now is None:
            now = time.monotonic()
        if self.heatmap_dirty and (now - self.last_heatmap_save_time) >= 15.0:
            if self.current_heatmap_map:
                self._save_heatmap(self.current_heatmap_map)
            self.heatmap_dirty = False
            self.last_heatmap_save_time = now

    def get_heatmap_sectors(self) -> dict[tuple[int, int], int]:
        """Retorna setores de calor (sx, sy) -> count."""
        result = {}
        with self.lock:
            for k, v in self.heatmap_data.items():
                if "," in str(k):
                    parts = str(k).split(",")
                    try:
                        result[(int(parts[0]), int(parts[1]))] = int(v)
                    except ValueError:
                        pass
                elif "_" in str(k):
                    parts = str(k).split("_")
                    try:
                        result[(int(parts[0]), int(parts[1]))] = int(v)
                    except ValueError:
                        pass
        return result

    def get_gat(self):
        """Retorna a instância Gat do mapa atual se terrain estiver configurado."""
        if getattr(self, "terrain", None) and getattr(self, "map", None):
            try:
                return self.terrain.for_map(self.map)
            except Exception:
                return None
        return None

    def set_active_connection(self, server, port):
        with self.lock:
            self.active_server = server
            self.active_port = port

    def add_tcp_bytes(self, count):
        with self.lock:
            self.tcp_bytes += count

    def inc_packets_ok(self):
        with self.lock:
            self.packets_ok += 1

    def stats(self):
        with self.lock:
            now = time.monotonic()
            pos = self.player.cell(now) if self.player else None
            mobs = sum(1 for (kind, _) in self.entities.keys() if kind == "mob")
            drops = sum(1 for (kind, _) in self.entities.keys() if kind == "drop")
            npcs = sum(1 for (kind, _) in self.entities.keys() if kind == "npc")
            warps = sum(1 for (kind, _) in self.entities.keys() if kind == "warp")
            b_lvl = getattr(self.player, "base_level", self.player_base_level) if self.player else self.player_base_level
            j_lvl = getattr(self.player, "job_level", self.player_job_level) if self.player else self.player_job_level
            w = getattr(self.player, "weight", self.player_weight) if self.player else self.player_weight
            mw = max(1, getattr(self.player, "max_weight", self.player_max_weight) if self.player else self.player_max_weight)
            w_ratio = getattr(self.player, "weight_ratio", (w / mw)) if self.player else (w / mw)
            return {
                "tcp_bytes": self.tcp_bytes,
                "packets_ok": self.packets_ok,
                "map": self.map,
                "current_map": self.current_map,
                "pos": pos,
                "mobs": mobs,
                "drops": drops,
                "npcs": npcs,
                "warps": warps,
                "hp": self.player_hp,
                "max_hp": self.player_max_hp,
                "sp": self.player_sp,
                "max_sp": self.player_max_sp,
                "base_level": b_lvl,
                "job_level": j_lvl,
                "weight": w,
                "max_weight": mw,
                "weight_ratio": w_ratio,
                "npc_dialog": dict(self.npc_dialog),
                "server": self.active_server,
                "port": self.active_port,
            }

    def _is_player_walking(self):
        if not self.player:
            return False
        now = time.perf_counter()
        return bool(self.player.duration and now < self.player.started + self.player.duration)

    def _log_unknown_packet(self, opcode, data):
        walking = self._is_player_walking()
        data_len = len(data)
        is_walking_candidate = walking and (14 <= data_len <= 28)

        if self.unknown_log_count < 200 or is_walking_candidate:
            if not is_walking_candidate:
                self.unknown_log_count += 1
            hex_sample = data[:32].hex(" ").upper()
            tag = " [ZC_ITEM_ENTRY_CANDIDATE]" if is_walking_candidate else ""
            line = f"[0x{opcode:04X}] len={data_len} hex={hex_sample}{tag}\n"
            try:
                with open("packets_debug.log", "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass

    def reset(self, preserve_transfer=False):
        with self.lock:
            if not preserve_transfer:
                self.next_map = None
            self.map = self.next_map or self.initial_map
            self.current_map = f"{self.map}.gat" if self.map else None
            self.player = None
            self.player_id = None
            self.player_hp = 0
            self.player_max_hp = 0
            self.player_sp = 0
            self.player_max_sp = 0
            self.entities = {}
            self.aid_to_gid.clear()
            self.mobs_damaged_by_me.clear()
            self.mobs_damaged_by_others.clear()
            self.mobs_engaged_other.clear()
            self.other_players.clear()
            self.players.clear()
            self.entity_names.clear()
            self.vending_aids.clear()
            self.patrol_npc_blocked_cells.clear()
            self.recent_dead_mobs.clear()
            self.recent_drop_gids.clear()
            self.recent_picked_items.clear()
            self.recent_seen_drops.clear()
            self.npc_dialog["open"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog["options"] = []
            self.npc_dialog["text"] = ""
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()

    def get_npcs(self):
        with self.lock:
            return [ent for (kind, _), ent in self.entities.items()
                    if kind == "npc" and ent.catalog_id not in (111, 139, 32767)]

    def get_warps(self):
        with self.lock:
            return [ent for (kind, _), ent in self.entities.items() if kind == "warp"]

    def get_mobs(self):
        with self.lock:
            return [ent for (kind, _), ent in self.entities.items() if kind == "mob"]

    def get_drops(self):
        with self.lock:
            return [ent for (kind, _), ent in self.entities.items() if kind == "drop"]

    def get_recent_drop_gids(self):
        with self.lock:
            now = time.monotonic()
            self.recent_drop_gids = {
                gid: ts for gid, ts in self.recent_drop_gids.items()
                if now - ts <= 10.0
            }
            return list(self.recent_drop_gids.keys())

    def get_entity_cell(self, kind, key_str):
        with self.lock:
            try:
                aid = int(key_str.split(":", 1)[1])
                ent = self.entities.get((kind, aid))
                if ent:
                    return ent.track.target
            except Exception:
                pass
            return None

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            # Atualiza posse de drops expirados após 12 segundos (fim da proteção)
            updated = {}
            for key, entity in self.entities.items():
                if entity.kind == "drop" and entity.ownership == "LOCKED":
                    if now - entity.created_at >= 12.0:
                        entity = Entity(
                            entity.kind, entity.catalog_id, entity.track,
                            entity.amount, entity.speed, ownership="FREE",
                            created_at=entity.created_at
                        )
                updated[key] = entity
            self.entities = updated
            return self.map, self.player, tuple(self.entities.items())

    def consume(self, opcode, data):
        if opcode not in KNOWN_SYNC_OPCODES:
            self._log_unknown_packet(opcode, data)
        try:
            with self.lock:
                self._apply(opcode, data)
                self._maybe_save_heatmap()
        except (struct.error, IndexError, UnicodeError) as exc:
            raise ValueError(f"Layout inválido para 0x{opcode:04X}.") from exc

    def _classify_drop(self, cell):
        now = time.monotonic()
        dx, dy = cell

        # 1. Checa proximidade com mob morto recentemente (< 15s e dist <= 2)
        for dead in reversed(self.recent_dead_mobs):
            if now - dead["time"] < 15.0:
                mx, my = dead["cell"]
                if max(abs(dx - mx), abs(dy - my)) <= 2:
                    # Só bloqueia se confirmado que outro jogador atacou E eu não ataquei
                    if dead["by_others"] and not dead["by_me"]:
                        return "LOCKED"
                    if dead["by_me"]:
                        return "MINE"

        # 2. Checa proximidade de outros jogadores conhecidos (BL_PC)
        px, py = self.player.cell(now) if self.player else (None, None)
        dist_player = max(abs(dx - px), abs(dy - py)) if px is not None else 999

        for p_gid, (p_cell, p_time) in self.other_players.items():
            if self.player_id is not None and p_gid == self.player_id:
                continue
            if now - p_time < 10.0:
                p_dist = max(abs(dx - p_cell[0]), abs(dy - p_cell[1]))
                # Só bloqueia se outro jogador conhecido estava mais próximo que o player e colado no drop (dist <= 2)
                if p_dist <= 2 and p_dist < dist_player:
                    return "LOCKED"

        return "MINE"

    def mark_drop_locked(self, itaid):
        with self.lock:
            now = time.monotonic()
            key = ("drop", itaid)
            if key in self.entities:
                ent = self.entities[key]
                self.entities[key] = Entity(
                    ent.kind, ent.catalog_id, ent.track, ent.amount, ent.speed,
                    ownership="LOCKED", created_at=now
                )

    def mark_nearest_drop_locked(self):
        if not self.player:
            return
        now = time.monotonic()
        px, py = self.player.cell(now)
        nearest_key = None
        min_dist = 999
        for key, ent in self.entities.items():
            if ent.kind == "drop":
                dx, dy = ent.track.target
                dist = max(abs(dx - px), abs(dy - py))
                if dist < min_dist and dist <= 5:
                    min_dist = dist
                    nearest_key = key
        if nearest_key:
            ent = self.entities[nearest_key]
            # Marca como LOCKED com tempo ajustado para durar 10s adicionais
            self.entities[nearest_key] = Entity(
                ent.kind, ent.catalog_id, ent.track, ent.amount, ent.speed,
                ownership="LOCKED", created_at=now - 2.0
            )

    def _apply(self, opcode, data):
        if opcode == 0x0283:
            self.player_id = u32(data, 2)
        elif opcode in ACCEPT:
            self.entities.clear()
            self.npc_dialog["open"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            if len(data) >= 9:
                self._set_player_track(Track.stationary(position(data, 6)))
            self.next_map = None
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
        elif opcode == 0x0091:
            raw_name = extract_map_from_packet(data, 2)
            name = map_name(raw_name)
            prev_map = self.map
            prev_pos = self.player.cell() if self.player else None
            if prev_map and name and prev_map != name and prev_pos:
                try:
                    from rathena_terrain import save_learned_warp
                    save_learned_warp(prev_map, int(round(prev_pos[0])), int(round(prev_pos[1])), name)
                except Exception:
                    pass
            self.map = name
            self.current_map = f"{name}.gat" if name else None
            self.entities.clear()
            self.npc_dialog["open"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
            if len(data) >= 22:
                x = u16(data, 18)
                y = u16(data, 20)
                self._set_player_track(Track.stationary((x, y)))
        elif opcode in MAP_TRANSFER:
            raw_name = extract_map_from_packet(data, 2)
            name = map_name(raw_name)
            prev_map = self.map
            prev_pos = self.player.cell() if self.player else None
            if prev_map and name and prev_map != name and prev_pos:
                try:
                    from rathena_terrain import save_learned_warp
                    save_learned_warp(prev_map, int(round(prev_pos[0])), int(round(prev_pos[1])), name)
                except Exception:
                    pass
            self.map = name
            self.current_map = f"{name}.gat" if name else None
            self.entities.clear()
            self.npc_dialog["open"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
            if len(data) >= 22:
                x = u16(data, 18)
                y = u16(data, 20)
                self._set_player_track(Track.stationary((x, y)))
            self.next_map = name
        elif opcode == 0x00B4:  # ZC_SAY_DIALOG (texto NPC)
            plen = struct.unpack_from("<H", data, 2)[0] if len(data) >= 4 else len(data)
            npc_gid = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0
            raw_text = data[8:plen] if plen > 8 else data[8:]
            raw_text = raw_text.split(b"\x00", 1)[0]
            try:
                txt = raw_text.decode("cp1252")
            except Exception:
                txt = raw_text.decode("latin1", errors="replace")
            txt_clean = re.sub(r"\^[0-9A-Fa-f]{6}", "", txt).strip()

            curr_text = self.npc_dialog.get("text", "")
            if self.npc_dialog.get("open") and self.npc_dialog.get("npc_gid") == npc_gid and curr_text:
                accum_text = f"{curr_text}\n{txt_clean}" if txt_clean else curr_text
            else:
                accum_text = txt_clean

            now_perf = time.perf_counter()
            self.npc_dialog["open"] = True
            self.npc_dialog["type"] = "say"
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["npc_gid"] = npc_gid
            self.npc_dialog["npc_id"] = npc_gid
            self.npc_dialog["text"] = accum_text
            self.npc_dialog["last_ts"] = now_perf
            self.npc_dialog["last_update"] = time.monotonic()
        elif opcode == 0x00B5:  # ZC_WAIT_DIALOG (next)
            npc_gid = struct.unpack_from("<I", data, 2)[0] if len(data) >= 6 else 0
            now_perf = time.perf_counter()
            self.npc_dialog["open"] = True
            self.npc_dialog["type"] = "wait"
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["npc_gid"] = npc_gid
            self.npc_dialog["npc_id"] = npc_gid
            self.npc_dialog["last_ts"] = now_perf
            self.npc_dialog["last_update"] = time.monotonic()
        elif opcode == 0x00B6:  # ZC_CLOSE_DIALOG (close)
            npc_gid = struct.unpack_from("<I", data, 2)[0] if len(data) >= 6 else 0
            now_perf = time.perf_counter()
            self.npc_dialog["open"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["npc_gid"] = npc_gid
            self.npc_dialog["npc_id"] = npc_gid
            self.npc_dialog["options"] = []
            self.npc_dialog["last_ts"] = now_perf
            self.npc_dialog["last_update"] = time.monotonic()
        elif opcode == 0x00B7:  # ZC_MENU_LIST (menu NPC)
            plen = struct.unpack_from("<H", data, 2)[0] if len(data) >= 4 else len(data)
            npc_gid = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0
            raw_menu = data[8:plen] if plen > 8 else data[8:]
            raw_menu = raw_menu.split(b"\x00", 1)[0]
            try:
                menu_str = raw_menu.decode("cp1252")
            except Exception:
                menu_str = raw_menu.decode("latin1", errors="replace")
            raw_options = menu_str.split(":")
            clean_options = []
            for opt in raw_options:
                opt_c = re.sub(r"\^[0-9A-Fa-f]{6}", "", opt).strip()
                if opt_c:
                    clean_options.append(opt_c)
            now_perf = time.perf_counter()
            self.npc_dialog["open"] = True
            self.npc_dialog["type"] = "menu"
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["npc_gid"] = npc_gid
            self.npc_dialog["npc_id"] = npc_gid
            self.npc_dialog["options"] = clean_options
            self.npc_dialog["last_ts"] = now_perf
            self.npc_dialog["last_update"] = time.monotonic()
        elif opcode == 0x0087:
            self.last_player_move_pkt_time = time.monotonic()
            if len(data) >= 12:
                b = data[6:12]
                x0 = (b[0] << 2) | (b[1] >> 6)
                y0 = ((b[1] & 0x3F) << 4) | (b[2] >> 4)
                x1 = ((b[2] & 0x0F) << 6) | (b[3] >> 2)
                y1 = ((b[3] & 0x03) << 8) | b[4]
                cells = ((x0, y0), (x1, y1))
                now = time.perf_counter()
                self._set_player_track(Track.walking(cells, self.player_speed, now, gat=self.get_gat()))
                if self.on_player_move_callback is not None:
                    try:
                        self.on_player_move_callback(cells[1][0], cells[1][1])
                    except Exception:
                        pass
        elif opcode in (0x0088, 0x01FF):  # ZC_STOPMOVE / ZC_HIGHJUMP
            if len(data) >= 10:
                aid = u32(data, 2)
                x, y = struct.unpack_from("<HH", data, 6)
                target_id = self.aid_to_gid.get(aid, aid)
                stopped = Track.stationary((x, y))

                # Se for jogador ou se player_id ainda não foi associado e não for monstro/NPC conhecido
                is_known_entity = any(
                    (k, target_id) in self.entities or (k, aid) in self.entities
                    for k in ("mob", "npc", "warp")
                )
                if aid == self.player_id or (self.player_id is None and not is_known_entity):
                    self._set_player_track(stopped)
                    if self.on_player_move_callback is not None:
                        try:
                            self.on_player_move_callback(x, y)
                        except Exception:
                            pass

                # Atualiza imediatamente o Track ativo da entidade (mob, npc, warp)
                for k in ("mob", "npc", "warp"):
                    ent = self.entities.get((k, target_id)) or self.entities.get((k, aid))
                    if ent:
                        self.entities[k, target_id] = Entity(
                            ent.kind, ent.catalog_id, stopped, amount=ent.amount,
                            speed=ent.speed, ownership=ent.ownership, created_at=ent.created_at,
                            is_aggro=ent.is_aggro, last_aggro_ts=ent.last_aggro_ts,
                            is_hostile_now=ent.is_hostile_now,
                            consecutive_approaches=ent.consecutive_approaches,
                            last_player_dist=ent.last_player_dist
                        )
                        break

                if aid in self.other_players:
                    self.other_players[aid] = ((x, y), time.monotonic())
                if target_id in self.other_players:
                    self.other_players[target_id] = ((x, y), time.monotonic())
        elif opcode == 0x00B0:  # ZC_PAR_CHANGE
            if len(data) >= 8:
                var_id, value = struct.unpack_from("<HI", data, 2)
                if var_id == 0 and 50 <= value <= 1000:
                    self.player_speed = float(value)
                    if self.player is not None:
                        self.player.speed_ms = float(value)
                elif var_id == 5:
                    self.player_hp = int(value)
                elif var_id == 6:
                    self.player_max_hp = int(value)
                elif var_id == 7:
                    self.player_sp = int(value)
                elif var_id == 8:
                    self.player_max_sp = int(value)
                elif var_id == 11:
                    val = int(value)
                    self.player_base_level = val
                    if self.player is None:
                        self.player = Track.stationary((0, 0))
                    self.player.base_level = val
                elif var_id == 55:
                    val = int(value)
                    self.player_job_level = val
                    if self.player is None:
                        self.player = Track.stationary((0, 0))
                    self.player.job_level = val
                elif var_id == 24:
                    val = int(value)
                    self.player_weight = val
                    if self.player is None:
                        self.player = Track.stationary((0, 0))
                    self.player.weight = val
                elif var_id == 25:
                    val = max(1, int(value))
                    self.player_max_weight = val
                    if self.player is None:
                        self.player = Track.stationary((0, 0))
                    self.player.max_weight = val
        elif opcode == 0x0086:
            if len(data) >= 11:
                aid = u32(data, 2)
                target_id = self.aid_to_gid.get(aid, aid)
                if mob := (self.entities.get(("mob", target_id)) or self.entities.get(("mob", aid))):
                    track = Track.walking(movement(data, 6), mob.speed, time.perf_counter() - 0.035, gat=self.get_gat())
                    self.entities["mob", target_id] = Entity(
                        "mob", mob.catalog_id, track, speed=mob.speed,
                        ownership=mob.ownership, created_at=mob.created_at,
                        is_aggro=mob.is_aggro, last_aggro_ts=mob.last_aggro_ts,
                        is_hostile_now=mob.is_hostile_now,
                        consecutive_approaches=mob.consecutive_approaches,
                        last_player_dist=mob.last_player_dist
                    )
        elif opcode in (0x008A, 0x08C8, 0x02E1, 0x01DE, 0x0114):  # ZC_NOTIFY_ACT / ZC_NOTIFY_SKILL (ataques/dano/skills)
            now = time.monotonic()
            src_id = None
            dst_id = None
            if opcode in (0x01DE, 0x0114):
                if len(data) >= 12:
                    src_id = u32(data, 4)
                    dst_id = u32(data, 8)
            else:
                if len(data) >= 10:
                    src_id = u32(data, 2)
                    dst_id = u32(data, 6)

            if src_id is not None and dst_id is not None:
                real_dst = self.aid_to_gid.get(dst_id, dst_id)
                real_src = self.aid_to_gid.get(src_id, src_id)

                if self.player_id is None:
                    if src_id not in self.other_players and (("mob", real_dst) in self.entities or ("mob", dst_id) in self.entities):
                        self.player_id = src_id

                # Se o próprio jogador atacou
                if self.player_id is not None and (src_id == self.player_id or real_src == self.player_id):
                    self.mobs_damaged_by_me[real_dst] = now
                    self.mobs_damaged_by_me[dst_id] = now
                else:
                    is_other_src = (src_id in self.other_players or real_src in self.other_players) and (src_id != self.player_id and real_src != self.player_id)
                    is_other_dst = (dst_id in self.other_players or real_dst in self.other_players) and (dst_id != self.player_id and real_dst != self.player_id)
                    is_mob_dst = ("mob", real_dst) in self.entities or ("mob", dst_id) in self.entities
                    is_mob_src = ("mob", real_src) in self.entities or ("mob", src_id) in self.entities

                    # Caso 1: Outro jogador batendo em um monstro
                    if is_other_src and is_mob_dst:
                        m_gid = real_dst if ("mob", real_dst) in self.entities else dst_id
                        self.mobs_damaged_by_others[m_gid] = now
                        self.mobs_engaged_other[m_gid] = now + 8.0
                        mob_ent = self.entities.get(("mob", m_gid))
                        if mob_ent is not None:
                            try:
                                mob_ent.engaged_other_until = now + 8.0
                            except Exception:
                                pass

                    # Caso 2: Monstro atacando outro jogador
                    elif is_mob_src and is_other_dst:
                        m_gid = real_src if ("mob", real_src) in self.entities else src_id
                        self.mobs_damaged_by_others[m_gid] = now
                        self.mobs_engaged_other[m_gid] = now + 8.0
                        mob_ent = self.entities.get(("mob", m_gid))
                        if mob_ent is not None:
                            try:
                                mob_ent.engaged_other_until = now + 8.0
                            except Exception:
                                pass

                # Marca aggro se o alvo for o jogador (target_aid == player.aid / player_id)
                p_aid = getattr(self.player, "aid", None) or self.player_id
                if (p_aid and (dst_id == p_aid or real_dst == p_aid)) or (self.player_id and (dst_id == self.player_id or real_dst == self.player_id)):
                    mob_ent = self.entities.get(("mob", src_id)) or self.entities.get(("mob", self.aid_to_gid.get(src_id, src_id)))
                    if mob_ent is not None:
                        try:
                            mob_ent.is_aggro = True
                            mob_ent.is_hostile_now = True
                            mob_ent.last_aggro_ts = now
                        except Exception:
                            pass
        elif opcode in (0x008E, 0x0295, 0x09DF):  # Chat / mensagens do servidor
            try:
                text = data[2:].decode("cp1252", errors="ignore").lower()
                triggers = ("não pode", "outro jogador", "cannot pick", "someone else", "pertence", "bloqueado")
                if any(w in text for w in triggers):
                    self.mark_nearest_drop_locked()
            except Exception:
                pass
        elif opcode == 0x0095:  # ZC_ACK_REQNAME (30 bytes: opcode u16 + aid u32 + name 24s)
            if len(data) >= 30:
                aid_name = u32(data, 2)
                raw = data[6:30].split(b"\x00", 1)[0]
                try:
                    name_str = raw.decode("cp1252")
                except Exception:
                    name_str = raw.decode("latin1", errors="replace")
                if name_str:
                    self.entity_names[aid_name] = name_str
        elif opcode == 0x0195:  # ZC_ACK_REQNAMEALL (102 bytes)
            if len(data) >= 30:
                aid_name = u32(data, 2)
                raw = data[6:30].split(b"\x00", 1)[0]
                try:
                    name_str = raw.decode("cp1252")
                except Exception:
                    name_str = raw.decode("latin1", errors="replace")
                if name_str:
                    self.entity_names[aid_name] = name_str
        elif opcode == 0x0136:  # ZC_SHOW_VENDING_BOARD
            if len(data) >= 6:
                self.vending_aids.add(u32(data, 2))
        elif opcode == 0x0137:  # ZC_CLOSE_VENDING_BOARD
            if len(data) >= 6:
                self.vending_aids.discard(u32(data, 2))
        elif opcode == 0x00D7:  # ZC_SHOW_CHAT_ROOM
            if len(data) >= 8:
                self.vending_aids.add(u32(data, 4))
        elif opcode == 0x00D8:  # ZC_CLOSE_CHAT_ROOM
            if len(data) >= 6:
                self.vending_aids.discard(u32(data, 2))
        elif opcode in (0x0080, 0x00A1):
            if len(data) >= 6:
                gid = u32(data, 2)
                kind = "mob" if opcode == 0x0080 else "drop"
                real_gid = self.aid_to_gid.get(gid, gid)
                if opcode == 0x0080:
                    vanish_type = data[6] if len(data) >= 7 else 0
                    mob = self.entities.get(("mob", real_gid)) or self.entities.get(("mob", gid))
                    if mob and vanish_type == 1:  # 1 = DEAD
                        now = time.monotonic()
                        mob_cell = mob.track.cell(now)
                        by_me = (gid in self.mobs_damaged_by_me or real_gid in self.mobs_damaged_by_me) and (
                            now - self.mobs_damaged_by_me.get(real_gid, self.mobs_damaged_by_me.get(gid, 0)) < 15.0
                        )
                        by_others = (gid in self.mobs_damaged_by_others or real_gid in self.mobs_damaged_by_others) and (
                            now - self.mobs_damaged_by_others.get(real_gid, self.mobs_damaged_by_others.get(gid, 0)) < 15.0
                        )
                        self.recent_dead_mobs.append({
                            "gid": real_gid,
                            "catalog_id": mob.catalog_id if mob else 0,
                            "cell": mob_cell,
                            "by_me": by_me,
                            "by_others": by_others,
                            "time": now,
                            "kill_ts": time.strftime("%H:%M:%S"),  # ← adicionar este campo
                        })
                        if len(self.recent_dead_mobs) > 40:
                            self.recent_dead_mobs.pop(0)
                    self.other_players.pop(gid, None)
                    self.other_players.pop(real_gid, None)
                    self.players.pop(gid, None)
                    self.players.pop(real_gid, None)
                self.vending_aids.discard(gid)
                self.vending_aids.discard(real_gid)
                for k in ("mob", "npc", "warp", "drop"):
                    self.entities.pop((k, real_gid), None)
                    self.entities.pop((k, gid), None)
        elif opcode == 0x00A0:  # ZC_ITEM_PICKUP_ACK
            if len(data) >= 8:
                count = struct.unpack_from("<H", data, 4)[0]
                fail = data[-1]
                # Suporte a nameid de 4 bytes (clientes mais novos) ou 2 bytes (legado)
                if len(data) >= 25:
                    nameid4 = struct.unpack_from("<I", data, 6)[0]
                    nameid = nameid4 if 1 <= nameid4 <= 100000 else struct.unpack_from("<H", data, 6)[0]
                else:
                    nameid = struct.unpack_from("<H", data, 6)[0]
                now = time.monotonic()
                self.recent_picked_items.append({
                    "item_id": nameid,
                    "count": max(1, count),
                    "fail": fail,
                    "time": now,
                })
                if len(self.recent_picked_items) > 40:
                    self.recent_picked_items.pop(0)
                if self.on_pickup_ack_callback is not None:
                    try:
                        self.on_pickup_ack_callback(nameid, max(1, count), fail)
                    except Exception:
                        pass
        elif opcode == 0x011C:  # ZC_WARPLIST (teleportes/destinos)
            if len(data) >= 4:
                map_names = []
                for offset in range(4, min(len(data), 68), 16):
                    chunk = data[offset:offset + 16].split(b"\x00", 1)[0]
                    m_name = chunk.decode("ascii", errors="replace").strip()
                    if m_name:
                        map_names.append(m_name)
                self.npc_dialog = {
                    "open": True,
                    "npc_id": 0,
                    "type": "warplist",
                    "text": "",
                    "options": map_names,
                    "last_update": time.monotonic(),
                }
        elif opcode in FLOOR:
            self._drop(opcode, data)
        elif opcode in UNIT:
            self._unit(opcode, data)

    def _drop(self, opcode, data):
        now = time.monotonic()
        aid = None
        item = None
        x = None
        y = None
        cnt = 1

        if opcode == 0x0ADD:
            # Layout ZC_ITEM_FALL_ENTRY moderno (rAthena Renewal, 24 bytes):
            #  0: opcode(2) | 2: ITAID(u32) | 6: ITID(u32) | 10: type(u16)
            # 12: IsIdentified(B) | 13: x(u16) | 15: y(u16) | 17: subX(B) | 18: subY(B) | 19: count(u16)
            aid  = u32(data, 2) if len(data) >= 6 else None
            item = u32(data, 6) if len(data) >= 10 else None
            cnt  = u16(data, 19) if len(data) >= 21 else 1
            for off_x in (13, 11, 9):
                if len(data) >= off_x + 4:
                    cx = u16(data, off_x)
                    cy = u16(data, off_x + 2)
                    if 0 < cx < 2048 and 0 < cy < 2048:
                        x, y = cx, cy
                        break

        elif opcode in (0x0B14, 0x0B15):
            aid  = u32(data, 2) if len(data) >= 6 else None
            item = u32(data, 6) if len(data) >= 10 else None
            cnt  = 1
            for off_x in (13, 11, 9):
                if len(data) >= off_x + 4:
                    cx = u16(data, off_x)
                    cy = u16(data, off_x + 2)
                    if 0 < cx < 2048 and 0 < cy < 2048:
                        x, y = cx, cy
                        cnt = u16(data, off_x + 4) if len(data) >= off_x + 6 else 1
                        break

        elif opcode in (0x084A, 0x084B):
            aid  = u32(data, 2) if len(data) >= 6 else None
            item = u32(data, 6) if (opcode == 0x084A and len(data) >= 19) else (u16(data, 6) if len(data) >= 8 else None)
            cnt  = u16(data, 17) if (opcode == 0x084B and len(data) >= 19) else 1
            cand_offsets = (10, 9, 11) if opcode == 0x084B else (9, 10, 11)
            for off_x in cand_offsets:
                if len(data) >= off_x + 4:
                    cx = u16(data, off_x)
                    cy = u16(data, off_x + 2)
                    if 0 < cx < 2048 and 0 < cy < 2048:
                        x, y = cx, cy
                        break

        elif opcode in (0x009D, 0x009E):
            # 0x009D legacy (17 bytes) e moderno (19 bytes com ITID u32)
            aid  = u32(data, 2) if len(data) >= 6 else None
            item = u32(data, 6) if len(data) >= 19 else (u16(data, 6) if len(data) >= 8 else None)
            cnt  = u16(data, 15) if len(data) >= 17 else 1
            cand_offsets = (11, 9, 13) if len(data) >= 19 else (9, 11, 13)
            for off_x in cand_offsets:
                if len(data) >= off_x + 4:
                    cx = u16(data, off_x)
                    cy = u16(data, off_x + 2)
                    if 0 < cx < 2048 and 0 < cy < 2048:
                        x, y = cx, cy
                        break

        if aid is not None:
            self.recent_drop_gids[aid] = now

        if aid is not None and item is not None and x is not None and y is not None:
            px, py = self.player.cell(now) if self.player else (None, None)
            dist = max(abs(x - px), abs(y - py)) if px is not None else 0
            if 0 < x < 2048 and 0 < y < 2048 and item and (px is None or dist <= 60):
                cell = (x, y)
                ownership = self._classify_drop(cell)
                self.entities["drop", aid] = Entity(
                    "drop", item, Track.stationary(cell), max(1, cnt),
                    ownership=ownership, created_at=now
                )
                self.recent_seen_drops.append({
                    "aid": aid,
                    "item_id": item,
                    "count": max(1, cnt),
                    "cell": cell,
                    "time": now,
                })
                if len(self.recent_seen_drops) > 40:
                    self.recent_seen_drops.pop(0)

    def _unit(self, opcode, data):
        if len(data) < 5:
            return
        is_legacy = opcode in (0x0078, 0x007B)
        walking = UNIT[opcode] == "walk"
        has_aid = opcode >= 0x0900
        wide = opcode in (0x09FD, 0x09FE, 0x09FF, 0x0AFD, 0x0AFE, 0x0AFF) and not self.legacy_fields

        if is_legacy:
            if len(data) >= 55:
                if data[2] in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0x0D, 0x0E):
                    obj_type = data[2]
                    gid = u32(data, 3)
                    speed = max(1, u16(data, 7))
                    job = u16(data, 15)
                    pos_off = 48 if walking else 46
                else:
                    obj_type = data[4] if len(data) > 4 else 1
                    gid = u32(data, 2)
                    speed = max(1, u16(data, 6))
                    job = u16(data, 14)
                    pos_off = 48 if walking else 46
                aid = gid
                entity_id = gid
                effective_offset = pos_off
            else:
                return
        else:
            obj_type = data[4]
            # Offset PosDir tabelado conforme opcode do protocolo rAthena/Hercules:
            # 0x09FF (90b idle) / 0x09FD (96b walk): offset 63 (PosDir base)
            if opcode in (0x09FD, 0x09FE, 0x09FF, 0x0AFD, 0x0AFE, 0x0AFF, 0x09DB, 0x09DC, 0x09DD):
                effective_offset = 63
            elif opcode in (0x0914, 0x090F, 0x0915):
                effective_offset = 59
            elif opcode in (0x0856, 0x0857, 0x0858, 0x07F7, 0x07F8, 0x07F9):
                effective_offset = 55
            elif opcode in (0x02EC, 0x02ED, 0x02EE):
                effective_offset = 53
            else:
                effective_offset = 53 + 4 * has_aid + 4 * wide + 2 * (opcode >= 0x07F7)

            speed = max(1, u16(data, 9 + 4 * has_aid)) if len(data) >= 11 + 4 * has_aid else 150
            job = u16(data, 19 + 4 * has_aid) if len(data) >= 21 + 4 * has_aid else 1002

            aid = u32(data, 5) if len(data) >= 9 else 0
            gid = u32(data, 9) if len(data) >= 13 else 0
            entity_id = aid if aid > 0 else gid

        gat = self.get_gat()
        map_w = getattr(gat, "width", 1024) or 1024
        map_h = getattr(gat, "height", 1024) or 1024

        def _is_valid_coord(cx, cy):
            return 0 < cx < map_w and 0 < cy < map_h

        # Rastreia outros jogadores (BL_PC = 0)
        if obj_type == 0:
            if len(data) >= 9:
                p_aid = aid if not is_legacy else gid
                p_gid = gid if len(data) >= 13 else p_aid
                p_id = p_gid if p_gid >= 100000 else p_aid
                self.aid_to_gid[p_aid] = p_id
                if self.player_id is None or p_id != self.player_id:
                    p_cell = None
                    p_cands = [effective_offset, 63, 59, 55, 53, 46]
                    for p_off in p_cands:
                        if walking and len(data) >= p_off + 7:
                            try:
                                _, (p_dx, p_dy) = movement(data, p_off + 4)
                                if _is_valid_coord(p_dx, p_dy):
                                    p_cell = (p_dx, p_dy)
                                    break
                            except Exception:
                                pass
                        elif not walking and len(data) >= p_off + 3:
                            try:
                                p_cx, p_cy = position(data, p_off)
                                if _is_valid_coord(p_cx, p_cy):
                                    p_cell = (p_cx, p_cy)
                                    break
                            except Exception:
                                pass
                    if p_cell:
                        self.other_players[p_id] = (p_cell, time.monotonic())
                        self.other_players[p_aid] = (p_cell, time.monotonic())
                        self.players[p_id] = (p_cell, time.monotonic())
            return

        # Ignora Pet, Homunculus, Mercenary, Elemental como NPC
        if obj_type in (7, 8, 9, 10):
            return

        track = None
        candidates = [effective_offset, 63, 59, 55, 53, 46] if not is_legacy else [effective_offset]
        seen_offs = set()
        unique_candidates = [off for off in candidates if not (off in seen_offs or seen_offs.add(off))]

        for off in unique_candidates:
            if walking:
                move_off = off + 4
                if len(data) >= move_off + 5:
                    try:
                        (sx, sy), (dx, dy) = movement(data, move_off)
                        if _is_valid_coord(dx, dy) and _is_valid_coord(sx, sy):
                            effective_offset = off
                            track = Track.walking(((sx, sy), (dx, dy)), speed, time.perf_counter() - 0.035, gat=gat)
                            break
                    except Exception:
                        pass
            else:
                if len(data) >= off + 3:
                    try:
                        cx, cy = position(data, off)
                        if _is_valid_coord(cx, cy):
                            effective_offset = off
                            track = Track.stationary((cx, cy), speed_ms=speed)
                            break
                    except Exception:
                        pass

        if not track:
            return

        # Classificação da entidade:
        # - job == 45: portal de teleporte / warp
        # - obj_type == 1 ou IDs de NPC: npc
        # - obj_type in (0x05, 0x0D, 0x0E): mob
        if job == 45:
            kind = "warp"
        elif obj_type == 1 or (not (0x05 <= obj_type <= 0x0E) and (50000 <= entity_id or job < 1000)):
            kind = "npc"
        elif obj_type in (0x05, 0x0D, 0x0E):
            kind = "mob"
        else:
            kind = "npc"

        if aid and entity_id:
            self.aid_to_gid[aid] = entity_id
        if gid and gid != aid and entity_id:
            self.aid_to_gid[gid] = entity_id

        if entity_id:
            for other_kind in ("mob", "npc", "warp"):
                if other_kind != kind:
                    self.entities.pop((other_kind, entity_id), None)

            old_ent = self.entities.get((kind, entity_id))
            is_aggro = old_ent.is_aggro if old_ent else False
            last_aggro_ts = old_ent.last_aggro_ts if old_ent else 0.0
            is_hostile_now = old_ent.is_hostile_now if old_ent else False
            consecutive_approaches = old_ent.consecutive_approaches if old_ent else 0
            last_player_dist = old_ent.last_player_dist if old_ent else 999.0

            if kind == "mob":
                now_m = time.monotonic()
                px_m, py_m = self.player.cell(now_m) if self.player else (None, None)
                cell_m = track.target
                if px_m is not None:
                    cur_dist = math.hypot(cell_m[0] - px_m, cell_m[1] - py_m)
                    try:
                        from rathena_terrain import get_mob_catalog
                        cat_entry = get_mob_catalog().get("by_id", {}).get(job)
                    except Exception:
                        cat_entry = None
                    is_immobile = cat_entry.get("immobile", False) if cat_entry else False

                    # Camada 3: Vetor de aproximação (monstro não-imóvel reduzindo distância >= 2 passos consecutivos)
                    if not is_immobile:
                        if last_player_dist < 900.0:
                            diff = last_player_dist - cur_dist
                            if diff >= 0.35:
                                consecutive_approaches += 1
                                if consecutive_approaches >= 2:
                                    is_hostile_now = True
                            elif diff <= -0.5:
                                consecutive_approaches = 0
                        last_player_dist = cur_dist

                    # Mapa de Calor de Spawns: surgimento via 0x09FF a < 13 células do jogador
                    if opcode == 0x09FF and cur_dist < 13.0:
                        self._sync_heatmap_map()
                        sec_key = f"{int(cell_m[0]) // 16},{int(cell_m[1]) // 16}"
                        self.heatmap_data[sec_key] = self.heatmap_data.get(sec_key, 0) + 1
                        self.heatmap_dirty = True

            self.entities[kind, entity_id] = Entity(
                kind, job, track, speed=speed,
                is_aggro=is_aggro, last_aggro_ts=last_aggro_ts,
                is_hostile_now=is_hostile_now,
                consecutive_approaches=consecutive_approaches,
                last_player_dist=last_player_dist
            )

            # Log [MobDebug] dos primeiros 3 mobs para confirmar detecção e offset
            if kind == "mob" and self._mob_debug_count < 3:
                self._mob_debug_count += 1
                try:
                    cell = track.target
                    now = time.monotonic()
                    px, py = self.player.cell(now) if self.player else (None, None)
                    dist = max(abs(cell[0] - px), abs(cell[1] - py)) if px is not None else -1
                    print(
                        f"[MobDebug #{self._mob_debug_count}] entity_id={entity_id} "
                        f"job={job} x={int(cell[0])} y={int(cell[1])} dist_jogador={dist:.1f} "
                        f"(aid={aid}, gid={gid}, offset={effective_offset}, op=0x{opcode:04X}, len={len(data)})",
                        flush=True,
                    )
                except Exception:
                    pass


def scapy_capture():
    try:
        from scapy.all import IP, IPv6, TCP, conf, sniff
    except ImportError as exc:
        raise RuntimeError("Instale as dependências de requirements-live.txt.") from exc
    if not conf.use_pcap:
        raise RuntimeError("Instale o Npcap para captura passiva, incluindo loopback.")
    return IP, IPv6, TCP, conf, sniff


def list_interfaces():
    _, _, _, conf, _ = scapy_capture()
    conf.ifaces.show()


class ConnectionTarget:
    def __init__(self, server, port, local_port=None):
        self.lock = Lock()
        self.server = str(server)
        self.port = int(port)
        self.local_port = int(local_port) if local_port else None
        self.last_packet_time = time.monotonic()
        self.changed = Event()

    def update(self, server, port, local_port=None):
        with self.lock:
            s_str = str(server)
            p_int = int(port)
            lp_int = int(local_port) if local_port else None
            if (self.server, self.port) != (s_str, p_int):
                print(
                    f"\n[Live] Transição de map-server detectada: {self.server}:{self.port} -> {s_str}:{p_int}",
                    flush=True,
                )
                self.server = s_str
                self.port = p_int
                self.local_port = lp_int
                self.last_packet_time = time.monotonic()
                self.changed.set()
            elif lp_int and self.local_port != lp_int:
                self.local_port = lp_int
                self.changed.set()

    def touch(self):
        self.last_packet_time = time.monotonic()

    def get(self):
        with self.lock:
            return self.server, self.port, self.local_port


def monitor_ragexe_connections(target, ragexe_pid, stop):
    """Monitora conexões TCP ESTABLISHED do Ragexe.exe para atualizar map-server dinamicamente."""
    import psutil
    target_proc = None

    while not stop.is_set():
        try:
            if not target_proc or not target_proc.is_running():
                for p in psutil.process_iter(["pid", "name"]):
                    try:
                        if (ragexe_pid and p.pid == ragexe_pid) or (p.info["name"] and p.info["name"].lower() == "ragexe.exe"):
                            target_proc = p
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

            if target_proc and target_proc.is_running():
                conns = target_proc.net_connections(kind="tcp")
                est_conns = [c for c in conns if c.status == psutil.CONN_ESTABLISHED and c.raddr]
                map_conns = [c for c in est_conns if c.raddr.port not in (6900, 6121, 443)]

                if map_conns:
                    curr_srv, curr_pt, _ = target.get()
                    matching = [c for c in map_conns if c.raddr.port == curr_pt and c.raddr.ip == curr_srv]
                    now = time.monotonic()
                    # Se a conexão atual encerrou ou está sem tráfego recente e existe outra ativa
                    if not matching:
                        target.update(map_conns[0].raddr.ip, map_conns[0].raddr.port, map_conns[0].laddr.port)
                    elif len(map_conns) > 1 and (now - target.last_packet_time > 2.5):
                        alt = [c for c in map_conns if c != matching[0]][0]
                        target.update(alt.raddr.ip, alt.raddr.port, alt.laddr.port)
        except Exception:
            pass

        stop.wait(1.0)


def capture(world, lengths, interface, server, port, local_port, stop, ready, ragexe_pid=None):
    IP, IPv6, TCP, conf, sniff = scapy_capture()
    target = ConnectionTarget(server, port, local_port)

    # Inicia monitor leve em background quando o PID do jogo estiver disponível
    if ragexe_pid:
        Thread(
            target=monitor_ragexe_connections,
            args=(target, ragexe_pid, stop),
            daemon=True,
        ).start()

    diag_count = 0

    while not stop.is_set():
        current_server, current_port, current_local = target.get()
        world.set_active_connection(current_server, current_port)
        packet_filter = f"tcp and host {current_server} and port {current_port}"
        if current_local:
            packet_filter += f" and port {current_local}"

        target.changed.clear()
        active = None
        stream = None

        def receive(packet):
            nonlocal active, stream, diag_count
            ip = packet.getlayer(IP) or packet.getlayer(IPv6)
            tcp = packet.getlayer(TCP)
            if ip is None or tcp is None:
                return
            flags = int(tcp.flags)
            if ip.src != current_server or tcp.sport != current_port:
                if (ip.dst, tcp.dport) == (current_server, current_port) and active == (ip.src, tcp.sport) and flags & 0x05:
                    active = stream = None
                    world.reset(preserve_transfer=True)
                return
            peer = ip.dst, tcp.dport

            # Inicializa o stream tanto em nova conexão (SYN) quanto em conexão já ativa
            if active is None:
                active = peer
                frames = PacketStream(lengths, world.consume, world)
                initial_seq = (tcp.seq + 1) if (flags & 0x02) else tcp.seq
                stream = TcpStream(initial_seq, frames.feed)
                if flags & 0x02:
                    world.reset(preserve_transfer=True)

            if active != peer:
                return

            payload = bytes(tcp.payload)
            # Ethernet padding may be dissected as a TCP payload layer by Scapy.
            padding = tcp.getlayer("Padding")
            if padding and len(bytes(padding)) <= len(payload):
                payload = payload[:len(payload) - len(bytes(padding))]

            if payload:
                target.touch()
                world.add_tcp_bytes(len(payload))

                op = u16(payload, 0) if len(payload) >= 2 else None
                hex_prefix = payload[:4].hex(" ").upper() if len(payload) >= 2 else payload.hex(" ").upper()

                # Diagnóstico rápido do stream TCP: imprime apenas os primeiros 15 pacotes recebidos
                if diag_count < 15:
                    diag_count += 1
                    if op is not None:
                        if op in KNOWN_SYNC_OPCODES:
                            status = "reconhecido pelo parser"
                        elif op in lengths:
                            status = f"conhecido no PACKETVER (len={lengths[op]}), sem ação"
                        else:
                            status = "ignorado/não mapeado"
                        print(
                            f"[TCP #{diag_count:02d}] {len(payload):4d} bytes | "
                            f"Hex: {hex_prefix:11s} | Opcode 0x{op:04X} ({status})",
                            flush=True,
                        )
                    else:
                        print(
                            f"[TCP #{diag_count:02d}] {len(payload):4d} bytes | Hex: {hex_prefix}",
                            flush=True,
                        )
                    if diag_count == 15:
                        print("[TCP] Diagnóstico inicial dos 15 primeiros pacotes concluído.", flush=True)

                # Diagnóstico contínuo de pacotes de chão/candidatos (16 a 26 bytes) conhecidos mas sem ação
                if op is not None and op not in KNOWN_SYNC_OPCODES and op in lengths:
                    if 16 <= len(payload) <= 26:
                        print(
                            f"[TCP Chão/Candidato] {len(payload)} bytes | "
                            f"Opcode 0x{op:04X} (len={lengths[op]}) | Hex: {payload.hex(' ').upper()}",
                            flush=True,
                        )

                try:
                    if stream:
                        stream.feed((tcp.seq + bool(flags & 0x02)) & MASK, payload)
                except ValueError as exc:
                    print(f"Captura pausada para esta conexão. {exc}", flush=True)
                    stream = None
                    world.reset()

            if flags & 0x05:
                active = stream = None
                world.reset(preserve_transfer=True)

        try:
            with conf.L2listen(iface=interface, filter=packet_filter, promisc=False) as listener:
                ready.set()
                while not stop.is_set() and not target.changed.is_set():
                    sniff(opened_socket=listener, timeout=.5, store=False, prn=receive)
        except Exception as exc:
            if not stop.is_set():
                print(f"[Captura] Erro ao escutar em {packet_filter}: {exc}. Reiniciando em 1s...", flush=True)
                stop.wait(1.0)

        # Ao trocar de conexão ou parar, reseta para não misturar streams TCP
        world.reset(preserve_transfer=True)
