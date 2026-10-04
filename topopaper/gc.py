#!/usr/bin/env python3
"""`topopaper-ctl gc`: list (and, with --yes, delete) stale auto-built atlas packs.

Only packs stamped auto=true in meta.json (search builds) are ever
candidates; curated packs are untouchable. Age = pack directory mtime.
Scaffold rungs auto-built FOR an auto pack are collected with it (same
"<name>-" prefix + scaffold flag).

  topopaper-ctl gc                dry list (default: candidates older than 45 days)
  topopaper-ctl gc --days 90      change the age threshold
  topopaper-ctl gc --yes          actually delete the listed candidates

Function API (used by the settings app):
  candidates(days) -> [dict(age=days, size=bytes, group=[pack, rungs...])], oldest first
  delete(groups)   -> [removed pack names]
"""
import argparse
import json
import os
import shutil
import time

from . import config, paths, util

DEFAULT_DAYS = 45.0


def _packs():
    root = str(paths.areas_dir())
    packs = {}
    if not os.path.isdir(root):
        return packs
    for d in sorted(os.listdir(root)):
        mj = os.path.join(root, d, "meta.json")
        if not os.path.isfile(mj):
            continue
        try:
            with open(mj, encoding="utf-8") as f:
                packs[d] = json.load(f)
        except (OSError, ValueError):
            continue
    return packs


def _size(names):
    root = paths.areas_dir()
    total = 0
    for g in names:
        try:
            total += sum((root / g / f).stat().st_size for f in os.listdir(root / g))
        except OSError:
            pass
    return total


def candidates(days=DEFAULT_DAYS, now=None):
    """Auto-built pack groups older than `days`, never the pack on screen or
    home (nor anything on their chain). Oldest first."""
    root = str(paths.areas_dir())
    cur = util.current_area()
    home = config.load().get("home_area")
    packs = _packs()
    now = time.time() if now is None else now
    out = []
    for name, m in packs.items():
        if not m.get("auto") or name == "earth":
            continue
        if name in (cur, home) or cur.startswith(name + "-") or home.startswith(name + "-"):
            continue                       # never GC the pack on screen or home
        try:
            age = (now - os.path.getmtime(os.path.join(root, name))) / 86400.0
        except OSError:
            continue
        if age < days:
            continue
        group = [name] + [n for n, mm in packs.items()
                          if mm.get("scaffold") and n.startswith(name + "-")]
        out.append(dict(age=age, size=_size(group), group=group))
    out.sort(key=lambda c: c["age"], reverse=True)
    return out


def _safe_name(name):
    return bool(name) and name not in (".", "..", "earth") and "/" not in name


def delete(groups):
    """Remove every pack in `groups` (lists of names, or dicts from
    candidates()). Never removes `earth`. Returns the names removed."""
    root = paths.areas_dir()
    removed = []
    for g in groups:
        names = g["group"] if isinstance(g, dict) else g
        for n in names:
            if not _safe_name(n) or not (root / n).is_dir():
                continue
            shutil.rmtree(root / n)
            removed.append(n)
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="topopaper-ctl gc")
    ap.add_argument("--days", type=float, default=DEFAULT_DAYS)
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if not paths.areas_dir().is_dir():
        print("no packs installed")
        return

    cands = candidates(a.days)
    if not cands:
        print(f"no auto packs older than {a.days:.0f} days — nothing to do")
        return
    total = 0
    for c in cands:
        total += c["size"]
        print(f"{c['age']:6.0f}d  {c['size']/1e6:7.1f} MB  {' + '.join(c['group'])}")
    print(f"{'DELETING' if a.yes else 'would free'} {total/1e6:.1f} MB "
          f"({len(cands)} pack group(s))")
    if a.yes:
        for n in delete(cands):
            print(f"  removed {n}")


if __name__ == "__main__":
    main()
