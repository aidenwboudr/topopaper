"""Shared GTK plumbing: background work, the page base class, row binders,
thumbnails and the theme preview widget."""
import math
import threading

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk

from .. import config


# ---- background work ----------------------------------------------------------
def run_async(fn, *args, done=None):
    """Run fn(*args) on a worker thread; done(result, error) on the main loop."""
    def work():
        try:
            res, err = fn(*args), None
        except Exception as e:  # noqa: BLE001 - reported to the caller
            res, err = None, e
        if done:
            GLib.idle_add(_call_once, done, res, err)
    threading.Thread(target=work, daemon=True).start()


def _call_once(fn, *a):
    fn(*a)
    return False


def open_path(path, parent=None):
    """Open a file or URL with the user's default handler."""
    uri = path if "://" in str(path) else Gio.File.new_for_path(str(path)).get_uri()
    launcher = Gtk.UriLauncher.new(uri)
    launcher.launch(parent, None, None, None)


def has(widget_name):
    return hasattr(Adw, widget_name)


def spinner():
    if has("Spinner"):                  # libadwaita >= 1.6
        return Adw.Spinner()
    s = Gtk.Spinner()
    s.start()
    return s


def icon_button(icon, tooltip, cb=None, *css):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    for c in css:
        b.add_css_class(c)
    if cb:
        b.connect("clicked", lambda *_: cb())
    b.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return b


def text_button(label, cb=None, *css):
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    for c in css:
        b.add_css_class(c)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


def pill(text, *css):
    lab = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lab.add_css_class("tp-badge")
    for c in css:
        lab.add_css_class(c)
    return lab


def status_icon(status):
    name, css = {"ok": ("object-select-symbolic", "success"),
                 "warn": ("dialog-warning-symbolic", "warning"),
                 "fail": ("dialog-error-symbolic", "error")}.get(
                     status, ("dialog-question-symbolic", "dim-label"))
    img = Gtk.Image(icon_name=name, valign=Gtk.Align.CENTER)
    img.add_css_class(css)
    return img


def doc(key):
    d = config.BY_NAME[key].doc
    return d[0].upper() + d[1:] if d else ""


# ---- pages ------------------------------------------------------------------------
class Page:
    """One sidebar entry: an Adw.PreferencesPage plus refresh-from-config."""
    name = ""
    title = ""
    icon = ""

    def __init__(self, win):
        self.win = win
        self.store = win.store
        self.widget = Adw.PreferencesPage()
        self._refreshers = []
        self._syncing = False
        self.build()
        self.refresh()

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

    def toast(self, msg, timeout=3):
        self.win.toast(msg, timeout)

    def group(self, title="", description=""):
        g = Adw.PreferencesGroup(title=title, description=description)
        self.widget.add(g)
        return g

    # ---- binders: config key <-> row, guarded against refresh loops ----------
    def switch(self, key, title, subtitle=None):
        row = Adw.SwitchRow(title=title, subtitle=doc(key) if subtitle is None else subtitle)
        self._refreshers.append(lambda: row.set_active(self.store.get(key)))

        def changed(*_):
            if not self._syncing:
                self.store.set(key, row.get_active())
                self.after_change(key)
        row.connect("notify::active", changed)
        return row

    def combo(self, key, title, values, labels, subtitle=None):
        row = Adw.ComboRow(title=title, subtitle=doc(key) if subtitle is None else subtitle,
                           model=Gtk.StringList.new(list(labels)))

        def refresh():
            v = self.store.raw(key)
            row.set_selected(values.index(v) if v in values else 0)
        self._refreshers.append(refresh)

        def changed(*_):
            i = row.get_selected()
            if not self._syncing and 0 <= i < len(values):
                self.store.set(key, values[i])
                self.after_change(key)
        row.connect("notify::selected", changed)
        return row

    def spin(self, key, title, lo, hi, step=1, digits=0, subtitle=None):
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 10)
        row = Adw.SpinRow(title=title, subtitle=doc(key) if subtitle is None else subtitle,
                          adjustment=adj, digits=digits, climb_rate=1)
        self._refreshers.append(lambda: row.set_value(self.store.get(key)))

        def changed(*_):
            if not self._syncing:
                v = row.get_value()
                self.store.set(key, int(v) if digits == 0 else round(v, digits), debounce=True)
                self.after_change(key)
        row.connect("notify::value", changed)
        return row

    def entry(self, key, title):
        row = Adw.EntryRow(title=title, show_apply_button=True)
        self._refreshers.append(lambda: row.set_text(self.store.raw(key)))

        def apply(*_):
            self.store.set(key, row.get_text().strip())
            self.after_change(key)
            self.toast(f"Saved {title.lower()}")
        row.connect("apply", apply)
        return row

    def after_change(self, key):
        pass


class SliderRow(Adw.PreferencesRow):
    """Title + value on one line, a full-width slider under it (works narrow)."""

    def __init__(self, title, subtitle, lo, hi, step, marks=(), fmt=str):
        super().__init__(title=title)
        self.fmt = fmt
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=10, margin_bottom=6, margin_start=12, margin_end=12)
        head = Gtk.Box(spacing=6)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=title, xalign=0)
        titles.append(t)
        if subtitle:
            s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
            s.add_css_class("dim-label")
            s.add_css_class("caption")
            titles.append(s)
        head.append(titles)
        self.value_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.value_label.add_css_class("numeric")
        self.value_label.add_css_class("dim-label")
        head.append(self.value_label)
        box.append(head)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        for v, label in marks:
            self.scale.add_mark(v, Gtk.PositionType.BOTTOM, label)
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.scale.connect("value-changed", lambda *_: self._update_label())
        box.append(self.scale)
        self.set_child(box)
        self.set_activatable(False)

    def _update_label(self):
        self.value_label.set_label(self.fmt(self.scale.get_value()))


# ---- thumbnails ---------------------------------------------------------------
_thumbs = {}


def load_thumbnail(path, mtime, width, height, done):
    """Scaled preview texture for `path`, cached; decoded off the main thread."""
    key = (path, mtime, width, height)
    if key in _thumbs:
        done(_thumbs[key])
        return

    def work():
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, width * 2, height * 2, True)
        fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        return pb.get_width(), pb.get_height(), fmt, pb.read_pixel_bytes(), pb.get_rowstride()

    def finish(res, err):
        if err or not res:
            return
        w, h, fmt, data, stride = res
        tex = Gdk.MemoryTexture.new(w, h, fmt, data, stride)
        _thumbs[key] = tex
        done(tex)
    run_async(work, done=finish)


# ---- theme preview ------------------------------------------------------------
def _rgba(c, a=1.0):
    r = Gdk.RGBA()
    r.red, r.green, r.blue, r.alpha = float(c[0]), float(c[1]), float(c[2]), a
    return r


def _mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _point(x, y):
    p = Graphene.Point()
    p.init(x, y)
    return p


def _blob(cx, cy, r, phase, n=56):
    pts = []
    for i in range(n):
        th = 2 * math.pi * i / n
        rr = r * (1 + 0.16 * math.sin(3 * th + phase) + 0.07 * math.cos(5 * th - phase))
        pts.append((cx + rr * math.cos(th) * 1.35, cy + rr * math.sin(th)))
    return pts


class ThemePreview(Gtk.Widget):
    """A tiny map in a theme's colours: background gradient, contour rings
    (line_lo -> line_hi), a lake, and road / ski run / lift accents."""

    def __init__(self, theme, width=148, height=84):
        super().__init__()
        self.t = theme
        self.set_size_request(width, height)
        self.add_css_class("tp-preview")
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def _stroke(self, snap, pts, color, width, closed=False):
        b = Gsk.PathBuilder.new()
        b.move_to(*pts[0])
        for x, y in pts[1:]:
            b.line_to(x, y)
        if closed:
            b.close()
        snap.append_stroke(b.to_path(), Gsk.Stroke.new(width), _rgba(color))

    def _fill(self, snap, pts, color):
        b = Gsk.PathBuilder.new()
        b.move_to(*pts[0])
        for x, y in pts[1:]:
            b.line_to(x, y)
        b.close()
        snap.append_fill(b.to_path(), Gsk.FillRule.WINDING, _rgba(color))

    def do_snapshot(self, snap):
        t = self.t
        w, h = self.get_width(), self.get_height()
        rect = Graphene.Rect()
        rect.init(0, 0, w, h)
        stops = []
        for off, c in ((0.0, t["bg_hi"]), (1.0, t["bg_lo"])):
            s = Gsk.ColorStop()
            s.offset, s.color = off, _rgba(c)
            stops.append(s)
        snap.append_linear_gradient(rect, _point(0, 0), _point(0, h), stops)
        if not hasattr(Gsk, "PathBuilder"):          # GTK < 4.14: plain swatches
            for i, role in enumerate(("line_lo", "line_hi", "road", "ski", "lift")):
                r = Graphene.Rect()
                r.init(10 + i * 22, h - 24, 16, 14)
                snap.append_color(_rgba(t[role]), r)
            return
        # lake
        lake = _blob(w * 0.20, h * 0.78, h * 0.20, 1.3, 40)
        self._fill(snap, lake, t["water"])
        self._stroke(snap, lake, t["shore"], 1.0, True)
        # contour rings around a summit
        cx, cy = w * 0.62, h * 0.46
        n = 7
        for k in range(n, 0, -1):
            col = _mix(t["line_lo"], t["line_hi"], (n - k) / (n - 1))
            self._stroke(snap, _blob(cx, cy, h * 0.062 * k, 0.35 * k), col, 1.2, True)
        # road (a gentle curve across the bottom)
        road = [(x, h * 0.92 - 10 * math.sin(x / w * math.pi) - 6 * math.sin(x / 13.0))
                for x in range(-2, w + 3, 4)]
        self._stroke(snap, road, t["road"], 1.6)
        # ski run down from the summit, lift straight up beside it
        run = [(cx + 6 * math.sin(i / 3.0) - i * 1.4, cy + i * 2.4) for i in range(0, 16)]
        self._stroke(snap, run, t["ski"], 1.6)
        lx0, ly0, lx1, ly1 = cx + 14, cy + 36, cx + 8, cy + 2
        self._stroke(snap, [(lx0, ly0), (lx1, ly1)], t["lift"], 1.4)
        for x, y in ((lx0, ly0), (lx1, ly1)):
            b = Gsk.PathBuilder.new()
            b.add_circle(_point(x, y), 2.2)
            snap.append_fill(b.to_path(), Gsk.FillRule.WINDING, _rgba(t["lift"]))
        # summit mark in the label ink
        b = Gsk.PathBuilder.new()
        b.add_circle(_point(cx, cy), 2.0)
        snap.append_fill(b.to_path(), Gsk.FillRule.WINDING, _rgba(t["ink"]))
