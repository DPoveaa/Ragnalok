#!/usr/bin/env python3
"""re_sniffer_test.py - Engenharia reversa estatica e validacao ao vivo do protocolo Ragexe."""

import argparse
import json
import os
import struct
import sys
import time
from pathlib import Path

DEFAULT_RAGEXE = r"D:\Jogos\Ragnarok\Ragexe.exe"
LCG_MULT = 0x343FD
LCG_INC = 0x269EC3
LCG_MULT_BYTES = b"\xfd\x43\x03\x00"
LCG_INC_BYTES = b"\xc3\x9e\x26\x00"

KNOWN_CLIENT_OPCODES = {
    0x007E: "CZ_REQUEST_TIME",
    0x0089: "CZ_REQUEST_MOVE",
    0x0090: "CZ_CONTACT_NPC",
    0x009B: "CZ_DISCONNECT",
    0x009F: "CZ_ITEM_PICKUP",
    0x00B8: "CZ_CHOOSE_MENU",
    0x00B9: "CZ_REQ_NEXT_SCRIPT",
    0x0146: "CZ_CLOSE_DIALOG",
    0x0281: "CZ_USE_ITEM",
    0x035F: "CZ_REQUEST_MOVE2",
    0x0360: "CZ_REQUEST_TIME2",
    0x0362: "CZ_ITEM_PICKUP2",
    0x0437: "CZ_REQUEST_ACT",
    0x089C: "CZ_USE_ITEM2",
    0x09CF: "CZ_REQUEST_MOVE3",
}


def u16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def unpack_coords(data: bytes, offset: int = 2):
    """Extrai coordenadas empacotadas de 3 bytes (x, y, dir)."""
    if len(data) < offset + 3:
        return None, None, None
    a, b, c = data[offset], data[offset + 1], data[offset + 2]
    x = (a << 2) | (b >> 6)
    y = ((b & 0x3F) << 4) | (c >> 4)
    direction = c & 0x0F
    return x, y, direction


def extract_pe_keys(exe_path: str = DEFAULT_RAGEXE) -> dict:
    """Extrai chaves LCG e inspeciona seções PE de Ragexe.exe."""
    results = {
        "target_exe": exe_path,
        "is_packed": False,
        "packer_detected": None,
        "candidates": [],
        "keys": [0, 0, 0],
        "notes": "",
    }

    if not os.path.exists(exe_path):
        results["notes"] = f"Arquivo nao encontrado: {exe_path}"
        return results

    try:
        import pefile

        pe = pefile.PE(exe_path, fast_load=True)
    except Exception as exc:
        results["notes"] = f"Erro ao analisar PE com pefile: {exc}"
        return results

    section_names = []
    text_data = b""
    all_code = bytearray()

    for sec in pe.sections:
        sname = sec.Name.decode("latin1", errors="ignore").strip("\x00")
        section_names.append(sname)
        if ".themida" in sname.lower() or ".boot" in sname.lower():
            results["is_packed"] = True
            results["packer_detected"] = "Themida/WinLicense"

        sdata = sec.get_data()
        if sname.lower() in (".text", "code", ""):
            text_data += sdata
        all_code += sdata

    # Busca constantes LCG no .text e nas seções de código
    search_areas = [(".text", text_data), ("all_sections", bytes(all_code))]
    found_candidates = []

    for label, buf in search_areas:
        idx = 0
        while True:
            pos = buf.find(LCG_MULT_BYTES, idx)
            if pos == -1:
                break
            idx = pos + 1

            # Examina janela retroativa de 128 bytes em busca de PUSH imm32 (0x68) ou MOV reg, imm32
            win_start = max(0, pos - 128)
            window = buf[win_start:pos]
            immediates = []
            w_idx = 0
            while w_idx < len(window):
                op = window[w_idx]
                if op == 0x68 and w_idx + 5 <= len(window):  # PUSH imm32
                    val = u32(window, w_idx + 1)
                    immediates.append(val)
                    w_idx += 5
                elif 0xB8 <= op <= 0xBF and w_idx + 5 <= len(window):  # MOV r32, imm32
                    val = u32(window, w_idx + 1)
                    immediates.append(val)
                    w_idx += 5
                else:
                    w_idx += 1

            if len(immediates) >= 3:
                cand = immediates[-3:]
                if cand not in found_candidates:
                    found_candidates.append(cand)

    results["candidates"] = found_candidates
    if found_candidates:
        results["keys"] = found_candidates[0]
        results["notes"] = f"{len(found_candidates)} candidato(s) extraido(s) via scan estatico."
    else:
        results["keys"] = [0, 0, 0]
        results["notes"] = (
            "Nenhuma referencia direta exposta no binario estatico. "
            f"Secoes detectadas: {section_names}. "
            + ("Binario empacotado com Themida. " if results["is_packed"] else "")
            + "Chave padrao [0, 0, 0] (trafego puro/sem ofuscacao)."
        )

    return results


class LcgDecryptor:
    """Implementa decodificador LCG para pacotes de cliente."""

    def __init__(self, key1: int = 0, key2: int = 0, key3: int = 0):
        self.key1 = key1
        self.key2 = key2
        self.key3 = key3
        self.cur_key = key1
        self.packet_count = 0

    def reset(self, initial_key: int | None = None):
        self.cur_key = self.key1 if initial_key is None else initial_key
        self.packet_count = 0

    def decrypt(self, raw_op: int) -> int:
        if self.key1 == 0 and self.key2 == 0 and self.key3 == 0:
            return raw_op
        self.cur_key = (self.cur_key * LCG_MULT + LCG_INC) & 0xFFFFFFFF
        mask = (self.cur_key >> 16) & 0x7FFF
        self.packet_count += 1
        return raw_op ^ mask


def parse_client_payload(payload: bytes, decryptor: LcgDecryptor) -> dict:
    """Analisa e interpreta o payload de um pacote enviado pelo cliente."""
    total_len = len(payload)
    if total_len < 2:
        return {"raw_op": None, "dec_op": None, "desc": "Payload muito curto", "extra": b""}

    raw_op = u16(payload, 0)
    dec_op = decryptor.decrypt(raw_op)

    # Verifica se dec_op ou raw_op e um opcode conhecido
    recognized_op = dec_op if dec_op in KNOWN_CLIENT_OPCODES else (raw_op if raw_op in KNOWN_CLIENT_OPCODES else dec_op)
    op_name = KNOWN_CLIENT_OPCODES.get(recognized_op, "DESCONHECIDO")

    info = {
        "total_len": total_len,
        "raw_op": raw_op,
        "dec_op": dec_op,
        "recognized_op": recognized_op,
        "op_name": op_name,
        "fields": {},
        "extra_bytes": b"",
        "warning": None,
    }

    # Interpretacao de campos de acordo com o opcode
    if recognized_op in (0x0089, 0x035F, 0x09CF):  # Walk / Move
        std_len = 5
        x, y, d = unpack_coords(payload, 2)
        info["fields"] = {"type": "Walk/Move", "x": x, "y": y, "dir": d}
        if total_len > std_len:
            info["extra_bytes"] = payload[std_len:]
            info["warning"] = f"Assinatura/anti-cheat/trailer detectado: {info['extra_bytes'].hex(' ').upper()}"

    elif recognized_op == 0x0437:  # Attack / Action
        std_len = 7
        if total_len >= 7:
            target_gid = u32(payload, 2)
            action = payload[6]
            info["fields"] = {
                "type": "Attack/Action",
                "target_gid": target_gid,
                "action": action,
                "action_str": "Attack" if action == 7 else f"Action({action})",
            }
        if total_len > std_len:
            info["extra_bytes"] = payload[std_len:]
            info["warning"] = f"Assinatura/anti-cheat/trailer detectado: {info['extra_bytes'].hex(' ').upper()}"

    elif recognized_op in (0x009F, 0x0362):  # PickUp Item
        std_len = 6
        if total_len >= 6:
            item_oid = u32(payload, 2)
            info["fields"] = {"type": "PickUp", "item_oid": item_oid}
        if total_len > std_len:
            info["extra_bytes"] = payload[std_len:]
            info["warning"] = f"Assinatura/anti-cheat/trailer detectado: {info['extra_bytes'].hex(' ').upper()}"

    elif recognized_op == 0x0090:  # Contact NPC
        std_len = 7
        if total_len >= 7:
            npc_gid = u32(payload, 2)
            npc_type = payload[6]
            info["fields"] = {"type": "Contact NPC", "npc_gid": npc_gid, "npc_type": npc_type}
        if total_len > std_len:
            info["extra_bytes"] = payload[std_len:]
            info["warning"] = f"Assinatura/anti-cheat/trailer detectado: {info['extra_bytes'].hex(' ').upper()}"

    elif recognized_op in (0x007E, 0x0360):  # Sync / Time
        if total_len >= 6:
            info["fields"] = {"type": "Client Tick", "tick": u32(payload, 2)}
    else:
        info["fields"] = {"type": "Outro", "hex": payload.hex(" ").upper()}

    return info


def detect_ragexe_target():
    """Autodetecta o processo Ragexe.exe, conexao TCP ativa e interface de rede."""
    import psutil

    rag_proc = None
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] and p.info["name"].lower() == "ragexe.exe":
                rag_proc = p
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not rag_proc:
        return None, None, None, None, None

    conns = rag_proc.net_connections(kind="tcp")
    est_conns = [c for c in conns if c.status == psutil.CONN_ESTABLISHED and c.raddr]
    # Filtra portas comuns de login/char/web para pegar conexao com map-server
    map_conns = [c for c in est_conns if c.raddr.port not in (6900, 6121, 443, 80)]
    conn = map_conns[0] if map_conns else (est_conns[0] if est_conns else None)
    if not conn:
        return rag_proc.pid, None, None, None, None

    local_ip = conn.laddr.ip
    local_port = conn.laddr.port
    server_ip = conn.raddr.ip
    server_port = conn.raddr.port

    return rag_proc.pid, local_ip, local_port, server_ip, server_port


def find_scapy_interface(local_ip: str):
    """Encontra a interface Npcap correspondente ao IP local."""
    from scapy.all import conf

    if not conf.use_pcap:
        raise RuntimeError("Npcap nao esta habilitado no Scapy.")

    if local_ip in ("127.0.0.1", "localhost"):
        return conf.loopback_interface

    for _, iface in conf.ifaces.items():
        if getattr(iface, "ip", None) == local_ip:
            return iface

    return conf.iface


def run_live_sniffer(keys: list[int], local_ip: str, server_ip: str, server_port: int, iface):
    """Executa a captura passiva e validacao dos pacotes enviados pelo cliente."""
    from scapy.all import conf, sniff

    print("\n" + "=" * 70)
    print(" INICIANDO CAPTURA PASSIVA DE SAIDA (CLIENTE -> SERVIDOR)")
    print(f" Filtro: src host {local_ip} and dst host {server_ip} and dst port {server_port}")
    print(f" Interface: {getattr(iface, 'description', iface)}")
    print(f" Chaves de descriptografia ativas: {keys}")
    print("=" * 70)
    print("-> Clique manualmente no jogo (Andar, Atacar, Pegar Item, NPC)... [Ctrl+C para encerrar]\n")

    decryptor = LcgDecryptor(keys[0], keys[1], keys[2])
    packet_counter = 0

    bpf_filter = f"tcp and src host {local_ip} and dst port {server_port}"

    def on_packet(pkt):
        nonlocal packet_counter
        if not pkt.haslayer("TCP"):
            return
        tcp = pkt["TCP"]
        payload = bytes(tcp.payload)

        # Remove padding Ethernet se presente
        padding = tcp.getlayer("Padding")
        if padding and len(bytes(padding)) <= len(payload):
            payload = payload[: len(payload) - len(bytes(padding))]

        if not payload:
            return

        packet_counter += 1
        info = parse_client_payload(payload, decryptor)

        print(f"\n[CLI #{packet_counter:03d}] {info['total_len']} bytes enviados pelo cliente:")
        print(f"  Payload Hex: {payload.hex(' ').upper()}")
        print(f"  Opcode Bruto: 0x{info['raw_op']:04X} ({info['raw_op']}) | Descriptografado: 0x{info['dec_op']:04X} ({info['dec_op']})")
        print(f"  Acao/Tipo: {info['op_name']} | Interpretacao: {info['fields']}")

        if info["warning"]:
            print(f"  [AVISO] {info['warning']}")

    with conf.L2listen(iface=iface, filter=bpf_filter, promisc=False) as listener:
        sniff(opened_socket=listener, store=False, prn=on_packet)


def main():
    parser = argparse.ArgumentParser(description="Extrator estatico e sniffer de protocolo Ragexe")
    parser.add_argument("--exe", default=DEFAULT_RAGEXE, help="Caminho do Ragexe.exe")
    parser.add_argument("--keys-file", default="ragexe_keys.json", help="Arquivo de saida das chaves")
    parser.add_argument("--static-only", action="store_true", help="Executa apenas a extracao estatica")
    args = parser.parse_args()

    print(f"[*] Analisando binario estatico: {args.exe}")
    keys_data = extract_pe_keys(args.exe)

    with open(args.keys_file, "w", encoding="utf-8") as f:
        json.dump(keys_data, f, indent=2)
    print(f"[+] Resultado salvo em {args.keys_file}:")
    print(f"    Chaves selecionadas: {keys_data['keys']}")
    print(f"    Candidatos encontrados: {keys_data['candidates']}")
    print(f"    Notas: {keys_data['notes']}")

    if args.static_only:
        return

    pid, local_ip, local_port, server_ip, server_port = detect_ragexe_target()
    if not pid:
        print("\n[-] Ragexe.exe nao encontrado em execucao. Inicie o jogo para teste ao vivo.")
        return

    if not server_ip:
        print(f"\n[-] Ragexe.exe (PID {pid}) encontrado, mas sem conexao TCP ativa com servidor de mapa.")
        return

    print(f"\n[+] Ragexe.exe detectado (PID {pid})")
    print(f"    Conexao: {local_ip}:{local_port} -> {server_ip}:{server_port}")

    iface = find_scapy_interface(local_ip)
    try:
        run_live_sniffer(keys_data["keys"], local_ip, server_ip, server_port, iface)
    except KeyboardInterrupt:
        print("\n[*] Captura passiva finalizada pelo usuario.")


if __name__ == "__main__":
    main()
