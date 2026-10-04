"""`topopaper-ctl search`: fly the atlas anywhere.

A launcher menu (rofi, fuzzel, wofi or tofi in dmenu mode) lists the
installed packs (● marks the one on screen). Pick one to fly there; type any
place on Earth and a pack is geocoded, built in the background, routed into
the atlas, and flown to when it lands.
"""
import shutil
import subprocess
import sys

from . import config, geocode, jobs, util

MENUS = {
    "rofi": ["rofi", "-dmenu", "-i", "-p", "atlas",
             "-mesg", "pick a map, or type any place on Earth"],
    "fuzzel": ["fuzzel", "--dmenu", "--prompt", "atlas: "],
    "wofi": ["wofi", "--dmenu", "--insensitive", "--prompt", "atlas"],
    "tofi": ["tofi", "--prompt-text", "atlas: ", "--require-match=false"],
}


def pick_launcher(pref):
    if pref in MENUS and shutil.which(pref):
        return pref
    if pref == "settings":
        return None
    return next((m for m in MENUS if shutil.which(m)), None)


def menu(choices, launcher):
    r = subprocess.run(MENUS[launcher], input="\n".join(choices) + "\n",
                       capture_output=True, text=True, check=False)
    return r.stdout.strip()


def resolve(choice, dry=False):
    """Fly to, or build, whatever `choice` names. Returns an exit code."""
    cur = util.current_area()
    if util.pack_exists(choice):
        if dry:
            print(f"dry: would fly to pack '{choice}'")
        else:
            util.fly(choice)
        return 0
    near = geocode.near_installed(choice, cur)
    if near:
        if dry:
            print(f'dry: "{choice}" ~ installed pack \'{near}\' -> fly')
        else:
            util.fly(near)
        return 0
    try:
        p = geocode.plan(choice, cur)
    except geocode.GeocodeError as e:
        util.notify({"toobig": f'"{choice}" is bigger than a continent — try a smaller region',
                     "notfound": f'couldn\'t find "{choice}"'}.get(
                         e.kind, "the place search service is unreachable — try again"), 4000)
        return 1
    scale = geocode.TIER_NAMES.get(p["tier"], "")
    if dry:
        fz = " [spelling corrected]" if p["fuzzy"] else ""
        print(f'dry: "{choice}" -> {p["display"]} ({p["addresstype"]}) slug={p["slug"]} '
              f'tier=K{p["tier"]} z{p["zoom"]} bbox={p["bbox"]}{fz}')
        return 0
    if util.pack_exists(p["slug"]):
        util.fly(p["slug"], p["display"])
        return 0
    util.notify(f"building a {scale} map of {p['display']} — a few minutes…", 5000)
    jobs.start(p)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = False
    if argv[:1] == ["--dry"]:
        dry, argv = True, argv[1:]
    choice = " ".join(argv).strip()
    if not choice:
        launcher = pick_launcher(config.load().get("launcher"))
        if not launcher:
            # no dmenu-style launcher: the settings window has a search page
            from . import cli
            return cli.open_settings("places")
        cur = util.current_area()
        names = [f"{n} ●" if n == cur else n
                 for n, m in util.list_packs()
                 if not m.get("scaffold") or n == cur]
        choice = menu(names, launcher).removesuffix(" ●").strip()
        if not choice:
            return 0
    return resolve(choice, dry)
