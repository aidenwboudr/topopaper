// topopaper — the Wayland backend (platform.h): one wlr-layer-shell surface
// per selected output, EGL + GLES2, frame callbacks pace the drawing.
#include "platform.h"
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <wayland-client.h>
#include <wayland-egl.h>
#include "wlr-layer-shell-client-protocol.h"
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct wl_display    *display;
static struct wl_compositor *compositor;
static struct zwlr_layer_shell_v1 *layer_shell;
static EGLDisplay egl_display;
static EGLConfig  egl_config;
static EGLContext egl_context = EGL_NO_CONTEXT;

// One per wl_output global. The surface exists only while the output is
// selected (cfg.outputs) and the compositor hasn't closed it; outputs come
// and go at runtime (hotplug), and a settings change re-evaluates the set.
struct output {
    struct wl_output *wl_output;
    uint32_t global, version;       // registry name (global_remove) + bound version
    char     name[64];              // wl_output v4 name event ("" before v4)
    int      ready;                 // first done event seen: name + scale known
    int      closed;                // compositor closed our layer surface
    int      layer;                 // cfg.layer_background the surface was made with
    int      configured;            // first configure acked, EGL surface live
    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer_surface;
    struct wl_egl_window *egl_window;
    EGLSurface egl_surface;
    struct wl_callback *frame_cb;   // non-NULL: a frame is in flight
    int32_t width, height, scale, buf_scale;
    struct output *next;
};
static struct output *outputs;      // simple linked list
static int g_wl_ready = 0;          // initial roundtrips done: surfaces may be made

// The compositor is ready for this output's next frame; the main loop draws
// it on the next tick.
static void frame_done(void *data, struct wl_callback *cb, uint32_t t) {
    (void)t; struct output *o = data;
    wl_callback_destroy(cb);
    o->frame_cb = NULL;
}
static const struct wl_callback_listener frame_listener = { .done = frame_done };

static void render(struct output *o, int primary) {
    int w = o->width * o->scale, h = o->height * o->scale;
    if (w <= 0 || h <= 0) return;
    if (!eglMakeCurrent(egl_display, o->egl_surface, o->egl_surface, egl_context)) return;
    engine_render(w, h, primary);
    o->frame_cb = wl_surface_frame(o->surface);
    wl_callback_add_listener(o->frame_cb, &frame_listener, o);
    eglSwapBuffers(egl_display, o->egl_surface);
}

// One frame tick: step the world ONCE, then draw it into every output whose
// previous frame the compositor has released. Returns 0 (nothing done) when
// no output is ready, so the world stays put while nothing can show it.
static int frame_tick(void) {
    struct output *first = NULL, *prim = NULL;
    long best = -1;
    for (struct output *o = outputs; o; o = o->next) {
        if (!o->configured || o->egl_surface == EGL_NO_SURFACE) continue;
        long px = (long)o->width * o->scale * (long)o->height * o->scale;
        if (px > best) { best = px; prim = o; }
        if (!o->frame_cb && !first) first = o;
    }
    if (!first) return 0;
    if (!eglMakeCurrent(egl_display, first->egl_surface, first->egl_surface, egl_context))
        return 0;
    engine_step(prim->width * prim->scale, prim->height * prim->scale);
    for (struct output *o = outputs; o; o = o->next)
        if (o->configured && o->egl_surface != EGL_NO_SURFACE && !o->frame_cb)
            render(o, o == prim || (prim->frame_cb && o == first));
    return 1;
}

// ---- outputs: surfaces, hotplug ----------------------------------------------
static void destroy_surface(struct output *o) {
    if (o->frame_cb) { wl_callback_destroy(o->frame_cb); o->frame_cb = NULL; }
    if (o->egl_surface != EGL_NO_SURFACE) {
        if (eglGetCurrentSurface(EGL_DRAW) == o->egl_surface)
            eglMakeCurrent(egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, egl_context);
        eglDestroySurface(egl_display, o->egl_surface);
        o->egl_surface = EGL_NO_SURFACE;
    }
    if (o->egl_window) { wl_egl_window_destroy(o->egl_window); o->egl_window = NULL; }
    if (o->layer_surface) { zwlr_layer_surface_v1_destroy(o->layer_surface); o->layer_surface = NULL; }
    if (o->surface) { wl_surface_destroy(o->surface); o->surface = NULL; }
    o->configured = 0;
    o->width = o->height = 0;
}

static void destroy_output(struct output *o) {
    destroy_surface(o);
    if (o->version >= 3) wl_output_release(o->wl_output);
    else wl_output_destroy(o->wl_output);
    for (struct output **pp = &outputs; *pp; pp = &(*pp)->next)
        if (*pp == o) { *pp = o->next; break; }
    free(o);
}

// ---- layer surface ---------------------------------------------------------
static void ls_configure(void *data, struct zwlr_layer_surface_v1 *s, uint32_t serial,
                         uint32_t w, uint32_t h) {
    struct output *o = data;
    o->width = w; o->height = h;
    zwlr_layer_surface_v1_ack_configure(s, serial);
    wl_surface_set_buffer_scale(o->surface, o->scale);
    o->buf_scale = o->scale;
    if (w == 0 || h == 0) return;
    if (!o->egl_window) {
        o->egl_window = wl_egl_window_create(o->surface, w * o->scale, h * o->scale);
        o->egl_surface = eglCreatePlatformWindowSurface(egl_display, egl_config, o->egl_window, NULL);
        if (o->egl_surface == EGL_NO_SURFACE) { fprintf(stderr, "topopaper: no EGL surface\n"); return; }
        eglMakeCurrent(egl_display, o->egl_surface, o->egl_surface, egl_context);
        eglSwapInterval(egl_display, 0);
        fprintf(stderr, "topopaper: drawing on %s (%dx%d@%d)\n",
                o->name[0] ? o->name : "output", (int)w, (int)h, o->scale);
    } else {
        wl_egl_window_resize(o->egl_window, w * o->scale, h * o->scale, 0, 0);
    }
    o->configured = 1;
}
// The compositor took the surface away (output disabled, or it refused the
// layer). Drop it; it comes back on a settings change or a re-plugged output.
static void ls_closed(void *data, struct zwlr_layer_surface_v1 *s) {
    (void)s; struct output *o = data;
    fprintf(stderr, "topopaper: surface on %s closed by the compositor\n",
            o->name[0] ? o->name : "output");
    destroy_surface(o);
    o->closed = 1;
}
static const struct zwlr_layer_surface_v1_listener ls_listener = {
    .configure = ls_configure, .closed = ls_closed };

static void create_surface(struct output *o) {
    o->surface = wl_compositor_create_surface(compositor);
    struct wl_region *empty = wl_compositor_create_region(compositor);   // click-through
    wl_surface_set_input_region(o->surface, empty);
    wl_region_destroy(empty);
    // BOTTOM by default, not BACKGROUND: same-layer stacking is creation
    // order, so another wallpaper tool started later lands ON TOP of a
    // persistent engine. One layer up, the engine always wins over background
    // wallpapers yet stays under every window. layer = background gives the
    // layer back for setups that want something else above it.
    o->layer = engine_layer_background();
    o->layer_surface = zwlr_layer_shell_v1_get_layer_surface(
        layer_shell, o->surface, o->wl_output,
        o->layer ? ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND : ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM,
        "topopaper");
    zwlr_layer_surface_v1_set_size(o->layer_surface, 0, 0);
    zwlr_layer_surface_v1_set_anchor(o->layer_surface,
        ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
        ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_exclusive_zone(o->layer_surface, -1);
    zwlr_layer_surface_v1_add_listener(o->layer_surface, &ls_listener, o);
    wl_surface_commit(o->surface);
}

// Bring the surfaces in line with the settings: create where selected,
// destroy where not, recreate where the layer changed.
static void reconcile_outputs(void) {
    if (!g_wl_ready) return;
    for (struct output *o = outputs; o; o = o->next) {
        if (!o->ready) continue;
        int want = engine_output_wanted(o->name) && !o->closed;
        if (o->surface && (!want || o->layer != engine_layer_background())) {
            fprintf(stderr, "topopaper: releasing %s\n", o->name[0] ? o->name : "output");
            destroy_surface(o);
        }
        if (want && !o->surface) create_surface(o);
    }
}

// ---- output listener ---------------------------------------------------------
static void o_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw,
    int32_t ph, int32_t sp, const char *m, const char *md, int32_t tr) {
    (void)d;(void)o;(void)x;(void)y;(void)pw;(void)ph;(void)sp;(void)m;(void)md;(void)tr; }
static void o_mode(void *d, struct wl_output *o, uint32_t f, int32_t w, int32_t h, int32_t r) {
    (void)d;(void)o;(void)f;(void)w;(void)h;(void)r; }
static void o_done(void *d, struct wl_output *wo) {
    (void)wo; struct output *o = d;
    if (!o->ready) { o->ready = 1; g_out_dirty = 1; return; }
    if (o->configured && o->scale != o->buf_scale) {   // scale changed live
        o->buf_scale = o->scale;
        wl_surface_set_buffer_scale(o->surface, o->scale);
        wl_egl_window_resize(o->egl_window, o->width * o->scale, o->height * o->scale, 0, 0);
    }
}
static void o_scale(void *d, struct wl_output *o, int32_t s) {
    (void)o; ((struct output *)d)->scale = s > 0 ? s : 1; }
static void o_name(void *d, struct wl_output *o, const char *n) {
    (void)o; struct output *op = d;
    snprintf(op->name, sizeof op->name, "%s", n ? n : "");
}
static void o_desc(void *d, struct wl_output *o, const char *n) { (void)d;(void)o;(void)n; }
static const struct wl_output_listener output_listener = {
    o_geometry, o_mode, o_done, o_scale, o_name, o_desc };

// ---- registry --------------------------------------------------------------
static void reg_global(void *data, struct wl_registry *reg, uint32_t name,
                       const char *iface, uint32_t ver) {
    (void)data;
    if (!strcmp(iface, wl_compositor_interface.name))
        compositor = wl_registry_bind(reg, name, &wl_compositor_interface, ver < 4 ? ver : 4);
    else if (!strcmp(iface, wl_output_interface.name)) {
        // v4 brings the name event (cfg.outputs); hotplugged outputs arrive
        // here too and get a surface once their first done event lands
        struct output *o = calloc(1, sizeof *o);
        if (!o) return;
        o->scale = 1;
        o->global = name;
        o->version = ver < 4 ? ver : 4;
        o->wl_output = wl_registry_bind(reg, name, &wl_output_interface, o->version);
        wl_output_add_listener(o->wl_output, &output_listener, o);
        o->next = outputs; outputs = o;
    } else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
        layer_shell = wl_registry_bind(reg, name, &zwlr_layer_shell_v1_interface, 1);
}
// unplugged output: its surface and EGL surface go with it
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) {
    (void)d; (void)r;
    for (struct output *o = outputs; o; o = o->next)
        if (o->global == name) {
            fprintf(stderr, "topopaper: output %s removed\n", o->name[0] ? o->name : "?");
            destroy_output(o);
            return;
        }
}
static const struct wl_registry_listener registry_listener = { reg_global, reg_remove };

static void init_egl(void) {
    egl_display = eglGetPlatformDisplay(EGL_PLATFORM_WAYLAND_KHR, display, NULL);
    if (egl_display == EGL_NO_DISPLAY || !eglInitialize(egl_display, NULL, NULL)) {
        fprintf(stderr, "topopaper: EGL init failed\n"); exit(1); }
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfg[] = { EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE,8, EGL_GREEN_SIZE,8, EGL_BLUE_SIZE,8, EGL_ALPHA_SIZE,8, EGL_NONE };
    EGLint n; if (!eglChooseConfig(egl_display, cfg, &egl_config, 1, &n) || n < 1) {
        fprintf(stderr, "topopaper: no EGL config\n"); exit(1); }
    const EGLint ctx[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    egl_context = eglCreateContext(egl_display, egl_config, EGL_NO_CONTEXT, ctx);
    if (egl_context == EGL_NO_CONTEXT) { fprintf(stderr, "topopaper: no EGL context\n"); exit(1); }
    eglMakeCurrent(egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, egl_context);
    engine_gl_init();
}

int main(void) {
    engine_setup();
    display = wl_display_connect(NULL);
    if (!display) { fprintf(stderr, "topopaper: no Wayland display\n"); return 1; }
    init_egl();
    struct wl_registry *reg = wl_display_get_registry(display);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(display);                 // bind globals
    if (!compositor || !layer_shell) { fprintf(stderr, "topopaper: missing wayland iface\n"); return 1; }
    wl_display_roundtrip(display);                 // enumerate outputs: names + scales
    g_wl_ready = 1;
    g_out_dirty = 1;

    // Main loop: poll the cheap state, keep the surfaces in line with the
    // settings, tick a frame when one is due and an output can take it, then
    // sleep on the Wayland fd until the next tick (or a frame callback, or a
    // signal). Never longer than 0.4 s, so settings/fly requests stay prompt
    // even with nothing on screen.
    int fd = wl_display_get_fd(display), err = 0;
    double next_tick = 0.0;
    while (g_running) {
        engine_poll();
        if (g_out_retry) {
            g_out_retry = 0;
            for (struct output *o = outputs; o; o = o->next) o->closed = 0;
        }
        if (g_out_dirty) { g_out_dirty = 0; reconcile_outputs(); }
        int stalled = 0;
        if (engine_now() >= next_tick) {
            if (frame_tick()) next_tick = engine_next_frame();
            else stalled = 1;                // every output busy: wait for a callback
        }
        while (wl_display_prepare_read(display) != 0)
            if (wl_display_dispatch_pending(display) < 0) { err = 1; break; }
        if (err) break;
        if (wl_display_flush(display) < 0 && errno != EAGAIN) {
            wl_display_cancel_read(display); err = 1; break;
        }
        double wait = stalled ? 0.4 : next_tick - engine_now();
        if (wait > 0.4) wait = 0.4;
        if (wait < 0.0) wait = 0.0;
        struct pollfd pfd = { .fd = fd, .events = POLLIN };
        int r = poll(&pfd, 1, (int)ceil(wait * 1000.0));
        if (r > 0 && (pfd.revents & POLLIN)) {
            if (wl_display_read_events(display) < 0) { err = 1; break; }
        } else {
            wl_display_cancel_read(display);
            if (r > 0 && (pfd.revents & (POLLERR | POLLHUP))) { err = 1; break; }
        }
        if (wl_display_dispatch_pending(display) < 0) { err = 1; break; }
    }
    if (err) fprintf(stderr, "topopaper: lost the Wayland connection\n");

    // clean exit: surfaces, outputs, EGL, connection
    while (outputs) destroy_output(outputs);
    eglMakeCurrent(egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(egl_display, egl_context);
    eglTerminate(egl_display);
    if (layer_shell) zwlr_layer_shell_v1_destroy(layer_shell);
    if (compositor) wl_compositor_destroy(compositor);
    wl_registry_destroy(reg);
    wl_display_disconnect(display);
    return err;
}
