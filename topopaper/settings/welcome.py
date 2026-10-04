"""First-run welcome: what it is, does this desktop work, where's home,
start at login, done."""
import os
import shutil

from gi.repository import Adw, GLib, Gtk

from .. import geocode, paths, util
from . import logic, optional
from .widgets import run_async, spinner, status_icon

STEPS = ("welcome", "check", "home", "login", "done")


def basic_checks():
    """A tiny stand-in for doctor.run_checks() when that module is missing."""
    out = []
    wl = bool(os.environ.get("WAYLAND_DISPLAY"))
    out.append(dict(id="wayland", title="Wayland session", status="ok" if wl else "fail",
                    detail="Running under Wayland" if wl else "This isn’t a Wayland session",
                    fix="" if wl else "Log in to a Wayland session (sway, Hyprland, niri, "
                                      "KDE Plasma…)"))
    eng = shutil.which("topopaper") or (paths.PKG.parent / "build" / "topopaper").is_file()
    out.append(dict(id="engine", title="Wallpaper engine",
                    status="ok" if eng else "warn",
                    detail="Installed" if eng else "The topopaper engine wasn’t found",
                    fix="" if eng else "Run `make install` in the source folder"))
    return out


def _page(tag, title, child, focus=None):
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar(show_title=False)
    tv.add_top_bar(hb)
    tv.set_content(child)
    page = Adw.NavigationPage(tag=tag, title=title, child=tv)
    if focus is not None:              # keyboard users land on the next step
        page.connect("shown", lambda *_: GLib.idle_add(lambda: focus.grab_focus() and False))
    return page


def _status(icon, title, desc):
    sp = Adw.StatusPage(icon_name=icon, title=title, description=desc)
    sp.add_css_class("compact")
    return sp


def _actions(*buttons):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                  halign=Gtk.Align.CENTER, margin_top=18)
    for b in buttons:
        box.append(b)
    return box


def _pill_button(label, cb, suggested=True):
    b = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
    b.add_css_class("pill")
    if suggested:
        b.add_css_class("suggested-action")
    else:
        b.add_css_class("flat")
    b.connect("clicked", lambda *_: cb())
    return b


def _clamped(*widgets):
    clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    for w in widgets:
        box.append(w)
    clamp.set_child(box)
    return clamp


class Welcome(Adw.Bin):
    def __init__(self, win, on_finished, step=None):
        super().__init__()
        self.win = win
        self.app = win.app
        self.on_finished = on_finished
        self.plan = None
        self._token = 0
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.app.ensure_globe()
        self.nav.add(self._welcome())
        self._built = {"welcome": True}
        self._timer = GLib.timeout_add(500, self._tick)
        self.connect("unrealize", lambda *_: self._stop())
        if step:
            for tag in STEPS[1:STEPS.index(step) + 1] if step in STEPS else ():
                self.go(tag, animate=False)

    def _stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def go(self, tag, animate=True):
        page = getattr(self, f"_{tag}")()
        self.nav.set_animate_transitions(animate)
        self.nav.push(page)
        self.nav.set_animate_transitions(True)

    # ---- 1. welcome --------------------------------------------------------
    def _welcome(self):
        sp = _status(self.app.icon_name, "Welcome to Topopaper",
                     "A live topographic map as your wallpaper. It drifts and breathes, "
                     "and flies between the places you care about — from the whole "
                     "globe down to a single ski lift.")
        b = _pill_button("Get Started", lambda: self.go("check"))
        sp.set_child(_actions(b))
        return _page("welcome", "Welcome", sp, b)

    # ---- 2. compatibility ------------------------------------------------------
    def _check(self):
        gnome = logic.desktop_is_gnome()
        if gnome:
            sp = _status("dialog-warning-symbolic", "GNOME Isn’t Supported",
                         "Topopaper draws the wallpaper with the layer-shell protocol, "
                         "which GNOME Shell doesn’t offer, so it can’t show up there. It "
                         "works on sway, Hyprland, niri, KDE Plasma, river, Wayfire and "
                         "other wlroots desktops.")
        else:
            sp = _status("video-display-symbolic", "Checking Your Desktop",
                         "Making sure the wallpaper can run here")
        self.check_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.check_list.add_css_class("boxed-list")
        wait = Adw.ActionRow(title="Checking…")
        sp_ = spinner()
        sp_.set_valign(Gtk.Align.CENTER)
        wait.add_prefix(sp_)
        self.check_list.append(wait)
        self.check_summary = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.check_summary.add_css_class("dim-label")
        btn = _pill_button("Continue Anyway" if gnome else "Continue", lambda: self.go("home"),
                           suggested=not gnome)
        sp.set_child(_clamped(self.check_summary, self.check_list, _actions(btn)))
        d = optional.get("doctor")
        run_async(d.run_checks if d else basic_checks, done=self._checks_done)
        return _page("check", "Compatibility", sp, btn)

    def _checks_done(self, results, err):
        while (row := self.check_list.get_row_at_index(0)) is not None:
            self.check_list.remove(row)
        if err is not None:
            results = basic_checks()
        ok, warn, fail = logic.doctor_summary(results)
        if fail:
            self.check_summary.set_label(f"{fail} problem{'s' if fail != 1 else ''} to fix "
                                         "before the wallpaper can run")
        elif warn:
            self.check_summary.set_label("Looks good, with a few notes")
        else:
            self.check_summary.set_label("Everything looks good")
        shown = [r for r in results if r.get("status") != "ok"] or results
        order = {"fail": 0, "warn": 1, "ok": 2}
        for r in sorted(shown, key=lambda r: order.get(r.get("status"), 1))[:8]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(r.get("title", "")))
            text = r.get("detail", "")
            if r.get("fix") and r.get("status") != "ok":
                text = (text + "\n" if text else "") + "Fix: " + r["fix"]
            row.set_subtitle(GLib.markup_escape_text(text))
            row.add_prefix(status_icon(r.get("status")))
            self.check_list.append(row)
        if ok and shown is not results:
            row = Adw.ActionRow(title=f"{ok} other check{'s' if ok != 1 else ''} passed")
            row.add_prefix(status_icon("ok"))
            self.check_list.append(row)

    # ---- 3. home ----------------------------------------------------------------
    def _home(self):
        sp = _status("go-home-symbolic", "Where’s Home?",
                     "Pick a place to make a map of. The wallpaper starts there and "
                     "returns to it. You can add more places later.")
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        self.home_search = Adw.EntryRow(title="Search, e.g. “Zermatt” or “Yosemite”")
        self.home_search.connect("entry-activated", lambda *_: self._search())
        self.home_spin = spinner()
        self.home_spin.set_visible(False)
        self.home_spin.set_valign(Gtk.Align.CENTER)
        self.home_search.add_suffix(self.home_spin)
        b = Gtk.Button(icon_name="system-search-symbolic", valign=Gtk.Align.CENTER,
                       tooltip_text="Search")
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self._search())
        self.home_search.add_suffix(b)
        lb.append(self.home_search)
        self.home_result = Adw.ActionRow(visible=False)
        self.home_result.set_subtitle_lines(2)
        self.home_check = Gtk.Image(icon_name="object-select-symbolic", visible=False)
        self.home_check.add_css_class("accent")
        self.home_result.add_suffix(self.home_check)
        lb.append(self.home_result)

        glb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        glb.add_css_class("boxed-list")
        self.globe_row = Adw.ActionRow(title="The globe")
        self.globe_row.add_prefix(Gtk.Image(icon_name="mark-location-symbolic"))
        self.globe_spin = spinner()
        self.globe_spin.set_valign(Gtk.Align.CENTER)
        self.globe_row.add_suffix(self.globe_spin)
        glb.append(self.globe_row)

        self.home_next = _pill_button("Continue", lambda: self.go("login"))
        self.home_next.set_sensitive(False)
        skip = _pill_button("Skip — Start With the Globe", self._skip_home, suggested=False)
        sp.set_child(_clamped(lb, glb, _actions(self.home_next, skip)))
        self._tick()
        return _page("home", "Home", sp, self.home_search)

    def _search(self):
        q = self.home_search.get_text().strip()
        if not q:
            return
        self._token += 1
        tok = self._token
        self.home_spin.set_visible(True)
        self.home_result.set_visible(False)

        def done(plan, err):
            if tok != self._token:
                return
            self.home_spin.set_visible(False)
            self.home_result.set_visible(True)
            if err is not None:
                kind = getattr(err, "kind", "fetch")
                self.plan = None
                self.home_check.set_visible(False)
                self.home_next.set_sensitive(False)
                self.home_result.set_title(
                    {"notfound": "No place by that name",
                     "toobig": "That’s bigger than a continent"}.get(
                         kind, "The place search is unreachable"))
                self.home_result.set_subtitle(
                    {"notfound": "Check the spelling, or add a region or country",
                     "toobig": "Try a smaller region"}.get(
                         kind, "Check your internet connection, or skip for now"))
                return
            self.plan = plan
            scale = geocode.TIER_NAMES.get(plan["tier"], "")
            self.home_result.set_title(GLib.markup_escape_text(plan["display"]))
            self.home_result.set_subtitle(GLib.markup_escape_text(
                f"{scale[:1].upper()}{scale[1:]} map · "
                f"{plan.get('full_name') or plan['display']}"))
            self.home_check.set_visible(True)
            self.home_next.set_sensitive(True)
            self.home_next.grab_focus()
        run_async(geocode.plan, q, "", done=done)

    def _skip_home(self):
        self.plan = None
        self.go("login")

    def _tick(self):
        if not hasattr(self, "globe_row"):
            return True
        st = self.app.starter_state
        if util.pack_exists("earth"):
            text, busy = "Ready", False
        elif st == "running":
            text, busy = (self.app.starter_line or "Downloading…"), True
        elif st == "failed":
            text, busy = "Couldn’t download the globe — see the build log", False
        elif st == "missing":
            text, busy = f"Can’t download it here: {optional.hint('starter')}", False
        else:
            text, busy = "Waiting…", True
        self.globe_row.set_subtitle(GLib.markup_escape_text(text))
        self.globe_spin.set_visible(busy)
        return True

    # ---- 4. login ---------------------------------------------------------------
    def _login(self):
        sp = _status("system-run-symbolic", "Start at Login?",
                     "Topopaper can start with your desktop, and add a keyboard "
                     "shortcut for jumping between places.")
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        self.want_auto = Adw.SwitchRow(title="Start automatically at login", active=True)
        self.want_key = Adw.SwitchRow(
            title="Add search shortcut",
            subtitle="Super+Shift+B opens a menu of your maps; type any place to build it",
            active=True)
        if not optional.has("autostart"):
            for r in (self.want_auto, self.want_key):
                r.set_active(False)
                r.set_sensitive(False)
            self.want_auto.set_subtitle(optional.hint("autostart"))
        lb.append(self.want_auto)
        lb.append(self.want_key)
        b = _pill_button("Continue", lambda: self.go("done"))
        sp.set_child(_clamped(lb, _actions(b)))
        return _page("login", "Login", sp, b)

    # ---- 5. done ------------------------------------------------------------------
    def _done(self):
        if self.plan:
            desc = (f"Your map of {self.plan['display']} will be built in the background; "
                    "the wallpaper flies there when it’s ready.")
        else:
            desc = "The wallpaper starts on the globe. Add places any time from Settings."
        sp = _status(self.app.icon_name, "All Set", desc)
        b = _pill_button("Start Topopaper", self._finish)
        sp.set_child(_actions(b))
        return _page("done", "Done", sp, b)

    def _finish(self):
        store = self.app.store
        if self.plan:
            store.cfg.set("home_area", self.plan["slug"])
        store.save_all()                       # creates config.ini
        msgs = []
        if self.plan:
            self.app.queue_build(self.plan)
        a = optional.get("autostart")
        want_auto = getattr(self, "want_auto", None)
        want_key = getattr(self, "want_key", None)
        if a:
            def apply():
                out = []
                if want_auto is not None and want_auto.get_active():
                    out.append(a.enable())
                if want_key is not None and want_key.get_active():
                    out.append(a.keybind_add())
                return out

            def applied(res, err):
                if err is not None:
                    self.win.toast(f"Couldn’t set up autostart: {err}", 5)
                    return
                for ok, msg in res or ():
                    if not ok and msg:
                        self.win.toast(msg, 5)
            run_async(apply, done=applied)
        s = optional.get("session")
        if s:
            run_async(s.start_detached,
                      done=lambda _r, e: e and self.win.toast(f"Couldn’t start the wallpaper: {e}", 5))
        else:
            msgs.append("Start the wallpaper with topopaper-session")
        self._stop()
        self.on_finished(msgs)
