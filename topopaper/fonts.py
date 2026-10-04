"""The label/HUD font: the bundled JetBrains Mono Bold, else a system mono via fontconfig."""
import shutil
import subprocess

from . import paths


def find_font():
    p = paths.font_file()
    if p.is_file():
        return str(p)
    if shutil.which("fc-match"):
        for pat in ("JetBrains Mono:bold", "DejaVu Sans Mono:bold", "monospace:bold"):
            r = subprocess.run(["fc-match", "-f", "%{file}", pat],
                               capture_output=True, text=True, check=False)
            if r.returncode == 0 and r.stdout.endswith((".ttf", ".otf")):
                return r.stdout
    raise SystemExit("topopaper: no usable font (bundled JetBrains Mono missing, "
                     "and fontconfig found no monospace font)")
