"""rAthena packet capture, TCP reassembly and world snapshots."""

from argparse import ArgumentParser
import base64
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import re
import struct
from threading import Event, Lock, Thread
import time


def _resolve_feed_resource(filename: str | Path) -> Path:
    p = Path(filename)
    if p.is_file():
        return p
    if getattr(sys, "frozen", False):
        cand1 = Path(sys.executable).resolve().parent / p.name
        if cand1.is_file():
            return cand1
        if hasattr(sys, "_MEIPASS"):
            cand2 = Path(sys._MEIPASS).resolve() / p.name
            if cand2.is_file():
                return cand2
    cand3 = Path(__file__).resolve().parent / p.name
    if cand3.is_file():
        return cand3
    return p


DEFAULT_LENGTHS = Path(__file__).with_name("rathena-lengths.txt")
DEFAULT_GRF = Path("D:/Jogos/Ragnarok/data.grf")
MAX_BUFFER = 1 << 20
MASK = 0xFFFFFFFF
NPC_NAMES_PATH = Path(__file__).with_name("npc_names_cache.json")
KAFRA_CACHE_PATH = Path(__file__).with_name("kafras_discovered.json")
KAFRA_NETWORK_PATH = Path(__file__).with_name("kafra_network.json")
KAFRA_JOBS = frozenset((*range(112, 118), 423, 581, 721, 791, 859, 860, 861, 10045, 10242))
KAFRA_TARGET_TERMS = (
    "kafra", "funcionária", "funcionaria",
    "eventos incríveis", "eventos incriveis",
    "pata de gato", "aeroplano", "recepcionista",
    "marinheiro", "papai noel", "éden", "eden"
)


def is_kafra(job, name):
    name = (name or "").casefold()
    return job in KAFRA_JOBS or any(term in name for term in KAFRA_TARGET_TERMS)


EDEN_JOBS = {951: "Mestre do Éden", 952: "Oficial do Éden", 953: "Guarda do Éden"}
NPC_JOB_NAMES = {**dict.fromkeys(KAFRA_JOBS, "Kafra"), **EDEN_JOBS}
EDEN_NPC_NAMES = {("moc_para01", 25, 35, 469): "Instrutora Boya",
                  ("moc_para01", 23, 35, 468): "Instrutor Ur"}

# Destino, início dos registros, tamanho do registro e largura do ID de item.
ITEM_LISTS = {
    0x0991: (0, 4, 24, 2), 0x0992: (0, 4, 31, 2), 0x0A0D: (0, 4, 57, 2),
    0x0995: (2, 28, 24, 2), 0x0996: (2, 28, 31, 2), 0x0A10: (2, 28, 57, 2),
    0x0B09: (None, 5, 34, 4), 0x0B0A: (None, 5, 67, 4), 0x0B39: (None, 5, 68, 4),
}
EQUIP_LISTS = {0x0992, 0x0A0D, 0x0996, 0x0A10, 0x0B0A, 0x0B39}


def inventory_tab(item_type, favorite=False):
    if favorite:
        return 3
    if item_type in (0, 2, 11, 18):
        return 0
    if item_type in (4, 5, 8, 10, 12):
        return 1
    return 2


ACCEPT = {0x0073, 0x02EB, 0x0A18}
MAP_TRANSFER = {0x0092, 0x0AC7}
MAP_ACTIVITY = ACCEPT | {0x0B1D, 0x0283, 0x035F, 0x09FF, 0x09FD}
FLOOR = {0x009D, 0x009E, 0x084A, 0x084B, 0x0ADD, 0x0B14, 0x0B15}
# Bytes depois de result e tamanho do layout com IDs de 32 bits.
# 0x02C9 é ZC_PARTY_CONFIG, sem campos de coleta.
PICKUP_ACK = {
    0x00A0: (0, 33), 0x029A: (4, 37), 0x02D4: (6, 39),
    0x0990: (6, 41), 0x0A0C: (31, 66), 0x0A37: (34, 69), 0x0B41: (36, 70),
}
UNIT = {
    0x0078: "idle", 0x007B: "walk", 0x007C: "spawn", 0x022C: "spawn",
    0x02EC: "walk", 0x02ED: "spawn", 0x02EE: "idle",
    0x07F7: "walk", 0x07F8: "spawn", 0x07F9: "idle",
    0x0856: "walk", 0x0858: "spawn", 0x0857: "idle",
    0x0914: "walk", 0x090F: "spawn", 0x0915: "idle",
    0x09DB: "walk", 0x09DC: "spawn", 0x09DD: "idle",
    0x09FD: "walk", 0x09FE: "spawn", 0x09FF: "idle",
    0x0AFD: "walk", 0x0AFE: "spawn", 0x0AFF: "idle",
}
CANONICAL_UNIT_TYPES = {
    0: "player", 1: "npc", 5: "mob", 6: "npc",
    7: "pet", 8: "homun", 9: "merc", 10: "elem",
}
CANONICAL_UNIT_OPCODES = {0x09FF, 0x09FD, 0x09FE, 0x0915, 0x090F, 0x0856, 0x0857, 0x0858}

KNOWN_PACKET_SIZES = {
    0x0080: 7,
    0x0086: 16,
    0x0087: 12,
    0x008A: 29,
    0x0091: 22,
    0x00A1: 6,
    0x0119: 13,  # ZC_STATE_CHANGE (opções de status)
    0x0229: 15,  # ZC_STATE_CHANGE com effectState de 32 bits
    0x02E1: 33,
    0x0291: 4,
    0x08C8: 34,
    0x0088: 10,
    0x00B0: 8,
    0x00B1: 8,
    0x0ACB: 12,
    0x00AF: 6, 0x07FA: 8,
    0x00F0: 3, 0x00F2: 6, 0x00F6: 8, 0x00F8: 2,
    0x0B44: 58, 0x0908: 5, 0x0999: 11, 0x099A: 9,
    0x0B08: -1, 0x0B0B: 4,
    0x0B1D: 2,
    0x0A0F: -1,  # lista de equipamentos do carrinho
    **dict.fromkeys(ITEM_LISTS, -1),
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
    0x007C: 44, 0x022C: 69,
    0x00C4: 6, 0x00C6: -1,
    0x01DE: 33, 0x07FB: 25,
    0x0915: -1, 0x0A30: 106,
    0x00B4: -1,  # ZC_SAY_DIALOG (tamanho variável)
    0x00B5: 6,   # ZC_WAIT_DIALOG
    0x00B6: 6,   # ZC_CLOSE_DIALOG
    0x00B7: -1,  # ZC_MENU_LIST (tamanho variável)
    0x0095: 30,
    0x0195: 102,
    0x0131: 86,  # ZC_STORE_ENTRY
    0x0132: 6,   # ZC_DISAPPEAR_ENTRY
    0x0133: -1,  # ZC_PC_PURCHASE_ITEMLIST
    0x0800: -1,  # ZC_PC_PURCHASE_ITEMLIST_FROMMC
    0x0813: -1,  # ZC_MYITEMLIST_BUYING_STORE
    0x0859: -1,  # ZC_EQUIPWIN_MICROSCOPE
    0x09CA: -1,  # ZC_SKILL_ENTRY5 / store variant
    0x0136: -1,  # variable
    0x0137: 6,
    0x00D7: -1,  # variable
    0x00D8: 6,
    0x00A0: 33,  # ZC_ITEM_PICKUP_ACK
    0x011C: 68,  # ZC_WARPLIST
    0x0438: 10,  # CZ_USE_SKILL
    0x010F: -1,  # ZC_SKILLINFO_LIST
    0x011A: 15,  # ZC_USE_SKILL
    0x09CB: 17,  # ZC_USE_SKILL (variante)
    0x0110: 10,  # ZC_ACK_TOUSESKILL
    0x019B: 10,  # ZC_NOTIFY_EFFECT / cancelamento
    0x0196: 9,   # ZC_MSG_STATE_CHANGE
    0x043F: 25,  # ZC_MSG_STATE_CHANGE (com RemainMS)
    0x0983: 29,  # ZC_MSG_STATE_CHANGE (com total/left)
}
NPC_DIALOG_TEXT_OPCODES = (0x00B4, 0x00B7)
CLIENT_DIALOG_SIZES = {0x00B8: 7, 0x00B9: 6}
KAFRA_MENU_TOKENS = {
    "0Ga0AQ": "Salvar Ponto de Retorno",
    "0Wa0AQ": "Usar o Armazém",
    "0ma0AQ": "Serviço de Teleporte",
    "02a0AQ": "Comprar um Carrinho",
    "1Ga0AQ": "Informações",
    "1Wa0AQ": "Cancelar",
    "g2e0AQ": "Bazar",
}
_KAFRA_MENU_LABELS_BY_ID = {
    int.from_bytes(base64.b64decode(token + "=="), "little"): label
    for token, label in KAFRA_MENU_TOKENS.items()
}

KNOWN_SYNC_OPCODES = (
    MAP_ACTIVITY
    | MAP_TRANSFER
    | PICKUP_ACK.keys()
    | FLOOR
    | set(UNIT.keys())
    | set(KNOWN_PACKET_SIZES.keys())
    | {
        0x0080, 0x0086, 0x0087, 0x0088, 0x008A, 0x008E, 0x0091, 0x00A0, 0x00A1,
        0x00B4, 0x00B5, 0x00B6, 0x00B7, 0x011C,
        0x01FF, 0x0283, 0x0295, 0x02E1, 0x08C8, 0x09DF, 0x0B18,
        0x0095, 0x0195, 0x0136, 0x0137, 0x00D7, 0x00D8,
        0x00FE, 0x02C6, 0x00E5, 0x01F4, 0x0207,  # Convites modais (Party, Troca, Amizade)
        0x013E, 0x07FB,                          # ZC_USESKILL_ACK / ZC_USE_SKILL2
    }
)


def u16(data, offset=0):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _decode_npc_dialog_text(data, *, prefer_utf8=False):
    payload = data[8:u16(data, 2)].replace(b"\x00", b"")
    encodings = ("utf-8", "cp1252") if prefer_utf8 else ("cp1252",)
    for encoding in encodings:
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = payload.decode("latin1")
    return re.sub(r"\^[0-9A-Fa-f]{6}", "", text)


def _translate_kafra_menu_option(option):
    cleaned = (option or "").replace("\x00", "").strip()
    if not cleaned:
        return cleaned
    if cleaned in KAFRA_MENU_TOKENS:
        return KAFRA_MENU_TOKENS[cleaned]
    if re.fullmatch(r"[A-Za-z0-9+/]{6}", cleaned):
        try:
            token_id = int.from_bytes(base64.b64decode(cleaned + "=="), "little")
            if token_id in _KAFRA_MENU_LABELS_BY_ID:
                return _KAFRA_MENU_LABELS_BY_ID[token_id]
        except Exception:
            pass
    return cleaned


def position(data, offset):
    a, b, c = data[offset:offset + 3]
    return (a << 2 | b >> 6, (b & 63) << 4 | c >> 4)


def movement(data, offset):
    c, d, e = data[offset + 2:offset + 5]
    return position(data, offset), ((c & 15) << 6 | d >> 2, (d & 3) << 8 | e)


def walk_speed(value, fallback=150):
    return value if 80 <= value <= 1000 else fallback


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
    p = _resolve_feed_resource(path)
    lengths = dict(KNOWN_PACKET_SIZES)
    for number, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
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
            minimum = 8 if opcode in NPC_DIALOG_TEXT_OPCODES else 4

            # Se o pacote não está na tabela ou tem tamanho incoerente, faz resync
            is_invalid_size = (
                size == -1
                and len(self.buffer) - start >= 4
                and not minimum <= u16(self.buffer, start + 2) <= 16384
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
                            if not (8 if cand_op in NPC_DIALOG_TEXT_OPCODES else 4) <= var_len <= 16384:
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


class _OutboundOpcodes:
    """Track the packet key on the reassembled client stream."""

    def __init__(self, sequence, lengths, keys, on_combat_packet, on_dialog_packet=None):
        seed, self.multiplier, self.increment = keys or (0, 1, 0)
        self.encrypted = bool(keys)
        self.key = (seed * self.multiplier + self.increment) & MASK
        self.lengths = lengths
        self.buffer = bytearray()
        self.decoded = {}
        self.on_combat_packet = on_combat_packet
        self.on_dialog_packet = on_dialog_packet
        self.last_dialog_sequence = None
        self.stream = TcpStream(sequence, self._consume)

    def _record_dialog(self, sequence, opcode, data):
        if self.last_dialog_sequence is not None:
            distance = (sequence - self.last_dialog_sequence) & MASK
            if not 0 < distance < 0x80000000:
                return
        if self.on_dialog_packet is not None:
            self.last_dialog_sequence = sequence
            self.on_dialog_packet(opcode, data)

    def _consume(self, data):
        sequence = self.stream.sequence - len(self.buffer)
        self.buffer.extend(data)
        offset = 0
        while self.key is not None and len(self.buffer) - offset >= 2:
            opcode = u16(self.buffer, offset) ^ ((self.key >> 16) & 0x7FFF)
            self.decoded[(sequence + offset) & MASK] = opcode
            if len(self.decoded) > 1024:
                del self.decoded[next(iter(self.decoded))]

            size = CLIENT_DIALOG_SIZES.get(opcode, self.lengths.get(opcode))
            if size == -1:
                if len(self.buffer) - offset < 4:
                    break
                size = u16(self.buffer, offset + 2)
                if size < 4 or size > 32768:
                    size = None
            if size is None or size < 2:
                self.key = None
                self.buffer.clear()
                return
            if len(self.buffer) - offset < size:
                break
            if opcode in (0x0437, 0x035F, 0x0438):
                self.on_combat_packet(opcode, self.buffer[offset:offset + size])
            elif opcode in CLIENT_DIALOG_SIZES:
                self._record_dialog((sequence + offset) & MASK, opcode, self.buffer[offset:offset + size])
            offset += size
            self.key = (self.key * self.multiplier + self.increment) & MASK
        del self.buffer[:offset]

    def feed(self, sequence, payload):
        if self.key is not None:
            try:
                self.stream.feed(sequence, payload)
            except ValueError:
                self.key = None
                self.buffer.clear()
        if self.key is None and not self.encrypted and len(payload) >= 2:
            # O cliente pode acrescentar um byte ao tamanho da tabela.
            # Após perder o alinhamento, só aceita diálogo no início do segmento.
            opcode = u16(payload)
            if opcode in CLIENT_DIALOG_SIZES and len(payload) >= CLIENT_DIALOG_SIZES[opcode]:
                self._record_dialog(sequence, opcode, payload)
        return self.decoded.get(sequence)


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
    move_start_ts: float = 0.0
    duration: float = 0.0
    segments: tuple = ()  # ((t_start, t_end, p_from, p_to), ...)
    _speed_ms: float = 150.0
    base_level: int = 1
    job_level: int = 1
    weight: int = 0
    max_weight: int = 1

    def __init__(self, origin, target, started=0.0, duration=0.0, segments=(), speed_ms=150.0,
                 base_level=1, job_level=1, weight=0, max_weight=1, *, move_start_ts=None):
        self.origin = origin
        self.target = target
        self.started = started
        self.move_start_ts = started if move_start_ts is None else move_start_ts
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

    def with_speed(self, speed_ms, now):
        current = self.cell(now)
        ratio = speed_ms / max(1.0, self.speed_ms)
        segments = []
        for t0, t1, start, end in self.segments:
            if t1 <= now:
                continue
            if t0 < now:
                t0, start = now, current
            segments.append((now + (t0 - now) * ratio, now + (t1 - now) * ratio, start, end))
        if not segments:
            return self.stationary(self.target, speed_ms)
        return Track(current, self.target, now, segments[-1][1] - now, tuple(segments), speed_ms,
                     move_start_ts=now)

    @classmethod
    def stationary(cls, cell, speed_ms=150.0):
        c = (float(cell[0]), float(cell[1]))
        return cls(c, c, 0.0, 0.0, (), speed_ms=float(speed_ms))

    @classmethod
    def walking(cls, cells, speed, started, gat=None, *, previous=None, path=None):
        origin, target = cells
        current = origin
        if previous is not None and previous.started <= started < previous.end_time:
            previous_cell = previous.cell(started)
            if math.dist(previous_cell, origin) <= 2.2:
                current = previous_cell
                current_tile = tuple(map(round, current))
                if gat is None or gat.is_walkable(*current_tile):
                    origin = current_tile

        if current == target:
            return cls.stationary(target, speed_ms=speed)

        if path is None and gat is not None and origin != target:
            path = gat.find_path(origin, target)
        if not path or path[0] != origin or path[-1] != target:
            path = diagonal_first_path(*origin, *target)

        # O primeiro passo pode começar no meio de uma célula durante uma mudança de rota.
        points = (current, *(path[1:] or (target,)))
        segments = []
        elapsed_ms = 0.0
        for start, end in zip(points, points[1:]):
            dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
            if (dx, dy) == (1, 1):
                distance = 1.4142
            else:
                distance = math.hypot(dx, dy)
            if not distance:
                continue
            step_start = started + elapsed_ms / 1000.0
            elapsed_ms += speed * distance
            segments.append((step_start, started + elapsed_ms / 1000.0, start, end))

        return cls(current, target, started, elapsed_ms / 1000.0, tuple(segments), speed,
                   move_start_ts=started)

    @classmethod
    def stopping(cls, target, speed, now, previous):
        if previous is not None:
            current = previous.cell(now)
            distance = math.dist(current, target)
            if 0 < distance < 0.6:
                duration = distance * max(0.001, speed / 1000.0)
                return cls(current, target, now, duration, ((now, now + duration, current, target),), speed,
                           move_start_ts=now)
        return cls.stationary(target, speed)

    def cell(self, now=None):
        if now is None:
            now = time.perf_counter()
        elapsed_ms = (now - self.move_start_ts) * 1000.0
        if not self.duration or elapsed_ms >= self.duration * 1000.0:
            return self.target
        if elapsed_ms <= 0:
            return self.origin

        for t0, t1, p0, p1 in self.segments:
            if now < t1:
                elapsed, duration = now - t0, t1 - t0
                t = max(0.0, min(1.0, elapsed / duration)) if duration > 0 else 1.0
                return (
                    p0[0] + (p1[0] - p0[0]) * t,
                    p0[1] + (p1[1] - p0[1]) * t,
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
    last_attacker_gid: int | None = None
    engaged_by_other_until: float = 0.0
    ignore_until: float = 0.0
    retry_after: float = 0.0
    suspected_other_owner: bool = False
    unreachable_until: float = 0.0
    is_kafra: bool = False
    avoid_until: float = 0.0
    has_vending_shop: bool = False
    last_seen: float = 0.0
    last_combat_damage: float = 0.0
    gid: int = 0
    move_start_ts: float = 0.0
    objecttype: int | None = None

    @property
    def is_portal(self):
        return self.kind == "warp" or (self.kind != "drop" and self.catalog_id == 45)


class World:
    def __init__(self, initial_map=None, legacy_fields=False, player_speed=150):
        self.lock = Lock()
        self.initial_map = map_name(initial_map) if initial_map else None
        self.legacy_fields = legacy_fields
        self.player_speed = walk_speed(player_speed)
        self.next_map = None
        self.tcp_bytes = 0
        self.packets_ok = 0
        self.active_server = None
        self.active_port = None
        self.unknown_log_count = 0
        self.aid_to_gid = {}
        self._entity_speed_cache: dict[int, int] = {}
        self.mobs_damaged_by_me = {}
        self.mobs_damaged_by_others = {}
        self._player_attacked_mobs = {}
        self.drop_is_priority = None
        self.last_invite_popup_ts = 0.0
        self.locked_combat_mob_gid = None
        self.entity_names = {}   # aid -> str (nome real do NPC/player)
        try:
            cached = json.loads(NPC_NAMES_PATH.read_text(encoding="utf-8"))
            self.npc_names = {
                key: {cell: name for cell, name in value.items() if isinstance(name, str) and name.strip()}
                for key, value in cached.get("by_job", {}).items() if isinstance(value, dict)
            }
        except (OSError, ValueError, AttributeError):
            self.npc_names = {}
        try:
            discovered = json.loads(KAFRA_CACHE_PATH.read_text(encoding="utf-8"))
            self.kafras_discovered = {
                map_name(key): row for key, row in discovered.items()
                if isinstance(row, dict) and all(type(row.get(axis)) is int for axis in ("x", "y"))
            }
        except (OSError, ValueError, AttributeError):
            self.kafras_discovered = {}
        self.last_pickup_ack = None
        self.vending_aids = set()  # aids com loja de venda ativa
        self.vending_window_open = False
        self.vending_opened_ts = 0.0
        self.patrol_npc_blocked_cells = {}
        self.recent_dead_mobs = []
        self.learned_hostile_mobs = set()
        self.recent_drop_gids = {}
        self.recent_picked_items = []
        self.recent_seen_drops = []
        self.last_player_move_pkt_time = 0.0
        self.player_move_pkt_count = 0
        self.current_map = f"{self.initial_map}.gat" if self.initial_map else None
        self.npc_dialog_active = False
        self.npc_dialog_last_ts = 0.0
        self.npc_dialog_opcode = 0
        self.dialog_menu_options = []
        self.dialog_menu_raw_options = []
        self.dialog_menu_seq = 0
        self.dialog_npc_id = 0
        self.dialog_has_next = False
        self.npc_dialog = {
            "open": False,
            "active": False,
            "type": "closed",
            "seq": 0,
            "npc_gid": 0,
            "npc_id": 0,
            "opcode": 0,
            "text": "",
            "options": [],
            "last_ts": 0.0,
            "last_update": 0.0,
        }
        self.on_player_move_callback = None
        self.on_map_change_callback = None
        self.last_warp_from = None
        self.warp_intent_callback = None
        self._warp_context = ((), False)
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
        self._seen_posdir_layouts = set()
        self._mob_debug_count = 0
        try:
            with open("packets_debug.log", "w", encoding="utf-8") as f:
                pass
        except OSError:
            pass
        self.reset()

    def _set_player_track(self, new_track):
        """Preserva atributos de level e peso ao recriar o Track do jogador."""
        self.predicted_player = None
        new_track._speed_ms = self.player_speed
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
        self._sync_player_status()

    def _sync_player_status(self):
        if self.player is not None:
            self.player.gid = self.player_id
            self.player.hp = self.player_hp
            self.player.max_hp = self.player_max_hp
            self.player.is_dead = self.player_is_dead
            self.player.is_sitting = self.player_is_sitting
            self.player.zeny = self.player_zeny
            self.player.zeny_known = self.zeny_known

    def _set_player_zeny(self, value):
        self.player_zeny = int(value)
        self.zeny_known = True
        self._sync_player_status()

    def npc_name(self, aid, job, cell, map_id):
        x, y = map(int, cell)
        key = f"{map_id},{x},{y}"
        return (self.entity_names.get(aid)
                or self.npc_names.get(str(job), {}).get(key)
                or EDEN_NPC_NAMES.get((map_id, x, y, job))
                or NPC_JOB_NAMES.get(job))

    def _remember_kafra(self, npc, name):
        npc.is_kafra = is_kafra(npc.catalog_id, name)
        if not npc.is_kafra or not self.map:
            return
        x, y = map(int, npc.track.target)
        row = {"x": x, "y": y, "name": name or "Kafra", "job": npc.catalog_id}
        if self.kafras_discovered.get(self.map) == row:
            return
        self.kafras_discovered[self.map] = row

        # Normaliza mapa para registrar também variações de camada (ex: gef_f10_a -> gef_f10 / gef_fild10)
        curr_m = str(self.map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
        base_m = re.sub(r"_[a-z0-9]$", "", curr_m)
        if base_m == "gef_f10":
            base_m = "gef_fild10"
        if base_m and base_m != curr_m and base_m not in self.kafras_discovered:
            self.kafras_discovered[base_m] = row

        # Auto-calibração contra kafra_network.json se houver divergência
        try:
            if KAFRA_NETWORK_PATH.exists():
                net_data = json.loads(KAFRA_NETWORK_PATH.read_text(encoding="utf-8"))
                net_kafras = net_data.get("kafras", {})
                entry = net_kafras.get(curr_m) or net_kafras.get(base_m)
                if entry:
                    static_locs = [(loc.get("x"), loc.get("y")) for loc in entry.get("locations", [])]
                    if (x, y) not in static_locs:
                        print(f"[Kafra Calibration] Mapa '{self.map}' NPC '{name}' em ({x}, {y}) diverge da base estática {static_locs}. Calibrado em kafras_discovered.json", flush=True)
        except Exception:
            pass

        temporary = KAFRA_CACHE_PATH.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.kafras_discovered, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(KAFRA_CACHE_PATH)
        except OSError as exc:
            print(f"[Feed] Falha ao salvar Kafras: {exc}", flush=True)

    def _remember_npc_name(self, aid, name):
        npc = self.entity("npc", aid)
        if not self.map or npc is None:
            return
        self._remember_kafra(npc, name)
        x, y = map(int, npc.track.target)
        key = f"{self.map},{x},{y}"
        job_cache = self.npc_names.setdefault(str(npc.catalog_id), {})
        if job_cache.get(key) == name:
            return
        job_cache[key] = name
        temporary = NPC_NAMES_PATH.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"by_job": self.npc_names, "base": NPC_JOB_NAMES},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(NPC_NAMES_PATH)
        except OSError as exc:
            print(f"[Feed] Falha ao salvar nomes de NPCs: {exc}", flush=True)

    def _read_item_list(self, opcode, data):
        destination, start, stride, width = ITEM_LISTS[opcode]
        modern = destination is None
        if len(data) < start:
            return
        if modern:
            destination = data[4]
        if destination not in (0, 2):
            return
        target = self.inventory if destination == 0 else self.storage_items
        equipped = opcode in EQUIP_LISTS
        legacy_stride = stride - 10  # ITID e quatro cartões de 16 bits
        sizes = [(stride, width)]
        if modern:
            sizes.append((legacy_stride, 2))
        items = None
        for size, id_width in sizes:
            if (len(data) - start) % size:
                continue
            rows = {}
            for offset in range(start, len(data), size):
                index = u16(data, offset)
                name_id = (u32 if id_width == 4 else u16)(data, offset + 2)
                type_at = offset + 2 + id_width
                item_type = data[type_at]
                count = 1 if equipped else u16(data, type_at + 1)
                worn = equipped and bool(u32(data, type_at + 5))
                flags = data[offset + size - 1]
                tab = inventory_tab(item_type, flags & (4 if equipped else 2))
                if index < (2 if destination == 0 else 1) or index in rows or not name_id or not count or item_type > 30:
                    break
                rows[index] = (name_id, count, tab, worn)
            else:
                if items is not None and items != rows:
                    if destination == 0:
                        self.inventory_list_valid = self.inventory_ready = False
                        self.inventory_synced = False
                    return
                items = rows
        if items is not None:
            if not modern:
                group = (destination, equipped)
                for index in self.legacy_item_indices.get(group, ()):
                    target.pop(index, None)
                self.legacy_item_indices[group] = items.keys()
                if destination == 0:
                    self.inventory_ready = True
            target.update(items)
            if destination == 2:
                self.storage_open = True
            if destination == 0:
                self.inventory_slots = {index: item[1] for index, item in target.items()}
                if not self.inventory_list_open:
                    self.inventory_synced = True
                    self.inventory_pickup_blocked = False
        elif destination == 0:
            self.inventory_list_valid = self.inventory_ready = False
            self.inventory_synced = False

    @property
    def estimated_slots_used(self) -> int:
        occupied = len(self.inventory_slots)
        if self.inventory_synced:
            return occupied
        return max(occupied, max(0, self.highest_inv_index - 1))

    @staticmethod
    def _decrement_item(items, index, count):
        item = items.get(index)
        if item and 0 < count < item[1]:
            items[index] = (item[0], item[1] - count, item[2], item[3])
        elif item and count >= item[1]:
            del items[index]

    def _mark_player_dead(self):
        if not self.player_is_dead:
            self.last_player_death_ts = time.monotonic()
        self.player_is_dead = True
        self.player_is_sitting = False
        self._sync_player_status()

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
        candidates = [Path(f"heatmap_{m}.json"), Path(__file__).parent / f"heatmap_{m}.json"]
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
        p = Path(f"heatmap_{m}.json")
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
            pos = self.player.cell() if self.player else None
            mobs = sum(1 for ent in self.entities.values() if ent.kind == "mob")
            drops = sum(1 for ent in self.entities.values() if ent.kind == "drop")
            npcs = sum(1 for ent in self.entities.values() if ent.kind == "npc")
            warps = sum(1 for ent in self.entities.values() if ent.kind == "warp")
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
                "last_warp_from": dict(self.last_warp_from) if self.last_warp_from else None,
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
                "estimated_slots_used": self.estimated_slots_used,
                "npc_dialog_active": self.npc_dialog_active,
                "npc_dialog_last_ts": self.npc_dialog_last_ts,
                "npc_dialog_opcode": self.npc_dialog_opcode,
                "npc_dialog_options": list(self.dialog_menu_options),
                "npc_dialog": self._npc_dialog_snapshot(),
                "storage_open": self.storage_open,
                "server": self.active_server,
                "port": self.active_port,
                "last_used_skill": dict(self.last_used_skill) if self.last_used_skill else None,
                "skill_info": dict(self.skill_info),
                "learned_skill_sp_cost": dict(self.learned_skill_sp_cost),
                "steal_confirmed_targets": set(self.steal_confirmed_targets),
                "last_skill_fail": dict(self.last_skill_fail) if self.last_skill_fail else None,
                "active_status_ids": set(self.active_status_ids),
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
                self._map_loading_until = 0.0
                self.account_id = None
                self.player_zeny = None
                self.zeny_known: bool = False
                self.inventory = {}
                self.inventory_ready = False
                self.inventory_list_valid = False
                self.inventory_list_open = False
                self.inventory_slots: dict[int, int] = {}
                self.inventory_synced = False
                self.highest_inv_index = 0
                self.inventory_pickup_blocked = False
                self.legacy_item_indices = {}
                self.next_map = None
                self.learned_hostile_mobs.clear()
                self.last_player_death_ts = 0.0
                self.last_map_warp_ts = 0.0
                self.last_map_warp = None
                self.last_map_arrival = None
                self.skill_info = {}
                self.learned_skill_sp_cost = {}
                self.active_status_ids = set()
            self.last_used_skill = None
            self.last_skill_fail = None
            self.steal_confirmed_targets = set()
            self._pending_skill_sp_sample = None
            self._last_steal_cast = None
            self.map = self.next_map or self.initial_map
            self.current_map = f"{self.map}.gat" if self.map else None
            self.player = None
            self.predicted_player = None
            self.storage_capacity = None
            self.storage_open = False
            self.storage_items = {}
            self.player_id = None
            self.player_hp = 0
            self.player_max_hp = 0
            self.player_is_dead = False
            self.player_is_sitting = False
            self.last_player_attack_ts = 0.0
            self.last_attack_target_id = None
            self.last_attack_action = None
            self.last_attack_ts = 0.0
            self.last_move_ts = 0.0
            self.last_mob_attack = (None, None, 0.0)
            self.last_player_damage_ts = 0.0
            self.last_player_aggro_ts = 0.0
            self.last_player_attacker_gid = None
            self.pending_pickup = None
            self.last_pickup_ack = None
            self.farm_target_gid = None
            self.player_sp = 0
            self.player_max_sp = 0
            self.entities = {}
            self.aid_to_gid.clear()
            self._entity_speed_cache.clear()
            self.mobs_damaged_by_me.clear()
            self.mobs_damaged_by_others.clear()
            self._player_attacked_mobs.clear()
            self.entity_names.clear()
            self.vending_aids.clear()
            self.patrol_npc_blocked_cells.clear()
            self.recent_dead_mobs.clear()
            self.recent_drop_gids.clear()
            self.recent_picked_items.clear()
            self.recent_seen_drops.clear()
            self.last_player_move_pkt_time = 0.0
            self.player_move_pkt_count = 0
            self.npc_dialog_active = False
            self.npc_dialog_opcode = 0
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []
            self.dialog_npc_id = 0
            self.dialog_has_next = False
            self.npc_dialog["open"] = False
            self.npc_dialog["active"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog.pop("opcode", None)
            self.npc_dialog["options"] = []
            self.npc_dialog["text"] = ""
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            self.vending_window_open = False
            self.vending_opened_ts = 0.0

    def entity(self, kind, entity_id):
        ent = self.entities.get(entity_id)
        return ent if ent is not None and ent.kind == kind else None

    def entity_items(self):
        return (((ent.kind, entity_id), ent) for entity_id, ent in self.entities.items())

    def is_self_entity(self, *ids):
        own_ids = (self.player_id, self.account_id, getattr(self.player, "aid", None))
        return any(entity_id and entity_id in own_ids for entity_id in ids)

    def _is_local_player_id(self, entity_id):
        return self.player_id is not None and self.player_id >= 100000 and entity_id == self.player_id

    def _remove_entity(self, entity_id):
        self._entity_speed_cache.pop(entity_id, None)
        ent = self.entities.pop(entity_id, None)
        if ent is not None:
            for alias in (entity_id, ent.gid):
                if self.aid_to_gid.get(alias) == entity_id:
                    self.aid_to_gid.pop(alias)
        return ent

    def get_npcs(self):
        with self.lock:
            return [ent for ent in self.entities.values()
                    if ent.kind == "npc" and ent.catalog_id not in (111, 139, 32767)]

    def get_warps(self):
        with self.lock:
            return [ent for ent in self.entities.values() if ent.kind == "warp"]

    def get_mobs(self):
        with self.lock:
            return [ent for ent in self.entities.values() if ent.kind == "mob"]

    def get_drops(self):
        with self.lock:
            return [ent for ent in self.entities.values() if ent.kind == "drop"]

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
                ent = self.entity(kind, aid)
                if ent:
                    return ent.track.target
            except Exception:
                pass
            return None

    def _npc_dialog_snapshot(self):
        dialog = dict(self.npc_dialog)
        opcode = dialog.get("opcode")
        has_menu = opcode in (0x00B4, 0x00B5, 0x00B7)
        dialog.update(
            dialog_has_next=opcode == 0x00B5 and self.dialog_has_next,
            dialog_has_close=opcode == 0x00B6,
            dialog_menu_options=list(self.dialog_menu_options) if has_menu else [],
            dialog_menu_raw_options=list(self.dialog_menu_raw_options) if has_menu else [],
            dialog_menu_seq=self.dialog_menu_seq,
            dialog_npc_id=self.dialog_npc_id,
            storage_open=self.storage_open,
        )
        return dialog

    def _record_warp_from(self, new_map):
        if not self.map or not new_map or self.map == new_map or self.player is None:
            return
        prev_x, prev_y = self.player.cell()
        self.last_warp_from = {
            "map": self.map, "x": int(round(prev_x)), "y": int(round(prev_y)), "ts": time.time(),
        }
        portals = {entity_id: tuple(map(round, ent.track.target))
                   for (_, entity_id), ent in self.entity_items() if ent.is_portal}
        intentional = self.player_is_dead
        if not intentional and self.warp_intent_callback is not None:
            try:
                intentional = self.warp_intent_callback(self.last_warp_from, portals)
            except Exception as exc:
                intentional = True
                print(f"[PortalGuard] Falha ao classificar transição: {exc}", flush=True)
        self._warp_context = tuple(portals.values()), intentional

    def snapshot(self, *, include_dialog=False, include_warp=False):
        with self.lock:
            frame_time = time.perf_counter()
            player_cell = self.player.cell(frame_time) if self.player else None
            can_prune = player_cell is not None and min(player_cell) > 0 and not self.is_map_loading
            for entity_id, entity in tuple(self.entities.items()):
                if (can_prune and math.dist(player_cell, entity.track.cell(frame_time)) > 22.0
                        and not (entity.kind == "drop"
                                 and (entity.suspected_other_owner or entity.retry_after
                                      or (self.drop_is_priority and self.drop_is_priority(entity.catalog_id))))):
                    self._remove_entity(entity_id)
                    if self.last_mob_attack[0] == entity_id:
                        self.last_mob_attack = (None, None, 0.0)
                    if self.locked_combat_mob_gid == entity_id:
                        self.locked_combat_mob_gid = None
                    if self.farm_target_gid == entity_id:
                        self.farm_target_gid = None
            entities = tuple(
                (key, entity) for key, entity in self.entity_items()
                if not self._is_local_player_id(key[1])
            )
            result = (self.map, self.player, entities)
            if include_dialog:
                result += (self._npc_dialog_snapshot(),)
            if include_warp:
                result += (dict(self.last_warp_from) if self.last_warp_from else None, self._warp_context)
            return result

    @property
    def is_map_loading(self):
        return time.monotonic() < self._map_loading_until

    def client_combat_snapshot(self):
        with self.lock:
            return self.last_attack_target_id, self.last_attack_action, self.last_attack_ts, self.last_move_ts

    def skill_snapshot(self):
        with self.lock:
            return {
                "last_used_skill": dict(self.last_used_skill) if self.last_used_skill else None,
                "skill_info": dict(self.skill_info),
                "learned_skill_sp_cost": dict(self.learned_skill_sp_cost),
                "steal_confirmed_targets": set(self.steal_confirmed_targets),
                "last_skill_fail": dict(self.last_skill_fail) if self.last_skill_fail else None,
                "active_status_ids": set(self.active_status_ids),
            }

    def record_client_combat_packet(self, opcode, data):
        now = time.monotonic()
        with self.lock:
            if opcode == 0x0437 and len(data) >= 7:
                raw_target = u32(data, 2)
                self.last_attack_target_id = self.aid_to_gid.get(raw_target, raw_target)
                self.last_attack_action = data[6]
                self.last_attack_ts = now
                if self.last_attack_action in (0x00, 0x07):
                    target = self.last_attack_target_id
                    self._player_attacked_mobs[raw_target] = self._player_attacked_mobs[target] = now
                    if self.entity("mob", target) is not None:
                        self.last_mob_attack = (target, self.last_attack_action, now)
                        self.locked_combat_mob_gid = target
            elif opcode == 0x0438 and len(data) >= 10:
                skill_lv = u16(data, 2)
                skill_id = u16(data, 4)
                raw_target = u32(data, 6)
                target_id = self.aid_to_gid.get(raw_target, raw_target)
                self.last_used_skill = {
                    "skill_id": skill_id,
                    "skill_lv": skill_lv,
                    "target_id": target_id,
                    "ts": time.time(),
                }
                if self.player_max_sp > 0:
                    self._pending_skill_sp_sample = (skill_id, self.player_sp, now)
                if skill_id == 50:
                    self._last_steal_cast = (target_id, raw_target, now)
            elif opcode == 0x035F and len(data) >= 5:
                self.last_move_ts = now
                self._map_loading_until = 0.0
                if self.player is not None and not self.player_is_dead and not self.player_is_sitting:
                    target = position(data, 2)
                    started = time.perf_counter()
                    prediction = self.predicted_player
                    predicting_target = prediction and started < prediction[1] and prediction[0].target == target
                    if target != self.player.target and not predicting_target:
                        self.predicted_player = None
                        origin = tuple(map(round, self.player.cell(started)))
                        gat = self.get_gat()
                        path = gat.find_path(origin, target) if gat is not None and gat.is_walkable(*target) else None
                        if path and path[0] == origin and path[-1] == target:
                            self.predicted_player = (Track.walking(
                                (origin, target), self.player_speed, started, path=path,
                            ), started + 0.35)

    def record_client_dialog_packet(self, opcode, data):
        with self.lock:
            if u32(data, 2) != self.dialog_npc_id or self.npc_dialog.get("opcode") not in (0x00B4, 0x00B5, 0x00B7):
                return
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []
            self.dialog_has_next = False
            self.npc_dialog_opcode = opcode
            self.npc_dialog.update(type="pending", opcode=opcode, options=[])

    def _apply_npc_dialog(self, opcode, data):
        npc_id = u32(data, 4 if opcode in NPC_DIALOG_TEXT_OPCODES else 2)
        now = time.monotonic()
        dialog = self.npc_dialog
        if opcode == 0x00B4:
            text = _decode_npc_dialog_text(data).strip()
            if dialog.get("type") == "say" and self.dialog_npc_id == npc_id and dialog.get("text"):
                text = f"{dialog['text']}\n{text}" if text else dialog["text"]
            dialog.update(text=text, text_ts=now, text_npc_gid=npc_id)
        if opcode == 0x00B7:
            decoded_text = _decode_npc_dialog_text(data, prefer_utf8=True)
            raw_options = [option.replace("\x00", "").strip() for option in decoded_text.split(":")]
            while raw_options and not raw_options[-1]:
                raw_options.pop()
            self.dialog_menu_raw_options = raw_options
            self.dialog_menu_options = [_translate_kafra_menu_option(option) for option in raw_options]
            self.dialog_menu_seq += 1
            print(f"[NPC-Menu] npc={npc_id} options={self.dialog_menu_options} raw={self.dialog_menu_raw_options}",
                  flush=True)
        elif opcode == 0x00B6 or npc_id != self.dialog_npc_id:
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []

        self.dialog_npc_id = npc_id
        self.dialog_has_next = opcode == 0x00B5
        self.npc_dialog_active = opcode != 0x00B6
        self.npc_dialog_last_ts = now
        self.npc_dialog_opcode = opcode
        dialog.update(
            open=self.npc_dialog_active, active=self.npc_dialog_active,
            type=("say", "wait", "closed", "menu")[opcode - 0x00B4],
            opcode=opcode, seq=dialog.get("seq", 0) + 1, npc_gid=npc_id, npc_id=npc_id,
            options=self.dialog_menu_options, last_ts=time.perf_counter(), last_update=now,
        )

    def consume(self, opcode, data):
        if opcode not in KNOWN_SYNC_OPCODES:
            self._log_unknown_packet(opcode, data)
        try:
            with self.lock:
                self._apply(opcode, data)
                if opcode in MAP_ACTIVITY:
                    self._map_loading_until = 0.0
                self._maybe_save_heatmap()
        except (struct.error, IndexError, UnicodeError) as exc:
            raise ValueError(f"Layout inválido para 0x{opcode:04X}.") from exc

    def _classify_drop(self, cell):
        now = time.monotonic()
        nearby = (dead for dead in self.recent_dead_mobs
                  if now - dead["time"] <= 2.0 and math.dist(cell, dead["cell"]) <= 3.0)
        dead = min(nearby, key=lambda row: (not row["by_me"], math.dist(cell, row["cell"]), -row["time"]),
                   default=None)
        if dead and dead["by_me"]:
            return "MINE", False
        return "FREE", bool(dead and dead["by_others"])

    def _record_pickup_denied(self, now):
        if self.pending_pickup is None:
            return
        gid, clicked_at, retry_delay = self.pending_pickup
        if now - clicked_at > 1.75:
            return
        self.pending_pickup = None
        self.last_pickup_ack = (gid, now, 53)
        if drop := self.entity("drop", gid):
            drop.retry_after = max(drop.retry_after, time.time() + retry_delay)

    def _update_drop_owner(self, drop):
        ownership, suspected = self._classify_drop(drop.track.target)
        if drop.ownership == "MINE" and ownership != "MINE":
            return
        if ownership == "MINE" and drop.suspected_other_owner:
            drop.retry_after = 0.0
            drop.unreachable_until = 0.0
        if suspected and not drop.suspected_other_owner:
            drop.unreachable_until = 0.0
            if self.drop_is_priority is None or not self.drop_is_priority(drop.catalog_id):
                drop.retry_after = max(drop.retry_after, time.time() + 3.5)
        drop.ownership = ownership
        drop.suspected_other_owner = suspected

    def _apply(self, opcode, data):
        if opcode == 0x0283:
            self.account_id = self.player_id = u32(data, 2)
            self._sync_player_status()
        elif opcode in ACCEPT:
            self._player_attacked_mobs.clear()
            self.last_attack_target_id = self.last_attack_action = None
            self.last_attack_ts = self.last_move_ts = 0.0
            self.last_mob_attack = (None, None, 0.0)
            self.entity_names.clear()
            self.storage_capacity = None
            self.storage_open = False
            self.storage_items.clear()
            self.npc_dialog_active = False
            self.npc_dialog_opcode = 0
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []
            self.dialog_npc_id = 0
            self.dialog_has_next = False
            self.npc_dialog["open"] = False
            self.npc_dialog["active"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog.pop("opcode", None)
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            self.vending_window_open = False
            self.vending_opened_ts = 0.0
            if len(data) >= 9:
                self._set_player_track(Track.stationary(position(data, 6)))
            else:
                self.player = None
            self.next_map = None
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
        elif opcode == 0x0091:
            self._map_loading_until = time.monotonic() + 2.0
            self.predicted_player = None
            raw_name = extract_map_from_packet(data, 2)
            name = map_name(raw_name)
            self._record_warp_from(name)
            self.map = name
            self.current_map = f"{name}.gat" if name else None
            if name:
                self.last_map_warp = name
                self.last_map_warp_ts = time.monotonic()
                self.player_is_dead = False
                self.player_is_sitting = False
                self.last_player_attack_ts = 0.0
                self.last_player_damage_ts = 0.0
                self.last_player_aggro_ts = 0.0
                self.last_player_attacker_gid = None
                self._sync_player_status()
            self.pending_pickup = None
            self.last_pickup_ack = None
            self.farm_target_gid = None
            self.recent_dead_mobs.clear()
            self.mobs_damaged_by_me.clear()
            self.mobs_damaged_by_others.clear()
            self._player_attacked_mobs.clear()
            self.locked_combat_mob_gid = None
            self.entities.clear()
            self.aid_to_gid.clear()
            self._entity_speed_cache.clear()
            self.last_attack_target_id = self.last_attack_action = None
            self.last_attack_ts = self.last_move_ts = 0.0
            self.last_mob_attack = (None, None, 0.0)
            self.entity_names.clear()
            self.storage_capacity = None
            self.storage_open = False
            self.storage_items.clear()
            self.npc_dialog_active = False
            self.npc_dialog_opcode = 0
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []
            self.dialog_npc_id = 0
            self.dialog_has_next = False
            self.npc_dialog["open"] = False
            self.npc_dialog["active"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog.pop("opcode", None)
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            self.vending_window_open = False
            self.vending_opened_ts = 0.0
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
            if len(data) >= 22:
                x = u16(data, 18)
                y = u16(data, 20)
                self._set_player_track(Track.stationary((x, y)))
                self.last_map_arrival = (name, (x, y), time.monotonic())
        elif opcode in MAP_TRANSFER:
            self._map_loading_until = time.monotonic() + 2.0
            self.predicted_player = None
            self._player_attacked_mobs.clear()
            self.mobs_damaged_by_me.clear()
            self.mobs_damaged_by_others.clear()
            self.recent_dead_mobs.clear()
            raw_name = extract_map_from_packet(data, 2)
            name = map_name(raw_name)
            self._record_warp_from(name)
            self.map = name
            self.current_map = f"{name}.gat" if name else None
            self.pending_pickup = None
            self.last_pickup_ack = None
            self.entities.clear()
            self.aid_to_gid.clear()
            self._entity_speed_cache.clear()
            self.last_attack_target_id = self.last_attack_action = None
            self.last_attack_ts = self.last_move_ts = 0.0
            self.last_mob_attack = (None, None, 0.0)
            self.storage_capacity = None
            self.storage_open = False
            self.storage_items.clear()
            self.npc_dialog_active = False
            self.npc_dialog_opcode = 0
            self.dialog_menu_options = []
            self.dialog_menu_raw_options = []
            self.dialog_npc_id = 0
            self.dialog_has_next = False
            self.npc_dialog["open"] = False
            self.npc_dialog["active"] = False
            self.npc_dialog["type"] = "closed"
            self.npc_dialog.pop("opcode", None)
            self.npc_dialog["options"] = []
            self.npc_dialog["seq"] = self.npc_dialog.get("seq", 0) + 1
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = time.monotonic()
            self.vending_window_open = False
            self.vending_opened_ts = 0.0
            if self.on_map_change_callback is not None:
                try:
                    self.on_map_change_callback(self.map, self.current_map)
                except Exception:
                    pass
            if len(data) >= 22:
                x = u16(data, 18)
                y = u16(data, 20)
                self._set_player_track(Track.stationary((x, y)))
                self.last_map_arrival = (name, (x, y), time.monotonic())
            self.next_map = name
            self.player_is_sitting = False
            self.last_player_attack_ts = 0.0
            self._sync_player_status()
        elif 0x00B4 <= opcode <= 0x00B7:
            self._apply_npc_dialog(opcode, data)
        elif opcode in (0x00C4, 0x00C6):
            now = time.monotonic()
            npc_gid = u32(data, 2) if opcode == 0x00C4 and len(data) >= 6 else self.npc_dialog.get("npc_gid", 0)
            self.npc_dialog.update(open=True, type="shop", opcode=opcode, npc_gid=npc_gid,
                                   npc_id=npc_gid, options=[], text="",
                                   seq=self.npc_dialog.get("seq", 0) + 1,
                                   last_ts=time.perf_counter(), last_update=now)
        elif opcode == 0x0087:
            if len(data) >= 12:
                now = time.perf_counter()
                self.last_player_move_pkt_time = time.monotonic()
                self.player_move_pkt_count += 1
                cells = movement(data, 6)
                self.player_is_sitting = False
                self._set_player_track(Track.walking(
                    cells, self.player_speed, now, self.get_gat(), previous=self.player,
                ))
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
                now = time.perf_counter()
                stop_cell = (x, y)

                ent = self.entities.get(target_id)
                if self.is_self_entity(aid, target_id) or (self.player_id is None and ent is None):
                    previous = self.player if opcode == 0x0088 else None
                    self._set_player_track(Track.stopping(stop_cell, self.player_speed, now, previous))
                    if self.on_player_move_callback is not None:
                        try:
                            self.on_player_move_callback(x, y)
                        except Exception:
                            pass
                elif ent is not None and ent.kind != "drop":
                    previous = ent.track if opcode == 0x0088 else None
                    stopped = Track.stopping(stop_cell, ent.speed, now, previous)
                    self.entities[target_id] = replace(
                        ent, track=stopped, move_start_ts=stopped.move_start_ts, last_seen=time.monotonic(),
                    )
        elif opcode == 0x0ACB:  # ZC_LONGLONGPAR_CHANGE
            if len(data) >= 12:
                var_id, value = struct.unpack_from("<HQ", data, 2)
                if var_id == 20:
                    self._set_player_zeny(value)
        elif opcode in (0x00B0, 0x00B1):  # ZC_PAR_CHANGE / ZC_LONGPAR_CHANGE
            if len(data) >= 8:
                var_id, value = struct.unpack_from("<HI", data, 2)
                if var_id == 20:
                    self._set_player_zeny(value)
                elif var_id == 0:  # SP_SPEED, em ms por célula
                    self.player_speed = walk_speed(value)
                    if self.player is not None:
                        self._set_player_track(self.player.with_speed(self.player_speed, time.perf_counter()))
                elif var_id == 5:
                    if value < self.player_hp:
                        self.last_player_damage_ts = time.monotonic()
                    self.player_hp = int(value)
                    if value == 0:
                        self._mark_player_dead()
                    else:
                        self.player_is_dead = False
                        self._sync_player_status()
                elif var_id == 6:
                    self.player_max_hp = int(value)
                    self._sync_player_status()
                elif var_id == 7:
                    old_sp = self.player_sp
                    new_sp = int(value)
                    self.player_sp = new_sp
                    if new_sp < old_sp and self._pending_skill_sp_sample is not None:
                        sample_id, sp_before, sample_ts = self._pending_skill_sp_sample
                        if time.monotonic() - sample_ts <= 1.5 and sp_before >= new_sp:
                            cost = sp_before - new_sp
                            if cost > 0:
                                self.learned_skill_sp_cost[sample_id] = cost
                            self._pending_skill_sp_sample = None
                elif var_id == 8:
                    self.player_max_sp = int(value)
                elif var_id == 11:
                    val = int(value)
                    self.player_base_level = val
                    if self.player is None:
                        self._set_player_track(Track.stationary((0, 0)))
                    self.player.base_level = val
                elif var_id == 55:
                    val = int(value)
                    self.player_job_level = val
                    if self.player is None:
                        self._set_player_track(Track.stationary((0, 0)))
                    self.player.job_level = val
                elif var_id == 24:
                    val = int(value)
                    self.player_weight = val
                    if self.player is None:
                        self._set_player_track(Track.stationary((0, 0)))
                    self.player.weight = val
                elif var_id == 25:
                    val = max(1, int(value))
                    self.player_max_weight = val
                    if self.player is None:
                        self._set_player_track(Track.stationary((0, 0)))
                    self.player.max_weight = val
        elif opcode == 0x0086:
            if len(data) >= 16:
                started = time.perf_counter()
                aid = u32(data, 2)
                target_id = self.aid_to_gid.get(aid, aid)
                ent = self.entities.get(target_id)
                if self.is_self_entity(aid, target_id):
                    self._set_player_track(Track.walking(
                        movement(data, 6), self.player_speed, started,
                        self.get_gat(), previous=self.player,
                    ))
                elif ent is not None and ent.kind != "drop":
                    speed = self._entity_speed_cache.get(target_id, ent.speed)
                    track = Track.walking(movement(data, 6), speed, started, self.get_gat(), previous=ent.track)
                    self.entities[target_id] = replace(
                        ent, track=track, speed=speed, move_start_ts=track.move_start_ts, last_seen=time.monotonic(),
                    )
        elif opcode in (0x00FE, 0x02C6, 0x00E5, 0x01F4, 0x0207):
            self.last_invite_popup_ts = time.monotonic()
        elif opcode in (0x008A, 0x08C8, 0x02E1, 0x013E, 0x07FB, 0x01DE):  # ZC_NOTIFY_ACT / ZC_USESKILL (ataques/dano/skills)
            if len(data) >= 10:
                offset = 4 if opcode == 0x01DE else 2
                if len(data) < offset + 8:
                    return
                src_id = u32(data, offset)
                dst_id = u32(data, offset + 4)
                real_dst = self.aid_to_gid.get(dst_id, dst_id)
                real_src = self.aid_to_gid.get(src_id, src_id)
                now = time.monotonic()
                for entity_id in (real_src, real_dst):
                    if (ent := self.entities.get(entity_id)) is not None and ent.kind != "drop":
                        ent.last_seen = now
                dealt_damage = False
                if opcode in (0x008A, 0x02E1, 0x08C8) and len(data) >= KNOWN_PACKET_SIZES[opcode]:
                    action_offset = {0x008A: 26, 0x02E1: 28, 0x08C8: 29}[opcode]
                    action = data[action_offset]
                    if action in (2, 3):  # DMG_SIT_DOWN / DMG_STAND_UP
                        if self.is_self_entity(src_id, real_src) and not self.player_is_dead:
                            self.player_is_sitting = action == 2
                            self._sync_player_status()
                        return
                    if action == 1:  # DMG_PICKUP_ITEM
                        return
                    damage_format = "<h" if opcode == 0x008A else "<i"
                    damage = struct.unpack_from(damage_format, data, 22)[0]
                    damage2 = struct.unpack_from(damage_format, data, action_offset + 1)[0]
                    dealt_damage = (damage > 0 or damage2 > 0) and (opcode != 0x08C8 or data[26] == 0)
                elif opcode == 0x01DE and len(data) >= KNOWN_PACKET_SIZES[opcode]:
                    dealt_damage = struct.unpack_from("<i", data, 24)[0] > 0

                if (opcode in (0x008A, 0x08C8, 0x01DE) and dealt_damage and src_id >= 100000
                        and (self.player_id is None or self.player_id < 100000)):
                    requested_at = max(self._player_attacked_mobs.get(dst_id, -math.inf),
                                       self._player_attacked_mobs.get(real_dst, -math.inf))
                    source = self.entities.get(real_src)
                    if now - requested_at <= 2.0 and (source is None or source.kind == "player"):
                        self.player_id = src_id
                        self._sync_player_status()
                is_src_player = self.is_self_entity(src_id, real_src)
                is_dst_player = self.is_self_entity(dst_id, real_dst)
                if dealt_damage:
                    if is_dst_player:
                        self.last_player_damage_ts = now
                    if opcode in (0x008A, 0x08C8) and (is_src_player or is_dst_player):
                        combat_mob = self.entity("mob", real_dst if is_src_player else real_src)
                        if combat_mob:
                            combat_mob.last_combat_damage = now

                # Registra mob.last_attacker_gid quando dst_gid for monstro ativo
                mob_ent = self.entity("mob", real_dst)
                mob_gid = real_dst
                if mob_ent is not None:
                    try:
                        mob_ent.last_attacker_gid = real_src if real_src else src_id
                    except Exception:
                        pass

                if is_src_player:
                    if opcode == 0x07FB and len(data) >= 25:
                        skill_id = u16(data, 14)
                        if skill_id == 50 and data[24] != 0:
                            self.steal_confirmed_targets.add(real_dst)
                    if mob_ent is not None or opcode == 0x01DE:
                        self.last_player_attack_ts = now
                    if dealt_damage:
                        self.mobs_damaged_by_me[real_dst] = now
                        self.mobs_damaged_by_me[dst_id] = now
                    if mob_ent is not None:
                        self.locked_combat_mob_gid = mob_gid
                elif (mob_ent is not None and dealt_damage and src_id >= 100000
                      and self.player_id is not None and self.player_id >= 100000
                      and (real_src not in self.entities or self.entity("player", real_src) is not None)):
                    # Outro player atacando mob que não é o combat mob travado
                    if mob_gid != getattr(self, "locked_combat_mob_gid", None):
                        try:
                            mob_ent.engaged_by_other_until = now + 8.0
                        except Exception:
                            pass
                    self.mobs_damaged_by_others[real_dst] = now
                    self.mobs_damaged_by_others[dst_id] = now

                # Marca aggro e locked_combat_mob_gid se o alvo for o jogador
                if is_dst_player:
                    src_mob_ent = self.entity("mob", real_src)
                    src_mob_gid = real_src
                    if src_mob_ent is not None:
                        if self.locked_combat_mob_gid is None:
                            self.locked_combat_mob_gid = src_mob_gid
                        if opcode in (0x008A, 0x02E1, 0x08C8):
                            self.last_player_aggro_ts = now
                            self.last_player_attacker_gid = src_mob_gid
                            self.learned_hostile_mobs.add(src_mob_ent.catalog_id)
                        try:
                            src_mob_ent.is_aggro = True
                            src_mob_ent.is_hostile_now = True
                            src_mob_ent.last_aggro_ts = now
                        except Exception:
                            pass
        elif opcode == 0x0291 and len(data) >= 4 and u16(data, 2) == 53:
            self._record_pickup_denied(time.monotonic())
        elif opcode in (0x0095, 0x0195, 0x0A30):  # ZC_ACK_REQNAME / ZC_ACK_REQNAMEALL
            if len(data) >= 30:
                aid_name = u32(data, 2)
                raw = data[6:30].split(b"\x00", 1)[0]
                try:
                    name_str = raw.decode("cp1252")
                except Exception:
                    name_str = raw.decode("latin1", errors="replace")
                if name_str:
                    self.entity_names[aid_name] = name_str
                    npc_id = self.aid_to_gid.get(aid_name, aid_name)
                    self.entity_names[npc_id] = name_str
                    self._remember_npc_name(npc_id, name_str)
        elif opcode in ITEM_LISTS:
            self._read_item_list(opcode, data)
        elif opcode == 0x0B08 and len(data) >= 5:
            if data[4] == 0:
                self.inventory.clear()
                self.inventory_slots.clear()
                self.inventory_synced = False
                self.highest_inv_index = 0
                self.inventory_ready = False
                self.inventory_list_valid = True
                self.inventory_list_open = True
                for group in ((0, False), (0, True)):
                    self.legacy_item_indices.pop(group, None)
            elif data[4] == 2:
                self.storage_items.clear()
                self.storage_capacity = None
        elif opcode == 0x0B0B and len(data) >= 4 and data[2] == 0:
            self.inventory_ready = self.inventory_list_valid
            self.inventory_list_open = False
            self.inventory_synced = self.inventory_ready
            if self.inventory_synced:
                self.inventory_pickup_blocked = False
        elif opcode in (0x00F2, 0x0B18) and len(data) >= 4:
            if opcode == 0x00F2 and len(data) >= 6:
                self.storage_capacity = (u16(data, 2), u16(data, 4))
                self.storage_open = True
            now_mono = time.monotonic()
            if (opcode == 0x0B18 and not self.storage_open and not self.pending_pickup
                    and self._last_steal_cast is not None):
                steal_target, _, steal_ts = self._last_steal_cast
                if now_mono - steal_ts <= 0.8:
                    self.steal_confirmed_targets.add(steal_target)
                    self._last_steal_cast = None
            self.npc_dialog_active = True
            self.npc_dialog_last_ts = now_mono
            self.npc_dialog_opcode = opcode
            self.npc_dialog["open"] = True
            self.npc_dialog["active"] = True
            self.npc_dialog["opcode"] = opcode
            self.npc_dialog["last_ts"] = time.perf_counter()
            self.npc_dialog["last_update"] = now_mono
        elif opcode == 0x00F8:
            self.npc_dialog_active = False
            self.npc_dialog_opcode = opcode
            self.npc_dialog["open"] = False
            self.npc_dialog["active"] = False
            self.npc_dialog["last_update"] = time.monotonic()
            self.storage_capacity = None
            self.storage_open = False
            self.npc_dialog["opcode"] = opcode
            self.storage_items.clear()
            self.highest_inv_index = len(self.inventory_slots)
            self.inventory_pickup_blocked = False
        elif opcode == 0x00F6 and len(data) == 8:
            self._decrement_item(self.storage_items, u16(data, 2), u32(data, 4))
        elif opcode in (0x00F4, 0x01C4, 0x0A0A, 0x0B44) and len(data) >= 11:
            # Este índice é do armazém. O débito da mochila chega em 0x07FA/0x00AF.
            index, count = u16(data, 2), u32(data, 4)
            wide = opcode == 0x0B44 or (opcode == 0x0A0A and len(data) >= 57)
            name_id = (u32 if wide else u16)(data, 8)
            item_type = 3 if opcode == 0x00F4 else data[12 if wide else 10]
            previous = self.storage_items.get(index)
            tab = previous[2] if previous and previous[0] == name_id else inventory_tab(item_type)
            total = count + (previous[1] if previous and previous[0] == name_id else 0)
            self.storage_items[index] = (name_id, total, tab, False)
        elif opcode in (0x00AF, 0x07FA) and len(data) >= (6 if opcode == 0x00AF else 8):
            offset = 2 if opcode == 0x00AF else 4
            index, count = u16(data, offset), u16(data, offset + 2)
            self._decrement_item(self.inventory, index, count)
            if item := self.inventory.get(index):
                self.inventory_slots[index] = item[1]
            else:
                self.inventory_slots.pop(index, None)
            if count:
                self.inventory_pickup_blocked = False
        elif opcode in (0x0999, 0x099A, 0x0908):
            index = u16(data, 2)
            if item := self.inventory.get(index):
                if opcode == 0x0908:
                    tab = 3 if data[4] else (1 if item[3] else 2)
                    if not data[4]:
                        self.inventory_ready = False  # A aba anterior requer nova lista.
                    self.inventory[index] = (*item[:2], tab, item[3])
                elif opcode == 0x0999 and u32(data, 4):
                    self.inventory[index] = (*item[:3], True)
                elif opcode == 0x099A and data[-1] == 0:
                    self.inventory[index] = (*item[:3], False)
        elif opcode in (0x0131, 0x0136, 0x0813, 0x09CA):  # ZC_STORE_ENTRY / Lojas
            aid = None
            if len(data) >= 8 and u16(data, 2) == len(data):
                aid = u32(data, 4)
            elif len(data) >= 6:
                aid = u32(data, 2)
            if aid:
                self.vending_aids.add(aid)
                for eid, ent in self.entities.items():
                    if eid == aid or self.aid_to_gid.get(aid) == eid or self.aid_to_gid.get(eid) == aid:
                        ent.has_vending_shop = True
            if opcode in UNIT:
                self._unit(opcode, data)
        elif opcode in (0x0132, 0x0137, 0x0859):  # ZC_DISAPPEAR_ENTRY / Fechamento de loja
            aid = None
            if len(data) >= 8 and u16(data, 2) == len(data):
                aid = u32(data, 4)
            elif len(data) >= 6:
                aid = u32(data, 2)
            if aid:
                self.vending_aids.discard(aid)
                for eid, ent in self.entities.items():
                    if eid == aid or self.aid_to_gid.get(aid) == eid or self.aid_to_gid.get(eid) == aid:
                        ent.has_vending_shop = False
        elif opcode in (0x0133, 0x0800):  # ZC_PC_PURCHASE_ITEMLIST / ZC_PC_PURCHASE_ITEMLIST_FROMMC
            self.vending_window_open = True
            self.vending_opened_ts = time.monotonic()
        elif opcode == 0x00D7:  # ZC_SHOW_CHAT_ROOM
            if len(data) >= 8:
                self.vending_aids.add(u32(data, 4))
        elif opcode == 0x00D8:  # ZC_CLOSE_CHAT_ROOM
            if len(data) >= 6:
                self.vending_aids.discard(u32(data, 2))
        elif opcode in (0x0080, 0x00A1):
            if len(data) >= KNOWN_PACKET_SIZES[opcode]:
                gid = u32(data, 2)
                real_gid = self.aid_to_gid.get(gid, gid) if opcode == 0x0080 else gid
                if opcode == 0x0080:
                    vanish_type = data[6]
                    ids = {gid, real_gid}
                    ids.update(alias for alias, canonical in self.aid_to_gid.items() if canonical == real_gid)
                    attacked = any(entity_id in self._player_attacked_mobs for entity_id in ids)
                    for entity_id in ids:
                        self._player_attacked_mobs.pop(entity_id, None)
                    if vanish_type == 1 and self.is_self_entity(gid, real_gid):
                        self.player_hp = 0
                        self._mark_player_dead()
                    mob = self.entity("mob", real_gid) or self.entity("mob", gid)
                    if mob and vanish_type == 1:  # 1 = DEAD
                        now = time.monotonic()
                        mob_cell = mob.track.cell()
                        by_me = attacked or any(eid in self.mobs_damaged_by_me for eid in ids)
                        by_others = not by_me and any(eid in self.mobs_damaged_by_others for eid in ids)
                        near_player = self.player is not None and math.dist(mob_cell, self.player.cell()) <= 14
                        self.recent_dead_mobs.append({
                            "gid": real_gid,
                            "aid": gid,
                            "was_target": any(target in (gid, real_gid)
                                              for target in (self.locked_combat_mob_gid, self.farm_target_gid)),
                            "catalog_id": mob.catalog_id if mob else 0,
                            "job_id": mob.catalog_id if mob else 0,
                            "cell": mob_cell,
                            "by_me": by_me,
                            "by_others": by_others,
                            "near_player": near_player,
                            "time": now,
                            "kill_ts": time.strftime("%H:%M:%S"),
                        })
                        if len(self.recent_dead_mobs) > 40:
                            self.recent_dead_mobs.pop(0)
                        for drop in self.entities.values():
                            if (drop.kind == "drop" and now - drop.created_at <= 2.0
                                    and math.dist(drop.track.target, mob_cell) <= 3.0):
                                self._update_drop_owner(drop)
                    for entity_id in ids:
                        self.mobs_damaged_by_me.pop(entity_id, None)
                        self.mobs_damaged_by_others.pop(entity_id, None)
                    if getattr(self, "locked_combat_mob_gid", None) in (gid, real_gid):
                        self.locked_combat_mob_gid = None
                    if self.farm_target_gid in (gid, real_gid):
                        self.farm_target_gid = None
                    self._remove_entity(real_gid)
                    self.entity_names.pop(real_gid, None)
                    if self.last_mob_attack[0] == real_gid:
                        self.last_mob_attack = (None, None, 0.0)
                else:
                    # 0x00A1 ZC_ITEM_DISAPPEAR: registra posição do drop antes de removê-lo
                    # para permitir cruzamento com 0x00A0 (item coletado a <= 2.5 células)
                    now_a1 = time.monotonic()
                    drop_ent = self.entity("drop", real_gid) or self.entity("drop", gid)
                    if drop_ent:
                        self.recent_seen_drops.append({
                            "aid": real_gid,
                            "item_id": drop_ent.catalog_id,
                            "count": drop_ent.amount,
                            "cell": drop_ent.track.target,
                            "time": now_a1,
                            "removed_at": now_a1,  # marcador para cruzamento com 0x00A0
                        })
                        if len(self.recent_seen_drops) > 40:
                            self.recent_seen_drops.pop(0)
                self.vending_aids.discard(gid)
                self.vending_aids.discard(real_gid)
                if opcode == 0x00A1 and drop_ent is not None:
                    self._remove_entity(real_gid)
        elif opcode in PICKUP_ACK:
            trailing, wide_size = PICKUP_ACK[opcode]
            result_offset = len(data) - trailing - 1
            if result_offset >= 10:
                count = u16(data, 4)
                fail = data[result_offset]
                nameid = u32(data, 6) if len(data) >= wide_size else u16(data, 6)
                index = u16(data, 2)
                if not fail and index >= 2 and nameid and count:
                    item_type = data[result_offset - 1]
                    previous = self.inventory.get(index)
                    favorite = data[-5] if opcode == 0x0B41 else data[-3] if opcode == 0x0A37 else 0
                    tab = inventory_tab(item_type, favorite)
                    if previous and previous[0] == nameid:
                        self.inventory[index] = (nameid, previous[1] + count, *previous[2:])
                    else:
                        self.inventory[index] = (nameid, count, tab, False)
                    self.inventory_slots[index] = self.inventory[index][1]
                    self.highest_inv_index = max(self.highest_inv_index, index)
                    if not self.pending_pickup and self._last_steal_cast is not None:
                        steal_target, _, steal_ts = self._last_steal_cast
                        now_steal = time.monotonic()
                        if now_steal - steal_ts <= 0.8:
                            p_cell = self.player.cell() if self.player else None
                            near_drop = any(
                                d.get("item_id") == nameid and now_steal - d.get("removed_at", 0.0) <= 0.8
                                and (p_cell is None or math.dist(p_cell, d["cell"]) <= 2.5)
                                for d in self.recent_seen_drops
                            )
                            if not near_drop:
                                self.steal_confirmed_targets.add(steal_target)
                                self._last_steal_cast = None
                elif fail in (2, 4):  # Sobrepeso ou ausência de slot, se a prevenção não disparou.
                    self.inventory_pickup_blocked = True
                now = time.monotonic()
                if self.pending_pickup:
                    gid, clicked_at, retry_delay = self.pending_pickup
                    drop = self.entity("drop", gid)
                    if now - clicked_at <= 1.75 and (drop is None or nameid in (0, drop.catalog_id)):
                        self.pending_pickup = None
                        self.last_pickup_ack = (gid, now, fail)
                        if not fail and drop and nameid == drop.catalog_id and index >= 2 and count:
                            self._remove_entity(gid)
                        elif fail and drop and (opcode == 0x00A0 or fail not in (2, 4)):
                            drop.retry_after = max(drop.retry_after, time.time() + retry_delay)
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
        elif opcode == 0x010F:  # ZC_SKILLINFO_LIST
            if len(data) >= 41 and (len(data) - 4) % 37 == 0:
                for pos in range(4, len(data), 37):
                    skill_id = u16(data, pos)
                    level = u16(data, pos + 6)
                    sp_cost = u16(data, pos + 8)
                    if skill_id:
                        self.skill_info[skill_id] = {
                            "skill_id": skill_id,
                            "level": level,
                            "sp_cost": sp_cost,
                        }
                        if sp_cost > 0:
                            self.learned_skill_sp_cost[skill_id] = sp_cost
        elif opcode in (0x011A, 0x09CB):  # ZC_USE_SKILL
            if opcode == 0x011A and len(data) >= 15:
                skill_id = u16(data, 2)
                dst_id = u32(data, 6)
                src_id = u32(data, 10)
                result = data[14]
            elif opcode == 0x09CB and len(data) >= 17:
                skill_id = u16(data, 2)
                dst_id = u32(data, 8)
                src_id = u32(data, 12)
                result = data[16]
            else:
                return
            real_src = self.aid_to_gid.get(src_id, src_id)
            real_dst = self.aid_to_gid.get(dst_id, dst_id)
            if self.is_self_entity(src_id, real_src):
                if skill_id == 50 and result != 0:
                    self.steal_confirmed_targets.add(real_dst)
                    self._last_steal_cast = None
        elif opcode == 0x0110:  # ZC_ACK_TOUSESKILL (falha de uso/cooldown/SP)
            if len(data) >= 10:
                skill_id = u16(data, 2)
                fail = data[8]
                cause = data[9]
                if fail == 0:
                    self.last_skill_fail = {
                        "skill_id": skill_id,
                        "reason": cause,
                        "ts": time.time(),
                    }
                    self._pending_skill_sp_sample = None
        elif opcode in (0x019B, 0x0B1A):  # Cancelamento de cast / efeito
            if len(data) >= 6:
                aid = u32(data, 2)
                real_aid = self.aid_to_gid.get(aid, aid)
                if self.is_self_entity(aid, real_aid):
                    skill_id = self.last_used_skill["skill_id"] if self.last_used_skill else 0
                    self.last_skill_fail = {
                        "skill_id": skill_id,
                        "reason": f"cancel_0x{opcode:04X}",
                        "ts": time.time(),
                    }
                    self._pending_skill_sp_sample = None
        elif opcode in (0x0196, 0x043F, 0x0983):  # ZC_MSG_STATE_CHANGE
            if len(data) >= 9:
                status_id = u16(data, 2)
                aid = u32(data, 4)
                state = data[8]
                real_aid = self.aid_to_gid.get(aid, aid)
                if self.is_self_entity(aid, real_aid):
                    if state:
                        self.active_status_ids.add(status_id)
                    else:
                        self.active_status_ids.discard(status_id)
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

        if opcode in (0x009E, 0x084B, 0x0ADD):
            legacy_size = {0x009E: 17, 0x084B: 19, 0x0ADD: 22}[opcode]
            if len(data) >= legacy_size:
                wide = len(data) >= legacy_size + 2
                pos_offset = 9 + 2 * wide + 2 * (opcode != 0x009E)
                aid = u32(data, 2)
                item = u32(data, 6) if wide else u16(data, 6)
                x = u16(data, pos_offset)
                y = u16(data, pos_offset + 2)
                cnt = u16(data, pos_offset + 6)

        elif opcode in (0x0B14, 0x0B15):
            # ZC_ITEM_ENTRY / ZC_ITEM_FALL_ENTRY5 moderno: suporta 19 e 21 bytes
            if len(data) >= 15:
                aid  = u32(data, 2)
                item = u32(data, 6)
                if len(data) >= 19:
                    x21 = u16(data, 13)
                    y21 = u16(data, 15)
                    if 0 <= x21 < 2048 and 0 <= y21 < 2048 and (x21 > 0 or y21 > 0):
                        x, y = x21, y21
                        cnt = u16(data, 17) if len(data) >= 19 else 1
                if x is None or y is None:
                    if len(data) >= 15:
                        x19 = u16(data, 11)
                        y19 = u16(data, 13)
                        x, y = x19, y19
                        cnt = u16(data, 15) if len(data) >= 17 else 1

        elif opcode == 0x084A:
            if len(data) >= 14:
                aid  = u32(data, 2)
                item = u32(data, 6) if len(data) >= 19 else u16(data, 6)
                x   = u16(data, 9)
                y   = u16(data, 11)
                cnt = u16(data, 13) if len(data) >= 15 else 1

        elif opcode == 0x009D:
            # 0x009D legacy (17 bytes) e moderno (19 bytes com ITID u32):
            #   offset 2: ITAID u32 | offset 6: ITID u32 (19b) ou u16 (17b)
            #   19 bytes: x=u16@11, y=u16@13, count=u16@15
            #   17 bytes: x=u16@9,  y=u16@11, count=u16@13
            if len(data) >= 19:
                # Layout moderno: ITID de 4 bytes (u32)
                # [DropRaw] real: 9D 00 C8 DE 00 00 8C 1D 00 00 01 36 00 DB 00 01 00 09 06
                # offset 2=ITAID(u32), offset 6=ITID(u32), offset 10=IsIdentified(u8),
                # offset 11=x(u16), offset 13=y(u16), offset 15=count(u16)
                aid  = u32(data, 2)
                item = u32(data, 6)
                x    = u16(data, 11)
                y    = u16(data, 13)
                cnt  = u16(data, 15) if len(data) >= 17 else 1
            elif len(data) >= 13:
                # Layout legado: ITID de 2 bytes (u16)
                # offset 2=ITAID(u32), offset 6=ITID(u16), offset 9=x(u16), offset 11=y(u16)
                aid  = u32(data, 2)
                item = u16(data, 6)
                x    = u16(data, 9)
                y    = u16(data, 11)
                cnt  = u16(data, 13) if len(data) >= 15 else 1

        if aid is not None:
            self.recent_drop_gids[aid] = now

        if aid is not None and item is not None and x is not None and y is not None:
            if 0 <= x < 2048 and 0 <= y < 2048 and item:
                cell = (x, y)
                current = self.entity("drop", aid)
                if current and current.catalog_id == item and current.track.target == cell:
                    self.entities[aid] = replace(current, amount=max(1, cnt), last_seen=now)
                    return
                if current:
                    self._remove_entity(aid)
                elif aid in self.entities:
                    return
                drop = Entity("drop", item, Track.stationary(cell), max(1, cnt),
                              created_at=now, last_seen=now)
                self._update_drop_owner(drop)
                self.entities[aid] = drop
                self.recent_seen_drops.append({
                    "aid": aid,
                    "item_id": item,
                    "count": max(1, cnt),
                    "cell": cell,
                    "time": now,
                })
                if len(self.recent_seen_drops) > 40:
                    self.recent_seen_drops.pop(0)

    # Detector automático de offset PosDir para pacotes 0x09FF/0x09FD deste servidor
    # Testamos candidatos [63, 65, 67] e salvamos o que produz (x,y) dentro de ±25 do jogador

    def _detect_posdir_offset(self, data, candidates, *, walking, track=None):
        if not self.player:
            return None
        player_cell = self.player.cell()
        previous = track.cell() if track else None
        gat = self.get_gat()
        ranked = []
        for preference, offset in enumerate(dict.fromkeys(candidates)):
            limit = offset + (9 if walking else 3)
            if len(data) < limit:
                continue
            cells = movement(data, offset + 4) if walking else (position(data, offset),)
            if any(not (0 <= x < (gat.width if gat else 1024) and 0 <= y < (gat.height if gat else 1024))
                   for x, y in cells):
                continue
            if walking and math.dist(*cells) > 25:
                continue
            distance = min(math.dist(cell, player_cell) for cell in cells)
            if distance > 30:
                continue
            if not walking and (data[offset + 2] & 0x0F) > 7:
                continue
            continuity = min(math.dist(cell, previous) for cell in cells) if previous else distance
            ranked.append((continuity, distance, preference, offset))
        best = min(ranked, default=None)
        return best[-1] if best else None

    def _unit(self, opcode, data):
        if len(data) < 5:
            return
        now = time.perf_counter()
        is_legacy = opcode in (0x0078, 0x007B, 0x007C, 0x022C)
        walking = UNIT[opcode] == "walk"
        has_aid = opcode >= 0x0900
        wide = opcode in (0x09FD, 0x09FE, 0x09FF, 0x0AFD, 0x0AFE, 0x0AFF) and not self.legacy_fields

        if opcode in (0x007C, 0x022C):
            typed = len(data) not in (41, 64)
            offset = 3 if typed else 2
            obj_type = data[2] if typed else 1
            aid = gid = entity_id = u32(data, offset)
            speed = max(1, u16(data, offset + 4))
            job = u16(data, offset + (14 if opcode == 0x022C else 12))
            effective_offset = len(data) - (9 if opcode == 0x022C else 5)
            if opcode == 0x007C and len(data) == 44:
                effective_offset -= 2
        elif is_legacy:
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
            # Offset PosDir base calculado pelo protocolo
            base_offset = 53 + 4 * has_aid + 4 * wide + 2 * (opcode >= 0x07F7)

            wide_layout = opcode in (0x09FD, 0x09FE, 0x09FF, 0x0AFD, 0x0AFE, 0x0AFF)
            if opcode in (0x09FD, 0x0915, 0x0856, 0x09FF):
                speed = 0
                for speed_offset in (13, 12):
                    if len(data) >= speed_offset + 2:
                        speed = walk_speed(u16(data, speed_offset), fallback=0)
                        if speed:
                            break
            else:
                speed_offset = 9 + 4 * has_aid
                speed = u16(data, speed_offset) if len(data) >= speed_offset + 2 else 0
            job = u16(data, 19 + 4 * has_aid) if len(data) >= 21 + 4 * has_aid else 1002
            aid = u32(data, 5) if len(data) >= 9 else 0
            gid = u32(data, 9) if has_aid and len(data) >= 13 else 0
            entity_id = aid if opcode in CANONICAL_UNIT_OPCODES else aid or gid
            effective_offset = base_offset

            if wide_layout and obj_type != 0:
                previous = (self.entity("npc", entity_id) or self.entity("warp", entity_id)
                            or self.entity("mob", entity_id))
                detected = self._detect_posdir_offset(
                    data, (base_offset, 63, 65, 67), walking=walking,
                    track=previous.track if previous else None)
                if detected is not None:
                    effective_offset = detected
                    signature = (opcode, len(data), detected)
                    if signature not in self._seen_posdir_layouts and len(self._seen_posdir_layouts) < 12:
                        self._seen_posdir_layouts.add(signature)
                        print(f"[Feed] PosDir {detected} no pacote 0x{opcode:04X} de {len(data)} bytes.", flush=True)

        if not entity_id:
            return
        if self._is_local_player_id(entity_id):
            self._remove_entity(entity_id)
            return

        kind = None
        if opcode in CANONICAL_UNIT_OPCODES or obj_type in (0, 1, 5, 6):
            kind = CANONICAL_UNIT_TYPES.get(obj_type)
        if kind is None:
            if obj_type != 0 and job >= 1000:
                kind = "mob"
            elif opcode in CANONICAL_UNIT_OPCODES or obj_type in (7, 8, 9, 10):
                return

        if opcode == 0x0856 and len(data) < effective_offset + 10:
            pos_off = effective_offset
            if len(data) < pos_off + 3:
                return
            cell = position(data, pos_off)
            walking = False
        elif walking:
            move_off = effective_offset + 4
            if len(data) < move_off + 6:
                return
            cells = movement(data, move_off)
        else:
            if len(data) < effective_offset + 3:
                return
            cell = position(data, effective_offset)

        name = self.entity_names.get(entity_id) or self.entity_names.get(gid)
        if not name and opcode in (0x09FD, 0x09FE, 0x09FF, 0x0AFD, 0x0AFE, 0x0AFF) and len(data) >= effective_offset + 32:
            inline_name = data[-24:].split(b"\x00", 1)[0].decode("cp1252", errors="replace").strip()
            if inline_name.isprintable():
                name = inline_name
        if job == 45 and kind in (None, "npc"):
            kind = "warp"
        elif kind is None:
            kind = "mob" if obj_type in (0x05, 0x0D, 0x0E) and not is_kafra(job, name) else "npc"

        # AID é a chave; GID só resolve referências dos outros pacotes.
        old_ent = self.entity(kind, entity_id)
        cached_speed = self._entity_speed_cache.get(entity_id) if old_ent is not None else None
        aliases = {aid, gid} - {0}
        for alias in aliases:
            previous_id = self.aid_to_gid.get(alias, alias)
            if previous_id != entity_id:
                previous_speed = self._entity_speed_cache.get(previous_id)
                previous = self._remove_entity(previous_id)
                if old_ent is None and previous is not None and previous.kind == kind:
                    old_ent = previous
                    cached_speed = previous_speed
        self._remove_entity(entity_id)
        for alias in aliases:
            self.aid_to_gid[alias] = entity_id

        if entity_id:
            fallback_speed = 250 if kind == "mob" else 150
            speed = walk_speed(speed, fallback=cached_speed or fallback_speed)
            self._entity_speed_cache[entity_id] = speed
            previous_track = old_ent.track if old_ent else None
            if walking:
                track = Track.walking(cells, speed, now, self.get_gat(), previous=previous_track)
            else:
                track = Track.stationary(cell, speed)
            is_aggro = old_ent.is_aggro if old_ent else False
            last_aggro_ts = old_ent.last_aggro_ts if old_ent else 0.0
            is_hostile_now = old_ent.is_hostile_now if old_ent else False
            consecutive_approaches = old_ent.consecutive_approaches if old_ent else 0
            last_player_dist = old_ent.last_player_dist if old_ent else 999.0
            last_attacker_gid = getattr(old_ent, "last_attacker_gid", None) if old_ent else None
            engaged_by_other_until = getattr(old_ent, "engaged_by_other_until", 0.0) if old_ent else 0.0
            ignore_until = getattr(old_ent, "ignore_until", 0.0) if old_ent else 0.0

            if kind == "mob":
                px_m, py_m = self.player.cell() if self.player else (None, None)
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

            self.entities[entity_id] = Entity(
                kind, job, track, speed=speed, last_seen=time.monotonic(), gid=gid,
                move_start_ts=track.move_start_ts, objecttype=obj_type,
                has_vending_shop=(kind == "player" and
                                  (bool(aliases & self.vending_aids) or bool(old_ent and old_ent.has_vending_shop))),
                is_aggro=is_aggro, last_aggro_ts=last_aggro_ts,
                is_hostile_now=is_hostile_now,
                consecutive_approaches=consecutive_approaches,
                last_player_dist=last_player_dist,
                last_attacker_gid=last_attacker_gid,
                last_combat_damage=old_ent.last_combat_damage if old_ent else 0.0,
                engaged_by_other_until=engaged_by_other_until,
                ignore_until=ignore_until,
                unreachable_until=old_ent.unreachable_until if old_ent else 0.0,
                avoid_until=old_ent.avoid_until if old_ent else 0.0,
            )
            if name and kind in ("npc", "player"):
                self.entity_names[entity_id] = name
            if kind == "npc":
                if name:
                    self._remember_npc_name(entity_id, name)
                else:
                    self._remember_kafra(self.entities[entity_id],
                                         self.npc_name(entity_id, job, track.target, self.map))

            # Log [MobDebug] dos primeiros 3 mobs para confirmar detecção e offset
            if kind == "mob" and self._mob_debug_count < 3:
                self._mob_debug_count += 1
                try:
                    cell = track.target
                    now = time.monotonic()
                    px, py = self.player.cell() if self.player else (None, None)
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


def _capture_packet_keys(packet_keys):
    if packet_keys is None:
        # The overlay accepts this flag but does not pass it to capture().
        parser = ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--packet-keys")
        packet_keys = parser.parse_known_args()[0].packet_keys
    if packet_keys is None:
        return None
    if isinstance(packet_keys, str):
        packet_keys = tuple(int(value.strip(), 0) for value in packet_keys.split(","))
    else:
        packet_keys = tuple(packet_keys)
    if len(packet_keys) != 3 or any(not 0 <= key <= MASK for key in packet_keys):
        raise ValueError("--packet-keys requer três inteiros de 32 bits separados por vírgula.")
    return packet_keys if any(packet_keys) else None


def capture(world, lengths, interface, server, port, local_port, stop, ready, ragexe_pid=None, *, packet_keys=None):
    packet_keys = _capture_packet_keys(packet_keys)
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
        outbound = {}

        def outbound_decoder(peer, sequence):
            def record(opcode, data):
                if active == peer:
                    world.record_client_combat_packet(opcode, data)

            def record_dialog(opcode, data):
                if active == peer:
                    world.record_client_dialog_packet(opcode, data)

            return _OutboundOpcodes(sequence, lengths, packet_keys, record, record_dialog)

        def receive(packet):
            nonlocal active, stream, diag_count
            ip = packet.getlayer(IP) or packet.getlayer(IPv6)
            tcp = packet.getlayer(TCP)
            if ip is None or tcp is None:
                return
            flags = int(tcp.flags)
            from_server = (ip.src, tcp.sport) == (current_server, current_port)
            to_server = (ip.dst, tcp.dport) == (current_server, current_port)
            if not from_server and not to_server:
                return

            payload = bytes(tcp.payload)
            # Ethernet padding may be dissected as a TCP payload layer by Scapy.
            padding = tcp.getlayer("Padding")
            if padding:
                padding_size = len(bytes(padding))
                if 0 < padding_size <= len(payload):
                    payload = payload[:-padding_size]

            if not from_server:
                peer = ip.src, tcp.sport
                sequence = (tcp.seq + bool(flags & 0x02)) & MASK
                if peer not in outbound and (flags & 0x02 or (payload and not packet_keys)):
                    outbound[peer] = outbound_decoder(peer, sequence)
                if payload:
                    raw_op = u16(payload) if len(payload) >= 2 else None
                    decoder = outbound.get(peer)
                    dec_op = decoder.feed(sequence, payload) if decoder else None
                    if not packet_keys:
                        dec_op = raw_op
                    raw_text = f"0x{raw_op:04X}" if raw_op is not None else "N/D"
                    dec_text = f"0x{dec_op:04X}" if dec_op is not None else "N/D"
                    print(
                        f"[TCP C->S] len={len(payload)} | RawOp={raw_text} | "
                        f"DecOp={dec_text} | Hex: {payload.hex(' ').upper()}",
                        flush=True,
                    )
                if flags & 0x05:
                    outbound.pop(peer, None)
                    if active == peer:
                        active = stream = None
                        world.reset(preserve_transfer=True)
                return
            peer = ip.dst, tcp.dport
            if (flags & 0x12) == 0x12 and peer not in outbound:
                outbound[peer] = outbound_decoder(peer, tcp.ack)

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
                outbound.pop(peer, None)
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
