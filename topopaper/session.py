"""topopaper-session: what autostart runs. Supervises the wallpaper.

    engine     restarted when it crashes (backoff; gives up after repeated
               fast crashes and says so)
    watcher    `topopaper-ctl watch` (covered flag), restarted if it dies
               (Linux; on Windows and macOS the engine sees occlusion itself)
    weather    built-in Open-Meteo fetch every 15 min, sooner after a failure,
               a network change or a resume from suspend
    starter    no packs at all -> fetch the starter globe once, in the background
    hotkey     Windows: the search shortcut (search_hotkey), via RegisterHotKey

One session per user (a lock on <runtime dir>/topopaper-session.lock; the
pid sits next to it). Signals: TERM/INT stop everything, USR1 restarts the
engine (that is what `topopaper-ctl restart` sends). Windows has no signals:
there `stop`/`restart` drop a request file next to the lock, polled every
second. Log: <state dir>/logs/session.log (engine output: engine.log).

No GUI toolkit here: the settings app imports this module for is_running(),
start_detached(), restart() and stop().
"""
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, paths, system, util

WEATHER_EVERY = 15 * 60
WEATHER_RETRY = (60, 120, 300, 600)        # after failures, then back to 15 min
FAST_CRASH_S = 30.0                         # an engine run shorter than this "crashed fast"
MAX_FAST_CRASHES = 5
WATCH_RESTART_S = 30.0
LOG_MAX = 1_000_000
# the engine exits on SIGTERM, but only once its Wayland dispatch wakes up;
# a fully covered surface may get no frame callbacks, so don't wait long
ENGINE_GRACE = 1.5

log = logging.getLogger("topopaper.session")


# ---- locations ------------------------------------------------------------------
def lock_file():
    return paths.runtime_dir() / "topopaper-session.lock"


def pid_file():
    return paths.runtime_dir() / "topopaper-session.pid"


def request_file(what):
    """Windows: `stop`/`restart` request files the session polls."""
    return paths.runtime_dir() / f"topopaper-session.{what}"


def find_engine():
    """The engine binary: $TOPOPAPER_ENGINE, else beside the launcher
    ($TOPOPAPER_BIN, set by bin/topopaper-ctl), a source checkout's build/,
    an install's $PREFIX/bin, then PATH."""
    cands = []
    if os.environ.get("TOPOPAPER_ENGINE"):
        cands.append(Path(os.environ["TOPOPAPER_ENGINE"]))
    exe = system.engine_name()
    if os.environ.get("TOPOPAPER_BIN"):
        cands.append(Path(os.environ["TOPOPAPER_BIN"]) / exe)
    root = paths.PKG.parent
    cands.append(root / "build" / exe)                       # <repo>/build/topopaper
    cands.append(root / "build" / "windows" / exe)
    cands.append(root.parent.parent / "bin" / exe)           # $PREFIX/share/topopaper -> $PREFIX/bin
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which("topopaper")


# ---- library API (settings app, CLI) ------------------------------------------------
def read_pid():
    try:
        return int(pid_file().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def is_running():
    """True while a session holds the lock (a stale pid file doesn't count)."""
    try:
        with open(lock_file(), "a") as f:
            if not system.try_lock(f):
                return True
            system.unlock(f)
            return False
    except OSError:
        return False


def session_cmd():
    _, env = util.ctl_cmd()
    return [system.gui_python(), "-m", "topopaper.session"], env


def start_detached():
    """Start a session in the background (own process group, survives the
    caller). Returns True when one is running afterwards."""
    if is_running():
        return True
    cmd, env = session_cmd()
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **system.detached())
    for _ in range(30):
        if is_running():
            return True
        time.sleep(0.1)
    return is_running()


def restart(argv=None):
    """Restart the engine of the running session, or start a session."""
    pid = read_pid()
    if is_running() and pid:
        try:
            if system.WINDOWS:
                util.write_atomic(str(request_file("restart")), "1\n")
            else:
                os.kill(pid, signal.SIGUSR1)
            if argv is not None:
                print("topopaper: restarting the wallpaper")
            return 0
        except OSError:
            pass
    ok = start_detached()
    if argv is not None:
        print("topopaper: started the wallpaper session" if ok else
              f"topopaper: could not start the session; see {paths.log_dir() / 'session.log'}")
    return 0 if ok else 1


def stop(argv=None, timeout=8.0):
    """Stop the running session (and its engine). 0 when nothing runs after."""
    pid = read_pid()
    if not is_running() or not pid:
        if argv is not None:
            print("topopaper: no session running")
        return 0
    try:
        if system.WINDOWS:
            util.write_atomic(str(request_file("stop")), "1\n")
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not is_running():
            if argv is not None:
                print("topopaper: stopped")
            return 0
        time.sleep(0.1)
    if argv is not None:
        print("topopaper: session did not stop in time", file=sys.stderr)
    return 1


# ---- the session ---------------------------------------------------------------------
def setup_logging(foreground):
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(paths.log_dir() / "session.log",
                                             maxBytes=LOG_MAX, backupCount=1)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    if foreground and sys.stderr is not None and sys.stderr.isatty():
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def open_log(name):
    """Append handle for a child's output; truncates past LOG_MAX."""
    p = paths.log_dir() / name
    try:
        if p.stat().st_size > LOG_MAX:
            p.rename(p.with_suffix(".log.1"))
    except OSError:
        pass
    return open(p, "ab")


def die_with(sig):
    """Popen kwargs: on Linux the child gets signal `sig` (a name: Windows has
    no SIGKILL) if the session itself is killed -9, so a crashed session never
    leaves an orphan engine drawing. (Elsewhere the engine watches
    TOPOPAPER_PARENT itself.)"""
    if not system.LINUX:
        return system.quiet()
    num = getattr(signal, sig)

    def pre():
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(1, num)     # PR_SET_PDEATHSIG
        except (OSError, AttributeError):
            pass
    return {"preexec_fn": pre}


def stop_proc(p, name, grace=3.0):
    if p is None or p.poll() is not None:
        return
    if system.WINDOWS and name == "engine" and system.close_engine_windows():
        try:                                # it hands the desktop back on WM_CLOSE
            p.wait(grace)
            return
        except subprocess.TimeoutExpired:
            pass
    p.terminate()
    try:
        p.wait(grace)
    except subprocess.TimeoutExpired:
        log.info("%s ignored SIGTERM; killing", name)
        p.kill()
        p.wait()


def wayland_gone():
    """The compositor exited (its socket vanished): the session should too."""
    disp = os.environ.get("WAYLAND_DISPLAY")
    if not disp:
        return False
    sock = Path(disp) if os.path.isabs(disp) else paths.runtime_dir() / disp
    return not sock.exists()


def net_signature():
    """Cheap fingerprint of the routing table: changes on (re)connects."""
    try:
        return Path("/proc/net/route").read_text() + Path("/proc/net/ipv6_route").read_text()
    except OSError:
        return ""


class Session:
    def __init__(self, engine, watch=True, weather=True):
        self.engine_path = engine
        self.engine = None
        self.engine_started = 0.0
        self.fast_crashes = 0
        self.next_engine = 0.0
        self.want_watch = watch and system.LINUX
        self.watcher = None
        self.watch_started = 0.0
        self.want_weather = weather
        self.weather_thread = None
        self.weather_fails = 0
        self.weather_ok_once = False
        self.next_weather = 0.0
        self.stopping = False
        self.restart_req = False
        self.net = net_signature()
        self.hotkey = None                  # Windows: (keys, thread)

    # engine
    def start_engine(self):
        log.info("starting engine %s", self.engine_path)
        out = open_log("engine.log")
        env = dict(os.environ, TOPOPAPER_PARENT=str(os.getpid()))
        try:
            self.engine = subprocess.Popen([self.engine_path], stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=subprocess.STDOUT, env=env,
                                           **die_with("SIGKILL"))
        except OSError as e:
            log.info("engine failed to start: %s", e)
            self.engine = None
        finally:
            out.close()
        self.engine_started = time.monotonic()

    def check_engine(self, now):
        if self.restart_req:
            self.restart_req = False
            log.info("restart requested")
            stop_proc(self.engine, "engine", ENGINE_GRACE)
            self.fast_crashes = 0
            self.start_engine()
            return True
        if self.engine is not None and self.engine.poll() is None:
            if self.fast_crashes and now - self.engine_started > FAST_CRASH_S:
                self.fast_crashes = 0       # it has been up a while: forgive
            return True
        if self.engine is not None:         # it exited
            rc = self.engine.returncode
            self.engine = None
            if wayland_gone():
                log.info("engine exited (%s) and the compositor is gone; ending session", rc)
                return False
            up = now - self.engine_started
            self.fast_crashes = self.fast_crashes + 1 if up < FAST_CRASH_S else 1
            if self.fast_crashes >= MAX_FAST_CRASHES:
                log.info("engine crashed %d times in a row; giving up", self.fast_crashes)
                util.notify("the wallpaper keeps crashing; giving up. See "
                            f"{paths.log_dir() / 'engine.log'} or run `topopaper-ctl doctor`", 8000)
                return False
            delay = min(60, 2 ** self.fast_crashes)
            log.info("engine exited with %s after %.0fs; restarting in %ds", rc, up, delay)
            self.next_engine = now + delay
        if now >= self.next_engine:
            self.start_engine()
        return True

    # watcher
    def check_watch(self, now):
        if not self.want_watch:
            return
        if self.watcher is not None and self.watcher.poll() is None:
            return
        if self.watcher is not None:
            log.info("watcher exited (%s)", self.watcher.returncode)
            self.watcher = None
        if now - self.watch_started < WATCH_RESTART_S and self.watch_started:
            return
        cmd, env = util.ctl_cmd("watch")
        out = open_log("watch.log")
        try:
            self.watcher = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                            stdout=out, stderr=subprocess.STDOUT,
                                            **die_with("SIGTERM"))
        finally:
            out.close()
        self.watch_started = now

    # weather
    def weather_wanted(self):
        if not self.want_weather:
            return False
        cfg = config.load()
        return cfg.get("builtin_weather") and (cfg.get("show_weather") or cfg.get("show_news"))

    def weather_soon(self, now, delay, why):
        if self.next_weather > now + delay:
            log.info("weather refresh in %ds (%s)", delay, why)
            self.next_weather = now + delay

    def check_weather(self, now):
        if self.weather_thread is not None and self.weather_thread.is_alive():
            return
        sig = net_signature()
        if sig != self.net:
            self.net = sig
            if self.weather_fails or now - self.next_weather + WEATHER_EVERY > 60:
                self.weather_soon(now, 10, "network changed")
        if now < self.next_weather or not self.weather_wanted():
            return
        self.next_weather = now + WEATHER_EVERY      # until the result says otherwise
        self.weather_thread = threading.Thread(target=self._weather, daemon=True)
        self.weather_thread.start()

    def _weather(self):
        from . import weather
        try:
            ok = weather.refresh()
        except Exception as e:              # never let a fetch take the session down
            log.info("weather crashed: %r", e)
            ok = False
        now = time.monotonic()
        if ok:
            if self.weather_fails or not self.weather_ok_once:
                log.info("weather updated")
            self.weather_ok_once = True
            self.weather_fails = 0
            self.next_weather = now + WEATHER_EVERY
        else:
            delay = WEATHER_RETRY[min(self.weather_fails, len(WEATHER_RETRY) - 1)]
            self.weather_fails += 1
            log.info("weather fetch failed; retrying in %ds", delay)
            self.next_weather = now + delay

    # lifecycle
    def maybe_starter(self):
        if util.list_packs():
            return
        log.info("no maps installed: fetching the starter globe in the background")
        cmd, env = util.ctl_cmd("starter")
        with open_log("starter.log") as out:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, **system.detached())

    # Windows: requests from `topopaper-ctl stop/restart`, and the search hotkey
    def check_requests(self):
        for what in ("stop", "restart"):
            f = request_file(what)
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
                log.info("%s requested", what)
                if what == "stop":
                    self.stopping = True
                else:
                    self.restart_req = True

    def check_hotkey(self):
        from . import hotkey
        keys = config.load().get("search_hotkey").strip()
        cur = self.hotkey[0] if self.hotkey else ""
        if keys == cur and (not self.hotkey or self.hotkey[1].is_alive()):
            return
        if self.hotkey:
            hotkey.stop(self.hotkey[1])
            self.hotkey = None
        if keys:
            self.hotkey = (keys, hotkey.start(keys, self.open_search))

    def open_search(self):
        cmd, env = util.ctl_cmd("search")
        cmd[0] = system.gui_python()
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **system.detached())

    def run(self):
        self.maybe_starter()
        self.start_engine()
        last_wall, last_mono = time.time(), time.monotonic()
        while not self.stopping:
            now = time.monotonic()
            # monotonic time stops while suspended, wall time doesn't
            if (time.time() - last_wall) - (now - last_mono) > 60:
                log.info("resumed from suspend")
                self.weather_soon(now, 15, "resume")
            last_wall, last_mono = time.time(), now
            if system.WINDOWS:
                self.check_requests()
                self.check_hotkey()
            if self.stopping:
                break
            if not self.check_engine(now):
                break
            self.check_watch(now)
            self.check_weather(now)
            time.sleep(1.0)
        return self.stopping            # False: gave up on the engine

    def shutdown(self):
        if self.engine is None and self.watcher is None and not self.hotkey:
            return
        log.info("stopping")
        stop_proc(self.engine, "engine", ENGINE_GRACE)
        stop_proc(self.watcher, "watcher")
        self.engine = self.watcher = None
        if self.hotkey:
            from . import hotkey
            hotkey.stop(self.hotkey[1])
            self.hotkey = None


def acquire_lock():
    """The open, flocked lock file, or None if a session already runs."""
    paths.runtime_dir().mkdir(parents=True, exist_ok=True)
    f = open(lock_file(), "a")
    if not system.try_lock(f):
        f.close()
        return None
    return f


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print("usage: topopaper-session [--no-watch] [--no-weather]\n\n"
              "Runs the wallpaper engine plus its helpers. Autostart this.\n"
              "topopaper-ctl restart / stop control a running session.")
        return 0
    lock = acquire_lock()
    if lock is None:
        print("topopaper: a session is already running (topopaper-ctl restart to restart it)",
              file=sys.stderr)
        return 0
    setup_logging(foreground=True)
    engine = find_engine()
    if not engine:
        log.info("engine binary not found")
        util.notify("can't find the topopaper engine binary; was it built/installed?", 6000)
        return 1
    util.write_atomic(str(pid_file()), f"{os.getpid()}\n")
    s = Session(engine, watch="--no-watch" not in argv, weather="--no-weather" not in argv)

    def on_stop(signum, _frame):
        log.info("got signal %d", signum)
        s.stopping = True

    def on_restart(*_):
        s.restart_req = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    if not system.WINDOWS:
        signal.signal(signal.SIGHUP, on_stop)
        signal.signal(signal.SIGUSR1, on_restart)
    for what in ("stop", "restart"):                # stale requests from a dead session
        try:
            request_file(what).unlink()
        except OSError:
            pass
    log.info("session %d up (engine %s)", os.getpid(), engine)
    ok = True
    try:
        ok = s.run()
    except Exception:
        log.exception("session crashed")    # under pythonw.exe there is no stderr
        raise
    finally:
        s.shutdown()
        try:
            if read_pid() == os.getpid():
                pid_file().unlink()
        except OSError:
            pass
        lock.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
