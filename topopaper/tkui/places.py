"""Places: the maps you have, going anywhere on Earth, builds in progress, disk use."""
import tkinter as tk
from tkinter import ttk

from .. import jobs, util
from ..settings import logic, optional
from . import helpers, widgets as W
from .pages import Page
from .widgets import px
from .work import resolve_error, resolve_place, sentence

THUMB_W, THUMB_H = 64, 40
POLL_MS = 2000


class PlacesPage(Page):
    name = "places"
    lead = "Your maps, and any place on Earth"

    def build(self):
        self._show_scaffold = False
        self._fp = None
        self._packs = []
        self._pack_rows = []
        self._job_rows = {}
        self._token = 0
        self._timer = None
        self._thumb_queue = []
        self._thumb_busy = False
        self._imgs = []
        self._blank = tk.PhotoImage(master=self.body, width=px(THUMB_W), height=px(THUMB_H))

        # -- go anywhere
        card = self.group("Go somewhere",
                          "Type any place on Earth. If you don’t have a map of it yet, one is "
                          "built from open data in a few minutes and the wallpaper flies "
                          "there when it’s ready.")
        box = ttk.Frame(card, style="Card.TFrame", padding=(px(14), px(12)))
        card.add(box)
        bar = ttk.Frame(box, style="Card.TFrame")
        bar.pack(fill="x")
        self.search_var = tk.StringVar(master=bar)
        self.search = ttk.Entry(bar, textvariable=self.search_var, style="Big.TEntry")
        self.search.pack(side="left", fill="x", expand=True)
        W.placeholder(self.search, self.search_var, "Zermatt, Yosemite, Hokkaido…", self.pal)
        self.search.bind("<Return>", lambda e: self.do_search())
        self.go = self.button(bar, "Go", self.do_search, "Accent.TButton")
        self.go.pack(side="left", fill="y", padx=(px(8), 0))
        self.status = W.autowrap(ttk.Label(box, style="Card.Dim.TLabel"))

        # -- builds in progress (only while there are some)
        self.building = self.group("Building")
        self.building.box.pack_forget()

        # -- installed maps
        self.maps = self.group("Your maps")

        # -- storage
        card = self.group("Storage")
        self.usage = self.row(card, "Space used")
        self.usage.add_suffix(self.button(self.usage.suffix, "Clean up…", self._cleanup))
        row = self.row(card, "Show connector maps",
                       "In-between maps that let a place zoom out smoothly to the globe")
        self.scaffold_t = row.add_suffix(W.Toggle(row.suffix, self.pal, self._toggle_scaffold))

    # ---- lifecycle -------------------------------------------------------------
    def on_refresh(self):
        self._fp = None                    # home may have changed: redraw rows
        if self.frame.winfo_ismapped():
            self._poll(reschedule=False)

    def shown(self):
        self._poll()

    def hidden(self):
        if self._timer:
            self.win.root.after_cancel(self._timer)
            self._timer = None

    def _poll(self, reschedule=True):
        if reschedule:
            if self._timer:
                self.win.root.after_cancel(self._timer)
            self._timer = self.after(POLL_MS, self._poll)
        self._refresh_jobs()
        fp = (logic.packs_fingerprint(), util.current_area(), self.store.raw("home_area"),
              self._show_scaffold)
        if fp != self._fp:
            self._fp = fp
            self._rebuild_packs()

    # ---- installed maps ----------------------------------------------------------
    def _rebuild_packs(self):
        home, cur = self.store.raw("home_area"), util.current_area()
        self._packs = logic.load_packs(home, cur)
        for r in self._pack_rows:
            self.maps.remove(r)
        self._pack_rows = []
        self._thumb_queue = []
        self._imgs = []
        shown = [p for p in self._packs if self._show_scaffold or not p.scaffold]
        if not any(p.name == "earth" for p in self._packs):
            self._pack_rows.append(self._globe_row())
        for p in shown:
            self._pack_rows.append(self._pack_row(p, home, cur))
        hidden = 0 if self._show_scaffold else sum(1 for p in self._packs if p.scaffold)
        self.set_description(self.maps, helpers.maps_summary(len(shown), hidden)
                             if self._packs else "")
        total = sum(p.size for p in self._packs)
        n = len(self._packs)
        self.usage.set_subtitle(f"{logic.fmt_size(total)} in {n} map{'s' if n != 1 else ''}")
        self._next_thumb()

    def _globe_row(self):
        row = self.row(self.maps, "No globe yet",
                       "The globe is the starting point for every map (about 30 MB)")
        b = row.add_suffix(self.button(row.suffix, "Get the globe", self._get_globe,
                                       "Accent.TButton"))
        if not optional.has("starter"):
            b.state(["disabled"])
            row.set_subtitle(optional.hint("starter"))
        elif self.win.starter.state == "running":
            b.state(["disabled"])
        return row

    def _pack_row(self, p, home, cur):
        sub = [p.scale, logic.fmt_size(p.size + sum(self._size(c) for c in p.children))]
        if p.scaffold:
            sub[0] = "Connector map"
        row = self.row(self.maps, p.title, " · ".join(sub))
        thumb = tk.Label(row.prefix, image=self._blank, background=self.pal["hover"],
                         borderwidth=0, highlightthickness=0)
        row.add_prefix(thumb)
        if p.preview:
            self._thumb_queue.append((thumb, p.preview, p.mtime))
        if p.is_current:
            row.add_suffix(ttk.Label(row.suffix, text="On screen", style="Badge.TLabel"))
        if p.is_home:
            row.add_suffix(ttk.Label(row.suffix, text="Home", style="Quiet.Badge.TLabel"))
        if not p.is_current:
            row.add_suffix(self.button(row.suffix, "Fly there", lambda n=p.name: self._fly(n)),
                           gap=10)
        menu = tk.Menu(row, tearoff=0)
        if not p.is_home:
            menu.add_command(label="Set as home", command=lambda n=p.name: self._set_home(n))
        if helpers.can_delete(p.name, home, cur):
            menu.add_command(label="Delete…", command=lambda n=p.name: self._ask_delete(n))
        more = self.button(row.suffix, "⋯", None, "Ghost.TButton")
        more.configure(width=2, command=lambda m=menu, b=more: self._post(m, b))
        if menu.index("end") is None:
            more.state(["disabled"])
        row.add_suffix(more, gap=4)
        for w in (row, row.title, row.sub, thumb):
            w.bind("<Double-Button-1>", lambda e, n=p.name: self._fly(n))
        return row

    def _post(self, menu, button):
        x, y = button.winfo_rootx(), button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _next_thumb(self):
        """One preview at a time, so the window stays responsive meanwhile."""
        if self._thumb_busy or not self._thumb_queue:
            return
        label, path, mtime = self._thumb_queue.pop(0)
        self._thumb_busy = True

        def done(img):
            self._thumb_busy = False
            if img is not None and label.winfo_exists():
                label.configure(image=img)
                self._imgs.append(img)
            self._next_thumb()
        W.thumbnail(self.win.worker, self.body, path, mtime, px(THUMB_W), px(THUMB_H), done)

    def _size(self, name):
        return next((p.size for p in self._packs if p.name == name), 0)

    def _toggle_scaffold(self, v):
        self._show_scaffold = v
        self._poll(reschedule=False)

    def _title(self, name):
        return logic.pack_title(name, util.read_meta(name))

    def _fly(self, name):
        util.fly(name, quiet=True)
        self.toast(f"Flying to {self._title(name)}")
        self._poll(reschedule=False)

    def _set_home(self, name):
        self.store.set("home_area", name)
        self.toast(f"{self._title(name)} is your home map")
        self._poll(reschedule=False)

    def _ask_delete(self, name):
        p = next((x for x in self._packs if x.name == name), None)
        home, cur = self.store.raw("home_area"), util.current_area()
        if not p or not helpers.can_delete(name, home, cur):
            return
        check = None
        if p.children:
            n = len(p.children)
            sz = sum(self._size(c) for c in p.children)
            check = (f"Also delete its {n} connector map{'s' if n != 1 else ''} "
                     f"({logic.fmt_size(sz)})")
        ok, also = W.ask(self.body, self.pal, self.fnt, f"Delete {p.title}?",
                         f"This frees {logic.fmt_size(p.size)}. You can build it again later "
                         "by searching for it.", ok="Delete", danger=True, check=check)
        if not ok:
            return
        names = [p.name] + (p.children if check and also else [])
        # the rows offering this were stale if home or the map on screen moved meanwhile
        names = [n for n in names if helpers.can_delete(n, self.store.raw("home_area"),
                                                         util.current_area())]

        def done(_removed, err):
            self.toast(f"Couldn’t delete {p.title}: {err}" if err else f"Deleted {p.title}")
            self._fp = None
            self._poll(reschedule=False)
        self.run(logic.delete_packs, names, done=done)

    def _get_globe(self):
        if not self.win.starter.start():
            self.toast(f"Couldn’t start the globe download: {self.win.starter.error}", 5)
        self._fp = None
        self._poll(reschedule=False)

    # ---- go somewhere ------------------------------------------------------------
    def do_search(self, text=None):
        if text is not None:
            self.search_var.set(text)
        q = self.search_var.get().strip()
        if not q:
            self.search.focus_set()
            return
        self._token += 1
        tok = self._token
        before = util.current_area()
        self.go.state(["disabled"])
        self._status(f"Looking up “{q}”…")

        def done(res, err):
            if tok != self._token:
                return                         # a newer search superseded this one
            self.go.state(["!disabled"])
            rc, msgs = res if res else (1, [])
            problem = resolve_error(q, rc, msgs, err)
            if problem:
                self._status(problem, error=True)
                return
            cur = util.current_area()
            if cur and cur != before:
                msg = f"Flying to {self._title(cur)}"
            else:
                msg = sentence(msgs[-1]) if msgs else "Done"
            self.search_var.set("")
            self._status(msg)
            self._poll(reschedule=False)
        self.run(resolve_place, q, done=done)

    def _status(self, text, error=False):
        self.status.configure(text=text,
                              style="Card.Danger.TLabel" if error else "Card.Dim.TLabel")
        if text:
            self.status.pack(fill="x", pady=(px(8), 0))
        else:
            self.status.pack_forget()

    # ---- builds ---------------------------------------------------------------
    def _refresh_jobs(self):
        js = jobs.list_jobs()
        st = self.win.starter.poll()
        if st in ("running", "failed"):
            js = [self.win.starter.as_job()] + js
        elif st == "done" and "__globe__" in self._job_rows:
            self._fp = None                     # the globe just landed
            self.toast("The globe is ready")
            self.win.starter.state = "idle"
        seen = set()
        for j in js:
            slug = j.get("slug", "")
            seen.add(slug)
            row = self._job_rows.get(slug)
            if row is None:
                row = self._job_row()
                self._job_rows[slug] = row
            self._update_job_row(row, j)
        for slug in list(self._job_rows):
            if slug not in seen:
                self.building.remove(self._job_rows.pop(slug))
        if self._job_rows and not self.building.box.winfo_ismapped():
            self.building.box.pack(fill="x", pady=(px(22), 0), before=self.maps.box)
        elif not self._job_rows:
            self.building.box.pack_forget()

    def _job_row(self):
        row = self.row(self.building)
        row.bar = ttk.Progressbar(row.suffix, mode="indeterminate", length=px(96))
        row.fly = self.button(row.suffix, "Fly there", lambda: self._fly(row.slug))
        row.log = self.button(row.suffix, "Show log", lambda: W.LogViewer.show(
            self.body, self.pal, self.fnt, row.log_path, f"Building {row.display}"))
        row.state_ = None
        return row

    def _update_job_row(self, row, j):
        row.slug = j.get("slug", "")
        row.display = j.get("display") or row.slug
        row.log_path = j.get("log") or str(jobs.log_path(row.slug))
        row.set_title(row.display)
        state = j.get("state", "running")
        row.set_subtitle(helpers.job_subtitle(j),
                         "Card.Danger.TLabel" if state == "failed" else "Card.Dim.TLabel")
        can_fly = state == "done" and util.pack_exists(row.slug)
        key = (state, can_fly)
        if key == row.state_:
            return
        row.state_ = key
        for w in (row.bar, row.fly, row.log):
            w.pack_forget()
        row.bar.stop()
        if state == "running":
            row.add_suffix(row.bar)
            row.bar.start(15)
        if can_fly:
            row.add_suffix(row.fly)
        row.add_suffix(row.log)

    # ---- clean-up -------------------------------------------------------------
    def _cleanup(self):
        from .. import gc
        d = tk.Toplevel(self.body, background=self.pal["card"])
        d.withdraw()
        d.title("Clean up old maps")
        d.transient(self.win.root)
        d.resizable(False, False)
        box = ttk.Frame(d, style="Card.TFrame", padding=(px(24), px(20), px(24), px(18)))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Clean up old maps", style="Card.TLabel",
                  font=self.fnt["big"]).pack(anchor="w")
        ttk.Label(box, style="Card.Dim.TLabel", wraplength=px(400), justify="left",
                  text="Removes maps that were built by a search and haven’t been rebuilt "
                       "since. Your home map, the map on screen and maps you added by hand "
                       "are never removed.").pack(anchor="w", pady=(px(8), px(14)))
        line = ttk.Frame(box, style="Card.TFrame")
        line.pack(anchor="w")
        ttk.Label(line, text="Older than", style="Card.TLabel").pack(side="left")
        days = tk.StringVar(master=d, value=f"{gc.DEFAULT_DAYS:g}")
        ent = ttk.Entry(line, textvariable=days, width=6, justify="right")
        ent.pack(side="left", padx=px(8))
        ttk.Label(line, text="days", style="Card.TLabel").pack(side="left")
        summary = ttk.Label(box, style="Card.Dim.TLabel", wraplength=px(400), justify="left")
        summary.pack(anchor="w", pady=(px(12), 0))
        state = {"cands": [], "after": None}
        btns = ttk.Frame(box, style="Card.TFrame")
        btns.pack(fill="x", pady=(px(20), 0))
        delete = ttk.Button(btns, text="Delete", style="Danger.TButton")
        delete.pack(side="right")
        ttk.Button(btns, text="Cancel", command=d.destroy).pack(side="right", padx=(0, px(8)))

        def update():
            state["after"] = None
            try:
                n = max(1.0, float(days.get()))
            except ValueError:
                summary.configure(text="Enter a number of days.")
                delete.state(["disabled"])
                return
            c = gc.candidates(n)
            state["cands"] = c
            if not c:
                summary.configure(text="Nothing to clean up.")
            else:
                names = ", ".join(logic.pack_title(x["group"][0]) for x in c[:6])
                more = f" and {len(c) - 6} more" if len(c) > 6 else ""
                summary.configure(text=f"{len(c)} map{'s' if len(c) != 1 else ''} "
                                       f"({logic.fmt_size(sum(x['size'] for x in c))}): "
                                       f"{names}{more}")
            delete.state(["!disabled"] if c else ["disabled"])

        def later(*_):
            if state["after"]:
                d.after_cancel(state["after"])
            state["after"] = d.after(300, update)
        days.trace_add("write", later)

        def go():
            cands = state["cands"]
            d.destroy()
            if not cands:
                return
            self.run(gc.delete, cands, done=lambda r, e: (self.toast(
                f"Couldn’t clean up: {e}" if e else
                f"Removed {len(r)} map{'s' if len(r) != 1 else ''}"), self._poll(False)))
        delete.configure(command=go)
        d.bind("<Escape>", lambda e: d.destroy())
        update()
        W.center(d, self.win.root)
        d.deiconify()
        try:
            d.wait_visibility()
            d.grab_set()
        except tk.TclError:
            pass
        ent.focus_set()
