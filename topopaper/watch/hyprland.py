"""Hyprland backend: events from socket2, state from the request socket.

Hyprland keeps its sockets in $XDG_RUNTIME_DIR/hypr/<signature>/ (older
releases: /tmp/hypr/<signature>/). Socket2 streams `event>>data` lines; any
event that can change what is on screen triggers a fresh `j/monitors` +
`j/clients` query (the same JSON `hyprctl -j` prints).
"""
import json
import os
import select
import socket
import time

from . import retry_forever

# events after which the covered state may differ
EVENTS = {"workspace", "workspacev2", "focusedmon", "focusedmonv2", "openwindow",
          "closewindow", "movewindow", "movewindowv2", "changefloatingmode",
          "fullscreen", "monitoradded", "monitoraddedv2", "monitorremoved",
          "monitorremovedv2", "moveworkspace", "moveworkspacev2", "activespecial",
          "activespecialv2", "minimized", "destroyworkspace", "destroyworkspacev2",
          "createworkspace", "createworkspacev2", "configreloaded", "togglegroup"}
DEBOUNCE_S = 0.05
POLL_S = 30.0


def socket_dir(env=None):
    env = os.environ if env is None else env
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    rt = env.get("XDG_RUNTIME_DIR")
    cands = ([os.path.join(rt, "hypr", sig)] if rt else []) + [os.path.join("/tmp/hypr", sig)]
    for d in cands:
        if os.path.exists(os.path.join(d, ".socket2.sock")):
            return d
    return cands[0]


def request(cmd, timeout=3.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(os.path.join(socket_dir(), ".socket.sock"))
        s.sendall(cmd.encode())
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        s.close()


def parse_event(line):
    """b'openwindow>>80a6f50,2,kitty,~' -> ('openwindow', '80a6f50,2,kitty,~')."""
    name, _, data = line.decode("utf-8", "replace").strip().partition(">>")
    return name, data


def covered_in(monitors, clients):
    """Every active monitor shows a tiled or fullscreen window on its visible
    workspace (an open special workspace counts as visible on top)."""
    mons = [m for m in monitors if not m.get("disabled")]
    if not mons:
        return False
    solid = {}
    for c in clients:
        if c.get("hidden") or c.get("mapped") is False:
            continue
        fs = c.get("fullscreen")
        if c.get("floating") and not fs:
            continue
        wid = (c.get("workspace") or {}).get("id")
        solid[wid] = True
    for m in mons:
        ids = [(m.get("activeWorkspace") or {}).get("id")]
        sp = (m.get("specialWorkspace") or {}).get("id")
        if sp:
            ids.append(sp)
        if not any(solid.get(i) for i in ids):
            return False
    return True


def covered_now():
    try:
        return covered_in(json.loads(request("j/monitors")), json.loads(request("j/clients")))
    except (OSError, ValueError):
        return False


def run(flag):
    def once():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(os.path.join(socket_dir(), ".socket2.sock"))
            flag.set(covered_now())
            buf = b""
            while True:
                r, _, _ = select.select([s], [], [], POLL_S)
                if not r:
                    flag.set(covered_now())
                    continue
                dirty = False
                deadline = time.monotonic() + DEBOUNCE_S
                while r:
                    b = s.recv(65536)
                    if not b:
                        return              # Hyprland went away
                    buf += b
                    *lines, buf = buf.split(b"\n")
                    dirty = dirty or any(parse_event(ln)[0] in EVENTS for ln in lines)
                    r, _, _ = select.select([s], [], [], max(0.0, deadline - time.monotonic()))
                if dirty:
                    flag.set(covered_now())
        finally:
            s.close()

    retry_forever(once, "hyprland")
