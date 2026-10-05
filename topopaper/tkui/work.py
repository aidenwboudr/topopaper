"""Background work for the Tk windows, and the actions they share.

Tk is not thread-safe: workers only ever put results on a queue, and the
Tk thread drains it from an `after` poll. Nothing here touches a widget.
"""
import os
import queue
import subprocess
import sys
import threading

from .. import util

POLL_MS = 40


class Worker:
    """run(fn, *args, done=cb): fn runs on a thread, done(result, error) on Tk's."""

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self._alive = True
        self.root.after(POLL_MS, self._poll)

    def run(self, fn, *args, done=None):
        def work():
            try:
                res, err = fn(*args), None
            except Exception as e:      # noqa: BLE001 - reported to the caller
                res, err = None, e
            self.q.put((done, res, err))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self._alive = False

    def _poll(self):
        if not self._alive:
            return
        while True:
            try:
                done, res, err = self.q.get_nowait()
            except queue.Empty:
                break
            if done is None:
                continue
            try:
                done(res, err)
            except Exception as e:      # noqa: BLE001 - e.g. its page was rebuilt meanwhile
                if not _is_tcl_error(e):
                    import traceback
                    traceback.print_exc()
        try:
            self.root.after(POLL_MS, self._poll)
        except Exception:               # noqa: BLE001 - the window was destroyed
            self._alive = False


def _is_tcl_error(e):
    return type(e).__name__ == "TclError"


# ---- the place search ----------------------------------------------------------
# search.resolve() reports through util.notify (a desktop notification on
# Linux, stderr elsewhere). The windows also want that text, so while a
# resolve runs on a worker its messages are recorded per thread as well.
_tls = threading.local()
_orig_notify = None
_hook_lock = threading.Lock()


def _notify_hook(msg, ms=2600):
    rec = getattr(_tls, "messages", None)
    if rec is not None:
        rec.append(str(msg))
    _orig_notify(msg, ms)


def _install_hook():
    global _orig_notify
    with _hook_lock:
        if util.notify is not _notify_hook:      # first use, or someone swapped it since
            _orig_notify = util.notify
            util.notify = _notify_hook


def resolve_place(text):
    """search.resolve(text) -> (exit code, [messages it notified]). Flies to an
    installed map, or geocodes the text and starts a background build."""
    from .. import net, search
    _install_hook()
    net.prefer_ipv4()
    _tls.messages = []
    try:
        rc = search.resolve(text)
    finally:
        msgs, _tls.messages = _tls.messages, None
    return rc, msgs


def sentence(msg):
    """'couldn't find "x"' -> 'Couldn't find "x"'."""
    msg = (msg or "").strip()
    return msg[:1].upper() + msg[1:]


def resolve_error(text, rc, msgs, err=None):
    """What to tell the user after a failed resolve_place, or '' when it worked."""
    if err is not None:
        return f"The place search failed: {err}"
    if rc == 0:
        return ""
    if msgs:
        return sentence(msgs[-1])
    return f"Couldn’t find “{text}”"


# ---- files ------------------------------------------------------------------------
def open_path(path):
    """Open a file, folder or URL with the desktop's default handler."""
    p = str(path)
    if "://" in p:
        import webbrowser
        webbrowser.open(p)
        return
    if sys.platform.startswith("win"):
        os.startfile(p)
        return
    cmd = ["open", p] if sys.platform == "darwin" else ["xdg-open", p]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def read_tail(path, limit=64 * 1024):
    """The last `limit` bytes of a text file, decoded leniently ('' if missing)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if size > limit:
        text = text.split("\n", 1)[-1]   # drop the cut-off first line
    return text


# ---- the starter globe ----------------------------------------------------------------
class Starter:
    """`topopaper-ctl starter` in the background, its output in starter.log."""

    def __init__(self, log_path):
        self.log_path = str(log_path)
        self.proc = None
        self.state = "idle"             # idle | running | done | failed
        self.error = ""

    def start(self):
        if self.state == "running":
            return True
        cmd, env = util.ctl_cmd("starter")
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "w", encoding="utf-8") as log:
                kw = {}
                if sys.platform.startswith("win"):
                    kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    kw["start_new_session"] = True
                self.proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, **kw)
        except OSError as e:
            self.state, self.error = "failed", str(e)
            return False
        self.state = "running"
        return True

    def poll(self):
        """Current state; call it from the Tk thread's own polling."""
        if self.state == "running" and self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                self.state = "done" if rc == 0 and util.pack_exists("earth") else "failed"
        return self.state

    def as_job(self):
        """The starter as a jobs.list_jobs()-style dict for the Building list."""
        from ..settings import logic
        line = logic.last_line(read_tail(self.log_path, 4096)) or self.error or "starting"
        state = {"running": "running", "done": "done"}.get(self.state, "failed")
        return dict(slug="__globe__", display="The globe", state=state,
                    stage=line, log=self.log_path)
