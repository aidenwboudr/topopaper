// topopaper: tell the session whether windows cover the wallpaper.
//
// Loaded by topopaper's watcher through org.kde.KWin /Scripting (it is not an
// installed KWin script, so nothing to enable or remove by hand). Plasma has
// no tiling, so a screen counts as covered when a fullscreen window, or one
// window filling >= 80% of it (maximized, or nearly), or several windows
// together filling it, sit on the current desktop. Reports on change via
// callDBus to the watcher's own D-Bus name. KWin 6 API.
const SVC = "io.github.aidenwboudr.topopaper.Watch";
const OBJ = "/io/github/aidenwboudr/topopaper/Watch";
let last = null;
const hooked = new Set();

function area(r) { return Math.max(0, r.width) * Math.max(0, r.height); }

function overlap(a, b) {
    const x0 = Math.max(a.x, b.x), y0 = Math.max(a.y, b.y);
    const x1 = Math.min(a.x + a.width, b.x + b.width);
    const y1 = Math.min(a.y + a.height, b.y + b.height);
    return Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
}

function onCurrentDesktop(w) {
    if (w.onAllDesktops) return true;
    const cur = workspace.currentDesktop;
    return (w.desktops || []).some(d => d === cur || (d && cur && d.id === cur.id));
}

function screenCovered(s) {
    const g = s.geometry, full = area(g);
    if (!full) return true;
    let sum = 0;
    for (const w of workspace.windowList()) {
        if (!w.normalWindow || w.minimized || w.hidden || !onCurrentDesktop(w)) continue;
        if (!w.output || w.output.name !== s.name) continue;
        const ov = overlap(w.frameGeometry, g);
        if (w.fullScreen || ov >= 0.8 * full) return true;
        sum += ov;
    }
    return sum >= 0.95 * full;
}

function update() {
    const scr = workspace.screens || [];
    const c = scr.length > 0 && scr.every(screenCovered);
    if (c === last) return;
    last = c;
    callDBus(SVC, OBJ, SVC, "SetCovered", c);
}

function hook(w) {
    if (!w || hooked.has(w)) return;
    hooked.add(w);
    for (const sig of ["frameGeometryChanged", "minimizedChanged", "fullScreenChanged",
                       "outputChanged", "desktopsChanged"]) {
        if (w[sig]) w[sig].connect(update);
    }
}

workspace.windowAdded.connect(w => { hook(w); update(); });
workspace.windowRemoved.connect(w => { hooked.delete(w); update(); });
workspace.windowActivated.connect(update);
workspace.currentDesktopChanged.connect(update);
if (workspace.screensChanged) workspace.screensChanged.connect(update);
for (const w of workspace.windowList()) hook(w);
update();
