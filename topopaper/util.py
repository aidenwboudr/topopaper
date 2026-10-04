"""Small helpers shared by the CLI, the session and the settings app."""
import json
import os
import re
import shutil
import subprocess

from . import paths

NOTIFY_HINT = "string:x-canonical-private-synchronous:topopaper"


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def notify(msg, ms=2600):
    """Desktop notification if a notifier exists; always echo to stderr."""
    print(f"topopaper: {msg}", file=os.sys.stderr, flush=True)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-t", str(ms), "-h", NOTIFY_HINT,
                        "topopaper", msg], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- packs ------------------------------------------------------------------
def read_meta(name):
    try:
        with open(paths.areas_dir() / name / "meta.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_packs(include_scaffold=True):
    """[(name, meta)] for every complete pack, sorted by name."""
    root = paths.areas_dir()
    out = []
    if not root.is_dir():
        return out
    for d in sorted(os.listdir(root)):
        if not (root / d / "terrain.bin").is_file():
            continue
        m = read_meta(d) or {}
        if m.get("scaffold") and not include_scaffold:
            continue
        out.append((d, m))
    return out


def pack_exists(name):
    return bool(name) and (paths.areas_dir() / name / "terrain.bin").is_file()


def current_area():
    try:
        return paths.area_file().read_text().split()[0]
    except (OSError, IndexError):
        return ""


def fly(name, label=None, quiet=False):
    """Ask the running engine to fly to a pack (it polls the area file)."""
    write_atomic(str(paths.area_file()), name + "\n")
    if not quiet:
        notify(f"flying to {label or name}", 2200)


def pack_size(name):
    d = paths.areas_dir() / name
    try:
        return sum((d / f).stat().st_size for f in os.listdir(d))
    except OSError:
        return 0


def ctl_cmd(*args):
    """argv + env that run `topopaper-ctl <args>` with this interpreter, so
    builds work the same from a checkout, an install, or the settings app."""
    import sys
    env = dict(os.environ)
    root = str(paths.PKG.parent)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return [sys.executable, "-m", "topopaper.cli", *args], env
