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
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenProcessToken.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
)
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
)
advapi32.GetTokenInformation.restype = wintypes.BOOL

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

u32.SendMessageTimeoutW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
)
u32.SendMessageTimeoutW.restype = wintypes.LPARAM

u32.WindowFromPoint.argtypes = (wintypes.POINT,)
u32.WindowFromPoint.restype = wintypes.HWND

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


_pending_touch_release = None
_touch_mouse_release = None
_physical_left_state = (0, False)
_tracks_physical_left = False


def track_physical_left(enabled):
    global _tracks_physical_left, _physical_left_state
    _tracks_physical_left = enabled
    _physical_left_state = (_physical_left_state[0] + 1,
                            bool(enabled and u32.GetAsyncKeyState(0x01) & 0x8000))


def observe_physical_left(down):
    global _physical_left_state
    _physical_left_state = (_physical_left_state[0] + 1, down)


def recover_touch_mouse_release():
    """Tenta soltar apenas uma pressão criada por toque do bot, sem interferir no mouse físico."""
    global _touch_mouse_release, _pending_touch_release
    if not (u32.GetAsyncKeyState(0x01) & 0x8000):
        _touch_mouse_release = None
        return False
    pending = _touch_mouse_release
    attempts = 0
    if pending is not None:
        generation, started, attempts = pending
        if not _tracks_physical_left or _physical_left_state != (generation, False):
            _touch_mouse_release = None
            return False
        if time.monotonic() - started < 0.12:
            return False
        _touch_mouse_release = (generation, time.monotonic(), attempts + 1)
    elif _tracks_physical_left and not _physical_left_state[1] and not is_bot_clicking(0.25):
        _touch_mouse_release = (_physical_left_state[0], time.monotonic(), 1)
        attempts = 1
    else:
        return False
    if _pending_touch_release is not None:
        accepted, error = _send_touch_frame(_pending_touch_release, POINTER_FLAG_UP | 0x8000)  # CANCELED
        if accepted or error == 87:
            _pending_touch_release = None
        if not (u32.GetAsyncKeyState(0x01) & 0x8000):
            _touch_mouse_release = None
            return True
    if attempts >= 1:
        inputs = (INPUT * 2)(INPUT(type=INPUT_MOUSE), INPUT(type=INPUT_MOUSE))
        inputs[0].mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        inputs[1].mi.dwFlags = MOUSEEVENTF_LEFTUP
        if u32.SendInput(2, inputs, ctypes.sizeof(INPUT)) != 2:
            u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        release = INPUT(type=INPUT_MOUSE)
        release.mi.dwFlags = MOUSEEVENTF_LEFTUP
        if u32.SendInput(1, ctypes.byref(release), ctypes.sizeof(release)) != 1:
            u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if not (u32.GetAsyncKeyState(0x01) & 0x8000):
        _touch_mouse_release = None
    return True


def _send_touch_frame(point, flags):
    global _touch_mouse_release
    track_down = (bool(flags & POINTER_FLAG_DOWN) and _tracks_physical_left
                  and not _physical_left_state[1] and not u32.GetAsyncKeyState(0x01) & 0x8000)
    physical_generation = _physical_left_state[0]
    contact = POINTER_TOUCH_INFO()
    contact.pointerInfo.pointerType = PT_TOUCH
    contact.pointerInfo.pointerId = 0
    contact.pointerInfo.pointerFlags = flags
    contact.pointerInfo.ptPixelLocation = wintypes.POINT(*point)
    ctypes.set_last_error(0)
    accepted = int(u32.InjectTouchInput(1, ctypes.byref(contact)))
    error = ctypes.get_last_error()
    if accepted and track_down:
        _touch_mouse_release = (physical_generation, time.monotonic(), 0)
    return accepted, error


def release_pending_touch():
    """Solta um contato da automação no ponto onde foi pressionado."""
    global _pending_touch_release
    if _pending_touch_release is None:
        return True
    accepted, error = _send_touch_frame(_pending_touch_release, POINTER_FLAG_UP)
    # O Windows pode já ter cancelado o contato, por exemplo após mudar a resolução.
    if accepted or error == 87:  # ERROR_INVALID_PARAMETER
        _pending_touch_release = None
    return _pending_touch_release is None


def move_cursor_with_touch_hover(sx, sy):
    """Envia apenas hover, sem iniciar um contato pressionado."""
    initialized, _ = ensure_touch_injection()
    if not initialized or not release_pending_touch():
        return False
    accepted, _ = _send_touch_frame((int(sx), int(sy)), 0x00020002)  # UPDATE | INRANGE
    return bool(accepted)


def _move_pointer_with_touch(sx: int, sy: int):
    """Move o ponteiro pela trajetória de toque usada nos cliques do mapa."""
    global _pending_touch_release
    initialized, error = ensure_touch_injection()
    if not initialized:
        return 0, error, 0, 0
    if not release_pending_touch():
        return 0, 0, 0, 0

    sx, sy = int(sx), int(sy)
    cursor = wintypes.POINT()
    points = [(sx, sy)]
    if u32.GetCursorPos(ctypes.byref(cursor)):
        cx, cy = cursor.x, cursor.y
        dx, dy = sx - cx, sy - cy
        distance = math.hypot(dx, dy)
        if distance > 8:
            curve = min(20.0, distance * 0.12)
            control_x = (cx + sx) / 2 - dy / distance * curve
            control_y = (cy + sy) / 2 + dx / distance * curve
            points[:0] = [
                (int((1 - t) ** 2 * cx + 2 * (1 - t) * t * control_x + t ** 2 * sx),
                 int((1 - t) ** 2 * cy + 2 * (1 - t) * t * control_y + t ** 2 * sy))
                for t in (0.2, 0.4, 0.6, 0.8)
            ]

    contact_flags = POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
    down, down_error = _send_touch_frame(points[0], POINTER_FLAG_DOWN | contact_flags)
    if not down:
        return down, down_error, 0, 0
    _pending_touch_release = points[0]
    try:
        for point in points[1:]:
            time.sleep(0.006)
            moved, _ = _send_touch_frame(point, POINTER_FLAG_UPDATE | contact_flags)
            if not moved:
                break
            _pending_touch_release = point
        time.sleep(0.006)
    finally:
        up, up_error = _send_touch_frame(_pending_touch_release, POINTER_FLAG_UP)
        if up or up_error == 87:
            _pending_touch_release = None
    return down, down_error, up, up_error


def tap_touch(point, *, hold=0.10):
    """Conclui um único toque estacionário numa posição já escolhida."""
    global _pending_touch_release
    initialized, error = ensure_touch_injection()
    if not initialized or not release_pending_touch():
        return 0, error, 0, 0
    down, down_error = _send_touch_frame(point, 0x00010006)  # DOWN | INRANGE | INCONTACT
    if not down:
        return down, down_error, 0, 0

    _pending_touch_release = point
    try:
        if hold >= 0.06:
            time.sleep(hold / 2)
            _send_touch_frame(point, 0x00020006)  # UPDATE | INRANGE | INCONTACT
            time.sleep(hold / 2)
        else:
            time.sleep(hold)
    finally:
        up, up_error = _send_touch_frame(point, 0x00040000)  # UP
        if up or up_error == 87:
            _pending_touch_release = None
    return down, down_error, up, up_error


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


def touch_postmsg(sx, sy, hwnd=None, root_hwnd=None, *, is_focused=None, debug=None, log=None, is_admin=False, test_mode=False, target_point=None, cursor_tolerance=3, pickup_click=False, hover_ms=None, hold_ms=None, **kwargs):
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

        if pickup_click:
            def aim_is_safe():
                return ((not callable(is_focused) or is_focused())
                        and not u32.GetAsyncKeyState(0x01) & 0x8000)

            target = target_point(0.075) if callable(target_point) else (sx, sy)
            if target is None or not aim_is_safe() or not release_pending_touch():
                return False
            target = tuple(map(int, target))
            if not move_cursor_with_touch_hover(*target):
                u32.SetCursorPos(*target)
            _wait_cursor_arrival(*target, tolerance=2)
            hover_s = (max(0, int(hover_ms)) / 1000.0) if hover_ms is not None else 0.055
            hold_s = (max(10, int(hold_ms)) / 1000.0) if hold_ms is not None else 0.085
            time.sleep(hover_s)
            latest = target_point(0.022) if callable(target_point) else target
            cursor = wintypes.POINT()
            if (latest is None or not aim_is_safe() or not u32.GetCursorPos(ctypes.byref(cursor))
                    or math.dist((cursor.x, cursor.y), latest) > 2):
                return False
            latest = tuple(map(int, latest))
            hit = u32.WindowFromPoint(wintypes.POINT(*latest))
            if not hit or u32.GetAncestor(hit, 2) != (u32.GetAncestor(game_hwnd, 2) or game_hwnd):
                return False
            down, down_error, up, up_error = tap_touch(latest, hold=hold_s)
            if down:
                if callable(log):
                    log(f"[Loot] Toque em {latest}: DOWN={down}/{down_error}, UP={up}/{up_error}.")
                return bool(up or release_pending_touch())
            pressed_at, failed, error = click_window_mouse(game_hwnd, latest, ready=aim_is_safe, hold=hold_s)
            if callable(log):
                log(f"[Loot] Clique em {latest}: toque recusado ({down_error}), "
                    f"Win32={failed or 'enviado'}/{error}.")
            return pressed_at is not None and failed is None

        # Atualiza a mira de sprites móveis depois do hover, antes de pressionar.
        touch_delivered = False
        for attempt in range(3):
            if u32.GetAsyncKeyState(0x01) & 0x8000:
                return False
            if callable(target_point):
                point = target_point(0.075)
                if point is None:
                    return False
                sx, sy = map(int, point)
            down, _, up, _ = _move_pointer_with_touch(sx, sy)
            pt_cur = _wait_cursor_arrival(sx, sy, tolerance=cursor_tolerance)
            touch_delivered = bool(down and up and math.dist((pt_cur.x, pt_cur.y), (sx, sy)) <= 5)
            general_hover_s = (max(0, int(hover_ms)) / 1000.0) if hover_ms is not None else 0.045
            time.sleep(general_hover_s)
            if not callable(target_point):
                break
            point = target_point(0.022)
            if point is None:
                return False
            if math.dist((sx, sy), point) <= 5:
                click_point = tuple(map(int, point))
                if cursor_tolerance == 1 and math.dist((sx, sy), click_point) > 1:
                    if u32.GetAsyncKeyState(0x01) & 0x8000:
                        return False
                    down, _, up, _ = _move_pointer_with_touch(*click_point)
                    pt_cur = _wait_cursor_arrival(*click_point, tolerance=1)
                    touch_delivered |= bool(down and up and math.dist((pt_cur.x, pt_cur.y), click_point) <= 2)
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
        if u32.GetAsyncKeyState(0x01) & 0x8000:
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
        post_errors = []
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
            general_hold_s = (max(10, int(hold_ms)) / 1000.0) if hold_ms is not None else 0.050
            time.sleep(general_hold_s)

            ctypes.set_last_error(0)
            r_up = int(u32.PostMessageW(target, 0x0202, 0, lparam))
            err_up = ctypes.get_last_error()
            if r_down and not r_up:
                result = ctypes.c_size_t()
                ctypes.set_last_error(0)
                r_up = int(bool(u32.SendMessageTimeoutW(
                    target, 0x0202, 0, lparam, 0x0002, 250, ctypes.byref(result),
                )))
                err_up = ctypes.get_last_error()
            clicked |= bool(r_down and r_up)
            if not (r_move and r_down and r_up):
                post_errors.append(f"0x{target:X}: MOVE={r_move}/{err_move}, "
                                   f"DOWN={r_down}/{err_down}, UP={r_up}/{err_up}")

        msg = f"touch_postmsg ({sx},{sy}) targets={len(targets)} cur=({pt_cur.x},{pt_cur.y})"
        if post_errors:
            msg += " PostMessageW: " + " | ".join(post_errors)
        if callable(log):
            if getattr(log, "__name__", "") == "log_click_file":
                log(msg)
            else:
                log(f"[ClickDebug] {msg}")
        return clicked or touch_delivered
    finally:
        LAST_BOT_CLICK_TS = time.monotonic()
        _BOT_CLICKING = False
        _TOUCH_CLICK_LOCK.release()


move_and_click = patrol_move_and_click = touch_postmsg


def is_window_process_elevated(hwnd):
    """Consulta a elevação do processo da janela, ou retorna None se indisponível."""
    pid = wintypes.DWORD()
    if not u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        elevation = wintypes.DWORD()
        returned_size = wintypes.DWORD()
        if advapi32.GetTokenInformation(
            token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(returned_size),
        ):  # TokenElevation
            return bool(elevation.value)
        return None
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def click_window_mouse(hwnd, point, *, ready, hold=0.07):
    """Envia um clique síncrono para a janela sob o ponto, no cliente do jogo."""
    root = u32.GetAncestor(hwnd, 2) or hwnd
    target = u32.WindowFromPoint(wintypes.POINT(*point))
    if not target or u32.GetAncestor(target, 2) != root:
        return None, "WindowFromPoint", 0

    client = wintypes.POINT(*point)
    ctypes.set_last_error(0)
    if not u32.ScreenToClient(target, ctypes.byref(client)):
        return None, "ScreenToClient", ctypes.get_last_error()

    wm_move, wm_down, wm_up = 0x0200, 0x0201, 0x0202
    ht_client, mk_lbutton = 1, 1
    coords = ((client.y & 0xFFFF) << 16) | (client.x & 0xFFFF)
    result = ctypes.c_size_t()

    def send(message, wparam, lparam):
        ctypes.set_last_error(0)
        delivered = u32.SendMessageTimeoutW(
            target, message, wparam, lparam, 0x0002, 250, ctypes.byref(result),
        )  # SMTO_ABORTIFHUNG
        return bool(delivered), ctypes.get_last_error()

    for message, wparam, lparam in (
        (0x0021, root, (wm_down << 16) | ht_client),  # WM_MOUSEACTIVATE
        (0x0020, target, (wm_move << 16) | ht_client),  # WM_SETCURSOR
        (wm_move, 0, coords),
    ):
        delivered, error = send(message, wparam, lparam)
        if not delivered:
            return None, f"0x{message:04X}", error

    if not ready() or u32.WindowFromPoint(wintypes.POINT(*point)) != target:
        return None, "mira", 0

    pressed_at = time.monotonic()
    try:
        down, down_error = send(wm_down, mk_lbutton, coords)
        if down:
            time.sleep(hold)
    finally:
        up, up_error = send(wm_up, 0, coords)
    if not up and up_error == 5:
        return pressed_at if down else None, "WM_LBUTTONUP", up_error
    if not down:
        return None, "WM_LBUTTONDOWN", down_error
    if not up:
        return pressed_at, "WM_LBUTTONUP", up_error
    return pressed_at, None, 0


def send_window_alt_action(hwnd, *, key=None, point=None):
    """Envia Alt com uma tecla ou botão direito para a janela cliente em foco."""
    root = u32.GetAncestor(hwnd, 2) or hwnd
    foreground = u32.GetForegroundWindow()
    if not foreground or u32.GetAncestor(foreground, 2) != root:
        return False

    target = hwnd
    if key is None:
        target = u32.WindowFromPoint(wintypes.POINT(*point))
        client = wintypes.POINT(*point)
        if (not target or u32.GetAncestor(target, 2) != root
                or not u32.ScreenToClient(target, ctypes.byref(client))):
            return False
        coords = ((client.y & 0xFFFF) << 16) | (client.x & 0xFFFF)
        down, up = (0x0204, 0x0002, coords), (0x0205, 0, coords)  # WM_RBUTTON, MK_RBUTTON
    else:
        scan = u32.MapVirtualKeyW(key, 0)
        if not scan:
            return False
        key_info = 1 | (scan << 16) | (1 << 29)
        down, up = (0x0104, key, key_info), (0x0105, key, key_info | (3 << 30))

    alt_info = 1 | (u32.MapVirtualKeyW(0x12, 0) << 16)
    result = ctypes.c_size_t()

    def send(message, wparam, lparam):
        return bool(u32.SendMessageTimeoutW(target, message, wparam, lparam, 0x0002, 250, ctypes.byref(result)))

    try:
        if not send(0x0104, 0x12, alt_info):  # WM_SYSKEYDOWN, VK_MENU
            return False
        if u32.GetForegroundWindow() != foreground:
            return False
        if key is None and not send(0x0200, 0, coords):
            return False
        try:
            pressed = send(*down)
            if pressed:
                time.sleep(0.04)
        finally:
            released = send(*up)
    finally:
        alt_released = send(0x0105, 0x12, alt_info | (7 << 29))  # WM_SYSKEYUP
    return pressed and released and alt_released


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
