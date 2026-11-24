"""Primitivas de mouse e teclado Win32 usadas pelo overlay."""
import ctypes
from ctypes import wintypes
import math
import random
import time


u32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    set_dpi_context = u32.SetProcessDpiAwarenessContext
    set_dpi_context.argtypes = (ctypes.c_void_p,)
    set_dpi_context.restype = wintypes.BOOL
    per_monitor_dpi = set_dpi_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except AttributeError:
    per_monitor_dpi = False
if not per_monitor_dpi:
    ctypes.windll.user32.SetProcessDPIAware()

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

LAST_BOT_CLICK_TS = float("-inf")
_BOT_CLICKING = False


def is_bot_clicking(grace_sec: float = 0.45) -> bool:
    return _BOT_CLICKING or time.monotonic() - LAST_BOT_CLICK_TS < grace_sec


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
MOUSEEVENTF_VIRTUALDESK = 0x4000
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


assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


# --- Windows Native Touch Injection Structures ---
PT_TOUCH = 2
POINTER_FLAG_NONE = 0x00000000
POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_CONFIDENCE = 0x00000400
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
u32.SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
u32.SetCursorPos.restype = wintypes.BOOL

u32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
u32.GetCursorPos.restype = wintypes.BOOL

u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wintypes.UINT

u32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
u32.MapVirtualKeyW.restype = wintypes.UINT

u32.mouse_event.argtypes = (wintypes.DWORD, ctypes.c_long, ctypes.c_long, wintypes.DWORD, ULONG_PTR)
u32.mouse_event.restype = None

u32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u32.PostMessageW.restype = wintypes.BOOL
u32.PostMessageA.argtypes = u32.SendNotifyMessageW.argtypes = u32.PostMessageW.argtypes
u32.PostMessageA.restype = u32.SendNotifyMessageW.restype = wintypes.BOOL

u32.IsWindow.argtypes = (wintypes.HWND,)
u32.IsWindow.restype = wintypes.BOOL

u32.InitializeTouchInjection.argtypes = (wintypes.UINT, wintypes.DWORD)
u32.InitializeTouchInjection.restype = wintypes.BOOL

u32.InjectTouchInput.argtypes = (wintypes.UINT, ctypes.POINTER(POINTER_TOUCH_INFO))
u32.InjectTouchInput.restype = wintypes.BOOL

u32.GetForegroundWindow.argtypes = ()
u32.GetForegroundWindow.restype = wintypes.HWND

u32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
u32.ScreenToClient.restype = wintypes.BOOL

u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
u32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
u32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
u32.AttachThreadInput.restype = wintypes.BOOL
u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
u32.ShowWindow.restype = wintypes.BOOL
u32.BringWindowToTop.argtypes = (wintypes.HWND,)
u32.BringWindowToTop.restype = wintypes.BOOL

u32.IsIconic.argtypes = (wintypes.HWND,)
u32.IsIconic.restype = wintypes.BOOL

u32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetSystemMetrics.argtypes = (ctypes.c_int,)
u32.GetSystemMetrics.restype = ctypes.c_int

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


def _position_click_cursor(sx, sy, debug):
    ctypes.set_last_error(0)
    u32.SetCursorPos(sx, sy)
    cursor_error = ctypes.get_last_error()

    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    left = u32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if not (left <= sx < left + width and top <= sy < top + height):
        debug(f"touch_postmsg ({sx}, {sy}): alvo fora da tela virtual")
        return

    move = INPUT(type=INPUT_MOUSE)
    move.mi.dx = ((sx - left) * 65536 + 32768) // width
    move.mi.dy = ((sy - top) * 65536 + 32768) // height
    move.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    ctypes.set_last_error(0)
    if not u32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT)):
        debug(f"touch_postmsg ({sx}, {sy}): SetCursorPos erro={cursor_error}, "
              f"SendInput erro={ctypes.get_last_error()}")


def _smooth_move_to_click(sx: int, sy: int, game_hwnd: int) -> tuple[int | None, int | None]:
    """Deslizamento ease-out com SetCursorPos (6ms) + PostMessageW + InjectTouchInput opcional para hover no DirectX."""
    ensure_touch_injection()
    pt = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    x0, y0 = pt.x, pt.y
    dist = math.hypot(sx - x0, sy - y0)

    def _post_wm_move(x, y):
        if not game_hwnd or not u32.IsWindow(game_hwnd):
            return None, None
        pt_cl = wintypes.POINT(x, y)
        if not u32.ScreenToClient(game_hwnd, ctypes.byref(pt_cl)):
            return None, None
        lp = ((pt_cl.y & 0xFFFF) << 16) | (pt_cl.x & 0xFFFF)
        ctypes.set_last_error(0)
        r = int(u32.PostMessageW(game_hwnd, 0x0200, 0, lp))
        e = ctypes.get_last_error()
        return r, e

    _FLAG_HOVER = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_CONFIDENCE | POINTER_FLAG_PRIMARY

    def _make_contact(x: int, y: int, flags: int) -> POINTER_TOUCH_INFO:
        c = POINTER_TOUCH_INFO()
        c.pointerInfo.pointerType = PT_TOUCH
        c.pointerInfo.pointerId = 0
        c.pointerInfo.hwndTarget = 0
        c.pointerInfo.ptPixelLocation.x = int(x)
        c.pointerInfo.ptPixelLocation.y = int(y)
        c.pointerInfo.pointerFlags = flags
        c.touchFlags = TOUCH_FLAG_NONE
        c.touchMask = 0x0003
        c.rcContact.left   = int(x) - 2
        c.rcContact.top    = int(y) - 2
        c.rcContact.right  = int(x) + 2
        c.rcContact.bottom = int(y) + 2
        c.pressure = 512
        c.orientation = 0
        return c

    if dist > 8.0:
        steps = max(5, min(12, int(dist / 55)))
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1.0 - (1.0 - t) ** 2
            if i == steps:
                ix, iy = int(sx), int(sy)
            else:
                ix = int(round(x0 + (sx - x0) * eased)) + random.choice([-2, -1, 1, 2])
                iy = int(round(y0 + (sy - y0) * eased)) + random.choice([-2, -1, 1, 2])

            u32.SetCursorPos(ix, iy)
            try:
                c = _make_contact(ix, iy, _FLAG_HOVER)
                u32.InjectTouchInput(1, ctypes.byref(c))
            except Exception:
                pass
            _post_wm_move(ix, iy)
            time.sleep(0.006)

    try:
        c_land = _make_contact(int(sx), int(sy), _FLAG_HOVER)
        u32.InjectTouchInput(1, ctypes.byref(c_land))
    except Exception:
        pass
    return _post_wm_move(int(sx), int(sy))

def touch_postmsg(sx, sy, hwnd, root_hwnd, *, is_focused, debug, log, is_admin=False):
    """Executa um clique Win32 em coordenadas absolutas de tela."""
    global LAST_BOT_CLICK_TS, _BOT_CLICKING
    sx, sy = int(sx), int(sy)
    d = ed = 0
    d_hold = ed_hold = 0
    d_up = ed_up = 0
    d_exit = ed_exit = 0
    lbtn = 0
    post_move = post_move_err = None
    mouse_down = mouse_up = 0
    fb_pa_d = fb_pa_d_e = fb_pa_u = fb_pa_u_e = 0
    fb_snm_d = fb_snm_d_e = fb_snm_u = fb_snm_u_e = 0
    fb_pw_d = fb_pw_d_e = fb_pw_u = fb_pw_u_e = 0
    try:
        _BOT_CLICKING = True
        LAST_BOT_CLICK_TS = time.monotonic()
        ensure_input_desktop()
        ensure_touch_injection()

        if not is_focused():
            u32.BringWindowToTop(root_hwnd)
            u32.SetForegroundWindow(root_hwnd)

        if not is_focused():
            time.sleep(0.030)
            if not is_focused():
                debug("touch_postmsg: jogo perdeu o foco antes do clique")
                return False
        _smooth_move_to_click(sx, sy, hwnd)
        _position_click_cursor(sx, sy, debug)

        # Atualiza o cursor do cliente após o movimento absoluto.
        pt_cl = wintypes.POINT(sx, sy)
        has_cc = bool(u32.ScreenToClient(hwnd, ctypes.byref(pt_cl)))
        lparam = (((pt_cl.y & 0xFFFF) << 16) | (pt_cl.x & 0xFFFF)) if has_cc else 0

        if has_cc:
            ctypes.set_last_error(0)
            post_move = int(u32.PostMessageW(hwnd, 0x0200, 0, lparam))
            post_move_err = ctypes.get_last_error()
        time.sleep(0.045)
        if not is_focused():
            debug("touch_postmsg: jogo perdeu o foco durante o hover")
            return False

        try:
            u32.mouse_event(0x0002, 0, 0, 0, 0)
            mouse_down = 1
        except Exception:
            mouse_down = 0
        pressed_at = time.monotonic()

        # Envio simultâneo de mensagens para a janela do Ragexe
        if has_cc and hwnd and u32.IsWindow(hwnd):
            ctypes.set_last_error(0)
            fb_pa_d = int(u32.PostMessageA(hwnd, 0x0201, 0x0001, lparam))
            fb_pa_d_e = ctypes.get_last_error()
            ctypes.set_last_error(0)
            fb_snm_d = int(u32.SendNotifyMessageW(hwnd, 0x0201, 0x0001, lparam))
            fb_snm_d_e = ctypes.get_last_error()
            ctypes.set_last_error(0)
            fb_pw_d = int(u32.PostMessageW(hwnd, 0x0201, 0x0001, lparam))
            fb_pw_d_e = ctypes.get_last_error()

        # InjectTouchInput opcional (não aborta se falhar com e=87)
        try:
            _FLAG_DOWN = (
                POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT |
                POINTER_FLAG_CONFIDENCE | POINTER_FLAG_PRIMARY
            )
            c_down = POINTER_TOUCH_INFO()
            c_down.pointerInfo.pointerType = PT_TOUCH
            c_down.pointerInfo.ptPixelLocation.x = int(sx)
            c_down.pointerInfo.ptPixelLocation.y = int(sy)
            c_down.pointerInfo.pointerFlags = _FLAG_DOWN
            c_down.touchMask = 0x0003
            c_down.rcContact.left = int(sx) - 2
            c_down.rcContact.top = int(sy) - 2
            c_down.rcContact.right = int(sx) + 2
            c_down.rcContact.bottom = int(sy) + 2
            c_down.pressure = 512
            ctypes.set_last_error(0)
            d = int(u32.InjectTouchInput(1, ctypes.byref(c_down)))
            ed = ctypes.get_last_error()
        except Exception:
            pass

        # ── 4. Retenção ativa (50ms) ───────────────────────────────────
        time.sleep(max(0.0, pressed_at + 0.025 - time.monotonic()))
        # Lê o estado real de VK_LBUTTON durante a retenção
        lbtn = int(bool(u32.GetAsyncKeyState(0x01) & 0x8000))

        try:
            _FLAG_HOLD = (
                POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT |
                POINTER_FLAG_CONFIDENCE | POINTER_FLAG_PRIMARY
            )
            c_down.pointerInfo.pointerFlags = _FLAG_HOLD
            ctypes.set_last_error(0)
            d_hold = int(u32.InjectTouchInput(1, ctypes.byref(c_down)))
            ed_hold = ctypes.get_last_error()
        except Exception:
            pass

        if not lbtn:
            lbtn = int(bool(u32.GetAsyncKeyState(0x01) & 0x8000))
        time.sleep(max(0.0, pressed_at + 0.050 - time.monotonic()))

        # ── 5. Clique Up via u32.mouse_event (LEFTUP) ──────────────────
        try:
            u32.mouse_event(0x0004, 0, 0, 0, 0)
            mouse_up = 1
        except Exception:
            mouse_up = 0

        if has_cc and hwnd and u32.IsWindow(hwnd):
            ctypes.set_last_error(0)
            fb_pa_u = int(u32.PostMessageA(hwnd, 0x0202, 0x0000, lparam))
            fb_pa_u_e = ctypes.get_last_error()
            ctypes.set_last_error(0)
            fb_snm_u = int(u32.SendNotifyMessageW(hwnd, 0x0202, 0x0000, lparam))
            fb_snm_u_e = ctypes.get_last_error()
            ctypes.set_last_error(0)
            fb_pw_u = int(u32.PostMessageW(hwnd, 0x0202, 0x0000, lparam))
            fb_pw_u_e = ctypes.get_last_error()

        # InjectTouchInput opcional Up
        try:
            _FLAG_UP = POINTER_FLAG_UP | POINTER_FLAG_INRANGE | POINTER_FLAG_CONFIDENCE | POINTER_FLAG_PRIMARY
            c_down.pointerInfo.pointerFlags = _FLAG_UP
            ctypes.set_last_error(0)
            d_up = int(u32.InjectTouchInput(1, ctypes.byref(c_down)))
            ed_up = ctypes.get_last_error()

            c_down.pointerInfo.pointerFlags = POINTER_FLAG_UPDATE | POINTER_FLAG_PRIMARY
            ctypes.set_last_error(0)
            d_exit = int(u32.InjectTouchInput(1, ctypes.byref(c_down)))
            ed_exit = ctypes.get_last_error()
        except Exception:
            pass

        fb_info = (
            f" MOUSE=({mouse_down},{mouse_up})"
            f" PostA=({fb_pa_d}:{fb_pa_d_e}/{fb_pa_u}:{fb_pa_u_e})"
            f" SNM=({fb_snm_d}:{fb_snm_d_e}/{fb_snm_u}:{fb_snm_u_e})"
            f" PostW=({fb_pw_d}:{fb_pw_d_e}/{fb_pw_u}:{fb_pw_u_e})"
        )
        msg = (
            f"[ClickDebug] touch_postmsg ({sx},{sy}) hwnd={hex(hwnd)} LBTN_STATE={lbtn}: "
            f"is_admin={is_admin} "
            f"INJ_D={d}(e={ed}) INJ_HOLD={d_hold}(e={ed_hold}) "
            f"INJ_UP={d_up}(e={ed_up}) INJ_EXIT={d_exit}(e={ed_exit}) "
            f"POST_MOVE={post_move}(e={post_move_err}){fb_info}"
        )
        if 5 in (ed, ed_hold, ed_up, ed_exit, fb_pa_d_e, fb_pa_u_e, fb_snm_d_e, fb_snm_u_e, fb_pw_d_e, fb_pw_u_e):
            msg += " | UIPI bloqueou."
        log(msg)

    finally:
        try:
            if mouse_down and not mouse_up:
                u32.mouse_event(0x0004, 0, 0, 0, 0)
        finally:
            LAST_BOT_CLICK_TS = time.monotonic()
            _BOT_CLICKING = False
    return bool(mouse_down or d or fb_pa_d or fb_snm_d or fb_pw_d)


def activate_game_window(hwnd):
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg_hwnd = u32.GetForegroundWindow()
    if fg_hwnd == hwnd:
        return True
    fg_tid = u32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = u32.GetWindowThreadProcessId(hwnd, None)
    cur_tid = kernel32.GetCurrentThreadId()

    if not u32.GetAsyncKeyState(0x12) & 0x8000:
        alt = (INPUT * 2)()
        for event in alt:
            event.type = INPUT_KEYBOARD
            event.ki.wVk = 0x12  # VK_MENU
        alt[1].ki.dwFlags = 0x0002  # KEYEVENTF_KEYUP
        size = ctypes.sizeof(INPUT)
        if u32.SendInput(2, alt, size) == 1:
            if not u32.SendInput(1, ctypes.byref(alt[1]), size):
                print("[SendInput] Falha ao soltar Alt após o pulso de foco.", flush=True)

    attached = []
    try:
        for thread in dict.fromkeys((fg_tid, target_tid)):
            if thread and thread != cur_tid and u32.AttachThreadInput(cur_tid, thread, True):
                attached.append(thread)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
    finally:
        for thread in reversed(attached):
            u32.AttachThreadInput(cur_tid, thread, False)

    for _ in range(3):
        if u32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.030)
    return u32.GetForegroundWindow() == hwnd

def _send_game_key(hwnd, vk_code: int, *, restore_focus=False) -> bool:
    """Envia tecla de jogo usando Win32 SendInput com Hardware ScanCode."""
    down_arrow = vk_code == 0x28
    if restore_focus or u32.IsIconic(hwnd) or u32.GetForegroundWindow() != hwnd:
        focused = activate_game_window(hwnd)
    else:
        focused = True
    if not focused:
        if down_arrow:
            print("[SendInput] Foco do Ragexe.exe ainda não confirmado. Enviando ↓.", flush=True)
        else:
            return False

    mapped = u32.MapVirtualKeyW(vk_code, 0 if down_arrow else 4)
    if not mapped:
        return False
    scan = mapped & 0xFF
    extended = down_arrow or (mapped >> 8) == 0xE0
    flags = 0x0008 | int(extended)  # KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    key = INPUT(type=INPUT_KEYBOARD)
    key.ki.wVk = 0
    key.ki.wScan = scan
    key.ki.dwFlags = flags
    size = ctypes.sizeof(INPUT)

    def send(phase):
        ctypes.set_last_error(0)
        if u32.SendInput(1, ctypes.byref(key), size):
            return True
        error = ctypes.get_last_error()
        hint = " Verifique se o Python e o Ragexe.exe têm o mesmo nível de privilégio." if error == 5 else ""
        print(f"[SendInput] VK=0x{vk_code:02X} {phase} retornou 0. Erro Win32={error}.{hint}", flush=True)
        return False

    if send("keydown"):
        time.sleep(0.045 if down_arrow else 0.040)
        key.ki.dwFlags |= 0x0002  # KEYEVENTF_KEYUP
        return send("keyup")

    if down_arrow:
        return False
    lparam = 1 | (scan << 16) | (int(extended) << 24)
    if not u32.PostMessageW(hwnd, 0x0100, vk_code, lparam):
        return False
    time.sleep(0.040)
    return bool(u32.PostMessageW(hwnd, 0x0101, vk_code, lparam | (3 << 30)))
