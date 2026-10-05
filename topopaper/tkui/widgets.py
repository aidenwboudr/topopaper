"""Tk building blocks: the root window, cards and rows, a switch, segmented
choices, a scrolling page, toasts, a confirm dialog, a log viewer, map
thumbnails and the little contour drawings (theme cards, the emblem, the
window icon)."""
import math
import os
import sys
import tkinter as tk
from tkinter import ttk

from . import look
from .work import open_path, read_tail

_scale = [1.0]


def px(n):
    """n design pixels (at 96 dpi) in screen pixels."""
    return int(round(n * _scale[0]))


def make_root(classname="topopaper"):
    """A hidden Tk root, DPI-aware on Windows, with px() calibrated."""
    if sys.platform.startswith("win"):
        try:                    # crisp text on scaled displays instead of a blurry bitmap
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:       # noqa: BLE001 - older Windows: stay blurry, still works
            pass
    root = tk.Tk(className=classname)
    root.withdraw()
    if sys.platform != "darwin":         # macOS Tk already works in logical points
        _scale[0] = max(1.0, min(3.0, root.winfo_fpixels("1i") / 96.0))
    return root


def center(win, parent=None, width=None, height=None):
    """Place `win` in the middle of `parent` (or of the screen)."""
    win.update_idletasks()
    w = width or win.winfo_reqwidth()
    h = height or win.winfo_reqheight()
    if parent is not None and parent.winfo_ismapped():
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")


def autowrap(label, pad=0, minimum=120):
    """Re-wrap a label to whatever width the geometry manager gives it."""
    label.configure(wraplength=px(320))

    def fit(e):
        w = max(px(minimum), e.width - pad)
        if str(w) != str(label.cget("wraplength")):
            label.configure(wraplength=w)
    label.bind("<Configure>", fit, add="+")
    return label


# ---- cards and rows ------------------------------------------------------------------
def placeholder(entry, var, text, pal, bg=None):
    """Grey hint text inside an empty entry (Tk has none of its own)."""
    lab = tk.Label(entry, text=text, background=bg or pal["field"], foreground=pal["faint"],
                   cursor="xterm", borderwidth=0, padx=0, pady=0)

    def update(*_):
        if var.get():
            lab.place_forget()
        else:
            lab.place(x=px(12), rely=0.5, anchor="w")
    lab.bind("<Button-1>", lambda e: entry.focus_set())
    var.trace_add("write", update)
    update()
    return lab


class Card(tk.Frame):
    """A bordered surface holding rows, with hairlines between the visible ones."""

    def __init__(self, master, pal):
        super().__init__(master, background=pal["card"], highlightthickness=1,
                         highlightbackground=pal["border"], highlightcolor=pal["border"])
        self.pal = pal
        self.rows = []
        self.hidden = set()
        self._lines = []

    def add(self, row):
        self.rows.append(row)
        self.relayout()
        return row

    def remove(self, row):
        if row in self.rows:
            self.rows.remove(row)
            self.hidden.discard(row)
            row.destroy()
            self.relayout()

    def show(self, row, visible=True):
        was = row not in self.hidden
        if visible:
            self.hidden.discard(row)
        else:
            self.hidden.add(row)
        if was != visible:
            self.relayout()

    def visible_rows(self):
        return [r for r in self.rows if r not in self.hidden]

    def relayout(self):
        for w in self.rows + self._lines:
            w.pack_forget()
        for ln in self._lines:
            ln.destroy()
        self._lines = []
        for i, r in enumerate(self.visible_rows()):
            if i:
                ln = tk.Frame(self, height=1, background=self.pal["border"])
                ln.pack(fill="x")
                self._lines.append(ln)
            r.pack(fill="x")


class Row(ttk.Frame):
    """title + subtitle on the left, controls (self.suffix) on the right."""

    def __init__(self, card, title="", subtitle="", prefix=None):
        super().__init__(card, style="Card.TFrame", padding=(px(14), px(10)))
        self.columnconfigure(1, weight=1)
        self.prefix = ttk.Frame(self, style="Card.TFrame")
        self.prefix.grid(row=0, column=0, sticky="w")
        text = ttk.Frame(self, style="Card.TFrame")
        text.grid(row=0, column=1, sticky="ew")
        text.columnconfigure(0, weight=1)
        self.title = ttk.Label(text, text=title, style="Card.TLabel")
        self.title.grid(row=0, column=0, sticky="ew")
        self.sub = autowrap(ttk.Label(text, text=subtitle, style="Card.Dim.TLabel",
                                      justify="left"))
        self.sub.grid(row=1, column=0, sticky="ew", pady=(px(1), 0))
        if not subtitle:
            self.sub.grid_remove()
        self.suffix = ttk.Frame(self, style="Card.TFrame")
        self.suffix.grid(row=0, column=2, sticky="e", padx=(px(20), 0))

    def set_title(self, text):
        self.title.configure(text=text)

    def set_subtitle(self, text, style="Card.Dim.TLabel"):
        self.sub.configure(text=text or "", style=style)
        if text:
            self.sub.grid()
        else:
            self.sub.grid_remove()

    def add_prefix(self, w):
        w.pack(in_=self.prefix, side="left", padx=(0, px(12)))
        return w

    def add_suffix(self, w, gap=6):
        w.pack(in_=self.suffix, side="left", padx=(px(gap), 0))
        return w


class Toggle(tk.Canvas):
    """An on/off switch. command(value) runs when the user flips it."""

    def __init__(self, master, pal, command=None, bg=None):
        self.pal = pal
        self.bgc = bg or pal["card"]
        self.w, self.h = px(40), px(22)
        super().__init__(master, width=self.w, height=self.h, background=self.bgc,
                         highlightthickness=px(2), highlightbackground=self.bgc,
                         highlightcolor=pal["accent"], takefocus=1, cursor="hand2",
                         borderwidth=0)
        self.value = False
        self.enabled = True
        self.command = command
        for seq in ("<Button-1>", "<space>", "<Return>"):
            self.bind(seq, self._flip)
        self._draw()

    def set(self, value):
        self.value = bool(value)
        self._draw()

    def get(self):
        return self.value

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.configure(takefocus=1 if on else 0, cursor="hand2" if on else "")
        self._draw()

    def _flip(self, _e=None):
        if not self.enabled:
            return "break"
        self.focus_set()
        self.set(not self.value)
        if self.command:
            self.command(self.value)
        return "break"

    def _draw(self):
        p = self.pal
        self.delete("all")
        w, h, r = self.w, self.h, self.h / 2.0
        on = self.value
        track = p["accent"] if on else p["border"]
        knob = p["on_accent"] if on else p["text"]
        if not self.enabled:
            track = look.to_hex(look.mix(look.from_hex(track), look.from_hex(self.bgc), 0.55))
            knob = look.to_hex(look.mix(look.from_hex(knob), look.from_hex(self.bgc), 0.45))
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(r, 0, w - r, h, fill=track, outline=track)
        m = px(3)
        x0 = w - h + m if on else m
        self.create_oval(x0, m, x0 + h - 2 * m, h - m, fill=knob, outline=knob)


class Segmented(ttk.Frame):
    """A row of mutually exclusive pills; command(value) on user choice."""

    def __init__(self, master, values, labels, command=None, style="Card.TFrame"):
        super().__init__(master, style=style)
        self.var = tk.StringVar(master=self, value=values[0] if values else "")
        self.values = list(values)
        self.command = command
        self.buttons = []
        for v, label in zip(values, labels):
            b = ttk.Radiobutton(self, text=label, value=v, variable=self.var,
                                style="Seg.Toolbutton", command=self._changed)
            b.pack(side="left")
            self.buttons.append(b)

    def set(self, value):
        self.var.set(value if value in self.values else self.values[0])

    def get(self):
        return self.var.get()

    def set_enabled(self, on):
        for b in self.buttons:
            b.state(["!disabled"] if on else ["disabled"])

    def _changed(self):
        if self.command:
            self.command(self.var.get())


class ScrollPage(ttk.Frame):
    """A vertically scrolling page whose content column is centred and at
    most MAX_W wide. Put children in self.body."""
    MAX_W = 760

    def __init__(self, master, pal):
        super().__init__(master)
        self.canvas = tk.Canvas(self, background=pal["bg"], highlightthickness=0,
                                borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Slim.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._set_bar)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=(px(28), px(22), px(28), px(36)))
        # always an explicit width: left at its natural width, the body and
        # its wrapping labels would keep resizing each other
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw",
                                              width=px(self.MAX_W) // 2)
        self.canvas.bind("<Configure>", self._layout)
        self.body.bind("<Configure>", self._content_changed)
        self.canvas._scroll_target = self.canvas

    def _set_bar(self, lo, hi):
        # the bar floats over the right margin: showing it never narrows the
        # canvas, which would re-wrap the text and could flip it back again
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.bar.place_forget()
        elif not self.bar.winfo_ismapped():
            self.bar.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self.bar.set(lo, hi)

    def _layout(self, e=None):
        cw = self.canvas.winfo_width()
        if cw <= 1:
            return                      # not laid out yet
        w = min(cw, px(self.MAX_W))
        self.canvas.itemconfigure(self._win, width=w)
        self.canvas.coords(self._win, max(0, (cw - w) // 2), 0)
        self._content_changed()

    def _content_changed(self, _e=None):
        h = max(self.body.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), h))

    def see(self, widget):
        """Scroll so `widget` (somewhere inside the body) is in view."""
        self.update_idletasks()
        total = max(1, self.body.winfo_height())
        y = widget.winfo_rooty() - self.body.winfo_rooty()
        top, bottom = self.canvas.yview()
        h = widget.winfo_height()
        if y < top * total or y + h > bottom * total:
            self.canvas.yview_moveto(max(0.0, (y - px(40)) / float(total)))


def install_wheel(root):
    """One wheel handler for every ScrollPage: scroll whichever is under the pointer."""
    def target(e):
        w = root.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            t = getattr(w, "_scroll_target", None)
            if t is not None:
                return t
            w = getattr(w, "master", None)
        return None

    def wheel(e, step=None):
        t = target(e)
        if t is None:
            return
        if step is None:
            d = e.delta
            if sys.platform == "darwin":
                step = -d
            else:
                step = -int(d / 120) or (-1 if d > 0 else 1)
        t.yview_scroll(step, "units")
    root.bind_all("<MouseWheel>", wheel, add="+")
    root.bind_all("<Button-4>", lambda e: wheel(e, -1), add="+")
    root.bind_all("<Button-5>", lambda e: wheel(e, 1), add="+")


class Toast:
    """A short message floating at the bottom of `master`."""

    def __init__(self, master, pal, fnt):
        self.master = master
        self.label = tk.Label(master, background=pal["hover"], foreground=pal["text"],
                              font=fnt["body"], padx=px(16), pady=px(8),
                              highlightthickness=1, highlightbackground=pal["border"],
                              wraplength=px(520), justify="left")
        self._after = None

    def show(self, msg, seconds=3):
        self.label.configure(text=msg)
        self.label.place(relx=0.5, rely=1.0, y=-px(22), anchor="s")
        self.label.lift()
        if self._after:
            self.master.after_cancel(self._after)
        self._after = self.master.after(int(seconds * 1000), self.hide)

    def hide(self):
        self._after = None
        if self.label.winfo_exists():        # gone when the window was rebuilt meanwhile
            self.label.place_forget()


# ---- dialogs ---------------------------------------------------------------------------
def _dialog(parent, pal, title):
    d = tk.Toplevel(parent, background=pal["card"])
    d.withdraw()
    d.title(title)
    d.transient(parent.winfo_toplevel())
    d.resizable(False, False)
    return d


def _modal(d, parent, focus):
    center(d, parent.winfo_toplevel())
    d.deiconify()
    try:
        d.wait_visibility()
        d.grab_set()
    except tk.TclError:
        pass
    focus.focus_set()
    d.wait_window()


def ask(parent, pal, fnt, heading, body, ok="OK", danger=False, check=None, cancel="Cancel"):
    """A modal question. Returns (accepted, check_value)."""
    d = _dialog(parent, pal, heading)
    box = ttk.Frame(d, style="Card.TFrame", padding=(px(24), px(20), px(24), px(18)))
    box.pack(fill="both", expand=True)
    ttk.Label(box, text=heading, style="Card.TLabel", font=fnt["big"]).pack(anchor="w")
    ttk.Label(box, text=body, style="Card.Dim.TLabel", wraplength=px(380),
              justify="left").pack(anchor="w", pady=(px(8), 0))
    var = tk.BooleanVar(master=d, value=True)
    if check:
        ttk.Checkbutton(box, text=check, variable=var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(px(14), 0))
    result = [False]

    def close(ok_):
        result[0] = ok_
        d.destroy()
    btns = ttk.Frame(box, style="Card.TFrame")
    btns.pack(fill="x", pady=(px(20), 0))
    yes = ttk.Button(btns, text=ok, style="Danger.TButton" if danger else "Accent.TButton",
                     command=lambda: close(True))
    yes.pack(side="right")
    no = ttk.Button(btns, text=cancel, command=lambda: close(False))
    no.pack(side="right", padx=(0, px(8)))
    d.bind("<Escape>", lambda e: close(False))
    d.bind("<Return>", lambda e: e.widget.invoke() if isinstance(e.widget, ttk.Button) else None)
    d.protocol("WM_DELETE_WINDOW", lambda: close(False))
    _modal(d, parent, no if danger else yes)
    return result[0], bool(var.get())


class LogViewer:
    """A window following the tail of a log file (one per path)."""
    _open = {}

    @classmethod
    def show(cls, parent, pal, fnt, path, title):
        v = cls._open.get(str(path))
        if v is not None and v.win.winfo_exists():
            v.win.deiconify()
            v.win.lift()
            return v
        v = cls(parent, pal, fnt, path, title)
        cls._open[str(path)] = v
        return v

    def __init__(self, parent, pal, fnt, path, title):
        self.path = str(path)
        self.win = tk.Toplevel(parent, background=pal["bg"])
        self.win.title(title)
        self.win.geometry(f"{px(720)}x{px(440)}")
        top = ttk.Frame(self.win, padding=(px(16), px(12)))
        top.pack(fill="x")
        ttk.Label(top, text=title, style="Strong.TLabel").pack(side="left")
        ttk.Button(top, text="Open folder",
                   command=lambda: open_path(os.path.dirname(self.path))).pack(side="right")
        ttk.Label(self.win, text=self.path, style="Dim.TLabel",
                  padding=(px(16), 0, px(16), px(8))).pack(fill="x")
        frame = tk.Frame(self.win, background=pal["field"], highlightthickness=1,
                         highlightbackground=pal["border"])
        frame.pack(fill="both", expand=True, padx=px(16), pady=(0, px(16)))
        self.text = tk.Text(frame, background=pal["field"], foreground=pal["text"],
                            insertbackground=pal["text"], relief="flat", borderwidth=0,
                            highlightthickness=0, wrap="word", font="TkFixedFont",
                            padx=px(10), pady=px(8), selectbackground=pal["accent"],
                            selectforeground=pal["on_accent"])
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview,
                            style="Slim.Vertical.TScrollbar")
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._sig = None
        self._tick()

    def _tick(self):
        if not self.win.winfo_exists():
            return
        try:
            st = os.stat(self.path)
            sig = (st.st_size, st.st_mtime)
        except OSError:
            sig = None
        if sig != self._sig:
            self._sig = sig
            at_end = self.text.yview()[1] >= 0.999
            text = read_tail(self.path) if sig else "(no log yet)"
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.text.configure(state="disabled")
            if at_end:
                self.text.see("end")
        self.win.after(1000, self._tick)


# ---- thumbnails ------------------------------------------------------------------------
_thumbs = {}


def thumb_data(path, w, h, fmt="PNG"):
    """Off the Tk thread: a w x h centre crop of an image, box-filtered, as
    base64 image data for tk.PhotoImage(data=...). Uses Pillow (installed
    for building maps) and returns None without it."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    import base64
    import io
    box = getattr(getattr(Image, "Resampling", Image), "BOX")
    with Image.open(path) as im:
        small = ImageOps.fit(im.convert("RGB"), (w, h), method=box)
    buf = io.BytesIO()
    small.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _tk_thumbnail(master, path, w, h):
    """Tk alone: point-sampled (grainy) and PNG only from Tk 8.6 on."""
    try:
        big = tk.PhotoImage(master=master, file=path)
        bw, bh = big.width(), big.height()
        s = max(1, min(bw // w, bh // h))
        x0, y0 = max(0, (bw - w * s) // 2), max(0, (bh - h * s) // 2)
        img = tk.PhotoImage(master=master, width=w, height=h)
        img.tk.call(img, "copy", big, "-from", x0, y0, min(bw, x0 + w * s),
                    min(bh, y0 + h * s), "-subsample", s, s)
        return img
    except (tk.TclError, OSError):
        return None


def thumbnail(worker, master, path, mtime, w, h, done):
    """done(PhotoImage or None) with a w x h crop of a pack preview; the
    decoding runs on `worker` when Pillow is there."""
    key = (path, mtime, w, h)
    if key in _thumbs:
        done(_thumbs[key])
        return
    fmt = "PNG" if master.tk.call("info", "patchlevel") >= "8.6" else "GIF"

    def got(data, err):
        img = None
        if data and err is None:
            try:
                img = tk.PhotoImage(master=master, data=data)
            except tk.TclError:
                img = None
        if img is None:
            img = _tk_thumbnail(master, path, w, h)
        _thumbs[key] = img
        done(img)
    worker.run(thumb_data, path, w, h, fmt, done=got)


# ---- contour drawings ------------------------------------------------------------------
def _blob(cx, cy, r, phase, n=40, stretch=1.35):
    pts = []
    for i in range(n):
        th = 2 * math.pi * i / n
        rr = r * (1 + 0.16 * math.sin(3 * th + phase) + 0.07 * math.cos(5 * th - phase))
        pts += [cx + rr * math.cos(th) * stretch, cy + rr * math.sin(th)]
    return pts


def _hexmix(a, b, t):
    return look.to_hex(look.mix(a, b, t))


def draw_map(c, t, w, h):
    """A tiny map in theme `t`'s colours (like the GTK theme preview): gradient,
    a lake, contour rings round a summit, a road, a ski run and a lift."""
    c.delete("all")
    bands = 16
    for i in range(bands):
        c.create_rectangle(0, h * i / bands, w, h * (i + 1) / bands + 1, width=0,
                           fill=_hexmix(t["bg_hi"], t["bg_lo"], i / (bands - 1)))
    lake = _blob(w * 0.20, h * 0.80, h * 0.20, 1.3)
    c.create_polygon(*lake, smooth=True, fill=look.to_hex(t["water"]),
                     outline=look.to_hex(t["shore"]))
    cx, cy, n = w * 0.62, h * 0.46, 7
    for k in range(n, 0, -1):
        col = _hexmix(t["line_lo"], t["line_hi"], (n - k) / (n - 1.0))
        c.create_polygon(*_blob(cx, cy, h * 0.062 * k, 0.35 * k), smooth=True, fill="",
                         outline=col, width=max(1, px(1)))
    road = []
    for x in range(-2, int(w) + 3, 4):
        road += [x, h * 0.92 - 10 * math.sin(x / float(w) * math.pi) - 6 * math.sin(x / 13.0)]
    c.create_line(*road, smooth=True, fill=look.to_hex(t["road"]), width=max(1, px(1.5)))
    run = []
    for i in range(16):
        run += [cx + 6 * math.sin(i / 3.0) - i * 1.4, cy + i * h / 35.0]
    c.create_line(*run, smooth=True, fill=look.to_hex(t["ski"]), width=max(1, px(1.5)))
    lx0, ly0, lx1, ly1 = cx + 14, cy + h * 0.42, cx + 8, cy + 2
    lift = look.to_hex(t["lift"])
    c.create_line(lx0, ly0, lx1, ly1, fill=lift, width=max(1, px(1.3)))
    for x, y in ((lx0, ly0), (lx1, ly1)):
        c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=lift, outline=lift)
    ink = look.to_hex(t["ink"])
    c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=ink, outline=ink)


def draw_emblem(c, t, x, y, size):
    """Contour rings brightening up to a warm summit: the app's mark."""
    n = 5
    cx, cy = x + size * 0.5, y + size * 0.52
    for k in range(n, 0, -1):
        col = _hexmix(t["line_hi"], t["ink"], (n - k) / (n - 1.0) * 0.8)
        c.create_polygon(*_blob(cx, cy, size * 0.095 * k, 0.5 * k, 36, 1.12), smooth=True,
                         fill="", outline=col, width=max(1, px(1.4)))
    s = max(2, px(2.5))
    warm = look.to_hex(t["road"])
    c.create_oval(cx - s, cy - s, cx + s, cy + s, fill=warm, outline=warm)


def icon_image(master, t, size=64):
    """The window icon, rasterised by hand (Tk 8.5/8.6 can't read SVG)."""
    img = tk.PhotoImage(master=master, width=size, height=size)
    bg_hi, bg_lo = t["bg_hi"], t["bg_lo"]
    rows = []
    cx, cy = size * 0.52, size * 0.50
    rad, levels = size * 0.62, 6
    corner = size * 0.2
    summit = t["road"]
    for yy in range(size):
        row = []
        for xx in range(size):
            # rounded tile: outside the corners stays transparent-ish (bg_lo)
            dx = max(corner - xx, 0, xx - (size - 1 - corner))
            dy = max(corner - yy, 0, yy - (size - 1 - corner))
            if dx * dx + dy * dy > corner * corner:
                row.append(None)
                continue
            col = look.mix(bg_hi, bg_lo, yy / float(size))
            ddx, ddy = (xx - cx) / 1.15, yy - cy
            r = math.hypot(ddx, ddy)
            th = math.atan2(ddy, ddx)
            r /= 1 + 0.14 * math.sin(3 * th + 0.6) + 0.06 * math.cos(5 * th)
            lv = r / rad * levels
            if 0.6 < lv < levels and abs(lv - round(lv)) < 0.09 * levels / 4:
                col = look.mix(t["line_hi"], t["ink"], max(0.0, 1 - lv / levels))
            if r < size * 0.055:
                col = summit
            row.append(col)
        rows.append(row)
    for yy, row in enumerate(rows):
        for xx, col in enumerate(row):
            if col is not None:
                img.put(look.to_hex(col), (xx, yy))
    return img
