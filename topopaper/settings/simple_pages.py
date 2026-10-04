"""Appearance, Motion, Clock & Weather and Displays."""
from gi.repository import Adw, Gdk, GLib, Gtk

from .. import geocode, themes
from . import logic, optional
from .widgets import Page, SliderRow, run_async, spinner, text_button


# ---- appearance ---------------------------------------------------------------------
class AppearancePage(Page):
    name = "appearance"
    title = "Appearance"
    icon = "applications-graphics-symbolic"

    def build(self):
        from .widgets import ThemePreview
        g = self.group("Theme", "Colours for the map, the clock and the labels")
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
                                min_children_per_line=2, max_children_per_line=3,
                                column_spacing=12, row_spacing=12,
                                valign=Gtk.Align.START)
        self.flow.add_css_class("tp-themes")
        self.cards = {}
        for key, t in themes.THEMES.items():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.append(ThemePreview(t))
            lab = Gtk.Label(label=t["label"], ellipsize=3, xalign=0.5)
            box.append(lab)
            child = Gtk.FlowBoxChild(child=box)
            child.update_property([Gtk.AccessibleProperty.LABEL], [t["label"]])
            child.theme = key
            self.flow.append(child)
            self.cards[key] = child
        self.flow.connect("selected-children-changed", self._picked)
        g.add(self.flow)
        self._refreshers.append(self._select_current)

        g = self.group("On the Map")
        g.add(self.switch("labels", "Place names", "Towns, peaks, lakes and lifts"))
        g.add(self.switch("city_lights", "City lights",
                          "Lights glow on the night side of the globe"))
        g.add(self.switch("aurora", "Aurora", "Northern and southern lights on the night side"))
        g.add(self.switch("react_to_cpu", "React to activity",
                          "Lines speed up and warm while the computer is busy"))

    def _select_current(self):
        cur = self.store.get("theme")
        child = self.cards.get(cur)
        if child:
            self.flow.select_child(child)

    def _picked(self, *_):
        if self._syncing:
            return
        sel = self.flow.get_selected_children()
        if sel and sel[0].theme != self.store.get("theme"):
            self.store.set("theme", sel[0].theme)


# ---- motion ---------------------------------------------------------------------------
class MotionPage(Page):
    name = "motion"
    title = "Motion"
    icon = "media-playback-start-symbolic"

    def build(self):
        g = self.group("Movement")
        self.roam = Adw.ComboRow(title="Fly to another map",
                                 subtitle="On average, how often the wallpaper travels "
                                          "to another of your maps on its own")
        self.roam.connect("notify::selected", self._roam_changed)
        self._roam_vals = []
        self._refreshers.append(self._roam_refresh)
        g.add(self.roam)

        self.speed = SliderRow("Animation speed", "Drift, contour crawl and zoom breathing",
                               0.25, 2.0, 0.05,
                               marks=((0.25, None), (1.0, "Normal"), (2.0, None)),
                               fmt=logic.speed_label)
        self.speed.scale.connect("value-changed", self._speed_changed)
        self._refreshers.append(
            lambda: self.speed.scale.set_value(self.store.get("animation_speed")))
        g.add(self.speed)

        g = self.group("Frame Rate", "Lower caps save power; the map moves slowly, "
                                     "so even 24 fps looks smooth")
        g.add(self.spin("fps", "On mains power", 5, 240, subtitle="Frames per second"))
        g.add(self.spin("fps_battery", "On battery", 5, 240, subtitle="Frames per second"))

        g = self.group("When Windows Cover the Wallpaper")
        g.add(self.switch("pause_when_covered", "Pause the animation",
                          "Freeze the map while it can’t be seen"))
        self.fps_cov = self.spin("fps_covered", "Frame rate while covered", 1, 60,
                                 subtitle="Frames per second while hidden behind windows")
        g.add(self.fps_cov)

    def _roam_refresh(self):
        cur = self.store.raw("roam_minutes")
        vals, labels = logic.roam_options(cur)
        if vals != self._roam_vals:
            self._roam_vals = vals
            self.roam.set_model(Gtk.StringList.new(labels))
        self.roam.set_selected(logic.roam_index(vals, cur))

    def _roam_changed(self, *_):
        i = self.roam.get_selected()
        if self._syncing or not (0 <= i < len(self._roam_vals)):
            return
        self.store.set("roam_minutes", f"{self._roam_vals[i]:g}")

    def _speed_changed(self, scale):
        if not self._syncing:
            self.store.set("animation_speed", f"{scale.get_value():.2f}", debounce=True)


# ---- clock & weather -------------------------------------------------------------------
class ClockPage(Page):
    name = "clock"
    title = "Clock & Weather"
    icon = "preferences-system-time-symbolic"

    def build(self):
        g = self.group("Clock")
        g.add(self.switch("show_clock", "Show the clock"))
        g.add(self.combo("clock_format", "Time format", ["12h", "24h"],
                         ["12-hour (3:45 PM)", "24-hour (15:45)"], subtitle=""))

        g = self.group("Weather")
        g.add(self.switch("show_weather", "Show the weather",
                          "Temperature and conditions under the clock"))
        g.add(self.switch("show_news", "Weather heads-up",
                          "A short line when rain is coming or frost is due"))
        g.add(self.combo("units", "Units", ["imperial", "metric"],
                         ["Fahrenheit and feet", "Celsius and metres"],
                         subtitle="Peak heights change on maps built from now on"))
        self.now = Adw.ActionRow(title="Current weather")
        self.refresh_btn = text_button("Refresh", self._refresh_weather)
        self.refresh_spin = spinner()
        self.refresh_spin.set_visible(False)
        self.refresh_spin.set_valign(Gtk.Align.CENTER)
        self.now.add_suffix(self.refresh_spin)
        self.now.add_suffix(self.refresh_btn)
        if not optional.has("weather"):
            self.refresh_btn.set_sensitive(False)
            self.refresh_btn.set_tooltip_text(optional.hint("weather"))
        g.add(self.now)

        g = self.group("Location", "Used for the weather and the clock’s time zone")
        self.mode = self.combo("location_mode", "Location", ["auto", "manual"],
                               ["Automatic", "Choose a place"], subtitle="")
        g.add(self.mode)
        self.auto_note = Adw.ActionRow(
            title="Approximate",
            subtitle="Found from your IP address and rounded to about 11 km. "
                     "Your precise location is never used.")
        self.auto_note.add_prefix(Gtk.Image(icon_name="find-location-symbolic"))
        g.add(self.auto_note)
        self.place = Adw.ActionRow(title="Place")
        self.place.add_css_class("property")
        g.add(self.place)
        self.loc_search = Adw.EntryRow(title="Search for a town or city")
        self.loc_search.connect("entry-activated", lambda *_: self._lookup())
        self.loc_spin = spinner()
        self.loc_spin.set_visible(False)
        self.loc_spin.set_valign(Gtk.Align.CENTER)
        self.loc_search.add_suffix(self.loc_spin)
        b = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Search",
                       valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self._lookup())
        self.loc_search.add_suffix(b)
        g.add(self.loc_search)
        self._token = 0

    def after_change(self, key):
        if key == "location_mode":
            self.on_refresh()

    def on_refresh(self):
        manual = self.store.get("location_mode") == "manual"
        self.auto_note.set_visible(not manual)
        self.place.set_visible(manual)
        self.loc_search.set_visible(manual)
        name = self.store.raw("place_name")
        lat, lon = self.store.raw("latitude"), self.store.raw("longitude")
        self.place.set_subtitle(GLib.markup_escape_text(
            logic.fmt_location(name, lat, lon) if (lat and lon) else
            "Not set yet — search below"))
        self._update_now()

    def _update_now(self):
        w = logic.read_weather()
        sub = logic.weather_summary(w, self.store.get("units"))
        if not optional.has("weather"):
            sub += " · refreshing " + optional.hint("weather")
        self.now.set_subtitle(sub)

    def shown(self):
        self._update_now()

    def _lookup(self):
        q = self.loc_search.get_text().strip()
        if not q:
            return
        self._token += 1
        tok = self._token
        self.loc_spin.set_visible(True)

        def done(res, err):
            if tok != self._token:
                return
            self.loc_spin.set_visible(False)
            if err is not None:
                kind = getattr(err, "kind", "fetch")
                self.toast(f"Couldn’t find “{q}”" if kind == "notfound" else
                           "The place search is unreachable — check your connection")
                return
            lat, lon, name = res
            self.store.set_many({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
                                 "place_name": name, "location_mode": "manual"})
            self.loc_search.set_text("")
            self.refresh()
            self.toast(f"Weather location set to {name}")
            if optional.has("weather"):
                self._refresh_weather(quiet=True)
        run_async(geocode.lookup_point, q, done=done)

    def _refresh_weather(self, quiet=False):
        w = optional.get("weather")
        if not w:
            return
        self.refresh_btn.set_sensitive(False)
        self.refresh_spin.set_visible(True)

        def done(ok, err):
            self.refresh_btn.set_sensitive(True)
            self.refresh_spin.set_visible(False)
            self._update_now()
            if err is not None or not ok:
                self.toast("Couldn’t reach the weather service — try again later")
            elif not quiet:
                self.toast("Weather updated")
        run_async(w.refresh, done=done)


# ---- displays ------------------------------------------------------------------------
class DisplaysPage(Page):
    name = "displays"
    title = "Displays"
    icon = "video-display-symbolic"

    def build(self):
        g = self.group("Show the Wallpaper On")
        self.all = Adw.SwitchRow(title="All displays",
                                 subtitle="Including displays connected later")
        self.all.connect("notify::active", self._all_changed)
        g.add(self.all)
        self.list = self.group()
        self._rows = []
        disp = Gdk.Display.get_default()
        self.monitors = disp.get_monitors() if disp else None
        if self.monitors is not None:
            self.monitors.connect("items-changed", lambda *_: self.refresh())

        g = self.group("Layer")
        g.add(self.combo("layer", "Layer", ["bottom", "background"],
                         ["Bottom", "Background"],
                         subtitle="Bottom draws above other wallpaper tools such as "
                                  "swaybg or hyprpaper. Choose Background to keep "
                                  "another tool on top."))

    def _connected(self):
        out = []
        if self.monitors is None:
            return out
        for i in range(self.monitors.get_n_items()):
            m = self.monitors.get_item(i)
            conn = m.get_connector() or f"Display {i + 1}"
            geo = m.get_geometry()
            scale = m.get_scale_factor() if hasattr(m, "get_scale_factor") else 1
            desc = m.get_description() if hasattr(m, "get_description") else ""
            sub = logic.monitor_subtitle(m.get_manufacturer() or "", m.get_model() or "",
                                         m.get_width_mm(), m.get_height_mm(),
                                         geo.width * scale, geo.height * scale,
                                         description=desc if desc and desc != conn else "")
            out.append((conn, sub))
        return out

    def on_refresh(self):
        value = self.store.raw("outputs")
        sel = logic.parse_outputs(value)
        self.all.set_active(sel is None)
        for r in self._rows:
            self.list.remove(r)
        self._rows = []
        conn = self._connected()
        names = [c for c, _ in conn]
        items = list(conn) + [(n, "Not connected") for n in (sel or []) if n not in names]
        for name, sub in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=GLib.markup_escape_text(sub))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER,
                                    active=sel is None or name in sel)
            check.update_property([Gtk.AccessibleProperty.LABEL], [name])
            check.connect("toggled", self._check_toggled, name)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self.list.add(row)
            self._rows.append(row)
        if not items:
            row = Adw.ActionRow(title="No displays found")
            self.list.add(row)
            self._rows.append(row)
        self.list.set_sensitive(sel is not None)
        self.list.set_title("Displays")
        self.list.set_description("Pick the displays to draw on" if sel is not None else None)

    def _all_changed(self, *_):
        if self._syncing:
            return
        if self.all.get_active():
            self.store.set("outputs", "all")
        else:
            names = [c for c, _ in self._connected()]
            self.store.set("outputs", logic.format_outputs(names))
        self.refresh()

    def _check_toggled(self, check, name):
        if self._syncing:
            return
        new = logic.toggle_output(self.store.raw("outputs"), name, check.get_active(),
                                  [c for c, _ in self._connected()])
        if new == "all" and not check.get_active():
            self._syncing = True
            check.set_active(True)              # keep at least one display
            self._syncing = False
            self.toast("The wallpaper needs at least one display")
            return
        self.store.set("outputs", new)
