"""Colours, fonts and ttk styles for the Tk windows.

The palette comes from the wallpaper's own theme (topopaper/themes.py), so
the settings sit in the same dark map colours they configure: the
background gradient for surfaces, contour lines for borders, the label ink
for text and the ski-run colour as the accent. Text colours are nudged
until they meet WCAG contrast against the surface they sit on.

palette() and the colour maths are pure (tests use them without a display);
fonts() and apply() need a Tk root.
"""
import math
import sys

from .. import config, themes

FALLBACK_THEME = config.BY_NAME["theme"].default


# ---- colour maths ---------------------------------------------------------------
def mix(a, b, t):
    """a -> b by t (0..1), per channel."""
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def to_hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v * 255)))) for v in c[:3])


def from_hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def luminance(c):
    """WCAG relative luminance of an sRGB colour (floats 0..1)."""
    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def readable(fg, bg, ratio=4.5):
    """fg, moved toward white (on dark bg) or black until it reaches `ratio`."""
    target = (1.0, 1.0, 1.0) if luminance(bg) < 0.25 else (0.0, 0.0, 0.0)
    c = tuple(fg)
    for i in range(21):
        if contrast(c, bg) >= ratio:
            return c
        c = mix(fg, target, i / 20.0)
    return target


# ---- palette --------------------------------------------------------------------
def theme_colours(name=None):
    """The raw role -> (r, g, b) dict of a wallpaper theme (unknown -> default)."""
    return themes.THEMES.get(name or "") or themes.THEMES[FALLBACK_THEME]


def palette(name=None):
    """Window colours (hex strings) derived from the wallpaper theme `name`."""
    t = theme_colours(name)
    side = t["bg_lo"]
    bg = t["bg_hi"]
    card = mix(t["bg_hi"], t["line_lo"], 0.5)
    field = mix(t["bg_lo"], t["bg_hi"], 0.4)
    border = mix(t["line_lo"], t["line_hi"], 0.35)
    hover = mix(card, t["line_hi"], 0.55)
    text = readable(t["ink"], card, 7.0)
    dim = readable(mix(t["ink"], card, 0.3), card, 4.5)
    accent = readable(t["ski"], card, 4.5)
    on_accent = t["bg_lo"] if contrast(t["bg_lo"], accent) >= 4.5 else readable(t["bg_lo"], accent)
    danger = readable(t["lift"], card, 4.5)
    pal = dict(
        side=side, bg=bg, card=card, field=field, border=border, hover=hover,
        side_sel=mix(side, card, 0.75), side_hover=mix(side, card, 0.4),
        text=text, dim=dim, faint=mix(t["ink"], card, 0.55),
        accent=accent, accent_hi=mix(accent, (1, 1, 1), 0.18), on_accent=on_accent,
        accent_soft=mix(card, accent, 0.18),
        danger=danger, on_danger=t["bg_lo"] if contrast(t["bg_lo"], danger) >= 4.5
        else readable(t["bg_lo"], danger),
        ok=readable(t["aurora_a"], card, 4.5), warn=readable(t["warm"], card, 4.5),
        summit=t["road"], line_lo=t["line_lo"], line_hi=t["line_hi"], ink=t["ink"],
        water=t["water"], shore=t["shore"],
    )
    return {k: to_hex(v) for k, v in pal.items()}


# ---- fonts ----------------------------------------------------------------------
def ui_family(root):
    """The platform's UI font: Segoe UI on Windows, the system font elsewhere."""
    from tkinter import font as tkfont
    if sys.platform.startswith("win"):
        return "Segoe UI"
    return tkfont.nametofont("TkDefaultFont", root=root).actual("family")


def fonts(root):
    """Named fonts for the windows: body, small, strong, heading, title, display, big."""
    from tkinter import font as tkfont
    base = tkfont.nametofont("TkDefaultFont", root=root)
    size = base.actual("size")
    if size < 0:                                  # negative = pixels
        size = round(-size * 72.0 / max(1.0, root.winfo_fpixels("1i")))
    if sys.platform.startswith("win"):
        size = 10
    elif sys.platform == "darwin":
        size = max(size, 13)
    else:
        size = max(size, 10)
    fam = ui_family(root)

    def f(scale=1.0, weight="normal"):
        return tkfont.Font(root=root, family=fam, size=max(8, int(round(size * scale))),
                           weight=weight)
    return dict(body=f(), small=f(0.9), strong=f(1.0, "bold"), heading=f(0.85, "bold"),
                title=f(1.55, "bold"), display=f(2.2, "bold"), big=f(1.3))


# ---- ttk styles -----------------------------------------------------------------
def apply(root, pal, fnt):
    """Style ttk (on top of 'clam') and the classic Tk option database."""
    from tkinter import ttk
    st = ttk.Style(root)
    st.theme_use("clam")
    p = pal
    root.configure(background=p["bg"])
    # only classic widgets here: a generic *font or *background would also
    # land on ttk widgets' own options and override their styles
    for opt, val in (("*TCombobox*Listbox.background", p["field"]),
                     ("*TCombobox*Listbox.foreground", p["text"]),
                     ("*TCombobox*Listbox.selectBackground", p["accent"]),
                     ("*TCombobox*Listbox.selectForeground", p["on_accent"]),
                     ("*TCombobox*Listbox.font", fnt["body"]),
                     ("*Menu.background", p["card"]), ("*Menu.foreground", p["text"]),
                     ("*Menu.activeBackground", p["accent"]),
                     ("*Menu.activeForeground", p["on_accent"]),
                     ("*Menu.font", fnt["body"]), ("*Menu.relief", "flat"),
                     ("*Menu.borderWidth", 1)):
        root.option_add(opt, val)

    st.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["field"],
                 bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                 troughcolor=p["field"], selectbackground=p["accent"],
                 selectforeground=p["on_accent"], insertcolor=p["text"],
                 focuscolor=p["accent"], font=fnt["body"])
    st.map(".", foreground=[("disabled", p["faint"])])

    # frames and labels: one family per surface they sit on
    for prefix, bg in (("", p["bg"]), ("Card.", p["card"]), ("Side.", p["side"])):
        st.configure(f"{prefix}TFrame", background=bg)
        st.configure(f"{prefix}TLabel", background=bg, foreground=p["text"])
        st.configure(f"{prefix}Dim.TLabel", background=bg, foreground=p["dim"],
                     font=fnt["small"])
        st.configure(f"{prefix}Strong.TLabel", background=bg, foreground=p["text"],
                     font=fnt["strong"])
        st.configure(f"{prefix}Danger.TLabel", background=bg, foreground=p["danger"],
                     font=fnt["small"])
        st.configure(f"{prefix}Ok.TLabel", background=bg, foreground=p["ok"],
                     font=fnt["small"])
    st.configure("Heading.TLabel", background=p["bg"], foreground=p["dim"], font=fnt["heading"])
    st.configure("Title.TLabel", background=p["bg"], foreground=p["text"], font=fnt["title"])
    st.configure("Display.TLabel", background=p["bg"], foreground=p["text"],
                 font=fnt["display"])
    st.configure("Lead.TLabel", background=p["bg"], foreground=p["dim"], font=fnt["body"])
    st.configure("Side.Title.TLabel", background=p["side"], foreground=p["text"],
                 font=fnt["big"])
    st.configure("Badge.TLabel", background=p["accent_soft"], foreground=p["accent"],
                 font=fnt["heading"], padding=(8, 1))
    st.configure("Quiet.Badge.TLabel", background=p["hover"], foreground=p["text"])
    st.configure("Card.TSeparator", background=p["border"])
    st.configure("Banner.TFrame", background=p["accent_soft"])
    st.configure("Banner.TLabel", background=p["accent_soft"], foreground=p["text"])
    st.configure("Banner.Strong.TLabel", background=p["accent_soft"], foreground=p["text"],
                 font=fnt["strong"])

    # buttons
    def button(name, bg, fg, hover, border=None):
        border = border or bg
        st.configure(name, background=bg, foreground=fg, bordercolor=border,
                     lightcolor=bg, darkcolor=bg, padding=(12, 4), relief="flat",
                     focusthickness=1, focuscolor=fg, anchor="center")
        st.map(name, background=[("disabled", p["card"]), ("pressed", hover), ("active", hover)],
               lightcolor=[("pressed", hover), ("active", hover)],
               darkcolor=[("pressed", hover), ("active", hover)],
               bordercolor=[("focus", p["accent"])],
               foreground=[("disabled", p["faint"])])
    button("TButton", p["hover"], p["text"], to_hex(mix(from_hex(p["hover"]),
                                                          from_hex(p["text"]), 0.12)))
    button("Accent.TButton", p["accent"], p["on_accent"], p["accent_hi"])
    button("Danger.TButton", p["danger"], p["on_danger"],
           to_hex(mix(from_hex(p["danger"]), (1, 1, 1), 0.15)))
    button("Ghost.TButton", p["card"], p["accent"], p["hover"])
    st.configure("Ghost.TButton", padding=(8, 4))
    button("Nav.TButton", p["side"], p["text"], p["side_hover"])

    # segmented choices: radio buttons drawn as one row of pills
    st.configure("Seg.Toolbutton", background=p["field"], foreground=p["dim"],
                 bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                 padding=(12, 4), relief="flat", anchor="center", focuscolor=p["accent"])
    st.map("Seg.Toolbutton",
           background=[("selected", p["accent"]), ("active", p["hover"])],
           foreground=[("selected", p["on_accent"]), ("active", p["text"]),
                       ("disabled", p["faint"])],
           lightcolor=[("selected", p["accent"]), ("active", p["hover"])],
           darkcolor=[("selected", p["accent"]), ("active", p["hover"])],
           bordercolor=[("focus", p["accent"]), ("selected", p["accent"])])

    # text input
    for name in ("TEntry", "TCombobox"):
        st.configure(name, fieldbackground=p["field"], foreground=p["text"],
                     bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                     insertcolor=p["text"], padding=(8, 5), arrowcolor=p["dim"],
                     background=p["field"], selectbackground=p["accent"],
                     selectforeground=p["on_accent"])
        st.map(name, bordercolor=[("focus", p["accent"])],
               lightcolor=[("focus", p["field"])], darkcolor=[("focus", p["field"])],
               fieldbackground=[("readonly", p["field"]), ("disabled", p["card"])],
               foreground=[("disabled", p["faint"])],
               selectbackground=[("readonly", p["field"])],
               selectforeground=[("readonly", p["text"])],
               background=[("active", p["hover"]), ("pressed", p["hover"])],
               arrowcolor=[("active", p["text"])])
    st.configure("Big.TEntry", padding=(12, 9))

    # check boxes (dialogs)
    st.configure("Card.TCheckbutton", background=p["card"], foreground=p["text"],
                 indicatorbackground=p["field"], indicatorforeground=p["on_accent"],
                 upperbordercolor=p["border"], lowerbordercolor=p["border"],
                 focuscolor=p["accent"])
    st.map("Card.TCheckbutton",
           indicatorbackground=[("selected", p["accent"])],
           background=[("active", p["card"])])

    # sliders, progress and scroll bars
    _slider(root, st, p)
    st.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["field"],
                 bordercolor=p["field"], lightcolor=p["accent"], darkcolor=p["accent"])
    st.layout("Slim.Vertical.TScrollbar",
              [("Vertical.Scrollbar.trough",
                {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                               {"expand": "1", "sticky": "nswe"})]})])
    st.configure("Slim.Vertical.TScrollbar", troughcolor=p["bg"], background=p["border"],
                 bordercolor=p["bg"], lightcolor=p["border"], darkcolor=p["border"],
                 gripcount=0, arrowsize=10)
    st.map("Slim.Vertical.TScrollbar", background=[("active", p["line_hi"])],
           lightcolor=[("active", p["line_hi"])], darkcolor=[("active", p["line_hi"])])
    return st


_made = [0]


def _disc(root, size, r, fill, bg, ring=None):
    """A size x size image of an anti-aliased disc (edges blended into bg,
    the rest transparent), optionally inside a 2 px ring of colour `ring`."""
    import tkinter as tk
    img = tk.PhotoImage(master=root, width=size, height=size)
    c = (size - 1) / 2.0
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c)
            cov = max(0.0, min(1.0, r + 0.5 - d))
            col = mix(bg, fill, cov) if cov > 0 else None
            if ring is not None and col is None:
                rc = max(0.0, min(1.0, r + 2.5 - d))
                col = mix(bg, ring, rc) if rc > 0 else None
            if col is not None:
                img.put(to_hex(col), (x, y))
    return img


def _slider(root, st, p):
    """Sliders: a slim groove and a round thumb, drawn as images (clam's own
    trough shares its outline colour with the thumb)."""
    import tkinter as tk
    _made[0] += 1
    n = _made[0]
    card, accent = from_hex(p["card"]), from_hex(p["accent"])
    size, r = 18, 6.5
    knob = _disc(root, size, r, accent, card)
    hot = _disc(root, size, r, from_hex(p["accent_hi"]), card)
    focus = _disc(root, size, r, accent, card, ring=mix(card, accent, 0.45))
    groove = tk.PhotoImage(master=root, width=8, height=size)
    for y in range(size // 2 - 2, size // 2 + 2):
        groove.put(p["border"], to=(0, y, 8, y + 1))
    root._tp_slider_images = getattr(root, "_tp_slider_images", []) + [knob, hot, focus, groove]
    trough, thumb = f"Groove{n}.Horizontal.Scale.trough", f"Knob{n}.Horizontal.Scale.slider"
    st.element_create(trough, "image", groove, border=(3, 0), sticky="ew")
    st.element_create(thumb, "image", knob, ("pressed", hot), ("active", hot), ("focus", focus))
    st.layout("Horizontal.TScale", [(trough, {"sticky": "ew", "children": [
        (thumb, {"side": "left", "sticky": ""})]})])
    st.configure("Horizontal.TScale", background=p["card"])
    # clam's generic map would light up the transparent parts when hovered
    st.map("Horizontal.TScale", background=[("active", p["card"]), ("disabled", p["card"])])
