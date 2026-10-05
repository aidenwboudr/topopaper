"""The few things that differ between Linux, macOS and Windows outside the
engine: process control, file locks, notifications, opening folders.

Linux and macOS share the POSIX paths (signals, flock); Windows has no
signals between processes, so the session there is stopped and restarted
through request files it polls, and the engine through a WM_CLOSE to its
controller window.
"""
import os
import shutil
import subprocess
import sys

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
LINUX = not (WINDOWS or MACOS)

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


def name():
    """'linux', 'macos' or 'windows'."""
    return "windows" if WINDOWS else "macos" if MACOS else "linux"


# ---- locks ---------------------------------------------------------------------------
def try_lock(f):
    """Non-blocking exclusive lock on an open file; True when we hold it."""
    try:
        if WINDOWS:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(f):
    try:
        if WINDOWS:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


# ---- processes -------------------------------------------------------------------------
def pid_alive(pid):
    """True while process `pid` runs. (os.kill(pid, 0) would send CTRL_C on Windows.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def detached():
    """Popen kwargs for a process that outlives its caller, with no console."""
    if WINDOWS:     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        return {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}
    return {"start_new_session": True}


def quiet():
    """Popen kwargs for a child that must not flash a console window."""
    return {"creationflags": 0x08000000} if WINDOWS else {}


def gui_python():
    """The interpreter for windowed processes: pythonw.exe on Windows (no
    console window), else this one."""
    if WINDOWS:
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(w):
            return w
    return sys.executable


def engine_name():
    return "topopaper.exe" if WINDOWS else "topopaper"


def engine_running():
    """Any topopaper engine of this user running (not just the session's)."""
    try:
        if WINDOWS:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq topopaper.exe", "/NH"],
                               capture_output=True, text=True, check=False, **quiet())
            return "topopaper.exe" in r.stdout
        r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "topopaper"],
                           capture_output=True, check=False)
        return r.returncode == 0
    except OSError:
        return False


def close_engine_windows():
    """Windows: ask every running engine to exit cleanly (WM_CLOSE to its
    controller window); it then hands the desktop back. Count asked."""
    if not WINDOWS:
        return 0
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.FindWindowExW.restype = wintypes.HWND
    u32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    n, h = 0, None
    while True:
        h = u32.FindWindowExW(None, h, "topopaperController", None)
        if not h:
            return n
        u32.PostMessageW(h, 0x0010, 0, 0)                 # WM_CLOSE
        n += 1


# ---- the desktop ----------------------------------------------------------------------
def notify(title, msg, ms=2600):
    """A desktop notification where one is cheap to send."""
    try:
        if LINUX and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-t", str(ms), "-h",
                            "string:x-canonical-private-synchronous:topopaper", title, msg],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif MACOS:
            esc = msg.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display notification "{esc}" with title "{title}"'],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def open_path(path):
    """Show a folder or file in the system's file manager / default app."""
    path = str(path)
    if WINDOWS:
        os.startfile(path)                                  # noqa: S606
        return
    tool = "open" if MACOS else "xdg-open"
    subprocess.Popen([tool, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detached())
