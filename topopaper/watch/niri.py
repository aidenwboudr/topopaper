"""niri backend: `niri msg --json event-stream`, state from `windows` +
`workspaces`.

niri's workspaces each belong to one output and exactly one per output is
active (visible). A tiled window in it counts as covering; floating ones
(niri 25.01+) don't. Older niri without the event stream gets polled.
"""
import json
import select
import subprocess
import time

from . import retry_forever

DEBOUNCE_S = 0.05
POLL_S = 2.0


def niri_json(*args):
    out = subprocess.run(["niri", "msg", "--json", *args], capture_output=True,
                         timeout=5, check=True).stdout
    return json.loads(out)


def covered_in(workspaces, windows):
    active = [w["id"] for w in workspaces if w.get("is_active")]
    if not active:
        return False
    tiled = {w.get("workspace_id") for w in windows if not w.get("is_floating")}
    return all(a in tiled for a in active)


def covered_now():
    try:
        return covered_in(niri_json("workspaces"), niri_json("windows"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def run(flag):
    def stream():
        proc = subprocess.Popen(["niri", "msg", "--json", "event-stream"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        t0 = time.monotonic()
        try:
            flag.set(covered_now())
            while True:
                r, _, _ = select.select([proc.stdout], [], [], 60.0)
                if r:
                    if not proc.stdout.readline():
                        break
                    # coalesce a burst (WindowsChanged + WorkspacesChanged ...)
                    while select.select([proc.stdout], [], [], DEBOUNCE_S)[0]:
                        if not proc.stdout.readline():
                            break
                flag.set(covered_now())
        finally:
            proc.kill()
            proc.wait()
        if time.monotonic() - t0 < 2.0:
            poll()                      # no event stream here (old niri): poll

    def poll():
        # returns (to the reconnect loop) as soon as niri stops answering
        while True:
            flag.set(covered_in(niri_json("workspaces"), niri_json("windows")))
            time.sleep(POLL_S)

    retry_forever(stream, "niri")
