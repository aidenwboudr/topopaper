"""`topopaper-ctl search` without rofi or fuzzel: a small search window.

    python -m topopaper.tkui.picker [TEXT]

Lists your maps (the one on screen first, connector rungs left out),
filtered as you type. Up/Down and Enter fly to one; Enter on any other text
looks the place up and builds a map of it in the background
(search.resolve, on a worker thread). Escape closes. The window stays open
with the reason when a lookup fails.
"""
import argparse
import sys

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:            # main() explains
    tk = ttk = None

from .. import config, util
from . import helpers, look
from .work import resolve_error, resolve_place

ROWS = 7
ROW_H = 34
WIDTH = 560


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="topopaper-ctl search",
                                 description="Pick one of your maps, or type any place on Earth")
    ap.add_argument("text", nargs="*", help="start with this text in the search box")
    return ap.parse_args(argv)


def entries(query, items):
    """What the list shows for `query`: the matching maps, then (for any
    non-empty text) a last row that searches the whole Earth for it."""
    out = helpers.rank(query, items)
    q = (query or "").strip()
    if q:
        out = out + [dict(search=q)]
    return out


def clip(text, font, width):
    """`text` cut to fit `width` pixels in `font`, with an ellipsis."""
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


class Picker:
    def __init__(self, root, text=""):
        from . import widgets as W
        from .work import Worker
        self.root, self.W = root, W
        px = W.px
        theme = config.load().get("theme")
        self.t = look.theme_colours(theme)
        self.pal = p = look.palette(theme)
        self.fnt = look.fonts(root)
        look.apply(root, p, self.fnt)
        self.worker = Worker(root)
        self.items = helpers.picker_items(util.list_packs(), util.current_area())
        self.shown = []
        self.sel = self.top = 0
        self.busy = False
        self.rc = 0
        self.row_h = px(ROW_H)

        root.title("Topopaper: go somewhere")
        root.configure(background=p["side"])
        root.resizable(False, False)
        try:
            root.attributes("-topmost", True)
            self._icon = W.icon_image(root, self.t, 32)
            root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

        outer = tk.Frame(root, background=p["side"], highlightthickness=1,
                         highlightbackground=p["border"], highlightcolor=p["border"])
        outer.pack(fill="both", expand=True)
        bar = tk.Frame(outer, background=p["side"])
        bar.pack(fill="x", padx=px(16), pady=(px(16), px(10)))
        size = px(28)
        cv = tk.Canvas(bar, width=size, height=size, background=p["side"],
                       highlightthickness=0, borderwidth=0)
        cv.pack(side="left", padx=(0, px(12)))
        W.draw_emblem(cv, self.t, 0, 0, size)
        self.var = tk.StringVar(master=root, value=text)
        self.entry = ttk.Entry(bar, textvariable=self.var, style="Big.TEntry",
                               font=self.fnt["big"])
        self.entry.pack(side="left", fill="x", expand=True)
        W.placeholder(self.entry, self.var, "Your maps, or any place on Earth", p)

        self.list = tk.Canvas(outer, width=px(WIDTH), height=ROWS * self.row_h,
                              background=p["side"], highlightthickness=0, borderwidth=0)
        self.list.pack(fill="x", padx=px(8))
        foot = tk.Frame(outer, background=p["side"])
        foot.pack(fill="x", padx=px(18), pady=(px(8), px(12)))
        self.status = tk.Label(foot, background=p["side"], foreground=p["dim"],
                               font=self.fnt["small"], anchor="w", justify="left",
                               wraplength=px(WIDTH - 60))
        self.status.pack(side="left", fill="x", expand=True)
        self.hint = tk.Label(foot, text="↑↓ choose   Enter go   Esc close",
                             background=p["side"], foreground=p["faint"],
                             font=self.fnt["small"])
        self.hint.pack(side="right")

        self.var.trace_add("write", lambda *_: self.filter())
        for seq, fn in (("<Up>", lambda e: self.move(-1)), ("<Down>", lambda e: self.move(1)),
                        ("<Prior>", lambda e: self.move(-ROWS)),
                        ("<Next>", lambda e: self.move(ROWS)),
                        ("<Return>", lambda e: self.activate()),
                        ("<KP_Enter>", lambda e: self.activate())):
            self.entry.bind(seq, fn)
        root.bind("<Escape>", lambda e: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.list.bind("<Motion>", self._hover)
        self.list.bind("<Button-1>", self._click)
        self.list.bind("<MouseWheel>", lambda e: self.scroll(-1 if e.delta > 0 else 1))
        self.list.bind("<Button-4>", lambda e: self.scroll(-1))
        self.list.bind("<Button-5>", lambda e: self.scroll(1))
        self.filter()

    # ---- the list --------------------------------------------------------------
    def filter(self):
        if self.busy:
            return
        self.shown = entries(self.var.get(), self.items)
        self.sel = self.top = 0
        self.say("")
        self.draw()

    def move(self, d):
        if self.shown and not self.busy:
            self.sel = max(0, min(len(self.shown) - 1, self.sel + d))
            self._keep_visible()
            self.draw()
        return "break"

    def scroll(self, d):
        self.top = max(0, min(max(0, len(self.shown) - ROWS), self.top + d))
        self.draw()

    def _keep_visible(self):
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + ROWS:
            self.top = self.sel - ROWS + 1

    def _row_at(self, y):
        i = self.top + int(y // self.row_h)
        return i if 0 <= i < len(self.shown) else None

    def _hover(self, e):
        i = self._row_at(e.y)
        if i is not None and i != self.sel and not self.busy:
            self.sel = i
            self.draw()

    def _click(self, e):
        i = self._row_at(e.y)
        if i is not None:
            self.sel = i
            self.activate()

    def draw(self):
        px, p, c = self.W.px, self.pal, self.list
        c.delete("all")
        w = int(c.cget("width"))
        if not self.shown:
            c.create_text(w // 2, ROWS * self.row_h // 2, fill=p["dim"], font=self.fnt["body"],
                          text="No maps yet. Type any place on Earth to build one.")
            return
        right_w = px(110)
        for k in range(ROWS):
            i = self.top + k
            if i >= len(self.shown):
                break
            it = self.shown[i]
            y0, y1 = k * self.row_h, (k + 1) * self.row_h
            ym = (y0 + y1) // 2
            sel = i == self.sel
            if sel:
                c.create_rectangle(0, y0 + 1, w, y1 - 1, fill=p["side_sel"], width=0)
                c.create_rectangle(0, y0 + 1, px(3), y1 - 1, fill=p["accent"], width=0)
            if "search" in it:
                title = f"Find “{it['search']}” on Earth"
                right, rcol = "new map", p["dim"]
                tcol = p["accent"]
            else:
                title = it["title"]
                right, rcol = ("On screen", p["accent"]) if it["current"] else \
                    (it["scale"], p["dim"])
                tcol = p["text"]
            c.create_text(px(16), ym, anchor="w", fill=tcol, font=self.fnt["body"],
                          text=clip(title, self.fnt["body"], w - right_w - px(40)))
            c.create_text(w - px(16), ym, anchor="e", fill=rcol, font=self.fnt["small"],
                          text=clip(right, self.fnt["small"], right_w))
        if len(self.shown) > ROWS:              # a thin position marker on the right
            h = ROWS * self.row_h
            top = h * self.top / len(self.shown)
            bot = h * min(len(self.shown), self.top + ROWS) / len(self.shown)
            c.create_rectangle(w - px(3), top + 2, w - 1, bot - 2, fill=p["border"], width=0)

    # ---- going -----------------------------------------------------------------
    def say(self, text, error=False):
        self.status.configure(text=text, foreground=self.pal["danger"] if error
                              else self.pal["dim"])

    def activate(self):
        if self.busy or not self.shown:
            return "break"
        it = self.shown[self.sel]
        target = it.get("name") or it.get("search")
        self.busy = True
        self.entry.state(["disabled"])
        self.say(f"Flying to {it['title']}…" if "name" in it else f"Looking up “{target}”…")

        def done(res, err):
            rc, msgs = res if res else (1, [])
            problem = resolve_error(target, rc, msgs, err)
            if not problem:
                self.rc = 0
                self.close()
                return
            self.rc = 1
            self.busy = False
            self.entry.state(["!disabled"])
            self.entry.focus_set()
            self.say(problem, error=True)
        self.worker.run(resolve_place, target, done=done)
        return "break"

    def close(self):
        self.worker.stop()
        self.root.destroy()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if tk is None:
        print("topopaper-ctl search needs Tkinter (Python's tk module), or a launcher "
              "such as rofi or fuzzel", file=sys.stderr)
        return 1
    from .widgets import center, make_root
    try:
        root = make_root()
    except tk.TclError as e:
        print(f"topopaper-ctl search: no graphical display ({e})", file=sys.stderr)
        return 1
    pk = Picker(root, " ".join(args.text).strip())
    center(root)
    root.deiconify()
    root.lift()

    def grab_focus():
        root.focus_force()
        pk.entry.focus_set()
        pk.entry.icursor("end")
    root.after(60, grab_focus)
    root.mainloop()
    return pk.rc


if __name__ == "__main__":
    sys.exit(main())
