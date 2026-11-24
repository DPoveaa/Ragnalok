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


# Assinaturas estritas na instância dedicada u32
u32.SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
u32.SetCursorPos.restype = wintypes.BOOL

u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.GetCursorPos.restype = wintypes.BOOL

u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u32.SendInput.restype = wintypes.UINT

u32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
u32.MapVirtualKeyW.restype = wintypes.UINT

u32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
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


def move_cursor_relative(sx: int, sy: int) -> tuple[int, int] | None:
    """Aproxima o cursor do alvo por deltas relativos com realimentação."""
    point = wintypes.POINT()
    if not u32.GetCursorPos(ctypes.byref(point)):
        return None

    distance = math.hypot(sx - point.x, sy - point.y)
    steps = max(8, min(16, math.ceil(distance / 65)))
    gain = 1.0
    stalls = 0
    for remaining in range(steps, 0, -1):
        error_x, error_y = sx - point.x, sy - point.y
        distance = math.hypot(error_x, error_y)
        if distance <= 2:
            break

        # Reserva os dois passos finais para corrigir a posição observada.
        correction = remaining <= 2
        divisor = max(1, remaining - 2) * (1.0 if correction and distance <= 6 else gain)
        dx, dy = round(error_x / divisor), round(error_y / divisor)
        if not correction:
            if abs(dx) > 2:
                dx += random.randint(-1, 1)
            if abs(dy) > 2:
                dy += random.randint(-1, 1)
        if not (dx or dy):
            if abs(error_x) >= abs(error_y):
                dx = 1 if error_x > 0 else -1
            else:
                dy = 1 if error_y > 0 else -1

        previous_x, previous_y = point.x, point.y
        u32.mouse_event(
            0x0001,
            ctypes.c_uint32(int(dx)).value,
            ctypes.c_uint32(int(dy)).value,
            0,
            0,
        )
        time.sleep(random.uniform(0.004, 0.006))
        if not u32.GetCursorPos(ctypes.byref(point)):
            return None
        observed = math.hypot(point.x - previous_x, point.y - previous_y)
        if observed:
            stalls = 0
            gain = max(0.25, min(4.0, observed / math.hypot(dx, dy)))
        else:
            stalls += 1
            if stalls >= 3:
                break

    return point.x, point.y


def _cursor_position():
    point = wintypes.POINT()
    if u32.GetCursorPos(ctypes.byref(point)):
        return point.x, point.y
    return None


def _at_target(position, sx, sy):
    return position is not None and math.dist(position, (sx, sy)) <= 2


def _notify_mouse_move(hwnd, lparam):
    ctypes.set_last_error(0)
    if u32.PostMessageW(hwnd, 0x0200, 0, lparam):
        return True, 0
    ctypes.set_last_error(0)
    sent = bool(u32.SendNotifyMessageW(hwnd, 0x0200, 0, lparam))
    return sent, 0 if sent else ctypes.get_last_error()


def _send_mouse_event(event):
    ctypes.set_last_error(0)
    sent = u32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) == 1
    return sent, 0 if sent else ctypes.get_last_error()


def _send_mouse_button(flag):
    event = INPUT(type=INPUT_MOUSE)
    event.mi.dwFlags = flag
    return _send_mouse_event(event)


def _land_cursor(sx, sy, position, is_focused):
    if _at_target(position, sx, sy) or not is_focused():
        return position, "relative"

    left, top, width, height = (u32.GetSystemMetrics(metric) for metric in (76, 77, 78, 79))
    if not (left <= sx < left + width and top <= sy < top + height):
        return position, "outside_virtual_screen"

    # SendInput recebe coordenadas normalizadas no centro do pixel físico.
    move = INPUT(type=INPUT_MOUSE)
    move.mi.dx = ((sx - left) * 65536 + 32768) // width
    move.mi.dy = ((sy - top) * 65536 + 32768) // height
    move.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | 0x4000 | 0x2000
    sent, error = _send_mouse_event(move)  # VIRTUALDESK | MOVE_NOCOALESCE
    if sent:
        for _ in range(6):
            position = _cursor_position()
            if _at_target(position, sx, sy) or not is_focused():
                return position, "SendInput"
            time.sleep(0.010)

    if not is_focused():
        return position, f"SendInput={int(sent)}:{error}"
    ctypes.set_last_error(0)
    moved = bool(u32.SetCursorPos(sx, sy))
    cursor_error = 0 if moved else ctypes.get_last_error()
    return _cursor_position(), f"SendInput={int(sent)}:{error},SetCursorPos={int(moved)}:{cursor_error}"


def move_and_click(sx, sy, hwnd=None, root_hwnd=None, *, is_focused=None, debug=None, log=None, is_admin=False):
    """Posiciona o cursor com u32.mouse_event em malha fechada e dispara o clique físico."""
    global LAST_BOT_CLICK_TS, _BOT_CLICKING
    _BOT_CLICKING = True
    LAST_BOT_CLICK_TS = time.monotonic()
    button_down = False
    try:
        ensure_input_desktop()
        if callable(is_focused) and not is_focused():
            if root_hwnd:
                u32.BringWindowToTop(root_hwnd)
                u32.SetForegroundWindow(root_hwnd)
                time.sleep(0.030)

        target_x, target_y = int(sx), int(sy)
        pt = wintypes.POINT()
        if u32.GetCursorPos(ctypes.byref(pt)):
            cx, cy = pt.x, pt.y
        else:
            cx, cy = target_x, target_y
        start_x, start_y = cx, cy

        steps = 0
        for step_idx in range(12):
            rem_x = target_x - cx
            rem_y = target_y - cy
            if abs(rem_x) <= 2 and abs(rem_y) <= 2:
                break

            if step_idx >= 10:
                step_dx = rem_x
                step_dy = rem_y
            else:
                step_dx = int(round(rem_x * 0.65))
                step_dy = int(round(rem_y * 0.65))

            if rem_x != 0 and step_dx == 0:
                step_dx = 1 if rem_x > 0 else -1
            if rem_y != 0 and step_dy == 0:
                step_dy = 1 if rem_y > 0 else -1

            u32.mouse_event(
                0x0001,
                ctypes.c_uint32(int(step_dx)).value,
                ctypes.c_uint32(int(step_dy)).value,
                0,
                0,
            )
            steps += 1
            time.sleep(0.004)
            if u32.GetCursorPos(ctypes.byref(pt)):
                cx, cy = pt.x, pt.y

        if u32.GetCursorPos(ctypes.byref(pt)):
            end_x, end_y = pt.x, pt.y
        else:
            end_x, end_y = cx, cy

        time.sleep(0.045)
        u32.mouse_event(0x0002, 0, 0, 0, 0)
        button_down = True
        time.sleep(0.050)
        u32.mouse_event(0x0004, 0, 0, 0, 0)
        button_down = False

        log_line = f"move_and_click ({target_x},{target_y}) start=({start_x},{start_y}) end=({end_x},{end_y}) steps={steps} clicked=True"
        if callable(log):
            if getattr(log, "__name__", "") == "log_click_file":
                log(log_line)
            else:
                log(f"[ClickDebug] {log_line}")
        else:
            print(f"[ClickDebug] {log_line}", flush=True)

        return True
    finally:
        if button_down:
            try:
                u32.mouse_event(0x0004, 0, 0, 0, 0)
            except Exception:
                pass
        LAST_BOT_CLICK_TS = time.monotonic()
        _BOT_CLICKING = False


patrol_move_and_click = touch_postmsg = move_and_click


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
