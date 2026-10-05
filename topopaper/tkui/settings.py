"""topopaper-settings without GTK: the Tk settings window (Windows, macOS).

    python -m topopaper.tkui.settings [--page places|appearance|motion|clock|general|about]
                                      [--search PLACE]

The same pages as the GTK window in topopaper/settings/, minus Displays
(output names only mean something to a Wayland compositor) and the welcome
flow: until config.ini exists the window opens on Clock & Weather, so the
location is set first. Edits go through store.Store and the engine applies
them live. A page that fails to build shows its error instead of taking the
window down, and a control whose helper module is missing (see
topopaper.settings.optional) is disabled with the reason.
"""
import os
import sys
import time

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:            # e.g. Debian without python3-tk: main() explains
    tk = ttk = None

from .. import __version__, paths
from . import helpers, look
from .helpers import PAGES, parse_args
from .store import Store


class NavItem:
    """One sidebar entry: an accent bar and a label, focusable and clickable."""

    def __init__(self, win, master, name, title):
        from .widgets import px
        self.win, self.name, self.pal = win, name, win.pal
        p = self.pal
        self.selected = False
        self.f = tk.Frame(master, background=p["side"], takefocus=1, highlightthickness=1,
                          highlightbackground=p["side"], highlightcolor=p["accent"],
                          cursor="hand2")
        self.bar = tk.Frame(self.f, width=px(3), background=p["side"])
        self.bar.pack(side="left", fill="y")
        self.lab = tk.Label(self.f, text=title, anchor="w", background=p["side"],
                            foreground=p["dim"], font=win.fnt["body"], padx=px(12),
                            pady=px(7))
        self.lab.pack(side="left", fill="x", expand=True)
        for w in (self.f, self.bar, self.lab):
            w.bind("<Button-1>", lambda e: self.win.select(self.name))
            w.bind("<Enter>", lambda e: self._paint(hover=True))
            w.bind("<Leave>", lambda e: self._paint())
        for seq in ("<Return>", "<space>"):
            self.f.bind(seq, lambda e: self.win.select(self.name))
        self.f.bind("<Up>", lambda e: self.win.step(-1, focus_nav=True))
        self.f.bind("<Down>", lambda e: self.win.step(1, focus_nav=True))
        self.f.pack(fill="x", padx=px(10), pady=1)

    def set_selected(self, on):
        self.selected = on
        self._paint()

    def _paint(self, hover=False):
        p = self.pal
        bg = p["side_sel"] if self.selected else (p["side_hover"] if hover else p["side"])
        self.f.configure(background=bg, highlightbackground=bg)
        self.lab.configure(background=bg, foreground=p["text"] if self.selected or hover
                           else p["dim"])
        self.bar.configure(background=p["accent"] if self.selected else bg)


class _BrokenPage:
    """Stands in for a page whose build raised: says so instead of crashing."""

    def __init__(self, win, parent, name, err):
        from .widgets import ScrollPage, autowrap
        self.name = name
        self.frame = ScrollPage(parent, win.pal)
        ttk.Label(self.frame.body, text=helpers.TITLES.get(name, name),
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.frame.body, text="This page couldn’t load",
                  style="Strong.TLabel").pack(anchor="w", pady=(18, 4))
        autowrap(ttk.Label(self.frame.body, text=str(err) or type(err).__name__,
                           style="Dim.TLabel")).pack(fill="x")

    def refresh(self):
        pass

    def shown(self):
        pass

    def hidden(self):
        pass


class Window:
    def __init__(self, root, page="places"):
        from . import widgets as W
        from .work import Starter, Worker
        self.root = root
        self.W = W
        self.store = Store(after=root.after, cancel=root.after_cancel)
        self.worker = Worker(root)
        self.starter = Starter(paths.log_dir() / "starter.log")
        self.fnt = look.fonts(root)
        self.pages = {}
        self.nav = {}
        self.current = None
        self.frame = None
        self.theme = None
        self._last_reload = 0.0
        self.store.connect(self._changed)
        W.install_wheel(root)
        root.title("Topopaper")
        root.minsize(W.px(640), W.px(460))
        root.geometry(f"{W.px(940)}x{W.px(700)}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<FocusIn>", self._focus_in, add="+")
        mod = "Command" if sys.platform == "darwin" else "Control"
        for k in ("w", "q"):
            root.bind(f"<{mod}-{k}>", lambda e: self.close())
        root.bind(f"<{mod}-f>", lambda e: self.focus_search())
        root.bind("<Control-Next>", lambda e: self.step(1))
        root.bind("<Control-Prior>", lambda e: self.step(-1))
        self.build(page)

    # ---- building (again on a theme change) --------------------------------------
    def build(self, page=None):
        from .pages import AboutPage, AppearancePage, ClockPage, GeneralPage, MotionPage
        from .places import PlacesPage
        W, px = self.W, self.W.px
        self.theme = self.store.get("theme")
        self.t = look.theme_colours(self.theme)
        self.pal = p = look.palette(self.theme)
        look.apply(self.root, p, self.fnt)
        try:
            self._icon = W.icon_image(self.root, self.t)
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        if self.frame is not None:
            if self.current in self.pages:
                self.pages[self.current].hidden()
            self.frame.destroy()
        self.pages, self.nav, self.current = {}, {}, None

        self.frame = tk.Frame(self.root, background=p["bg"])
        self.frame.pack(fill="both", expand=True)
        side = tk.Frame(self.frame, background=p["side"], width=px(212))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(self.frame, background=p["border"], width=1).pack(side="left", fill="y")
        self.content = tk.Frame(self.frame, background=p["bg"])
        self.content.pack(side="left", fill="both", expand=True)
        self.toasts = W.Toast(self.content, p, self.fnt)

        brand = ttk.Frame(side, style="Side.TFrame")
        brand.pack(fill="x", padx=px(20), pady=(px(22), px(20)))
        size = px(30)
        cv = tk.Canvas(brand, width=size, height=size, background=p["side"],
                       highlightthickness=0, borderwidth=0)
        cv.pack(side="left")
        W.draw_emblem(cv, self.t, 0, 0, size)
        ttk.Label(brand, text="Topopaper", style="Side.Title.TLabel").pack(
            side="left", padx=(px(10), 0))
        for name in PAGES:
            self.nav[name] = NavItem(self, side, name, helpers.TITLES[name])
        ttk.Label(side, text=f"Version {__version__}", style="Side.Dim.TLabel").pack(
            side="bottom", anchor="w", padx=px(22), pady=px(16))

        for cls in (PlacesPage, AppearancePage, MotionPage, ClockPage, GeneralPage, AboutPage):
            try:
                pg = cls(self, self.content)
            except Exception as e:      # noqa: BLE001 - one broken page must not kill the app
                import traceback
                traceback.print_exc()
                pg = _BrokenPage(self, self.content, cls.name, e)
            self.pages[cls.name] = pg
        self.select(page or "places")

    def retheme(self):
        """Rebuild in the theme just chosen, staying on the same page."""
        def rebuild():
            if self.store.get("theme") != self.theme:
                self.build(self.current)
        # after the click that asked for it has finished with its widget
        self.root.after_idle(rebuild)

    # ---- navigation ---------------------------------------------------------------
    def select(self, name):
        if name not in self.pages:
            name = "places"
        if self.current == name:
            return
        if self.current in self.pages:
            old = self.pages[self.current]
            old.hidden()
            old.frame.pack_forget()
            self.nav[self.current].set_selected(False)
        self.current = name
        pg = self.pages[name]
        pg.frame.pack(fill="both", expand=True)
        self.nav[name].set_selected(True)
        self.toasts.label.lift()
        pg.shown()

    def step(self, d, focus_nav=False):
        i = PAGES.index(self.current) if self.current in PAGES else 0
        name = PAGES[(i + d) % len(PAGES)]
        self.select(name)
        if focus_nav:
            self.nav[name].f.focus_set()
        return "break"

    def focus_search(self):
        self.select("places")
        pg = self.pages.get("places")
        if hasattr(pg, "search"):
            pg.search.focus_set()

    def toast(self, msg, seconds=3):
        self.toasts.show(msg, seconds)

    # ---- config sync -------------------------------------------------------------
    def _focus_in(self, _e=None):
        now = time.monotonic()
        if now - self._last_reload > 1.0:     # coming back to the window: external edits?
            self._last_reload = now
            self.store.reload()

    def _changed(self, keys):
        if "theme" in keys and self.store.get("theme") != self.theme:
            self.retheme()
            return
        for pg in self.pages.values():
            pg.refresh()

    def close(self):
        self.store.flush()
        if self.current in self.pages:
            self.pages[self.current].hidden()
        self.worker.stop()
        self.root.destroy()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)            # --help and bad options exit here, no display needed
    if tk is None:
        print("topopaper-settings needs Tkinter (Python's tk module). Install it, e.g. "
              "python3-tk (Debian/Ubuntu) or the python.org installer (Windows, macOS).",
              file=sys.stderr)
        return 1
    from .widgets import make_root
    try:
        root = make_root()
    except tk.TclError as e:
        print(f"topopaper-settings: no graphical display ({e})", file=sys.stderr)
        return 1
    page = helpers.first_page(args.page, args.search, os.path.exists(paths.config_file()))
    win = Window(root, page)
    root.deiconify()
    root.lift()
    if args.search and "places" in win.pages and hasattr(win.pages["places"], "do_search"):
        win.pages["places"].do_search(args.search)
    elif page == "places":
        root.after(100, win.focus_search)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
