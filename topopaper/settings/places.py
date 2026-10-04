"""Places: the maps you have, adding new ones, builds in progress, disk use."""
from gi.repository import Adw, Gio, GLib, Gtk

from .. import geocode, jobs, util
from . import logic, optional
from .widgets import (Page, icon_button, load_thumbnail, open_path, pill, run_async,
                      spinner, text_button)

THUMB_W, THUMB_H = 64, 40


class PlacesPage(Page):
    name = "places"
    title = "Places"
    icon = "mark-location-symbolic"

    def build(self):
        self._show_scaffold = False
        self._fp = None
        self._packs = []
        self._job_rows = {}
        self._pack_rows = []
        self._search_token = 0
        self._tick = 0
        self._timer = 0
        self._pulse = 0

        actions = Gio.SimpleActionGroup()
        for name, fn in (("fly", self._fly), ("home", self._set_home),
                         ("delete", self._ask_delete)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, v, fn=fn: fn(v.get_string()))
            actions.add_action(a)
        self.widget.insert_action_group("pack", actions)

        # -- add a place
        g = self.group("Add a Place",
                       "Build a map of anywhere on Earth from open data. "
                       "A new map takes a few minutes and joins the atlas.")
        self.search = Adw.EntryRow(title="Search, e.g. “Zermatt” or “Yosemite”")
        self.search.connect("entry-activated", lambda *_: self._do_search())
        self.search_spin = spinner()
        self.search_spin.set_visible(False)
        self.search_spin.set_valign(Gtk.Align.CENTER)
        self.search.add_suffix(self.search_spin)
        self.search.add_suffix(icon_button("system-search-symbolic", "Search",
                                           self._do_search))
        g.add(self.search)
        self.result = Adw.ActionRow(visible=False)
        self.result.set_title_lines(2)
        self.result_btn = text_button("Build Map", self._result_action, "suggested-action")
        self.result.add_suffix(self.result_btn)
        g.add(self.result)
        self._plan = None
        self._result_kind = None

        # -- builds in progress
        self.building = self.group("Building")
        self.building.set_visible(False)

        # -- installed maps
        self.maps = self.group("Your Maps")
        self.maps_empty = Adw.ActionRow(title="No maps yet",
                                        subtitle="The globe is the starting point for every map")
        self.maps_empty.add_suffix(text_button("Get the Globe", self._get_globe,
                                               "suggested-action"))

        # -- storage
        g = self.group("Storage")
        self.usage = Adw.ActionRow(title="Space used")
        self.usage.add_css_class("property")
        self.usage.add_suffix(text_button("Clean Up…", self._cleanup))
        g.add(self.usage)
        self.scaffold_row = Adw.SwitchRow(
            title="Show connector maps",
            subtitle="In-between maps that let a place zoom out smoothly to the globe")
        self.scaffold_row.connect("notify::active", self._toggle_scaffold)
        g.add(self.scaffold_row)

    # ---- lifecycle -------------------------------------------------------------
    def on_refresh(self):
        self._fp = None                    # home may have changed: redraw rows
        self._poll()

    def shown(self):
        if not self._timer:
            self._timer = GLib.timeout_add(1000, self._poll)
        self._poll()

    def hidden(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._stop_pulse()

    def _poll(self):
        self._refresh_jobs()
        fp = (logic.packs_fingerprint(), util.current_area(), self.store.raw("home_area"),
              self._show_scaffold)
        if fp != self._fp:
            self._fp = fp
            self._rebuild_packs()
        return True

    # ---- installed maps ----------------------------------------------------------
    def _rebuild_packs(self):
        home, cur = self.store.raw("home_area"), util.current_area()
        self._packs = logic.load_packs(home, cur)
        for r in self._pack_rows:
            self.maps.remove(r)
        self._pack_rows = []
        shown = [p for p in self._packs if self._show_scaffold or not p.scaffold]
        if not shown:
            self.maps.add(self.maps_empty)
            self._pack_rows.append(self.maps_empty)
        for p in shown:
            row = self._pack_row(p)
            self.maps.add(row)
            self._pack_rows.append(row)
        n = len(shown)
        hidden = sum(1 for p in self._packs if p.scaffold) if not self._show_scaffold else 0
        desc = f"{n} map{'s' if n != 1 else ''}"
        if hidden:
            desc += f" · {hidden} connector map{'s' if hidden != 1 else ''} hidden"
        self.maps.set_description(desc if self._packs else None)
        total = sum(p.size for p in self._packs)
        self.usage.set_subtitle(f"{logic.fmt_size(total)} in {len(self._packs)} "
                                f"map{'s' if len(self._packs) != 1 else ''}")

    def _pack_row(self, p):
        row = Adw.ActionRow(title=GLib.markup_escape_text(p.title))
        sub = [p.scale, logic.fmt_size(p.size + sum(self._size(c) for c in p.children))]
        if p.scaffold:
            sub[0] = "Connector map"
        row.set_subtitle(" · ".join(sub))
        # the overlay's size comes from the fixed box, never from the picture
        holder = Gtk.Overlay(valign=Gtk.Align.CENTER, overflow=Gtk.Overflow.HIDDEN)
        holder.set_child(Gtk.Box(width_request=THUMB_W, height_request=THUMB_H))
        holder.add_css_class("tp-thumb")
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        holder.add_overlay(pic)
        holder.set_measure_overlay(pic, False)
        row.add_prefix(holder)
        if p.preview:
            load_thumbnail(p.preview, p.mtime, THUMB_W, THUMB_H, pic.set_paintable)
        if p.is_current:
            row.add_suffix(pill("On screen", "accent"))
        if p.is_home:
            row.add_suffix(pill("Home"))
        if not p.is_current:
            fly = text_button("Fly", lambda n=p.name: self._fly(n), "flat")
            fly.set_tooltip_text(f"Show {p.title} on the wallpaper")
            row.add_suffix(fly)
        menu = Gio.Menu()
        if not p.is_home:
            menu.append("Set as Home", f"pack.home::{p.name}")
        if p.deletable:
            menu.append("Delete…", f"pack.delete::{p.name}")
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                            valign=Gtk.Align.CENTER, tooltip_text="More")
        mb.add_css_class("flat")
        mb.set_sensitive(menu.get_n_items() > 0)
        row.add_suffix(mb)
        return row

    def _size(self, name):
        return next((p.size for p in self._packs if p.name == name), 0)

    def _toggle_scaffold(self, *_):
        self._show_scaffold = self.scaffold_row.get_active()
        self._poll()

    def _fly(self, name):
        util.fly(name, quiet=True)
        self.toast(f"Flying to {logic.pack_title(name, util.read_meta(name))}")
        self._poll()

    def _set_home(self, name):
        self.store.set("home_area", name)
        self.toast(f"{logic.pack_title(name, util.read_meta(name))} is your home map")
        self._poll()

    def _ask_delete(self, name):
        p = next((x for x in self._packs if x.name == name), None)
        if not p or not p.deletable:
            return
        d = Adw.AlertDialog(heading=f"Delete {p.title}?",
                            body=f"This frees {logic.fmt_size(p.size)}. You can build "
                                 "it again later by searching for it.")
        check = None
        if p.children:
            sz = sum(self._size(c) for c in p.children)
            n = len(p.children)
            check = Gtk.CheckButton(
                label=f"Also delete its {n} connector map{'s' if n != 1 else ''} "
                      f"({logic.fmt_size(sz)})", active=True)
            d.set_extra_child(check)
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        def on_response(_d, resp):
            if resp != "delete":
                return
            names = [p.name] + (p.children if check and check.get_active() else [])
            self._delete(names, p.title)
        d.connect("response", on_response)
        d.present(self.win)

    def _delete(self, names, label):
        home, cur = self.store.raw("home_area"), util.current_area()
        if home in names:
            self.store.set("home_area", "earth")
        if cur in names:
            util.fly(self.store.raw("home_area") or "earth", quiet=True)

        def done(removed, err):
            if err:
                self.toast(f"Couldn’t delete {label}: {err}")
            else:
                self.toast(f"Deleted {label}")
            self._fp = None
            self._poll()
        run_async(logic.delete_packs, names, done=done)

    def _get_globe(self):
        app = self.win.app
        app.ensure_globe()
        if app.starter_state == "missing":
            self.toast(f"Can’t download the globe: {optional.hint('starter')}", 5)
        self._poll()

    # ---- search / add ------------------------------------------------------------
    def _do_search(self):
        q = self.search.get_text().strip()
        if not q:
            return
        self._search_token += 1
        tok = self._search_token
        self.search_spin.set_visible(True)
        self.result.set_visible(False)
        cur = util.current_area()
        near = geocode.near_installed(q, cur)
        if util.pack_exists(util.slugify(q)):
            near = util.slugify(q)
        if near:
            self._show_result(tok, ("installed", near), None)
            return
        run_async(geocode.plan, q, cur,
                  done=lambda res, err: self._show_result(tok, ("plan", res), err, q))

    def _show_result(self, tok, res, err, q=""):
        if tok != self._search_token:
            return                                 # a newer search superseded this one
        self.search_spin.set_visible(False)
        self.result.set_visible(True)
        self.result_btn.set_visible(True)
        self.result.remove_css_class("error")
        if err is not None:
            kind = getattr(err, "kind", "fetch")
            self._plan, self._result_kind = None, None
            self.result_btn.set_visible(False)
            self.result.set_title({"notfound": f"Couldn’t find “{GLib.markup_escape_text(q)}”",
                                   "toobig": f"“{GLib.markup_escape_text(q)}” is too big"}.get(
                                       kind, "The place search is unreachable"))
            self.result.set_subtitle({"notfound": "Check the spelling, or add a region or country",
                                      "toobig": "Maps can be up to about a continent — try a "
                                                "smaller region"}.get(
                                          kind, "Check your internet connection and try again"))
            return
        kind, val = res
        if kind == "installed":
            meta = util.read_meta(val)
            self._plan, self._result_kind = val, "fly"
            self.result.set_title(GLib.markup_escape_text(logic.pack_title(val, meta)))
            self.result.set_subtitle(f"{logic.scale_label(logic.tier_of(meta or {}))} · "
                                     "already installed")
            self.result_btn.set_label("Fly Here")
            return
        plan = val
        self._plan = plan
        scale = geocode.TIER_NAMES.get(plan["tier"], "")
        scale = f"{scale[0].upper()}{scale[1:]} map" if scale else "Map"
        full = plan.get("full_name") or plan["display"]
        self.result.set_title(GLib.markup_escape_text(plan["display"]))
        building = any(j.get("slug") == plan["slug"] and j.get("state") == "running"
                       for j in jobs.list_jobs())
        if util.pack_exists(plan["slug"]):
            self._result_kind = "fly"
            self._plan = plan["slug"]
            status, label = "already installed", "Fly Here"
        elif building:
            self._result_kind = None
            status, label = "building now", ""
        else:
            self._result_kind = "build"
            status, label = "new", "Build Map"
        extra = " · closest spelling match" if plan.get("fuzzy") else ""
        self.result.set_subtitle(GLib.markup_escape_text(
            f"{scale} · {status}{extra}\n{full}"))
        self.result_btn.set_label(label)
        self.result_btn.set_visible(bool(label))

    def _result_action(self):
        if self._result_kind == "fly" and self._plan:
            self._fly(self._plan)
        elif self._result_kind == "build" and self._plan:
            plan = self._plan
            try:
                jobs.start(plan)
            except OSError as e:
                self.toast(f"Couldn’t start the build: {e}")
                return
            self.toast(f"Building {plan['display']} — it will fly there when ready")
            self.result.set_visible(False)
            self.search.set_text("")
            self._poll()

    # ---- builds ---------------------------------------------------------------
    def _refresh_jobs(self):
        js = jobs.list_jobs()
        app = self.win.app
        if app.starter_state in ("running", "failed"):
            js = [dict(slug="__globe__", display="The globe",
                       state=app.starter_state,
                       stage=app.starter_line or "starting",
                       log=app.starter_log)] + js
        seen = set()
        for j in js:
            slug = j.get("slug", "")
            seen.add(slug)
            row = self._job_rows.get(slug)
            if row is None:
                row = self._job_row(j)
                self._job_rows[slug] = row
                self.building.add(row)
            self._update_job_row(row, j)
        for slug in list(self._job_rows):
            if slug not in seen:
                self.building.remove(self._job_rows.pop(slug))
        self.building.set_visible(bool(self._job_rows))
        running = any(j.get("state") == "running" for j in js)
        if running and not self._pulse:
            self._pulse = GLib.timeout_add(120, self._do_pulse)
        elif not running:
            self._stop_pulse()

    def _job_row(self, j):
        row = Adw.ActionRow()
        row.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=96)
        row.bar.set_pulse_step(0.08)
        row.add_suffix(row.bar)
        row.fly = text_button("Fly Here")
        row.fly.connect("clicked", lambda *_: self._fly(row.slug))
        row.add_suffix(row.fly)
        row.log = text_button("View Log", lambda: open_path(row.log_path, self.win), "flat")
        row.add_suffix(row.log)
        return row

    def _update_job_row(self, row, j):
        row.slug = j.get("slug", "")
        row.log_path = j.get("log") or str(jobs.log_path(row.slug))
        row.set_title(GLib.markup_escape_text(j.get("display") or row.slug))
        state = j.get("state", "running")
        stage = j.get("stage", "")
        row.set_subtitle(GLib.markup_escape_text(
            {"done": "Ready", "failed": f"Failed — {stage}"}.get(
                state, stage[:1].upper() + stage[1:] + "…")))
        row.bar.set_visible(state == "running")
        row.fly.set_visible(state == "done" and util.pack_exists(row.slug))
        if state == "failed":
            row.add_css_class("error")
        else:
            row.remove_css_class("error")
        row.running = state == "running"

    def _do_pulse(self):
        for row in self._job_rows.values():
            if getattr(row, "running", False):
                row.bar.pulse()
        return True

    def _stop_pulse(self):
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = 0

    # ---- clean-up -------------------------------------------------------------
    def _cleanup(self):
        from .. import gc
        d = Adw.AlertDialog(heading="Clean Up Old Maps",
                            body="Removes maps that were built by a search and haven’t "
                                 "been rebuilt since. Your home map, the map on screen "
                                 "and maps you added by hand are never removed.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        days = Adw.SpinRow(title="Older than", subtitle="days",
                           adjustment=Gtk.Adjustment(lower=1, upper=3650, value=gc.DEFAULT_DAYS,
                                                     step_increment=1, page_increment=30))
        lb.append(days)
        box.append(lb)
        summary = Gtk.Label(wrap=True, xalign=0)
        summary.add_css_class("dim-label")
        box.append(summary)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        state = {"cands": []}

        def update(*_):
            c = gc.candidates(days.get_value())
            state["cands"] = c
            if not c:
                summary.set_label("Nothing to clean up.")
            else:
                names = ", ".join(logic.pack_title(x["group"][0]) for x in c[:6])
                more = f" and {len(c) - 6} more" if len(c) > 6 else ""
                summary.set_label(f"{len(c)} map{'s' if len(c) != 1 else ''} "
                                  f"({logic.fmt_size(sum(x['size'] for x in c))}): "
                                  f"{names}{more}")
            d.set_response_enabled("delete", bool(c))
        days.connect("notify::value", update)
        update()

        def on_response(_d, resp):
            if resp != "delete" or not state["cands"]:
                return
            run_async(gc.delete, state["cands"],
                      done=lambda r, e: (self.toast(
                          f"Couldn’t clean up: {e}" if e else
                          f"Removed {len(r)} map{'s' if len(r) != 1 else ''}"), self._poll()))
        d.connect("response", on_response)
        d.present(self.win)

