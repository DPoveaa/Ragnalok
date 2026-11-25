"""Primitivas de mouse e teclado Win32 usadas pelo overlay."""
import ctypes
from ctypes import wintypes
import math
import random
import threading
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
LAST_BOT_KEY_TS = float("-inf")
_BOT_CLICKING = False
_TOUCH_CLICK_LOCK = threading.Lock()


def is_bot_clicking(grace_sec: float = 0.65) -> bool:
    now = time.monotonic()
    return _BOT_CLICKING or _TOUCH_CLICK_LOCK.locked() or (now - LAST_BOT_CLICK_TS < grace_sec) or (now - LAST_BOT_KEY_TS < grace_sec)


def is_bot_keying(grace_sec: float = 0.65) -> bool:
    return time.monotonic() - LAST_BOT_KEY_TS < grace_sec


def begin_bot_click():
    global LAST_BOT_CLICK_TS, _BOT_CLICKING
    _BOT_CLICKING = True
    LAST_BOT_CLICK_TS = time.monotonic()


def finish_bot_click():
    global LAST_BOT_CLICK_TS, _BOT_CLICKING
    LAST_BOT_CLICK_TS = time.monotonic()
    _BOT_CLICKING = False


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


# Assinaturas estritas na instância dedicada u32 (alinhadas com commit funcional 9e1ac16)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

u32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
u32.SetCursorPos.restype = wintypes.BOOL

u32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
u32.GetCursorPos.restype = wintypes.BOOL

u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wintypes.UINT

u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
u32.keybd_event.restype = None

u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
u32.MapVirtualKeyW.restype = wintypes.UINT

u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetAncestor.restype = wintypes.HWND

u32.mouse_event.argtypes = (wintypes.DWORD, ctypes.c_long, ctypes.c_long, wintypes.DWORD, ULONG_PTR)
u32.mouse_event.restype = None

u32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u32.PostMessageW.restype = wintypes.BOOL
u32.PostMessageA.argtypes = u32.SendNotifyMessageW.argtypes = u32.PostMessageW.argtypes
u32.PostMessageA.restype = u32.SendNotifyMessageW.restype = wintypes.BOOL

u32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
u32.ScreenToClient.restype = wintypes.BOOL

u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
u32.GetWindowThreadProcessId.restype = wintypes.DWORD

u32.EnumChildWindows.argtypes = (wintypes.HWND, WNDENUMPROC, wintypes.LPARAM)
u32.EnumChildWindows.restype = wintypes.BOOL

u32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
u32.EnumWindows.restype = wintypes.BOOL

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


def _move_pointer_with_touch(sx: int, sy: int):
    """Posiciona o cursor físico via InjectTouchInput com interpolação de trajetória suave."""
    ensure_touch_injection()
    sx, sy = int(sx), int(sy)

    pt_cur = wintypes.POINT()
    if u32.GetCursorPos(ctypes.byref(pt_cur)):
        cx, cy = pt_cur.x, pt_cur.y
    else:
        cx, cy = sx, sy

    contact = POINTER_TOUCH_INFO()
    contact.pointerInfo.pointerType = PT_TOUCH  # 2
    contact.pointerInfo.pointerId = 0
    contact.touchFlags = 0
    contact.touchMask = 0

    dist = math.hypot(sx - cx, sy - cy)
    r_down = 1
    err_down = 0

    if dist > 8:
        # Ponto de controle para curvatura suave (perpendicular à linha direta)
        nx = -(sy - cy) / dist
        ny = (sx - cx) / dist
        curve_mag = min(20.0, max(-20.0, dist * 0.12))
        ctrl_x = (cx + sx) / 2.0 + nx * curve_mag
        ctrl_y = (cy + sy) / 2.0 + ny * curve_mag

        # 4 pontos intermediários em curva suave (Bezier quadrática) ~6ms por passo
        steps_t = (0.2, 0.4, 0.6, 0.8)
        for i, t in enumerate(steps_t):
            inv_t = 1.0 - t
            ix = int(inv_t * inv_t * cx + 2.0 * inv_t * t * ctrl_x + t * t * sx)
            iy = int(inv_t * inv_t * cy + 2.0 * inv_t * t * ctrl_y + t * t * sy)
            contact.pointerInfo.ptPixelLocation = wintypes.POINT(ix, iy)
            if i == 0:
                contact.pointerInfo.pointerFlags = 0x00010006  # POINTER_FLAG_DOWN | INRANGE | INCONTACT
                ctypes.set_last_error(0)
                r_down = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
                err_down = ctypes.get_last_error()
            else:
                contact.pointerInfo.pointerFlags = 0x00020006  # POINTER_FLAG_UPDATE | INRANGE | INCONTACT
                ctypes.set_last_error(0)
                u32.InjectTouchInput(1, ctypes.byref(contact))
            time.sleep(0.006)

        # Finaliza trajetória exatamente em (sx, sy) via UPDATE
        contact.pointerInfo.ptPixelLocation = wintypes.POINT(sx, sy)
        contact.pointerInfo.pointerFlags = 0x00020006  # POINTER_FLAG_UPDATE | INRANGE | INCONTACT
        ctypes.set_last_error(0)
        u32.InjectTouchInput(1, ctypes.byref(contact))
        time.sleep(0.006)
    else:
        contact.pointerInfo.ptPixelLocation = wintypes.POINT(sx, sy)
        contact.pointerInfo.pointerFlags = 0x00010006  # POINTER_FLAG_DOWN | INRANGE | INCONTACT
        ctypes.set_last_error(0)
        r_down = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
        err_down = ctypes.get_last_error()
        time.sleep(0.015)

    contact.pointerInfo.ptPixelLocation = wintypes.POINT(sx, sy)
    contact.pointerInfo.pointerFlags = 0x00040000  # POINTER_FLAG_UP
    ctypes.set_last_error(0)
    r_up = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
    err_up = ctypes.get_last_error()
    return r_down, err_down, r_up, err_up


def _wait_cursor_arrival(sx, sy, tolerance=3):
    started = time.monotonic()
    correction_at = started + 0.032
    deadline = started + 0.080
    corrected = False
    point = wintypes.POINT()
    while True:
        if (u32.GetCursorPos(ctypes.byref(point))
                and abs(point.x - sx) <= tolerance and abs(point.y - sy) <= tolerance):
            break
        now = time.monotonic()
        if now >= deadline:
            break
        if now >= correction_at and not corrected:
            contact = POINTER_TOUCH_INFO()
            contact.pointerInfo.pointerType = PT_TOUCH
            contact.pointerInfo.pointerId = 0
            contact.pointerInfo.pointerFlags = 0x00020002  # UPDATE | INRANGE: hover sem pressionar
            contact.pointerInfo.ptPixelLocation = wintypes.POINT(sx, sy)
            u32.InjectTouchInput(1, ctypes.byref(contact))
            corrected = True
        time.sleep(min(0.004, max(0.0, deadline - time.monotonic())))
    return point


def touch_postmsg(sx, sy, hwnd=None, root_hwnd=None, *, is_focused=None, debug=None, log=None, is_admin=False, test_mode=False, target_point=None, cursor_tolerance=3, **kwargs):
    """Executa exatamente a mesma sequência Win32 de 9e1ac16:
    1. InjectTouchInput para posicionar o cursor real.
    2. 45ms hover delay para reconhecimento pelo Ragexe.exe.
    3. Micro-deslocamento relativo (1,0) -> (-1,0) para sincronizar ponteiro no DirectX.
    4. EnumChildWindows para enviar PostMessageW (0x0200, 0x0201, 0x0202) tanto para a janela principal
       quanto para todas as janelas filhas (D3DProxyWindow) do Ragexe.exe.
    """
    global LAST_BOT_CLICK_TS, _BOT_CLICKING
    _TOUCH_CLICK_LOCK.acquire()
    _BOT_CLICKING = True
    LAST_BOT_CLICK_TS = time.monotonic()
    try:
        ensure_input_desktop()
        game_hwnd = hwnd or root_hwnd
        if not game_hwnd or not u32.IsWindow(game_hwnd):
            if root_hwnd and u32.IsWindow(root_hwnd):
                game_hwnd = root_hwnd
            else:
                if callable(debug):
                    debug("touch_postmsg: janela do jogo indisponível")
                return False

        if callable(is_focused) and not is_focused():
            top = root_hwnd or game_hwnd
            if top:
                u32.BringWindowToTop(top)
                u32.SetForegroundWindow(top)
                time.sleep(0.030)

        sx, sy = int(sx), int(sy)

        # Atualiza a mira de sprites móveis depois do hover, antes de pressionar.
        for attempt in range(3):
            if callable(target_point):
                point = target_point(0.075)
                if point is None:
                    return False
                sx, sy = map(int, point)
            _move_pointer_with_touch(sx, sy)
            pt_cur = _wait_cursor_arrival(sx, sy, tolerance=cursor_tolerance)
            time.sleep(0.045)
            if not callable(target_point):
                break
            point = target_point(0.022)
            if point is None:
                return False
            if math.dist((sx, sy), point) <= 5:
                click_point = tuple(map(int, point))
                if cursor_tolerance == 1 and math.dist((sx, sy), click_point) > 1:
                    _move_pointer_with_touch(*click_point)
                    pt_cur = _wait_cursor_arrival(*click_point, tolerance=1)
                    time.sleep(0.045)
                    latest = target_point(0.022)
                    if latest is None:
                        return False
                    if math.dist(click_point, latest) > 5:
                        continue
                sx, sy = click_point
                break
        else:
            return False

        # 3. Micro-deslocamento relativo ((1,0) -> (-1,0)) para sincronizar o ponteiro dentro do DirectX
        try:
            u32.mouse_event(0x0001, 1, 0, 0, 0)
            time.sleep(0.004)
            u32.mouse_event(0x0001, -1, 0, 0, 0)
        except Exception:
            pass

        # 4. Converte (sx, sy) via ScreenToClient e envia WM_MOUSEMOVE -> 15ms -> WM_LBUTTONDOWN -> 50ms -> WM_LBUTTONUP
        # Targets incluem a janela principal e todas as janelas filhas (D3DProxyWindow) do Ragexe.exe
        targets = []
        top_hwnd = root_hwnd if (root_hwnd and u32.IsWindow(root_hwnd)) else game_hwnd
        if top_hwnd and u32.IsWindow(top_hwnd):
            targets.append(top_hwnd)
        if game_hwnd and u32.IsWindow(game_hwnd) and game_hwnd not in targets:
            targets.append(game_hwnd)

        game_pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(top_hwnd or game_hwnd, ctypes.byref(game_pid))
        if game_pid.value:
            @WNDENUMPROC
            def collect_child(child_hwnd, _lparam):
                child_pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(child_hwnd, ctypes.byref(child_pid))
                if game_pid.value and child_pid.value == game_pid.value and child_hwnd not in targets:
                    targets.append(child_hwnd)
                return True

            u32.EnumChildWindows(top_hwnd or game_hwnd, collect_child, 0)

        clicked = False
        for target in targets:
            pt = wintypes.POINT(sx, sy)
            if not u32.ScreenToClient(target, ctypes.byref(pt)):
                continue
            lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)

            ctypes.set_last_error(0)
            r_move = int(u32.PostMessageW(target, 0x0200, 0, lparam))
            err_move = ctypes.get_last_error()
            time.sleep(0.015)

            ctypes.set_last_error(0)
            r_down = int(u32.PostMessageW(target, 0x0201, 1, lparam))
            err_down = ctypes.get_last_error()
            time.sleep(0.050)

            ctypes.set_last_error(0)
            r_up = int(u32.PostMessageW(target, 0x0202, 0, lparam))
            err_up = ctypes.get_last_error()
            clicked |= bool(r_down and r_up)

        msg = f"touch_postmsg ({sx},{sy}) targets={len(targets)} cur=({pt_cur.x},{pt_cur.y})"
        if callable(log):
            if getattr(log, "__name__", "") == "log_click_file":
                log(msg)
            else:
                log(f"[ClickDebug] {msg}")
        return clicked
    finally:
        LAST_BOT_CLICK_TS = time.monotonic()
        _BOT_CLICKING = False
        _TOUCH_CLICK_LOCK.release()


move_and_click = patrol_move_and_click = touch_postmsg


def move_cursor_relative(sx: int, sy: int) -> tuple[int, int] | None:
    """Move o cursor físico via touch injection e micro-deslocamento."""
    ensure_input_desktop()
    _move_pointer_with_touch(sx, sy)
    pt = wintypes.POINT()
    if u32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return int(sx), int(sy)


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

def _send_game_key(hwnd=None, vk_code: int = None, hold_ms: int = 110, *, restore_focus=False) -> bool:
    """Envia tecla de jogo usando Win32 SendInput com Hardware ScanCode e fallback para keybd_event."""
    if vk_code is None and hwnd is not None:
        vk_code = hwnd
        hwnd = None
    if vk_code is None:
        return False

    root_hwnd = u32.GetAncestor(hwnd, 2) if hwnd else 0
    root_hwnd = root_hwnd or hwnd or u32.GetForegroundWindow()
    if root_hwnd:
        if restore_focus or u32.IsIconic(root_hwnd) or u32.GetForegroundWindow() != root_hwnd:
            activate_game_window(root_hwnd)
        u32.SetForegroundWindow(root_hwnd)
        time.sleep(0.060)
        if u32.GetForegroundWindow() != root_hwnd:
            return False

    scan_codes = {0x2D: 0x52, 0x28: 0x50, 0x1B: 0x01, 0x0D: 0x1C}
    scan = scan_codes.get(vk_code)
    if scan is None:
        scan = u32.MapVirtualKeyW(vk_code, 0)

    ext_flag = 0x0001 if vk_code in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E} else 0x0000

    key_down = INPUT(type=INPUT_KEYBOARD)
    key_down.ki.wVk = 0
    key_down.ki.wScan = scan & 0xFF
    key_down.ki.dwFlags = 0x0008 | ext_flag  # KEYEVENTF_SCANCODE | ext_flag
    key_down.ki.time = 0
    key_down.ki.dwExtraInfo = 0
    size = ctypes.sizeof(INPUT)

    ctypes.set_last_error(0)
    sent = u32.SendInput(1, ctypes.byref(key_down), size)
    if sent == 1:
        method = "SendInput"
    else:
        u32.keybd_event(vk_code, scan & 0xFF, ext_flag, 0)
        method = "keybd_event"

    time.sleep(max(hold_ms, 1) / 1000.0)

    key_up = INPUT(type=INPUT_KEYBOARD)
    key_up.ki.wVk = 0
    key_up.ki.wScan = scan & 0xFF
    key_up.ki.dwFlags = 0x0008 | 0x0002 | ext_flag  # KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | ext_flag
    key_up.ki.time = 0
    key_up.ki.dwExtraInfo = 0

    if method == "SendInput":
        if u32.SendInput(1, ctypes.byref(key_up), size) != 1:
            u32.keybd_event(vk_code, scan & 0xFF, ext_flag | 0x0002, 0)
    else:
        u32.keybd_event(vk_code, scan & 0xFF, ext_flag | 0x0002, 0)

    global LAST_BOT_KEY_TS
    LAST_BOT_KEY_TS = time.monotonic()
    print(f"[Teclado] VK=0x{vk_code:02X} ({method}=1).", flush=True)
    return True
