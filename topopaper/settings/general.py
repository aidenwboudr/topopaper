"""General (running, login, shortcut, setup check, advanced) and About."""
from gi.repository import Adw, GLib, Gtk

from .. import __version__, paths
from ..net import PROJECT_URL
from . import logic, optional
from .widgets import Page, icon_button, open_path, run_async, spinner, status_icon, text_button

CREDITS = [
    ("Map data", "© OpenStreetMap contributors (ODbL)"),
    ("Elevation", "AWS Terrain Tiles (Mapzen/Terrarium; SRTM, GMTED, ETOPO1, USGS NED), "
                  "Copernicus DEM GLO-30 © DLR e.V. 2010–2014 and © Airbus Defence and "
                  "Space GmbH 2014–2018, provided under COPERNICUS by the European Union "
                  "and ESA"),
    ("Lakes", "Natural Earth (public domain)"),
    ("Font", "JetBrains Mono (SIL OFL 1.1)"),
    ("Geocoding", "Nominatim, Photon (komoot)"),
    ("Weather", "Open-Meteo (CC BY 4.0)"),
]


def engine_running():
    s = optional.get("session")
    if s:
        return bool(s.is_running())
    from ..cli import engine_running as pgrep
    return pgrep()


class GeneralPage(Page):
    name = "general"
    title = "General"
    icon = "preferences-system-symbolic"

    def build(self):
        self._timer = 0
        has_session = optional.has("session")
        # -- running
        g = self.group()
        self.run_row = Adw.ActionRow(title="Wallpaper")
        self.run_spin = spinner()
        self.run_spin.set_visible(False)
        self.run_spin.set_valign(Gtk.Align.CENTER)
        self.run_row.add_suffix(self.run_spin)
        self.btn_start = text_button("Start", lambda: self._session("start_detached"),
                                     "suggested-action")
        self.btn_restart = text_button("Restart", lambda: self._session("restart"))
        self.btn_stop = text_button("Stop", lambda: self._session("stop"))
        for b in (self.btn_start, self.btn_restart, self.btn_stop):
            self.run_row.add_suffix(b)
            if not has_session:
                b.set_sensitive(False)
                b.set_tooltip_text(optional.hint("session"))
        g.add(self.run_row)

        # -- login + shortcut
        g = self.group("Startup and Shortcut")
        self.auto = Adw.SwitchRow(title="Start automatically at login")
        self.auto.connect("notify::active", self._auto_toggled)
        g.add(self.auto)
        self.key = Adw.SwitchRow(title="Search shortcut")
        self.key.connect("notify::active", self._key_toggled)
        g.add(self.key)
        g.add(self.combo("launcher", "Search menu", list(logic.LAUNCHERS),
                         [logic.launcher_label(n) for n in logic.LAUNCHERS],
                         subtitle="Shows your maps; type any place to build a new one"))
        if not optional.has("autostart"):
            for r in (self.auto, self.key):
                r.set_sensitive(False)
                r.set_subtitle(optional.hint("autostart"))

        # -- setup check
        self.doc_group = self.group("Setup Check",
                                    "Checks the desktop, the install and the network")
        self.doc_btn = text_button("Check My Setup", self._run_doctor)
        self.doc_btn.set_valign(Gtk.Align.CENTER)
        hdr = Gtk.Box(spacing=6)
        self.doc_spin = spinner()
        self.doc_spin.set_visible(False)
        hdr.append(self.doc_spin)
        hdr.append(self.doc_btn)
        self.doc_group.set_header_suffix(hdr)
        self._doc_rows = []
        if not optional.has("doctor"):
            self.doc_btn.set_sensitive(False)
            self.doc_btn.set_tooltip_text(optional.hint("doctor"))
            self.doc_group.set_description("Unavailable: " + optional.hint("doctor"))

        # -- advanced
        g = self.group()
        exp = Adw.ExpanderRow(title="Advanced",
                              subtitle="Outside data sources and power profiles")
        exp.add_row(self.switch("builtin_weather", "Built-in weather",
                                "Fetch weather from Open-Meteo in the background"))
        exp.add_row(self.entry("weather_file", "Weather file (instead of built-in)"))
        exp.add_row(self.entry("location_file", "Time zone file (JSON with “tz”)"))
        exp.add_row(self.combo("power_profile", "Power profile", ["none", "sway"],
                               ["None", "sway: switch output mode"],
                               subtitle="Change a display’s refresh rate with the power source"))
        exp.add_row(self.entry("power_output", "Output (e.g. eDP-1)"))
        exp.add_row(self.entry("power_mode_ac", "Mode on mains power (e.g. 2560x1600@120Hz)"))
        exp.add_row(self.entry("power_mode_battery", "Mode on battery (e.g. 2560x1600@60Hz)"))
        cfg_row = Adw.ActionRow(title="Settings file",
                                subtitle=GLib.markup_escape_text(str(self.store.file)))
        cfg_row.add_css_class("property")
        cfg_row.add_suffix(icon_button("document-open-symbolic", "Open the settings file",
                                       lambda: open_path(self.store.file, self.win)))
        exp.add_row(cfg_row)
        g.add(exp)

    # ---- lifecycle ------------------------------------------------------------
    def shown(self):
        self._poll()
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(2, self._poll)
        self._load_autostart()

    def hidden(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _poll(self):
        run_async(engine_running, done=self._show_running)
        return True

    def _show_running(self, running, err):
        if err is not None:
            running = None
        sub = {True: "Running", False: "Stopped", None: "Unknown"}[running]
        if not optional.has("session"):
            sub += f" · start/stop {optional.hint('session')}"
        self.run_row.set_subtitle(sub)
        self.btn_start.set_visible(not running)
        self.btn_restart.set_visible(bool(running))
        self.btn_stop.set_visible(bool(running))

    def _session(self, fn):
        s = optional.get("session")
        if not s:
            return
        self.run_spin.set_visible(True)

        def done(_r, err):
            self.run_spin.set_visible(False)
            if err is not None:
                self.toast(f"Couldn’t {fn.split('_')[0]} the wallpaper: {err}")
            GLib.timeout_add(700, lambda: (self._poll(), False)[1])
        run_async(getattr(s, fn), done=done)

    # ---- autostart / keybind --------------------------------------------------
    def _load_autostart(self):
        a = optional.get("autostart")
        if not a:
            return

        def work():
            return a.status(), a.keybind_status()

        def done(res, err):
            if err is not None:
                self.auto.set_subtitle(f"Couldn’t check: {err}")
                return
            st, kb = res
            self._syncing = True
            self.auto.set_active(bool(st.get("enabled")))
            self.key.set_active(bool(kb.get("enabled")))
            self._syncing = False
            detail = st.get("detail") or ""
            method = st.get("method") or ""
            self.auto.set_subtitle(GLib.markup_escape_text(
                " · ".join(x for x in (method, detail) if x) or
                "Starts the wallpaper when you log in"))
            self.key.set_subtitle(GLib.markup_escape_text(
                "Super+Shift+B runs topopaper-ctl search: pick one of your maps to "
                "fly there, or type any place to build it"
                + (f"\n{kb['detail']}" if kb.get("detail") else "")))
        run_async(work, done=done)

    def _apply(self, row, fn_on, fn_off):
        if self._syncing:
            return
        a = optional.get("autostart")
        if not a:
            return
        on = row.get_active()
        fn = getattr(a, fn_on if on else fn_off)

        def done(res, err):
            ok, msg = (False, str(err)) if err is not None else (res or (False, ""))
            if msg:
                self.toast(msg, 5 if not ok else 3)
            if not ok:
                self._syncing = True
                row.set_active(not on)
                self._syncing = False
            self._load_autostart()
        run_async(fn, done=done)

    def _auto_toggled(self, *_):
        self._apply(self.auto, "enable", "disable")

    def _key_toggled(self, *_):
        self._apply(self.key, "keybind_add", "keybind_remove")

    # ---- doctor ----------------------------------------------------------------
    def _run_doctor(self):
        d = optional.get("doctor")
        if not d:
            return
        self.doc_btn.set_sensitive(False)
        self.doc_spin.set_visible(True)
        run_async(d.run_checks, done=self._show_doctor)

    def _show_doctor(self, results, err):
        self.doc_btn.set_sensitive(True)
        self.doc_spin.set_visible(False)
        for r in self._doc_rows:
            self.doc_group.remove(r)
        self._doc_rows = []
        if err is not None:
            results = [dict(id="doctor", title="The setup check failed", status="fail",
                            detail=str(err), fix="")]
        ok, warn, fail = logic.doctor_summary(results)
        self.doc_group.set_description(
            f"{ok} passed · {warn} warning{'s' if warn != 1 else ''} · {fail} problem"
            f"{'s' if fail != 1 else ''}")
        order = {"fail": 0, "warn": 1, "ok": 2}
        for r in sorted(results or [], key=lambda r: order.get(r.get("status"), 1)):
            row = Adw.ActionRow(title=GLib.markup_escape_text(r.get("title", "")))
            row.set_subtitle_lines(0)
            text = r.get("detail", "")
            if r.get("fix") and r.get("status") != "ok":
                text = (text + "\n" if text else "") + "Fix: " + r["fix"]
            row.set_subtitle(GLib.markup_escape_text(text))
            row.add_prefix(status_icon(r.get("status")))
            self.doc_group.add(row)
            self._doc_rows.append(row)


class AboutPage(Page):
    name = "about"
    title = "About"
    icon = "help-about-symbolic"

    def build(self):
        g = self.group()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, halign=Gtk.Align.CENTER)
        img = Gtk.Image(icon_name=self.win.app.icon_name, pixel_size=96)
        img.add_css_class("icon-dropshadow")
        box.append(img)
        t = Gtk.Label(label="Topopaper")
        t.add_css_class("title-1")
        box.append(t)
        s = Gtk.Label(label="A live topographic map for your desktop", wrap=True,
                      justify=Gtk.Justification.CENTER)
        s.add_css_class("dim-label")
        box.append(s)
        v = Gtk.Label(label=__version__, halign=Gtk.Align.CENTER)
        v.add_css_class("tp-badge")
        box.append(v)
        g.add(box)

        g = self.group()
        web = Adw.ActionRow(title="Website", subtitle=PROJECT_URL, activatable=True)
        web.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        web.connect("activated", lambda *_: open_path(PROJECT_URL, self.win))
        g.add(web)
        issues = Adw.ActionRow(title="Report an issue", activatable=True)
        issues.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        issues.connect("activated", lambda *_: open_path(PROJECT_URL + "/issues", self.win))
        g.add(issues)
        lic = Adw.ActionRow(title="License", subtitle="GNU General Public License v3.0 or later")
        lic.add_css_class("property")
        g.add(lic)
        more = Adw.ActionRow(title="Credits and legal notices", activatable=True)
        more.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        more.connect("activated", lambda *_: self.show_dialog())
        g.add(more)

        g = self.group("Data Sources")
        for title, text in CREDITS:
            row = Adw.ActionRow(title=title, subtitle=text)
            row.add_css_class("property")
            row.set_subtitle_lines(0)
            g.add(row)

        g = self.group("Files")
        for title, p in (("Settings", paths.config_file()), ("Maps", paths.areas_dir()),
                         ("Build logs", paths.log_dir())):
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(p)))
            row.add_css_class("property")
            row.add_suffix(icon_button("folder-open-symbolic", f"Open {title.lower()}",
                                       lambda p=p: open_path(p if p.exists() else p.parent,
                                                             self.win)))
            g.add(row)

    def show_dialog(self):
        d = Adw.AboutDialog(application_name="Topopaper",
                            application_icon=self.win.app.icon_name,
                            version=__version__, website=PROJECT_URL,
                            issue_url=PROJECT_URL + "/issues",
                            license_type=Gtk.License.GPL_3_0,
                            comments="A live topographic map for your desktop: the globe, "
                                     "your places and the ski lifts, drawn in contour lines.",
                            developer_name="The Topopaper contributors")
        for title, text in CREDITS:
            d.add_legal_section(title, text, Gtk.License.UNKNOWN, None)
        d.present(self.win)

