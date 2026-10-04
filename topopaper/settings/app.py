"""The settings application and its window."""
import argparse
import os
import shutil
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import jobs, paths, util  # noqa: E402
from . import logic, optional  # noqa: E402
from .store import Store  # noqa: E402

APP_ID = "io.github.aidenwboudr.topopaper"
PAGES = ("places", "appearance", "motion", "clock", "displays", "general", "about")
HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="topopaper-settings",
                                 description="Settings for the topopaper wallpaper")
    ap.add_argument("--page", choices=PAGES + ("welcome",), help="open this page")
    ap.add_argument("--search", metavar="PLACE", help="look up a place on the Places page")
    ap.add_argument("--step", help=argparse.SUPPRESS)       # welcome step (screenshots)
    return ap.parse_args(argv)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Topopaper",
                         default_width=900, default_height=660)
        self.set_size_request(360, 420)
        self.app = app
        self.store = app.store
        self.pages = {}
        self.current = None
        self.split = None
        self.toasts = Adw.ToastOverlay()
        self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.root)
        self.set_content(self.toasts)
        self.connect("notify::is-active", self._on_active)
        self.connect("close-request", self._on_close)

    def toast(self, msg, timeout=3):
        t = Adw.Toast(title=GLib.markup_escape_text(msg), timeout=timeout)
        self.toasts.add_toast(t)

    # ---- welcome / main ----------------------------------------------------------
    def show_welcome(self, step=None):
        from .welcome import Welcome
        old = self.root.get_child_by_name("welcome")
        if old:
            self.root.remove(old)
        self.root.add_named(Welcome(self, self._welcome_done, step), "welcome")
        self.root.set_visible_child_name("welcome")
        self.set_title("Welcome to Topopaper")

    def _welcome_done(self, msgs):
        self.store.reload()
        self.show_main("places")
        for m in msgs:
            self.toast(m, 5)
        w = self.root.get_child_by_name("welcome")
        GLib.timeout_add(600, lambda: (self.root.remove(w), False)[1])

    def show_main(self, page=None):
        if self.split is None:
            self._build_main()
        self.root.set_visible_child_name("main")
        self.set_title("Topopaper")
        self.select(page or self.current or "places")

    def _build_main(self):
        from .general import AboutPage, GeneralPage
        from .places import PlacesPage
        from .simple_pages import AppearancePage, ClockPage, DisplaysPage, MotionPage
        classes = (PlacesPage, AppearancePage, MotionPage, ClockPage, DisplaysPage,
                   GeneralPage, AboutPage)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self._rows = {}
        for cls in classes:
            try:
                p = cls(self)
            except Exception as e:      # noqa: BLE001 - one broken page must not kill the app
                import traceback
                traceback.print_exc()
                p = _BrokenPage(self, cls, e)
            self.pages[p.name] = p
            self.stack.add_named(p.widget, p.name)
            self.store.connect(p.refresh)
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6,
                          margin_end=6)
            box.append(Gtk.Image(icon_name=p.icon))
            box.append(Gtk.Label(label=p.title, xalign=0, hexpand=True))
            row.set_child(box)
            row.page = p.name
            self.sidebar.append(row)
            self._rows[p.name] = row
        self.sidebar.connect("row-activated", lambda _l, r: self._row_activated(r))
        self.sidebar.connect("row-selected", lambda _l, r: r and self._open(r.page))

        sb_view = Adw.ToolbarView()
        sb_view.add_top_bar(Adw.HeaderBar())
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.sidebar)
        sb_view.set_content(sw)
        sidebar_page = Adw.NavigationPage(title="Topopaper", child=sb_view)

        ct_view = Adw.ToolbarView()
        ct_view.add_top_bar(Adw.HeaderBar())
        ct_view.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Places", child=ct_view)

        self.split = Adw.NavigationSplitView(sidebar=sidebar_page, content=self.content_page,
                                             min_sidebar_width=190, max_sidebar_width=250)
        self.root.add_named(self.split, "main")
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _row_activated(self, row):
        self._open(row.page)
        self.split.set_show_content(True)

    def select(self, name):
        if name not in self.pages:
            name = "places"
        row = self._rows[name]
        if self.sidebar.get_selected_row() is not row:
            self.sidebar.select_row(row)
        self._open(name)
        self.split.set_show_content(True)

    def _open(self, name):
        if self.current == name:
            return
        if self.current in self.pages:
            self.pages[self.current].hidden()
        self.current = name
        p = self.pages[name]
        self.stack.set_visible_child_name(name)
        self.content_page.set_title(p.title)
        p.shown()

    # ---- config sync -------------------------------------------------------------
    def _on_active(self, *_):
        if self.is_active():
            self.store.reload()           # external edits; pages refresh via listeners

    def _on_close(self, *_):
        self.store.flush()
        if self.current in self.pages:
            self.pages[self.current].hidden()
        return False


class _BrokenPage:
    def __init__(self, win, cls, err):
        self.name, self.title, self.icon = cls.name, cls.title, cls.icon
        self.widget = Adw.StatusPage(icon_name="dialog-error-symbolic",
                                     title="This page couldn’t load",
                                     description=GLib.markup_escape_text(str(err)))

    def refresh(self):
        pass

    def shown(self):
        pass

    def hidden(self):
        pass


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.store = None
        self.win = None
        self.icon_name = APP_ID
        self.starter_state = "idle"         # idle | running | done | failed | missing
        self.starter_line = ""
        self.starter_log = str(paths.log_dir() / "starter.log")
        self._queued = []
        self._held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.store = Store()
        self._setup_icon()
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, accels, fn in (("quit", ["<Control>q"], self.quit),
                                 ("close", ["<Control>w"], lambda: self.win and self.win.close())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, fn=fn: fn())
            self.add_action(a)
            self.set_accels_for_action(f"app.{name}", accels)

    def _setup_icon(self):
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if not theme.has_icon(APP_ID):
            # running from a checkout: serve the icon from a private search path
            svg = paths.share_dir() / "icons" / "topopaper.svg"
            if svg.is_file():
                d = paths.cache_dir() / "icons"
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(svg, d / f"{APP_ID}.svg")
                    theme.add_search_path(str(d))
                except OSError:
                    pass
            if not theme.has_icon(APP_ID):
                self.icon_name = "mark-location-symbolic"
        Gtk.Window.set_default_icon_name(self.icon_name)

    def do_command_line(self, cmdline):
        try:
            args = parse_args(cmdline.get_arguments()[1:])
        except SystemExit as e:
            return int(e.code or 0)
        if self.win is None:
            self.win = Window(self)
        first_run = not self.store.exists()
        if args.page == "welcome" or (first_run and self.win.split is None):
            self.win.show_welcome(args.step)
        else:
            self.win.show_main(args.page)
            if args.search and "places" in self.win.pages:
                p = self.win.pages["places"]
                p.search.set_text(args.search)
                p._do_search()
            elif args.page == "places" and "places" in self.win.pages:
                self.win.pages["places"].search.grab_focus()
        self.win.present()
        return 0

    # ---- the starter globe ---------------------------------------------------------
    def ensure_globe(self):
        """Download/build the globe in the background if it's missing."""
        if util.pack_exists("earth") or self.starter_state == "running":
            return
        if not optional.has("starter"):
            self.starter_state = "missing"
            return
        cmd, env = util.ctl_cmd("starter")
        os.makedirs(os.path.dirname(self.starter_log), exist_ok=True)
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, errors="replace", start_new_session=True)
        except OSError as e:
            self.starter_state, self.starter_line = "failed", str(e)
            return
        self.starter_state, self.starter_line = "running", "Starting…"
        self._hold()

        def reader():
            with open(self.starter_log, "w", encoding="utf-8") as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    s = logic.last_line(line)
                    if s:
                        GLib.idle_add(self._starter_line, s)
            rc = proc.wait()
            GLib.idle_add(self._starter_exit, rc)
        threading.Thread(target=reader, daemon=True).start()

    def _starter_line(self, s):
        self.starter_line = s
        return False

    def _starter_exit(self, rc):
        ok = rc == 0 and util.pack_exists("earth")
        self.starter_state = "done" if ok else "failed"
        if self.win:
            self.win.toast("The globe is ready" if ok else
                           "Couldn’t get the globe — see the starter log", 4)
        self._run_queue()
        return False

    def queue_build(self, plan):
        """Start a place build once the globe exists (the build routes into it)."""
        if util.pack_exists(plan["slug"]):
            util.fly(plan["slug"], quiet=True)
            return
        self._queued.append(plan)
        if self.starter_state != "running":
            self._run_queue()
        else:
            self._hold()

    def _run_queue(self):
        while self._queued:
            plan = self._queued.pop(0)
            try:
                jobs.start(plan)
            except OSError as e:
                if self.win:
                    self.win.toast(f"Couldn’t start building {plan['display']}: {e}", 5)
        self._release()

    def _hold(self):
        if not self._held:
            self.hold()          # keep running after the window closes
            self._held = True

    def _release(self):
        if self._held and self.starter_state != "running" and not self._queued:
            self.release()
            self._held = False


def run(argv):
    parse_args(argv[1:])                  # --help and bad options exit here, no display needed
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        print("topopaper-settings: no graphical display (is WAYLAND_DISPLAY set?)",
              file=sys.stderr)
        return 1
    Adw.init()
    return App().run(argv)
