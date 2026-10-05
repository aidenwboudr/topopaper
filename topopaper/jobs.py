"""Background place builds: pack -> route into the atlas -> fly -> ski sites.

`start()` detaches a worker (`topopaper-ctl build-place`) so a launcher or the
settings window can return immediately. The worker keeps a small status file
per job in $XDG_RUNTIME_DIR/topopaper-jobs/ that the settings app polls; the
full build output goes to $XDG_STATE_HOME/topopaper/logs/<slug>.log.
"""
import json
import os
import subprocess
import time

from . import paths, util


def jobs_dir():
    return paths.runtime_dir() / "topopaper-jobs"


def log_path(slug):
    return paths.log_dir() / f"{slug}.log"


def _status(slug, **kw):
    d = jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    kw.update(slug=slug, t=time.time(), pid=os.getpid(), log=str(log_path(slug)))
    util.write_atomic(str(d / f"{slug}.json"), json.dumps(kw))


def list_jobs():
    """Jobs whose worker is still alive, or that finished in the last 10 min."""
    out = []
    d = jobs_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        alive = _alive(j.get("pid"))
        if j.get("state") == "running" and not alive:
            j["state"] = "failed"
            j["stage"] = "stopped unexpectedly"
        if alive or time.time() - j.get("t", 0) < 600:
            out.append(j)
    return out


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def start(plan):
    """Detach a worker for a geocode plan (see geocode.plan). Returns the log path."""
    lp = log_path(plan["slug"])
    lp.parent.mkdir(parents=True, exist_ok=True)
    cmd, env = util.ctl_cmd("build-place", json.dumps(plan))
    with open(lp, "w") as log:
        subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    _status(plan["slug"], state="running", stage="starting", display=plan["display"])
    return lp


def run(plan):
    """The worker body. Exit code 0 when the pack and its route are ready."""
    slug, disp = plan["slug"], plan["display"]
    s, w, n, e = plan["bbox"]

    def step(stage, *args):
        _status(slug, state="running", stage=stage, display=disp)
        cmd, env = util.ctl_cmd(*args)
        print(f"== {stage}: {' '.join(args)}", flush=True)
        return subprocess.run(cmd, env=env).returncode == 0

    ok = step("building the map", "build", slug, "--bbox", f"{s:.4f}", f"{w:.4f}",
              f"{n:.4f}", f"{e:.4f}", "--zoom", str(plan["zoom"]),
              "--tier", str(plan["tier"]), "--auto")
    if not ok:
        _status(slug, state="failed", stage="build failed", display=disp)
        util.notify(f"map build failed — see {log_path(slug)}", 5000)
        return 1
    # fly as soon as the map exists: the in-between maps that smooth the zoom
    # out to the globe can take much longer (the first map in a new region
    # needs continent-scale ones), and the engine picks them up live
    util.fly(slug, quiet=True)
    util.notify(f"map ready — flying to {disp}", 3200)
    if not step("connecting it to the globe", "route", slug):
        util.notify(f"couldn't finish the zoom-out maps for {disp} — see {log_path(slug)}", 5000)
    step("looking for ski areas", "site", slug)
    _status(slug, state="done", stage="ready", display=disp)
    return 0
