import sys, time, ctypes, threading, importlib, argparse
from ctypes import wintypes

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

mod = importlib.import_module("overlay-eventos")
mod.ensure_input_desktop()
_INPUT, Overlay = mod._INPUT, mod.Overlay

def main():
    parser = argparse.ArgumentParser(description="Verificador de clique e monitor de rede")
    parser.add_argument("--packet-keys", nargs="*", default=None,
                        help="Chaves de rotação LCG do rAthena para deobfuscação de opcode (ex: --packet-keys k1 k2 k3)")
    cli_args, _ = parser.parse_known_args()

    packet_keys = None
    if cli_args.packet_keys:
        raw_keys = []
        for item in cli_args.packet_keys:
            raw_keys.extend(item.replace(",", " ").split())
        if len(raw_keys) >= 3:
            packet_keys = [int(k, 0) for k in raw_keys[:3]]

    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    if ctypes.sizeof(_INPUT) != expected_size:
        print(f"FAIL [1]: sizeof(_INPUT) == {ctypes.sizeof(_INPUT)} != {expected_size}")
        sys.exit(1)

    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("[INFO] Execute como Admin para testar movimento ao vivo no Ragnarok")
        sys.exit(0)

    u32 = ctypes.windll.user32
    target_hwnd = int(u32.FindWindowW(None, "Ragnarok") or 0)
    if not target_hwnd or not u32.IsWindow(target_hwnd):
        h, _, _ = mod.find_ragnarok_window()
        if h and u32.IsWindow(h):
            target_hwnd = int(h)

    if not target_hwnd or not u32.IsWindow(target_hwnd):
        print("FAIL: Janela do Ragnarok não encontrada")
        sys.exit(1)

    rect = wintypes.RECT()
    u32.GetClientRect(target_hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(rect.left + (rect.right - rect.left) // 2, rect.top + (rect.bottom - rect.top) // 2)
    u32.ClientToScreen(target_hwnd, ctypes.byref(pt))
    cx, cy = pt.x, pt.y
    print(f"[INFO] Janela do Ragnarok detectada: hwnd={hex(target_hwnd)} em centro ({cx}, {cy})")

    # Monitoramento passivo de rede com Scapy
    movement_packets = []
    sniff_stop_event = threading.Event()
    decryptor = None
    if packet_keys:
        try:
            from rathena_feed import LcgDecryptor
        except ImportError:
            try:
                from re_sniffer_test import LcgDecryptor
            except ImportError:
                class LcgDecryptor:
                    def __init__(self, key1: int = 0, key2: int = 0, key3: int = 0):
                        self.key1 = key1
                        self.key2 = key2
                        self.key3 = key3
                        self.cur_key = key1
                    def decrypt(self, raw_op: int) -> int:
                        if self.key1 == 0 and self.key2 == 0 and self.key3 == 0:
                            return raw_op
                        self.cur_key = (self.cur_key * 0x343FD + 0x269EC3) & 0xFFFFFFFF
                        mask = (self.cur_key >> 16) & 0x7FFF
                        return raw_op ^ mask
        decryptor = LcgDecryptor(*packet_keys)
    try:
        import psutil
        game_port = None
        local_ip = None
        for p in psutil.process_iter(["name"]):
            try:
                pname = p.info["name"]
                if pname and "ragexe" in pname.lower():
                    for c in p.net_connections(kind="tcp"):
                        if c.status == psutil.CONN_ESTABLISHED and c.raddr and c.raddr.port not in (443, 6900, 6121):
                            game_port = c.raddr.port
                            local_ip = c.laddr.ip
                            break
            except Exception:
                pass

        if game_port and local_ip:
            from scapy.all import conf, sniff, TCP, Raw
            target_iface = None
            for iface in conf.ifaces.values():
                ips = iface.ips.get(4, []) if hasattr(iface, "ips") else []
                if local_ip == getattr(iface, "ip", None) or local_ip in ips:
                    target_iface = iface
                    break
            if not target_iface:
                target_iface = conf.iface

            def _pkt_handler(pkt):
                if pkt.haslayer(TCP) and pkt.haslayer(Raw):
                    payload = bytes(pkt[Raw].load)
                    if len(payload) >= 2:
                        raw_op = payload[0] | (payload[1] << 8)
                        is_c2s = (pkt[TCP].dport == game_port)
                        if is_c2s and decryptor:
                            op = decryptor.decrypt(raw_op)
                        else:
                            op = raw_op
                        if op in (0x035F, 0x0087, 0x0089) or raw_op in (0x035F, 0x0087, 0x0089) or b"\x5f\x03" in payload or b"\x87\x00" in payload:
                            movement_packets.append(op)

            def _sniff_thread():
                try:
                    sniff(iface=target_iface, filter=f"tcp and port {game_port}", prn=_pkt_handler, store=0, stop_filter=lambda p: sniff_stop_event.is_set())
                except Exception:
                    pass

            t = threading.Thread(target=_sniff_thread, daemon=True)
            t.start()
            print(f"[INFO] Sniffer passivo iniciado na porta {game_port} ({getattr(target_iface, 'description', target_iface)})")
    except Exception as e:
        print(f"[AVISO] Scapy sniffer não pôde ser inicializado: {e}")

    app = Overlay.__new__(Overlay)
    app.game_hwnd = target_hwnd
    app._click_lock = threading.Lock()
    app._last_click_end_ts = 0.0
    app.last_bot_click_ts = 0.0

    # Ponto 1: (cx + 105, cy) (~2-3 células à direita)
    p1_x, p1_y = cx + 105, cy
    app._last_click_res = None
    r1 = app.touch_postmsg(p1_x, p1_y, target_hwnd)
    if not r1:
        print(f"FAIL: Clique 1 rejeitado pelo rate-limit: r1={r1}")
        sys.exit(1)

    t0 = time.time()
    while time.time() - t0 < 0.65:
        if getattr(app, "_last_click_res", None) is not None and (time.time() - t0) >= 0.20:
            break
        time.sleep(0.01)

    res1 = getattr(app, "_last_click_res", None)
    if not res1 or res1["r_move"] != 1 or res1["r_down"] != 1 or res1["r_up"] != 1 or res1["err"] != 0:
        print(f"FAIL: Clique 1 falhou nos valores Win32 SendInput: {res1}")
        sys.exit(1)

    pt_cursor = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt_cursor))
    dist1 = ((pt_cursor.x - p1_x) ** 2 + (pt_cursor.y - p1_y) ** 2) ** 0.5
    if dist1 > 3.0:
        print(f"FAIL: Cursor clique 1 não chegou ao alvo ({p1_x}, {p1_y}): atual=({pt_cursor.x}, {pt_cursor.y}), dist={dist1:.2f}px > 3px")
        sys.exit(1)

    # Cooldown para clique 2
    time.sleep(0.45)

    # Ponto 2: (cx - 105, cy) (~2-3 células à esquerda)
    p2_x, p2_y = cx - 105, cy
    app._last_click_res = None
    r2 = app.touch_postmsg(p2_x, p2_y, target_hwnd)
    if not r2:
        print(f"FAIL: Clique 2 rejeitado pelo rate-limit: r2={r2}")
        sys.exit(1)

    t1 = time.time()
    while time.time() - t1 < 2.0:
        if getattr(app, "_last_click_res", None) is not None and (time.time() - t1) >= 1.5:
            break
        time.sleep(0.01)

    res2 = getattr(app, "_last_click_res", None)
    if not res2 or res2["r_move"] != 1 or res2["r_down"] != 1 or res2["r_up"] != 1 or res2["err"] != 0:
        print(f"FAIL: Clique 2 falhou nos valores Win32 SendInput: {res2}")
        sys.exit(1)

    u32.GetCursorPos(ctypes.byref(pt_cursor))
    dist2 = ((pt_cursor.x - p2_x) ** 2 + (pt_cursor.y - p2_y) ** 2) ** 0.5
    if dist2 > 3.0:
        print(f"FAIL: Cursor clique 2 não chegou ao alvo ({p2_x}, {p2_y}): atual=({pt_cursor.x}, {pt_cursor.y}), dist={dist2:.2f}px > 3px")
        sys.exit(1)

    # Janela de espera para o sniffer Scapy capturar pacotes de rede (buffer Npcap / latência de resposta 0x0087)
    if time.time() - t1 < 1.5:
        time.sleep(1.5 - (time.time() - t1))

    sniff_stop_event.set()

    if not movement_packets:
        print("FAIL: Nenhum pacote de movimento (0x035F / 0x0087) capturado na rede")
        sys.exit(1)

    pkts_str = ", ".join(f"0x{p:04X}" if isinstance(p, int) else str(p) for p in movement_packets)
    print(f"[REDE] Movimento confirmado no jogo! Pacotes capturados: [{pkts_str}]")
    print("SUCCESS: 2 cliques ao redor do personagem validados (MOVE=1 DOWN=1 UP=1 err=0, dist<=3px)")
    sys.exit(0)

if __name__ == "__main__":
    main()
