// topopaper — the Windows backend (platform.h): one child window per monitor
// inside the desktop's wallpaper layer, so the map sits behind the desktop
// icons like a wallpaper, drawn with WGL (desktop OpenGL 2.1).
//
// Explorer has no API for this. The well-known recipe (Lively, weebp): send
// Progman the undocumented 0x052C message, which makes it split the desktop
// into SHELLDLL_DefView (the icons) and a WorkerW (the wallpaper), then
// parent our windows into that layer.
//   Windows 10 / 11 before 24H2: the WorkerW is a top-level window right
//     behind the one holding SHELLDLL_DefView; we become its children.
//   Windows 11 24H2 and later (Progman has WS_EX_NOREDIRECTIONBITMAP):
//     WorkerW and SHELLDLL_DefView are children of Progman. Microsoft's
//     guidance: a WS_EX_LAYERED child of Progman (alpha 255), stacked right
//     under the icons, with the WorkerW kept at the bottom.
// Without a usable desktop (no Explorer shell, Wine) the windows fall back to
// plain borderless windows kept at the bottom of the z-order.
//
// Explorer restarts (the TaskbarCreated broadcast), monitor changes
// (WM_DISPLAYCHANGE) and a vanished parent rebuild everything. The windows
// never take input or focus. Occlusion is detected here (a window covering
// a monitor's work area, or maximized on it) instead of by a watcher.
#include "platform.h"
#include <dwmapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TOPA_GL_DEF(type, name) type topa_##name;
TOPA_GL_FUNCS(TOPA_GL_DEF)
#undef TOPA_GL_DEF

typedef BOOL (WINAPI *swap_interval_fn)(int);

#define MAXMON 16
struct view {
    HWND   hwnd;
    HDC    dc;
    char   name[64];                // "DISPLAY1" (the monitor's device name)
    RECT   rc;                      // monitor rect, screen coordinates
    RECT   work;                    // its work area (without the taskbar)
    int    w, h;
};
static struct view views[MAXMON];
static int    n_views = 0;
static HGLRC  glrc = NULL;
static HWND   ctl = NULL;           // hidden top-level window: broadcasts land here
static HWND   parent = NULL;        // WorkerW or Progman; NULL in fallback mode
static HWND   below_icons = NULL;   // 24H2: SHELLDLL_DefView, we go right under it
static int    raised = 0;           // 24H2 layout
static int    layered = 0;          // our windows are WS_EX_LAYERED
// TOPA_WIN_ATTACH: "window" skips the desktop layer (plain bottom windows);
// diagnostics: worker, worker-layered, top
static const char *attach_mode = "";
// The desktop layer can accept our windows without DWM ever showing them
// (seen on Windows Server 2025, reported on some 24H2/25H2 builds). A few
// seconds after attaching, the screen is compared with our own frame; if
// the desktop shows none of it, we fall back to plain bottom windows.
static int    no_desktop = 0;       // proved invisible: stay in window mode
static double vis_check_at = -1.0;  // engine_now() of the next check (<0: none)
static int    vis_tries = 0;
static UINT   msg_taskbar = 0;
static int    rebuild = 1;
static const wchar_t *VIEW_CLASS = L"topopaperView";
static const wchar_t *CTL_CLASS  = L"topopaperController";

// ---- the desktop's wallpaper layer ---------------------------------------------
static BOOL CALLBACK find_defview(HWND top, LPARAM out) {
    HWND dv = FindWindowExW(top, NULL, L"SHELLDLL_DefView", NULL);
    if (dv) {
        // the WorkerW we want is the next top-level window after this one
        *(HWND *)out = FindWindowExW(NULL, top, L"WorkerW", NULL);
        return FALSE;
    }
    return TRUE;
}

static HWND find_worker(HWND progman) {
    if (raised) return FindWindowExW(progman, NULL, L"WorkerW", NULL);
    HWND worker = NULL;
    EnumWindows(find_defview, (LPARAM)&worker);
    return worker;
}

// Sets parent / raised / below_icons; 0 when there is no usable desktop.
// 0x052C is reference-counted by Explorer and flashes the desktop, so it is
// sent only while the WorkerW is missing: (0xD, 1) first, the legacy (0, 0)
// only if that made none. The WorkerW can take a moment to appear.
static HWND worker = NULL;
static int attach_desktop(void) {
    parent = below_icons = worker = NULL;
    HWND progman = FindWindowW(L"Progman", NULL);
    if (!progman) return 0;
    raised = (GetWindowLongPtrW(progman, GWL_EXSTYLE) & WS_EX_NOREDIRECTIONBITMAP) != 0;
    worker = find_worker(progman);
    DWORD_PTR r;
    for (int i = 0; !worker && i < 20; i++) {
        if (i == 0) SendMessageTimeoutW(progman, 0x052C, 0xD, 0x1, SMTO_NORMAL, 1000, &r);
        if (i == 10) SendMessageTimeoutW(progman, 0x052C, 0, 0, SMTO_NORMAL, 1000, &r);
        Sleep(100);
        worker = find_worker(progman);
    }
    if (!worker) return 0;
    below_icons = raised ? FindWindowExW(progman, NULL, L"SHELLDLL_DefView", NULL) : NULL;
    parent = raised ? progman : worker;
    layered = raised;
    if (!strncmp(attach_mode, "worker", 6)) {
        parent = worker;
        layered = !strcmp(attach_mode, "worker-layered");
    }
    return 1;
}

// TOPA_WIN_DEBUG=1: the desktop's window tree, top of the z-order first
static void dump_tree(HWND p, int depth) {
    if (!getenv("TOPA_WIN_DEBUG") || depth > 2) return;
    for (HWND h = GetWindow(p, GW_CHILD); h; h = GetWindow(h, GW_HWNDNEXT)) {
        wchar_t cls[64] = L"";
        char c8[64];
        RECT r;
        GetClassNameW(h, cls, 64);
        GetWindowRect(h, &r);
        WideCharToMultiByte(CP_UTF8, 0, cls, -1, c8, sizeof c8, NULL, NULL);
        fprintf(stderr, "topopaper: %*s%s vis=%d ex=0x%lx (%ld,%ld %ldx%ld)\n", depth * 2, "", c8,
                IsWindowVisible(h), (long)GetWindowLongPtrW(h, GWL_EXSTYLE),
                r.left, r.top, r.right - r.left, r.bottom - r.top);
        if (!wcscmp(cls, L"WorkerW") || !wcscmp(cls, L"SHELLDLL_DefView")) dump_tree(h, depth + 1);
    }
}

// 24H2: the WorkerW (the plain wallpaper) must stay Progman's bottom child.
static void keep_order(HWND hw) {
    if (!strcmp(attach_mode, "top")) {
        SetWindowPos(hw, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        return;
    }
    if (parent != FindWindowW(L"Progman", NULL)) return;
    SetWindowPos(hw, below_icons ? below_icons : HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    if (worker && GetWindow(worker, GW_HWNDNEXT))
        SetWindowPos(worker, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
}

// Before 24H2 the WorkerW keeps showing our last frame after we go, until
// the wallpaper is set again (to itself). On 24H2 that would destroy the
// WorkerW; there DWM simply shows the WorkerW again.
static void repaint_desktop(void) {
    if (raised || !parent) return;
    wchar_t wp[MAX_PATH] = L"";
    if (SystemParametersInfoW(SPI_GETDESKWALLPAPER, MAX_PATH, wp, 0) && wp[0])
        SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, wp, SPIF_SENDCHANGE);
}

// ---- windows ----------------------------------------------------------------------
static LRESULT CALLBACK view_proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
    case WM_NCHITTEST: return HTTRANSPARENT;           // clicks go to the desktop
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_ERASEBKGND: return 1;
    case WM_PAINT: ValidateRect(h, NULL); return 0;
    case WM_WINDOWPOSCHANGING:
        if (!parent) {                                 // fallback: stay at the bottom
            WINDOWPOS *wp = (WINDOWPOS *)l;
            wp->hwndInsertAfter = HWND_BOTTOM;
            wp->flags &= ~SWP_NOZORDER;
        }
        break;
    }
    return DefWindowProcW(h, m, w, l);
}

static LRESULT CALLBACK ctl_proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == msg_taskbar && m) {                       // Explorer (re)started, or DPI
        fprintf(stderr, "topopaper: the taskbar was recreated; re-attaching\n");
        rebuild = 1;
        return 0;
    }
    switch (m) {
    case WM_DISPLAYCHANGE:
    case WM_SETTINGCHANGE:
        if (m == WM_DISPLAYCHANGE || w == SPI_SETWORKAREA) rebuild = 1;
        break;
    case WM_CLOSE:                                     // the session's polite stop
    case WM_ENDSESSION:
        g_running = 0;
        return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

static void destroy_views(void) {
    if (glrc) wglMakeCurrent(NULL, NULL);
    for (int i = 0; i < n_views; i++) {
        if (views[i].dc) ReleaseDC(views[i].hwnd, views[i].dc);
        if (views[i].hwnd) DestroyWindow(views[i].hwnd);
    }
    n_views = 0;
}

static BOOL CALLBACK add_monitor(HMONITOR mon, HDC dc, LPRECT r, LPARAM lp) {
    (void)dc; (void)r; (void)lp;
    if (n_views >= MAXMON) return FALSE;
    MONITORINFOEXW mi;
    memset(&mi, 0, sizeof mi);
    mi.cbSize = sizeof mi;
    if (!GetMonitorInfoW(mon, (MONITORINFO *)&mi)) return TRUE;
    struct view *v = &views[n_views];
    memset(v, 0, sizeof *v);
    const wchar_t *dev = mi.szDevice;                  // "\\.\DISPLAY1"
    if (!wcsncmp(dev, L"\\\\.\\", 4)) dev += 4;
    WideCharToMultiByte(CP_UTF8, 0, dev, -1, v->name, sizeof v->name, NULL, NULL);
    if (!engine_output_wanted(v->name)) return TRUE;
    v->rc = mi.rcMonitor;
    v->work = mi.rcWork;
    v->w = mi.rcMonitor.right - mi.rcMonitor.left;
    v->h = mi.rcMonitor.bottom - mi.rcMonitor.top;
    n_views++;
    return TRUE;
}

static int set_format(HDC dc) {
    PIXELFORMATDESCRIPTOR pfd;
    memset(&pfd, 0, sizeof pfd);
    pfd.nSize = sizeof pfd;
    pfd.nVersion = 1;
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType = PFD_TYPE_RGBA;
    pfd.cColorBits = 32;
    pfd.cAlphaBits = 8;
    pfd.iLayerType = PFD_MAIN_PLANE;
    int pf = ChoosePixelFormat(dc, &pfd);
    return pf && SetPixelFormat(dc, pf, &pfd);
}

static void gl_load(void) {
#define TOPA_GL_LOAD(type, name) \
    topa_##name = (type)(void *)wglGetProcAddress(#name); \
    if (!topa_##name) { \
        fprintf(stderr, "topopaper: OpenGL 2.0 is missing (%s); update the graphics driver\n", #name); \
        exit(1); }
    TOPA_GL_FUNCS(TOPA_GL_LOAD)
#undef TOPA_GL_LOAD
}

// (Re)attach to the desktop and make one window per selected monitor.
static void build_views(void) {
    destroy_views();
    int desk = (no_desktop || !strcmp(attach_mode, "window")) ? 0 : attach_desktop();
    if (!desk) parent = NULL;
    vis_check_at = desk ? engine_now() + 4.0 : -1.0;
    vis_tries = 0;
    EnumDisplayMonitors(NULL, NULL, add_monitor, 0);
    HINSTANCE inst = GetModuleHandleW(NULL);
    for (int i = 0; i < n_views; i++) {
        struct view *v = &views[i];
        HWND hw;
        if (desk) {
            // made as a popup, then turned into a child of the desktop layer:
            // layered (24H2) before SetParent, WS_CHILD set before it too
            hw = CreateWindowExW(layered ? WS_EX_LAYERED : 0, VIEW_CLASS, L"topopaper",
                                 WS_POPUP, 0, 0, v->w, v->h, NULL, NULL, inst, NULL);
            if (hw) {
                if (layered) SetLayeredWindowAttributes(hw, 0, 255, LWA_ALPHA);
                SetWindowLongPtrW(hw, GWL_STYLE, WS_CHILD | WS_CLIPSIBLINGS | WS_CLIPCHILDREN);
                if (!SetParent(hw, parent)) {
                    fprintf(stderr, "topopaper: SetParent failed (%lu); retrying\n", GetLastError());
                    DestroyWindow(hw);
                    rebuild = 1;
                    continue;
                }
                POINT p = { v->rc.left, v->rc.top };
                ScreenToClient(parent, &p);
                SetWindowPos(hw, NULL, p.x, p.y, v->w, v->h, SWP_NOZORDER | SWP_NOACTIVATE);
                keep_order(hw);
            }
        } else {
            hw = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
                                 VIEW_CLASS, L"topopaper", WS_POPUP,
                                 v->rc.left, v->rc.top, v->w, v->h, NULL, NULL, inst, NULL);
            if (hw) SetWindowPos(hw, HWND_BOTTOM, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
        if (!hw) { fprintf(stderr, "topopaper: CreateWindow failed (%lu)\n", GetLastError()); continue; }
        v->hwnd = hw;
        v->dc = GetDC(hw);
        if (!set_format(v->dc)) {
            fprintf(stderr, "topopaper: no OpenGL pixel format on %s\n", v->name);
            continue;
        }
        if (!glrc) {
            glrc = wglCreateContext(v->dc);
            if (!glrc || !wglMakeCurrent(v->dc, glrc)) {
                fprintf(stderr, "topopaper: no OpenGL context (%lu)\n", GetLastError());
                exit(1);
            }
            gl_load();
            swap_interval_fn si = (swap_interval_fn)(void *)wglGetProcAddress("wglSwapIntervalEXT");
            if (si) si(0);                   // the engine paces itself
            engine_gl_init();
        }
        ShowWindow(hw, SW_SHOWNOACTIVATE);
        fprintf(stderr, "topopaper: drawing on %s (%dx%d%s%s%s)\n", v->name, v->w, v->h,
                desk ? (raised ? ", desktop layer 24H2" : ", desktop layer") : ", bottom window",
                attach_mode[0] ? ", attach " : "", attach_mode);
    }
    if (desk) dump_tree(FindWindowW(L"Progman", NULL), 0);
}

// ---- occlusion ----------------------------------------------------------------------
// A monitor counts as covered while a real window (visible, not minimized,
// not cloaked on another virtual desktop, not the shell) is maximized on it
// or covers its whole work area. The wallpaper is covered when every monitor
// it draws on is.
struct cover { int covered[MAXMON]; };

// the shell's own surfaces: Start, search, Alt-Tab, Task view, widgets, taskbars
static const wchar_t *SHELL_CLASSES[] = {
    L"Progman", L"WorkerW", L"Shell_TrayWnd", L"Shell_SecondaryTrayWnd",
    L"Windows.UI.Core.CoreWindow", L"MultitaskingViewFrame", L"XamlExplorerHostIslandWindow",
    L"WindowsDashboard", L"NotifyIconOverflowWindow", L"topopaperView", L"topopaperController" };

// The filter Lively uses for "a real app window".
static int counts(HWND h) {
    if (!IsWindowVisible(h) || IsIconic(h)) return 0;
    DWORD cloaked = 0;                       // UWP on another virtual desktop, suspended
    if (SUCCEEDED(DwmGetWindowAttribute(h, DWMWA_CLOAKED, &cloaked, sizeof cloaked)) && cloaked)
        return 0;
    LONG_PTR ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
    if (ex & (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)) return 0;
    if ((ex & WS_EX_NOACTIVATE) && !(ex & WS_EX_APPWINDOW)) return 0;
    if (GetWindowTextLengthW(h) == 0) return 0;
    wchar_t cls[96];
    if (!GetClassNameW(h, cls, 96)) return 0;
    for (size_t i = 0; i < sizeof SHELL_CLASSES / sizeof *SHELL_CLASSES; i++)
        if (!_wcsicmp(cls, SHELL_CLASSES[i])) return 0;
    return 1;
}

static long area(const RECT *r) {
    return r->right > r->left && r->bottom > r->top
         ? (long)(r->right - r->left) * (r->bottom - r->top) : 0;
}

static BOOL CALLBACK cover_one(HWND h, LPARAM lp) {
    struct cover *c = (struct cover *)lp;
    if (!counts(h)) return TRUE;
    RECT r;
    if (FAILED(DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof r)) &&
        !GetWindowRect(h, &r))
        return TRUE;
    HMONITOR hm = IsZoomed(h) ? MonitorFromWindow(h, MONITOR_DEFAULTTONULL) : NULL;
    MONITORINFO mi = { .cbSize = sizeof mi };
    if (hm && !GetMonitorInfoW(hm, &mi)) hm = NULL;
    for (int i = 0; i < n_views; i++) {
        RECT x;                              // 95% of the work area: shadows, borders
        const RECT *w = &views[i].work;
        int covers = IntersectRect(&x, &r, w) && area(&x) * 100 >= area(w) * 95;
        if (hm && EqualRect(&mi.rcMonitor, &views[i].rc)) covers = 1;
        if (covers) c->covered[i] = 1;
    }
    return TRUE;
}

static int all_covered(void) {
    if (!n_views) return 0;
    struct cover c;
    memset(&c, 0, sizeof c);
    EnumWindows(cover_one, (LPARAM)&c);
    for (int i = 0; i < n_views; i++) if (!c.covered[i]) return 0;
    return 1;
}

// ---- main loop ----------------------------------------------------------------------
// Is our frame what the screen shows? Compares a grid of points on the
// primary monitor's work area that no app window covers; desktop icons
// spoil a few, so most must match. Called with the frame just rendered (back
// buffer), before the swap: the screen then still shows the frame before,
// which differs by a fraction of a contour's drift.
static void visibility_check(struct view *v) {
    enum { G = 12 };
    struct pt { int x, y; } pts[G * G];
    int n = 0;
    RECT w = v->work;
    for (int j = 0; j < G; j++)
        for (int i = 0; i < G; i++) {
            POINT p = { w.left + (w.right - w.left) * (2 * i + 1) / (2 * G),
                        w.top + (w.bottom - w.top) * (2 * j + 1) / (2 * G) };
            HWND top = WindowFromPoint(p), root = top ? GetAncestor(top, GA_ROOT) : NULL;
            if (root && counts(root)) continue;  // an app window is in the way
            pts[n].x = p.x; pts[n].y = p.y; n++;
        }
    if (n < 16) {                            // the desktop is mostly covered: later
        vis_check_at = ++vis_tries < 30 ? engine_now() + 10.0 : -1.0;
        return;
    }
    vis_check_at = -1.0;
    HDC screen = GetDC(NULL);
    int match = 0;
    for (int k = 0; k < n; k++) {
        unsigned char px[4];
        glReadPixels(pts[k].x - v->rc.left, v->h - 1 - (pts[k].y - v->rc.top), 1, 1,
                     GL_RGBA, GL_UNSIGNED_BYTE, px);
        COLORREF c = GetPixel(screen, pts[k].x, pts[k].y);
        if (c != CLR_INVALID && abs(GetRValue(c) - px[0]) < 40 &&
            abs(GetGValue(c) - px[1]) < 40 && abs(GetBValue(c) - px[2]) < 40) match++;
    }
    ReleaseDC(NULL, screen);
    fprintf(stderr, "topopaper: the desktop shows %d of %d sampled points of our frame\n", match, n);
    if (match * 3 < n) {
        fprintf(stderr, "topopaper: this desktop doesn't show its wallpaper layer's windows; "
                        "drawing as a bottom window instead (it covers the desktop icons)\n");
        no_desktop = 1;
        rebuild = 1;
    }
}

static void frame(void) {
    int prim = -1;
    long best = -1;
    for (int i = 0; i < n_views; i++) {
        if (!views[i].hwnd) continue;
        long px = (long)views[i].w * views[i].h;
        if (px > best) { best = px; prim = i; }
    }
    if (prim < 0 || !wglMakeCurrent(views[prim].dc, glrc)) return;
    engine_step(views[prim].w, views[prim].h);
    for (int k = 0; k < n_views; k++) {
        int i = (prim + k) % n_views;       // primary first: it eases shared state
        if (!views[i].hwnd || !wglMakeCurrent(views[i].dc, glrc)) continue;
        engine_render(views[i].w, views[i].h, i == prim);
        if (i == prim && vis_check_at >= 0.0 && engine_now() >= vis_check_at) visibility_check(&views[i]);
        SwapBuffers(views[i].dc);
    }
}

int main(void) {
    engine_setup();
    HINSTANCE inst = GetModuleHandleW(NULL);
    WNDCLASSW wc;
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = view_proc;
    wc.hInstance = inst;
    wc.style = CS_OWNDC;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.lpszClassName = VIEW_CLASS;
    RegisterClassW(&wc);
    wc.lpfnWndProc = ctl_proc;
    wc.style = 0;
    wc.lpszClassName = CTL_CLASS;
    RegisterClassW(&wc);
    msg_taskbar = RegisterWindowMessageW(L"TaskbarCreated");
    if (getenv("TOPA_WIN_ATTACH")) attach_mode = getenv("TOPA_WIN_ATTACH");
    ctl = CreateWindowExW(WS_EX_TOOLWINDOW, CTL_CLASS, L"topopaper", WS_POPUP,
                          0, 0, 0, 0, NULL, NULL, inst, NULL);
    g_covered_src = 0;

    double next_tick = 0.0, next_check = 0.0, next_build = 0.0;
    while (g_running) {
        engine_poll();
        double now = engine_now();
        if (g_out_dirty) { g_out_dirty = 0; rebuild = 1; }
        if (now >= next_check) {
            next_check = now + 1.0;
            // the desktop went away under us (Explorer died, layer rebuilt,
            // a slideshow or unlock replaced the WorkerW)
            if (parent && (!IsWindow(parent) || (worker && !IsWindow(worker)))) rebuild = 1;
            for (int i = 0; i < n_views && !rebuild; i++)
                if (views[i].hwnd && parent && GetParent(views[i].hwnd) != parent) rebuild = 1;
            g_covered_src = all_covered();
        }
        if (rebuild && now >= next_build) {
            rebuild = 0;
            next_build = now + 2.0;          // a burst of triggers rebuilds once
            build_views();
            next_tick = 0.0;
        }
        if (engine_now() >= next_tick) {
            frame();
            next_tick = engine_next_frame();
        }
        double wait = next_tick - engine_now();
        if (wait > 0.4) wait = 0.4;
        if (wait < 0.0) wait = 0.0;
        MsgWaitForMultipleObjects(0, NULL, FALSE, (DWORD)(wait * 1000.0), QS_ALLINPUT);
        MSG m;
        while (PeekMessageW(&m, NULL, 0, 0, PM_REMOVE)) {
            if (m.message == WM_QUIT) g_running = 0;
            TranslateMessage(&m);
            DispatchMessageW(&m);
        }
    }
    destroy_views();
    if (glrc) wglDeleteContext(glrc);
    repaint_desktop();
    if (ctl) DestroyWindow(ctl);
    return 0;
}
