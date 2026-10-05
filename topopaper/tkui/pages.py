"""The page base class and the simple pages: Appearance, Motion, Clock &
Weather, General and About (Places has its own module)."""
import sys
import tkinter as tk
from tkinter import ttk

from .. import __version__, config, geocode, paths, themes
from ..net import PROJECT_URL
from ..settings import logic, optional
from . import helpers, look, widgets as W
from .widgets import px
from .work import open_path, sentence

CREDITS = [            # as in the GTK About page (topopaper/settings/general.py)
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


def doc(key):
    d = config.BY_NAME[key].doc
    return d[0].upper() + d[1:] if d else ""


# ---- the page base ------------------------------------------------------------------
class Page:
    """One sidebar entry: a scrolling column of cards, refreshed from config."""
    name = ""
    lead = ""

    def __init__(self, win, parent):
        self.win = win
        self.store = win.store
        self.pal, self.fnt, self.t = win.pal, win.fnt, win.t
        self.frame = W.ScrollPage(parent, self.pal)
        self.body = self.frame.body
        self._refreshers = []
        self._syncing = False
        ttk.Label(self.body, text=self.title, style="Title.TLabel").pack(anchor="w")
        if self.lead:
            W.autowrap(ttk.Label(self.body, text=self.lead, style="Lead.TLabel")).pack(
                fill="x", pady=(px(2), 0))
        self.build()
        self.refresh()

    @property
    def title(self):
        return helpers.TITLES.get(self.name, self.name)

    def build(self):
        raise NotImplementedError

    # called by the window
    def refresh(self):
        self._syncing = True
        try:
            for fn in self._refreshers:
                fn()
            self.on_refresh()
        finally:
            self._syncing = False

    def on_refresh(self):
        pass

    def shown(self):
        pass

    def hidden(self):
        pass

    def toast(self, msg, seconds=3):
        self.win.toast(msg, seconds)

    def run(self, fn, *args, done=None):
        self.win.worker.run(fn, *args, done=done)

    def after(self, ms, fn):
        return self.win.root.after(ms, fn)

    # ---- layout --------------------------------------------------------------
    def group(self, title="", description="", parent=None):
        box = ttk.Frame(parent or self.body)
        box.pack(fill="x", pady=(px(22), 0))
        head = ttk.Frame(box)
        head.pack(fill="x", padx=px(2))
        if title:
            ttk.Label(head, text=title, style="Heading.TLabel").pack(side="left", anchor="sw")
        card = W.Card(box, self.pal)
        card.box, card.head = box, head
        card.desc = W.autowrap(ttk.Label(box, text=description, style="Dim.TLabel"))
        if description:
            card.desc.pack(fill="x", padx=px(2), pady=(px(2), 0))
        card.pack(fill="x", pady=(px(6), 0))
        return card

    def set_description(self, card, text):
        card.desc.configure(text=text or "")
        if text and not card.desc.winfo_ismapped():
            card.desc.pack(fill="x", padx=px(2), pady=(px(2), 0), before=card)
        elif not text:
            card.desc.pack_forget()

    def row(self, card, title="", subtitle=""):
        return card.add(W.Row(card, title, subtitle))

    def button(self, master, text, command, style="TButton"):
        return ttk.Button(master, text=text, command=command, style=style, takefocus=1)

    def below(self, row, widget, pady=(8, 0)):
        """Put `widget` on its own line under a row's text, full width."""
        widget.grid(in_=row, row=1, column=0, columnspan=3, sticky="ew",
                    pady=(px(pady[0]), px(pady[1])))
        return widget

    # ---- binders: config key <-> control, guarded against refresh loops -----
    def switch(self, card, key, title, subtitle=None):
        row = self.row(card, title, doc(key) if subtitle is None else subtitle)
        tog = row.add_suffix(W.Toggle(row.suffix, self.pal))
        row.toggle = tog
        self._refreshers.append(lambda: tog.set(self.store.get(key)))

        def changed(v):
            if not self._syncing:
                self.store.set(key, v)
                self.after_change(key)
        tog.command = changed
        row.title.bind("<Button-1>", lambda e: tog._flip())
        return row

    def choice(self, card, key, title, values, labels, subtitle=None):
        """Few short choices as pills, more as a drop-down."""
        row = self.row(card, title, doc(key) if subtitle is None else subtitle)

        def changed(v):
            if not self._syncing:
                self.store.set(key, v)
                self.after_change(key)
        if len(values) <= 3:
            seg = row.add_suffix(W.Segmented(row.suffix, values, labels, changed))
            self._refreshers.append(lambda: seg.set(self.store.raw(key)))
            row.control = seg
            return row
        cb = ttk.Combobox(row.suffix, values=list(labels), state="readonly",
                          width=max(len(x) for x in labels) + 1)
        row.add_suffix(cb)

        def refresh():
            v = self.store.raw(key)
            cb.current(values.index(v) if v in values else 0)
        self._refreshers.append(refresh)

        def picked(_e=None):
            cb.selection_clear()
            i = cb.current()
            if 0 <= i < len(values):
                changed(values[i])
        cb.bind("<<ComboboxSelected>>", picked)
        row.control = cb
        return row

    def slider(self, card, key, title, subtitle, lo, hi, step, fmt):
        """A full-width slider under the row's title, value on the right;
        saves are debounced while it moves."""
        row = self.row(card, title, subtitle)
        value = ttk.Label(row.suffix, style="Card.Dim.TLabel", width=9, anchor="e")
        row.add_suffix(value)
        var = tk.DoubleVar(master=row)
        digits = 0 if float(step).is_integer() else 2

        def snap(v):
            v = round(round((float(v) - lo) / step) * step + lo, 4)
            return min(hi, max(lo, v))

        def moved(v):
            v = snap(v)
            value.configure(text=fmt(v))
            if not self._syncing:
                self.store.set(key, int(v) if digits == 0 else f"{v:.{digits}f}",
                               debounce=True)
                self.after_change(key)
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var, command=moved,
                          orient="horizontal")
        self.below(row, scale, (6, 2))

        def refresh():
            v = self.store.get(key)
            var.set(v)
            value.configure(text=fmt(snap(v)))
        self._refreshers.append(refresh)
        row.scale = scale
        return row

    def entry(self, card, key, title, subtitle=""):
        """A text setting saved by its button or Enter."""
        row = self.row(card, title, subtitle)
        var = tk.StringVar(master=row)
        ent = ttk.Entry(row, textvariable=var)
        self.below(row, ent, (6, 0))
        save = self.button(row.suffix, "Save", lambda: apply())
        row.add_suffix(save)
        self._refreshers.append(lambda: var.set(self.store.raw(key)))

        def apply():
            self.store.set(key, var.get().strip())
            self.after_change(key)
            self.toast(f"Saved: {title.lower()}")
        ent.bind("<Return>", lambda e: apply())
        return row

    def after_change(self, key):
        pass


# ---- appearance ---------------------------------------------------------------------
class AppearancePage(Page):
    name = "appearance"
    lead = "Colours for the map, the clock and the labels"

    def build(self):
        card = self.group("Theme")
        grid = ttk.Frame(card, style="Card.TFrame", padding=px(12))
        grid.pack(fill="x")
        self.cards = {}
        cols = 3
        w, h = px(148), px(84)
        for i, key in enumerate(config.BY_NAME["theme"].choices):
            t = look.theme_colours(key)
            label = themes.THEMES[key]["label"] if key in themes.THEMES else key
            cell = tk.Frame(grid, background=self.pal["card"], highlightthickness=px(2),
                            highlightbackground=self.pal["card"],
                            highlightcolor=self.pal["accent"], takefocus=1, cursor="hand2",
                            padx=px(6), pady=px(6))
            cell.grid(row=i // cols, column=i % cols, padx=px(4), pady=px(4), sticky="n")
            cv = tk.Canvas(cell, width=w, height=h, highlightthickness=0, borderwidth=0,
                           background=self.pal["card"])
            cv.pack()
            W.draw_map(cv, t, w, h)
            lab = ttk.Label(cell, text=label, style="Card.TLabel", anchor="center")
            lab.pack(fill="x", pady=(px(6), 0))
            for wdg in (cell, cv, lab):
                wdg.bind("<Button-1>", lambda e, k=key: self._pick(k))
            for seq in ("<space>", "<Return>"):
                cell.bind(seq, lambda e, k=key: self._pick(k))
            cell.lab = lab
            self.cards[key] = cell
        for c in range(cols):
            grid.columnconfigure(c, weight=1)
        self._refreshers.append(self._mark_current)

        card = self.group("On the map")
        self.switch(card, "labels", "Place names", "Towns, peaks, lakes and lifts")
        self.switch(card, "city_lights", "City lights",
                    "Lights glow on the night side of the globe")
        self.switch(card, "aurora", "Aurora", "Northern and southern lights on the night side")
        self.switch(card, "react_to_cpu", "React to activity",
                    "Lines speed up and warm while the computer is busy")

    def _mark_current(self):
        cur = self.store.get("theme")
        for key, cell in self.cards.items():
            sel = key == cur
            cell.configure(highlightbackground=self.pal["accent"] if sel else self.pal["card"])
            cell.lab.configure(style="Card.Strong.TLabel" if sel else "Card.TLabel")

    def _pick(self, key):
        self.cards[key].focus_set()
        if key != self.store.get("theme"):
            self.store.set("theme", key)
            self.win.retheme()           # the window follows the wallpaper's colours


# ---- motion ---------------------------------------------------------------------------
class MotionPage(Page):
    name = "motion"
    lead = "How the map moves, and how much power it may use"

    def build(self):
        card = self.group("Movement")
        row = self.row(card, "Fly to another map",
                       "On average, how often the wallpaper travels to another of your maps "
                       "on its own")
        self.roam = ttk.Combobox(row.suffix, state="readonly", width=18)
        row.add_suffix(self.roam)
        self.roam.bind("<<ComboboxSelected>>", self._roam_changed)
        self._roam_vals = []
        self._refreshers.append(self._roam_refresh)
        self.slider(card, "animation_speed", "Animation speed",
                    "Drift, contour crawl and zoom breathing", 0.25, 2.0, 0.05,
                    logic.speed_label)

        card = self.group("Frame rate", "Lower caps save power; the map moves slowly, "
                                        "so even 24 fps looks smooth")
        fps = lambda v: f"{int(v)} fps"
        self.slider(card, "fps", "On mains power", "", 5, 240, 1, fps)
        self.slider(card, "fps_battery", "On battery", "", 5, 240, 1, fps)

        card = self.group("When windows cover the wallpaper")
        self.switch(card, "pause_when_covered", "Pause the animation",
                    "Freeze the map while it can’t be seen")
        self.slider(card, "fps_covered", "Frame rate while covered",
                    "While hidden behind windows", 1, 60, 1, fps)

    def _roam_refresh(self):
        cur = self.store.raw("roam_minutes")
        vals, labels = logic.roam_options(cur)
        if vals != self._roam_vals:
            self._roam_vals = vals
            self.roam.configure(values=labels)
        self.roam.current(logic.roam_index(vals, cur))

    def _roam_changed(self, _e=None):
        self.roam.selection_clear()
        i = self.roam.current()
        if self._syncing or not (0 <= i < len(self._roam_vals)):
            return
        self.store.set("roam_minutes", f"{self._roam_vals[i]:g}")


# ---- clock & weather -------------------------------------------------------------------
class ClockPage(Page):
    name = "clock"
    lead = "The clock and the weather in the corner of the map"

    def build(self):
        # first run: no config.ini yet, so this is where the user starts
        self.banner = ttk.Frame(self.body, style="Banner.TFrame", padding=(px(16), px(12)))
        ttk.Label(self.banner, text="Welcome to Topopaper",
                  style="Banner.Strong.TLabel").pack(anchor="w")
        W.autowrap(ttk.Label(self.banner, style="Banner.TLabel", text=(
            "Start by setting where you are: the clock’s time zone and the weather follow "
            "this location. Automatic works from your IP address, rounded to about 11 km."))
        ).pack(fill="x", pady=(px(4), px(8)))
        btns = ttk.Frame(self.banner, style="Banner.TFrame")
        btns.pack(anchor="w")
        self.keep_btn = self.button(btns, "Keep automatic", self._keep_auto, "Accent.TButton")
        self.keep_btn.pack(side="left")
        self.button(btns, "Choose a place", self._choose_place).pack(side="left",
                                                                     padx=(px(8), 0))

        card = self.group("Clock")
        self.switch(card, "show_clock", "Show the clock")
        self.choice(card, "clock_format", "Time format", ["12h", "24h"],
                    ["12-hour", "24-hour"], subtitle="3:45 PM or 15:45")

        card = self.group("Weather")
        self.switch(card, "show_weather", "Show the weather",
                    "Temperature and conditions under the clock")
        self.switch(card, "show_news", "Weather heads-up",
                    "A short line when rain is coming or frost is due")
        self.choice(card, "units", "Units", ["imperial", "metric"], ["°F, feet", "°C, metres"],
                    subtitle="Peak heights change on maps built from now on")
        self.now = self.row(card, "Current weather")
        self.refresh_btn = self.button(self.now.suffix, "Refresh", self._refresh_weather)
        self.now.add_suffix(self.refresh_btn)
        if not optional.has("weather"):
            self.refresh_btn.state(["disabled"])

        card = self.group("Location", "Used for the weather and the clock’s time zone")
        self.loc_card = card
        self.choice(card, "location_mode", "Location", ["auto", "manual"],
                    ["Automatic", "Choose a place"], subtitle="")
        self.auto_note = self.row(card, "Approximate",
                                  "Found from your IP address and rounded to about 11 km. "
                                  "Your precise location is never used.")
        self.place = self.row(card, "Place")
        # search for the manual location
        self.find_row = self.row(card, "Find a town or city")
        bar = ttk.Frame(self.find_row, style="Card.TFrame")
        self.below(self.find_row, bar, (8, 0))
        self.loc_var = tk.StringVar(master=bar)
        self.loc_entry = ttk.Entry(bar, textvariable=self.loc_var)
        self.loc_entry.pack(side="left", fill="x", expand=True)
        W.placeholder(self.loc_entry, self.loc_var, "e.g. Chamonix, or Denver, Colorado", self.pal)
        self.loc_btn = self.button(bar, "Find", self._lookup)
        self.loc_btn.pack(side="left", padx=(px(8), 0))
        self.loc_entry.bind("<Return>", lambda e: self._lookup())
        self.loc_status = W.autowrap(ttk.Label(self.find_row, style="Card.Dim.TLabel"))
        # or type coordinates (works offline)
        self.coord_row = self.row(card, "Or enter coordinates", "Decimal degrees")
        self.lat_var, self.lon_var = tk.StringVar(master=card), tk.StringVar(master=card)
        for var, hint in ((self.lat_var, "Latitude"), (self.lon_var, "Longitude")):
            e = ttk.Entry(self.coord_row.suffix, textvariable=var, width=11)
            self.coord_row.add_suffix(e)
            W.placeholder(e, var, hint, self.pal)
            e.bind("<Return>", lambda _e: self._save_coords())
        self.coord_row.add_suffix(self.button(self.coord_row.suffix, "Save", self._save_coords))
        self._token = 0

    def after_change(self, key):
        if key == "location_mode":
            self.refresh()

    def on_refresh(self):
        if self.store.exists():
            self.banner.pack_forget()
        elif not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", pady=(px(16), 0), after=self.body.winfo_children()[1]
                             if self.lead else None)
        manual = self.store.get("location_mode") == "manual"
        for r in (self.place, self.find_row, self.coord_row):
            self.loc_card.show(r, manual)
        self.loc_card.show(self.auto_note, not manual)
        name = self.store.raw("place_name")
        lat, lon = self.store.raw("latitude"), self.store.raw("longitude")
        self.place.set_subtitle(logic.fmt_location(name, lat, lon) if (lat and lon) else
                                "Not set yet: search below")
        self._update_now()

    def _update_now(self):
        w = logic.read_weather()
        sub = logic.weather_summary(w, self.store.get("units"))
        if not optional.has("weather"):
            sub += " · refreshing " + optional.hint("weather")
        self.now.set_subtitle(sub)

    def shown(self):
        self._update_now()
        if not self.store.exists():
            self.after(60, self._focus_location)

    def _focus_location(self):
        if not self.loc_entry.winfo_exists():
            return                              # the page was rebuilt meanwhile
        if self.loc_entry.winfo_ismapped():
            self.frame.see(self.find_row)
            self.loc_entry.focus_set()
        elif self.banner.winfo_ismapped():
            self.keep_btn.focus_set()

    def _choose_place(self):
        self.store.set("location_mode", "manual")
        self.refresh()
        self.after(60, self._focus_location)

    def _keep_auto(self):
        self.store.set("location_mode", "auto")
        self.store.save_all()
        self.refresh()
        self.toast("Using your approximate location")
        if optional.has("weather"):
            self._refresh_weather(quiet=True)

    def _lookup(self):
        q = self.loc_var.get().strip()
        if not q:
            self.loc_entry.focus_set()
            return
        self._token += 1
        tok = self._token
        self.loc_btn.state(["disabled"])
        self._status(f"Looking up “{q}”…")

        def done(res, err):
            if tok != self._token:
                return                         # a newer lookup superseded this one
            self.loc_btn.state(["!disabled"])
            if err is not None:
                kind = getattr(err, "kind", "fetch")
                self._status(f"Couldn’t find “{q}”. Check the spelling, or add a region "
                             "or country." if kind == "notfound" else
                             "The place search is unreachable. Check your connection.",
                             error=True)
                return
            lat, lon, name = res
            self.store.set_many({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                                 "place_name": name, "location_mode": "manual"})
            self.loc_var.set("")
            self._status("")
            self.refresh()
            self.toast(f"Weather location set to {name}")
            if optional.has("weather"):
                self._refresh_weather(quiet=True)
        self.run(geocode.lookup_point, q, done=done)

    def _status(self, text, error=False):
        self.loc_status.configure(text=text,
                                  style="Card.Danger.TLabel" if error else "Card.Dim.TLabel")
        if text:
            self.loc_status.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(px(6), 0))
        else:
            self.loc_status.grid_remove()

    def _save_coords(self):
        try:
            lat, lon = helpers.parse_coords(self.lat_var.get(), self.lon_var.get())
        except ValueError as e:
            self.toast(str(e), 4)
            return
        self.store.set_many({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                             "place_name": "", "location_mode": "manual"})
        self.lat_var.set("")
        self.lon_var.set("")
        self.refresh()
        self.toast("Weather location saved")
        if optional.has("weather"):
            self._refresh_weather(quiet=True)

    def _refresh_weather(self, quiet=False):
        w = optional.get("weather")
        if not w:
            return
        self.refresh_btn.state(["disabled"])
        self.now.set_subtitle("Fetching the weather…")

        def done(ok, err):
            self.refresh_btn.state(["!disabled"])
            self._update_now()
            if err is not None or not ok:
                self.toast("Couldn’t reach the weather service. Try again later.")
            elif not quiet:
                self.toast("Weather updated")
        self.run(w.refresh, done=done)


# ---- general --------------------------------------------------------------------------
STATUS_COLOURS = {"ok": "ok", "warn": "warn", "fail": "danger"}


class GeneralPage(Page):
    name = "general"
    lead = "Running, starting at login, the search shortcut and the setup check"

    def build(self):
        self._timer = None
        has_session = optional.has("session")
        card = self.group("Wallpaper")
        self.run_row = self.row(card, "Wallpaper", "Checking…")
        self.btn_start = self.button(self.run_row.suffix, "Start",
                                     lambda: self._session("start_detached"), "Accent.TButton")
        self.btn_restart = self.button(self.run_row.suffix, "Restart",
                                       lambda: self._session("restart"))
        self.btn_stop = self.button(self.run_row.suffix, "Stop", lambda: self._session("stop"))
        if not has_session:
            for b in (self.btn_start, self.btn_restart, self.btn_stop):
                b.state(["disabled"])
            self.run_row.add_suffix(self.btn_start)

        card = self.group("Startup and shortcut")
        self.auto = self.row(card, "Start automatically at login", "Checking…")
        self.auto_t = self.auto.add_suffix(W.Toggle(self.auto.suffix, self.pal,
                                                    lambda v: self._apply(self.auto_t, v,
                                                                          "enable", "disable")))
        self.key = self.row(card, "Search shortcut", "Checking…")
        self.key_t = self.key.add_suffix(W.Toggle(self.key.suffix, self.pal,
                                                  lambda v: self._apply(self.key_t, v,
                                                                        "keybind_add",
                                                                        "keybind_remove")))
        if sys.platform.startswith("linux"):
            self.choice(card, "launcher", "Search menu", list(logic.LAUNCHERS),
                        [logic.launcher_label(n) for n in logic.LAUNCHERS],
                        subtitle="Shows your maps; type any place to build a new one")
        if not optional.has("autostart"):
            for r, t in ((self.auto, self.auto_t), (self.key, self.key_t)):
                t.set_enabled(False)
                r.set_subtitle(optional.hint("autostart"))

        self.doc_card = self.group("Setup check")
        self.doc_hint = self.row(self.doc_card, "Not checked yet",
                                 "Checks the install, the desktop and the network. "
                                 "Nothing is changed.")
        self.doc_btn = self.doc_hint.add_suffix(self.button(self.doc_hint.suffix,
                                                            "Check my setup", self._run_doctor))
        self.doc_rows = []
        if not optional.has("doctor"):
            self.doc_btn.state(["disabled"])
            self.doc_hint.set_subtitle("Unavailable: " + optional.hint("doctor"))

        card = self.group("Files")
        for title, p in (("Settings", paths.config_file()), ("Maps", paths.areas_dir()),
                         ("Build logs", paths.log_dir())):
            r = self.row(card, title, str(p))
            r.add_suffix(self.button(r.suffix, "Open", lambda p=p: self._open(p)))

        card = self.group("Advanced")
        adv = self.row(card, "Show advanced settings", "Outside weather and time zone sources")
        tog = adv.add_suffix(W.Toggle(adv.suffix, self.pal))
        rows = [self.switch(card, "builtin_weather", "Built-in weather",
                            "Fetch weather from Open-Meteo in the background"),
                self.entry(card, "weather_file", "Weather file",
                           "Read the weather from this file instead of the built-in fetcher"),
                self.entry(card, "location_file", "Time zone file",
                           "A JSON file with a “tz” key, e.g. {\"tz\": \"Europe/Zurich\"}")]

        def show(v):
            for r in rows:
                card.show(r, v)
        tog.command = show
        adv.title.bind("<Button-1>", lambda e: tog._flip())
        show(False)

    # ---- lifecycle ------------------------------------------------------------
    def shown(self):
        self._poll()
        self._load_autostart()

    def hidden(self):
        if self._timer:
            self.win.root.after_cancel(self._timer)
            self._timer = None

    def _poll(self):
        if self._timer:
            self.win.root.after_cancel(self._timer)
        self._timer = self.after(2000, self._poll)
        s = optional.get("session")
        if s is None:
            self._show_running(None, None)
            return
        self.run(s.is_running, done=self._show_running)

    def _show_running(self, running, err):
        if err is not None:
            running = None
        if not optional.has("session"):
            self.run_row.set_subtitle("Start and stop " + optional.hint("session"))
            return
        self.run_row.set_subtitle({True: "Running", False: "Stopped", None: "Unknown"}[running])
        for b in (self.btn_start, self.btn_restart, self.btn_stop):
            b.pack_forget()
        for b in ((self.btn_restart, self.btn_stop) if running else (self.btn_start,)):
            self.run_row.add_suffix(b)

    def _session(self, fn):
        s = optional.get("session")
        if not s:
            return
        verb = {"start_detached": "Starting", "restart": "Restarting", "stop": "Stopping"}[fn]
        self.run_row.set_subtitle(f"{verb}…")

        def done(_r, err):
            if err is not None:
                self.toast(f"Couldn’t {fn.split('_')[0]} the wallpaper: {err}", 5)
            self.after(700, self._poll)
        self.run(getattr(s, fn), done=done)

    def _open(self, p):
        try:
            open_path(p if p.exists() else p.parent)
        except OSError as e:
            self.toast(f"Couldn’t open {p}: {e}", 5)

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
            self.auto_t.set(bool(st.get("enabled")))
            self.key_t.set(bool(kb.get("enabled")))
            detail = st.get("detail") or ""
            method = st.get("method") or ""
            if method == "own-config":
                method = ""                  # the detail already says so in words
            self.auto.set_subtitle(sentence(" · ".join(x for x in (method, detail) if x)) or
                                   "Starts the wallpaper when you log in")
            sub = ("Super+Shift+B runs topopaper-ctl search: pick one of your maps to fly "
                   "there, or type any place to build it")
            if kb.get("detail"):
                sub += "\n" + sentence(kb["detail"])
            self.key.set_subtitle(sub)
            self.key_t.set_enabled(kb.get("supported", True) is not False or
                                   bool(kb.get("enabled")))
        self.run(work, done=done)

    def _apply(self, tog, on, fn_on, fn_off):
        a = optional.get("autostart")
        if not a:
            return
        fn = getattr(a, fn_on if on else fn_off)

        def done(res, err):
            ok, msg = (False, str(err)) if err is not None else (res or (False, ""))
            if msg:
                self.toast(msg, 5 if not ok else 3)
            if not ok:
                tog.set(not on)
            self._load_autostart()
        self.run(fn, done=done)

    # ---- doctor ----------------------------------------------------------------
    def _run_doctor(self):
        d = optional.get("doctor")
        if not d:
            return
        self.doc_btn.state(["disabled"])
        self.doc_hint.set_title("Checking…")
        self.doc_hint.set_subtitle("This takes a few seconds")
        self.run(d.run_checks, done=self._show_doctor)

    def _show_doctor(self, results, err):
        self.doc_btn.state(["!disabled"])
        self.doc_btn.configure(text="Check again")
        for r in self.doc_rows:
            self.doc_card.remove(r)
        self.doc_rows = []
        if err is not None:
            results = [dict(id="doctor", title="The setup check failed", status="fail",
                            detail=str(err), fix="")]
        ok, warn, fail = logic.doctor_summary(results)
        self.doc_hint.set_title(f"{ok} passed · {warn} warning{'s' if warn != 1 else ''} · "
                                f"{fail} problem{'s' if fail != 1 else ''}")
        self.doc_hint.set_subtitle("Problems first; each says how to fix it" if warn or fail
                                   else "Everything looks right")
        order = {"fail": 0, "warn": 1, "ok": 2}
        for r in sorted(results or [], key=lambda r: order.get(r.get("status"), 1)):
            text = sentence(r.get("detail", ""))
            if r.get("fix") and r.get("status") != "ok":
                text = (text + "\n" if text else "") + "Fix: " + r["fix"]
            row = W.Row(self.doc_card, r.get("title", ""), text)
            colour = self.pal[STATUS_COLOURS.get(r.get("status"), "warn")]
            dot = tk.Canvas(row.prefix, width=px(10), height=px(10), highlightthickness=0,
                            borderwidth=0, background=self.pal["card"])
            dot.create_oval(1, 1, px(10) - 1, px(10) - 1, fill=colour, outline=colour)
            row.add_prefix(dot)
            self.doc_card.add(row)
            self.doc_rows.append(row)


# ---- about ------------------------------------------------------------------------------
class AboutPage(Page):
    name = "about"

    def build(self):
        hero = ttk.Frame(self.body, padding=(0, px(18), 0, px(4)))
        hero.pack(fill="x")
        size = px(96)
        cv = tk.Canvas(hero, width=size, height=size, highlightthickness=0, borderwidth=0,
                       background=self.pal["bg"])
        cv.pack()
        W.draw_emblem(cv, self.t, 0, 0, size)
        ttk.Label(hero, text="Topopaper", style="Display.TLabel").pack(pady=(px(6), 0))
        ttk.Label(hero, text="A live topographic map for your desktop",
                  style="Lead.TLabel").pack(pady=(px(2), px(8)))
        ttk.Label(hero, text=__version__, style="Badge.TLabel").pack()

        card = self.group()
        for title, sub, url in (("Website", PROJECT_URL, PROJECT_URL),
                                ("Report an issue", PROJECT_URL + "/issues",
                                 PROJECT_URL + "/issues")):
            r = self.row(card, title, sub)
            r.add_suffix(self.button(r.suffix, "Open", lambda u=url: open_path(u),
                                     "Ghost.TButton"))
        self.row(card, "License", "GNU General Public License v3.0 or later")

        card = self.group("Data sources", "Maps are built from open data. If you share "
                                          "screenshots or maps, please keep the credits.")
        for title, text in CREDITS:
            self.row(card, title, text)
