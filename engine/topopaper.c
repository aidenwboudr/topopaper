// topopaper — real-time GLSL topographic wallpaper: a click-through surface
// per display (layer-shell on Wayland, behind the desktop icons on Windows,
// at desktop level on macOS; see platform.h) rendering a living
// ATLAS of real terrain, from the whole planet down to a single ski hill. CPU
// load bends the motion speed and warms the contour lines; the real sun
// lights the globe and dims the night side.
//
// Part of the topopaper app (docs/ARCHITECTURE.md). The engine never touches
// the network and never forks; everything it needs arrives through files:
//   config.ini   $XDG_CONFIG_HOME/topopaper/config.ini (schema + docs in
//                topopaper/config.py), re-read on mtime change or SIGHUP
//   area packs   $XDG_DATA_HOME/topopaper/areas/<name>/ (topopaper/build/
//                area.py documents the formats), rescanned while running
//   area file    $XDG_STATE_HOME/topopaper/area: the pack on screen; write a
//                name there (`topopaper-ctl fly NAME`, `topopaper-ctl search`)
//                and the engine flies to it
//   HUD inputs   weather.txt / location.json in $XDG_CACHE_HOME/topopaper,
//                the covered flag and build progress in $XDG_RUNTIME_DIR
// `topopaper-session` supervises it (plus the compositor watcher that writes
// the covered flag and the weather fetcher). Paths follow the same rules as
// topopaper/paths.py — see resolve_paths().
//
// SMOOTHNESS DESIGN: never multiply absolute time by a varying speed — that
// teleports the pattern when speed changes. All motion is an INTEGRATED phase
// (phase += speed*dt) and uniforms are smoothed with dt-scaled EMAs.
//
// A REGISTRY of every pack (metas scanned when the areas dir changes,
// textures uploaded lazily, LRU-capped) is arranged on a LADDER by scale:
//
//   * camera lives in normalized WEB-MERCATOR (x east, y south) — centre +
//     view height. Idle = Ken-Burns breathing around the pack focus point.
//   * each frame the ladder rungs covering the view are weighted by
//     continuous laws of the camera state (sharpness yield, view overlap,
//     territory between same-scale siblings — see step_world), and the top
//     three fill the shader slots. Only packs whose bbox INTERSECTS THE VIEW
//     are candidates — same-scale SIBLING packs elsewhere on the map (Tetons
//     vs Wind Rivers) never blend in by height alone, and a cross-country
//     flight naturally picks up each area it overflies. slot0 is always the
//     DOMINANT pack.
//   * fly-to (a new name in the area file) runs a van Wijk–Nuij path to the
//     target's idle camera; a long dive naturally sweeps through every
//     intermediate rung's imagery (rungs along the way preloaded at takeoff).
//     Flights run at max(fps, 30); covered-freeze pauses them mid-air.
//   * AUTO-ROAM: after a randomized stretch of atlas idle (mean roam_minutes,
//     0 = off, battery stretches x1.6) the engine writes another pack's name
//     into the area file itself — the ordinary watcher then flies there.
//   * contours are ABSOLUTE-elevation referenced (lev = elev_m/step_m) on a
//     fractal 5^k family chosen by camera height; the contour crawl is
//     carried in METRES (uCrawlM) so lines stay aligned across scales while
//     they drift.
//   * labels carry [hmin,hmax] view-height gates + frame-edge fades.
//
// 16-bit heightmaps stay hi/lo packed in LUMINANCE_ALPHA, sampled with manual
// smoothed B-spline bicubic (C2 — bilinear's derivative jumps shimmered the
// line widths; GL_LINEAR would blend the two bytes independently = garbage).
// Contour line width is constant in SCREEN pixels via fwidth(), with
// cartographic dropping where lines would crowd below ~5px (index lines
// persist 5x steeper) — like real maps treat cliffs.
//
// No packs -> a quiet gradient until the first one lands in the areas dir.
// Build: `make` (build/topopaper); `make install` puts it on the PATH.
// Debug env: TOPA_SUN_T=<epoch> pins the sun, TOPA_DEBUG=1 traces the camera
// per frame, TOPA_NOHUD=1 drops the HUD, TOPA_POWER_DIR fakes the sysfs
// power_supply tree, TOPA_SHOT=<file.ppm> saves the first display's frame
// after TOPA_SHOT_T seconds (default 8) and exits.
#include "platform.h"
#include "themes.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>
#include <dirent.h>
#include <strings.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

volatile sig_atomic_t g_running = 1, g_hup = 0;
int g_out_dirty = 0;                // re-run output selection in the main loop
int g_out_retry = 0;                // a deliberate change: retry closed outputs
int g_covered_src = -1;             // backends that see occlusion themselves set 0/1

// ---- area pack registry + scale ladder -------------------------------------
#define PKMAX 24
#define MAXPACKS 256
#define MAXRES 24                   // resident (GPU) pack cap; LRU beyond it
struct pack {
    char   name[64];
    int    have;                    // meta.bin read ok (registry member)
    int    gl_ok;                   // textures + labels resident
    int    bad;                     // textures failed to load: off the ladder
    int    stale;                   // rebuilt on disk: re-upload textures
    double meta_mtime;              // meta.bin mtime when read
    double last_used;               // now_sec() it last sat in a slot (LRU)
    // geo (normalized mercator; y grows SOUTH)
    double mx0, my0, msx, msy;
    float  elev_lo, elev_hi, step;
    float  focus_u, focus_v;
    float  stL[6], stA[6];          // r,g,b, w0px,w1px, opacity
    GLuint ter_tex, feat_tex, atlas_tex, water_tex;
    int    ter_w, ter_h, atlas_w, atlas_h;
    int    n_lab;
    float  lab_u[PKMAX], lab_v[PKMAX], lab_hmin[PKMAX], lab_hmax[PKMAX];
    int    lab_ax[PKMAX], lab_ay[PKMAX];
    int    lab_rx[PKMAX], lab_ry[PKMAX], lab_rw[PKMAX], lab_rh[PKMAX];
    float  lab_vis[PKMAX];          // eased declutter gate (label authority)
    char   tzname[40];              // IANA tz baked in meta.bin (optional tail)
    int    tz_min; long tz_when;    // cached utc-offset minutes + stamp
};
static struct pack packs[MAXPACKS];
static int n_packs = 0;
static int lad[MAXPACKS];           // pack indices sorted by msy ASC (fine->coarse)
static int n_lad = 0;
static int p_cur = -1;              // home pack: idle camera + roam source
static int s_slotpk[3] = {-1, -1, -1};  // shader slots: 0/1 full, 2 = guarantor
static float g_lf0 = 1.0f, g_lf1 = 1.0f; // label factors: territory share (sibling law)
static double sv_cx[2], sv_cy[2], sv_hx[2], sv_hy[2];   // slot view transforms
static const int UNIT_TER[2]  = {0, 3};
static const int UNIT_FEAT[2] = {2, 4};
static const int UNIT_WAT[2]  = {5, 6};
static const int UNIT_TER2    = 7;      // guarantor slot: terrain-only
static GLuint ph_ter_tex = 0, ph_feat_tex = 0, ph_wat_tex = 0;   // 1x1 placeholders

// ---- GL program ------------------------------------------------------------
static GLuint prog, vbo;
static GLint a_pos;
static GLint u_res, u_crawlm, u_cpu, u_night, u_w;
static GLint u_texel2, u_tc2, u_ts2, u_ctr2, u_fam;
static GLint u_radpx, u_globec, u_wrap, u_sun, u_sunh, u_snow, u_aur;
// city lights (lights.bin: world cities for the globe's night side)
#define MAXLTS 8192
static float  lt_lat[MAXLTS], lt_lon[MAXLTS], lt_w[MAXLTS];
static int    g_lts_n = 0;
static GLuint ltprog = 0;
static GLint  lta_pos = -1, ltu_pts = -1;
static GLint u_texel[2], u_tc[2], u_ts[2], u_ctr[2];
static GLint u_fcl[2], u_fpl[2], u_fca[2], u_fpa[2];
static GLuint lprog;
static GLint la_pos, lu_rect, lu_uv, lu_alpha;
// theme palette uniforms (main, label and city-light programs)
static GLint u_th[12], lu_ink = -1, lu_halo = -1, ltu_city = -1;
static int   g_theme = 0, g_theme_gen = -1;   // theme_rgb row + cfg_gen applied
static double t_start;

// ---- world state (integrated phases + smoothed values) ---------------------
static double t_prev = 0.0;
static double ph_crawlm = 0.0;              // contour crawl, metres (0..6000)
static double ph_pa = 0.0, ph_pb = 2.1;     // Ken-Burns pan phases
static double ph_zm = 4.0;                  // Ken-Burns zoom phase
static float  g_cpu = 0.0f, g_night = 0.0f;
static float  cpu_target = 0.0f, night_target = 0.0f;
// live sun: ENU direction at the view centre + hillshade light (see sun_update)
static double g_sunE = -0.55, g_sunN = 0.55, g_sunU = 0.30;
static float  g_sunh[4] = {-0.55f, 0.55f, 0.62f, 0.0f};
static double g_sdec = 0.0, g_sslon = 0.0;  // subsolar point (rad), for _at()
static double sun_elev_sin(double lat, double lon);
static double g_mdt = 0.0;                  // this frame's motion dt
static double chip_next = 0.0;              // context-chip resync (1s cadence)
static double ph_aur = 0.0;                 // aurora curtain drift (integrated)
static float  g_aurf = 0.0f;                // globe-scale factor for aurora/lights
static float  g_snow = 99999.0f, g_snow_t = 99999.0f;  // snowline metres (eased)
static int    g_on_battery = 0;
static int    g_covered = 0;
static char   covered_path[512];
static double last_sys = -1.0, scan_next = 0.0;
static double areas_mtime = -2.0;           // areas dir mtime at the last rescan
static int    scan_pending = 0;             // pack dirs not settled yet: rescan again
static int    g_lad_dirty = 0;              // a pack went bad: rebuild the ladder
static unsigned long long cpu_prev_idle = 0, cpu_prev_total = 0;
static double g_ar = 1.5;                   // camera aspect (largest output)
static double g_fly_start = 0.0;            // now_sec() of the current flight's start

static char  area_path[512], areas_dir[512], lights_path[768];
static char  area_seen[64] = "";            // last area-file word acted on

// ---- in-wallpaper download progress meter (baked by the pack builder) -------
// $XDG_RUNTIME_DIR/topopaper-progress.bin: 'TOPOPRG1' | u16 w,h | (L,A) bytes.
// Present + fresh -> fade a label-styled quad in bottom-left; the builder
// heartbeats mtime through long network backoffs and unlinks when done.
static char   progress_path[512];
static GLuint g_prog_tex = 0;
static int    g_prog_w = 0, g_prog_h = 0;
static double g_prog_mseen = 0.0;
static int    g_prog_dirty = 0;
static float  g_prog_vis = 0.0f, g_prog_a = 0.0f;

// ---- HUD clock (glyph atlas baked by topopaper/build/hud.py) ---------------
// hud.bin: 'TOPOHUD1' | u16 count,cellw,cellh,baseoff | charset | (L,A) cells
// stacked vertically. Drawn through the label program (same themed ink +
// halo), screen-anchored right-centre. Glyphs crossfade RIGHT-ALIGNED on
// change (so 9:59->10:00 fades per cell, nothing jumps); the readout hides
// during flights; the colon breathes on an integrated phase. Absent hud.bin
// or TOPA_NOHUD=1 (frozen A/B rigs) -> no clock, nothing else changes.
static char   hud_path[512];
static char   tz_path[512];                 // location.json (clock timezone)
static char   g_tz_cur[64] = "";
static long   g_tz_off = LONG_MIN;
static double tz_next_check = 0.0;
static char   wx_path[512];                 // weather.txt (HUD weather rows)
static char   wx_cur[40] = "", wx_prev[40] = "";
static double wx_flip = -10.0, wx_next_check = 0.0;
static float  g_wx_vis = 0.0f;              // fades with data freshness
static int    g_wx_want = 0;
static char   wx_ncur[44] = "", wx_nprev[44] = "";  // news line (third row)
static double wx_nflip = -10.0;
static char   wx_ccur[40] = "", wx_cprev[40] = "";  // far-roam context chip
static double wx_cflip = -10.0;
static GLuint g_hud_tex = 0;
static int    g_hud_n = 0, g_hud_cw = 0, g_hud_ch = 0, g_hud_bo = 0;
static char   g_hud_set[48];
static char   hud_cur[8] = "", hud_prev[8] = "";
static int    g_hud_24 = -1;                // format of hud_cur: 1 = "14:05", 0 = "2:05P"
static double hud_flip = -10.0;
static float  g_hud_vis = 1.0f;
static double ph_hud = 0.0;                 // colon breath (integrated)

// ---- mercator camera + flight + roam ---------------------------------------
static double cam_x = 0.5, cam_y = 0.5, cam_h = 1e-3;   // centre + view height
static int    cam_init = 0;
static int    fly_active = 0;               // 0 idle, 1 flying, 2 landing blend
static int    fly_tgt = -1;                 // pack index
static double fly_t = 0.0, fly_T = 1.0;
static double f_c0x, f_c0y, f_w0, f_c1x, f_c1y, f_w1;
static double f_r0 = 0.0, f_S = 1.0, f_d = 0.0, f_ux = 0.0, f_uy = 0.0;
static int    f_pure = 0;                   // degenerate pure-zoom path
static double land_t = 0.0, land_x, land_y, land_h;
static float  g_w0v = 1.0f, g_w1v = 0.0f, g_w2v = 0.0f;   // slot weights
#define FRACTAL_BASE 30.0   // contour family anchor (12: 60m packs exact; 30: ski-site packs exact)
static double g_step_cont = 60.0;           // continuous contour step (m)
static double g_fam_s = 60.0, g_fam_t = 0.0;// 5^k family base + band phase
static int    g_scr_h = 1920;               // render pixel height (weight law)
static double g_radpx = 1.0;                // ortho sphere radius in px (per frame)
static double g_glon = 0.0, g_glat = 0.0;   // view-centre lon/lat (radians)
static char   pending_area[64] = "";
static const double RHO = 1.42;
static double g_vis = 0.0;                  // visible atlas-idle seconds
static double roam_next = -1.0;             // sampled threshold for next roam
static double roam_mean_min = 8.0;          // config roam_minutes; <=0 disables

// ---- settings: config.ini (schema + docs in topopaper/config.py) -----------
// Flat key = value lines; [section] headers and #/; comments are ignored
// (every key name is unique across sections). Re-read whenever the file's
// mtime changes (polled with the other cheap state in update_targets), so
// the settings app applies changes live. cfg_gen bumps on every load for
// consumers that need to react once (shader palette, output set, ...).
static struct {
    char   home_area[64];
    double roam_minutes, animation_speed, flight_seconds;
    char   theme[32];
    int    city_lights, aurora, labels, react_to_cpu;
    int    show_clock, clock_24h, show_weather, show_news;
    int    fps, fps_battery, fps_covered, pause_when_covered;
    char   outputs[256];                    // "all" or comma-separated names
    int    layer_background;                // 0 = BOTTOM layer, 1 = BACKGROUND
    char   weather_file[512], location_file[512];
} cfg;
static char   cfg_path[512];
static double cfg_mtime = -2.0;
static int    cfg_gen = 0;
static char   cache_dir[400];

static void cfg_defaults(void) {
    memset(&cfg, 0, sizeof cfg);
    snprintf(cfg.home_area, sizeof cfg.home_area, "earth");
    cfg.roam_minutes = 8.0; cfg.animation_speed = 1.0; cfg.flight_seconds = 20.0;
    snprintf(cfg.theme, sizeof cfg.theme, "macchiato");
    cfg.city_lights = cfg.aurora = cfg.labels = cfg.react_to_cpu = 1;
    cfg.show_clock = cfg.show_weather = cfg.show_news = 1;
    cfg.fps = 60; cfg.fps_battery = 24; cfg.fps_covered = 3;
    cfg.pause_when_covered = 1;
    snprintf(cfg.outputs, sizeof cfg.outputs, "all");
}

static int cfg_bool(const char *v) {
    return !strcasecmp(v, "true") || !strcasecmp(v, "yes") ||
           !strcasecmp(v, "on") || !strcmp(v, "1");
}

static int cfg_int(const char *v, int lo, int hi, int dflt) {
    char *e; long n = strtol(v, &e, 10);
    if (e == v) return dflt;
    return n < lo ? lo : n > hi ? hi : (int)n;
}

static void cfg_set(const char *k, const char *v) {
#define STR(name) if (!strcmp(k, #name)) { snprintf(cfg.name, sizeof cfg.name, "%s", v); return; }
#define BOOL(name) if (!strcmp(k, #name)) { cfg.name = cfg_bool(v); return; }
    STR(home_area) STR(theme) STR(outputs) STR(weather_file) STR(location_file)
    BOOL(city_lights) BOOL(aurora) BOOL(labels) BOOL(react_to_cpu)
    BOOL(show_clock) BOOL(show_weather) BOOL(show_news) BOOL(pause_when_covered)
#undef STR
#undef BOOL
    if (!strcmp(k, "roam_minutes")) { cfg.roam_minutes = atof(v); return; }
    if (!strcmp(k, "flight_seconds")) {
        double a = atof(v);
        cfg.flight_seconds = a < 2.0 ? 2.0 : a > 120.0 ? 120.0 : a;
        return;
    }
    if (!strcmp(k, "animation_speed")) {
        double a = atof(v);
        cfg.animation_speed = a < 0.0 ? 0.0 : a > 4.0 ? 4.0 : a;
        return;
    }
    if (!strcmp(k, "clock_format")) { cfg.clock_24h = !strcmp(v, "24h"); return; }
    if (!strcmp(k, "layer")) { cfg.layer_background = !strcmp(v, "background"); return; }
    if (!strcmp(k, "fps")) { cfg.fps = cfg_int(v, 1, 240, 60); return; }
    if (!strcmp(k, "fps_battery")) { cfg.fps_battery = cfg_int(v, 1, 240, 24); return; }
    if (!strcmp(k, "fps_covered")) { cfg.fps_covered = cfg_int(v, 1, 60, 3); return; }
}

static char *trim(char *s) {
    while (*s == ' ' || *s == '\t') s++;
    char *e = s + strlen(s);
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\n' || e[-1] == '\r')) *--e = 0;
    return s;
}

static void cfg_load(void) {
    cfg_defaults();
    FILE *f = fopen(cfg_path, "r");
    if (f) {
        char ln[768];
        while (fgets(ln, sizeof ln, f)) {
            char *l = trim(ln);
            if (!*l || *l == '#' || *l == ';' || *l == '[') continue;
            char *eq = strchr(l, '=');
            if (!eq) continue;
            *eq = 0;
            cfg_set(trim(l), trim(eq + 1));
        }
        fclose(f);
    }
    // HUD inputs: the session's built-in fetcher unless a file is configured
    if (!cfg.weather_file[0])
        snprintf(cfg.weather_file, sizeof cfg.weather_file, "%s/weather.txt", cache_dir);
    if (!cfg.location_file[0])
        snprintf(cfg.location_file, sizeof cfg.location_file, "%s/location.json", cache_dir);
    snprintf(wx_path, sizeof wx_path, "%s", cfg.weather_file);
    snprintf(tz_path, sizeof tz_path, "%s", cfg.location_file);
    wx_next_check = tz_next_check = 0.0;     // re-read the HUD inputs now
    cfg_gen++;
}

// Reload when the file changed (or `force`, SIGHUP). Consumers keyed on
// cfg_gen (theme) react by themselves; the output set and layer are compared
// with what the surfaces were made with and flag a re-selection.
static void cfg_poll(int force) {
    static char out_applied[256] = "\x01";
    static int  layer_applied = -1;
    double m = path_mtime(cfg_path);
    if (m == cfg_mtime && !force) return;
    cfg_mtime = m;
    cfg_load();
    fprintf(stderr, "topopaper: settings %s%s\n",
            m < 0 ? "defaults (no config.ini)" : "loaded", force ? " (SIGHUP)" : "");
    if (strcmp(out_applied, cfg.outputs) || layer_applied != cfg.layer_background) {
        if (out_applied[0] != '\x01') {
            // a deliberate change: outputs the compositor closed get a new try
            g_out_retry = 1;
        }
        snprintf(out_applied, sizeof out_applied, "%s", cfg.outputs);
        layer_applied = cfg.layer_background;
        g_out_dirty = 1;
    }
}

static double now_sec(void) { return mono_sec() - t_start; }
static double smooth01(double x) {           // smoothstep
    if (x < 0) x = 0;
    if (x > 1) x = 1;
    return x * x * (3.0 - 2.0 * x);
}
static double smoother01(double x) {         // smootherstep (C2 ends)
    if (x < 0) x = 0;
    if (x > 1) x = 1;
    return x * x * x * (x * (x * 6.0 - 15.0) + 10.0);
}

static int find_pack(const char *name) {
    for (int i = 0; i < n_packs; i++)
        if (packs[i].have && !strcmp(packs[i].name, name)) return i;
    return -1;
}
static int ladder_pos(int idx) {
    for (int k = 0; k < n_lad; k++) if (lad[k] == idx) return k;
    return -1;
}

// ---- sibling law helpers (see the ladder weights block) --------------------
#define MAXSIB 16
static double rect_ovl3(const struct pack *a, const struct pack *b,
                        double vx0, double vy0, double vx1, double vy1, double varea) {
    double x0 = a->mx0 > b->mx0 ? a->mx0 : b->mx0; if (vx0 > x0) x0 = vx0;
    double y0 = a->my0 > b->my0 ? a->my0 : b->my0; if (vy0 > y0) y0 = vy0;
    double ax1 = a->mx0 + a->msx, bx1 = b->mx0 + b->msx;
    double ay1 = a->my0 + a->msy, by1 = b->my0 + b->msy;
    double x1 = ax1 < bx1 ? ax1 : bx1; if (vx1 < x1) x1 = vx1;
    double y1 = ay1 < by1 ? ay1 : by1; if (vy1 < y1) y1 = vy1;
    return (x1 > x0 && y1 > y0) ? (x1 - x0) * (y1 - y0) / varea : 0.0;
}
// territory allocation of one scale group's take tt among its members in
// order ord[]: each member takes the view coverage the earlier ones don't
// already provide (its own overlap minus the pairwise overlaps with them)
static void alloc_terr(int n, const int *ord, const double *gov,
                       const double gpo[MAXSIB][MAXSIB], double tt, double *out) {
    for (int a = 0; a < n; a++) {
        int i = ord[a];
        double add = gov[i];
        for (int b = 0; b < a; b++) add -= gpo[i][ord[b]];
        if (add < 0.0) add = 0.0;
        out[i] = tt * add;
    }
}

static void read_area_request(void) {
    FILE *f = fopen(area_path, "r");
    if (!f) return;
    char tok[64] = "";
    // act on CHANGES only: a name with no pack behind it (yet) is reported
    // once, not 2.5x/s; a rescan that adds packs clears area_seen so a
    // request that was waiting for its pack is honoured when it lands
    if (fscanf(f, "%63s", tok) == 1 && tok[0] && strcmp(tok, area_seen)) {
        snprintf(area_seen, sizeof area_seen, "%s", tok);
        int is_cur = (p_cur >= 0) && !strcmp(tok, packs[p_cur].name);
        int is_tgt = fly_active && fly_tgt >= 0 &&
                     !strcmp(tok, packs[fly_tgt].name);
        if (!is_cur && !is_tgt)
            snprintf(pending_area, sizeof pending_area, "%s", tok);
    }
    fclose(f);
}

// ---- pack registry (metas on (re)scan; textures on demand) -----------------
static double file_mtime(const char *path) { return path_mtime(path); }

// Fills the meta fields only: a pack rebuilt in place keeps its texture
// names until the next tick frees and re-uploads them (stale).
static int pack_read_meta(struct pack *p, const char *name) {
    char path[768];
    snprintf(path, sizeof path, "%s/%s/meta.bin", areas_dir, name);
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    char m[8]; double geo[4]; float sc[5], stl[6], sta[6];
    int ok = fread(m, 1, 8, f) == 8 && !memcmp(m, "TOPOAR01", 8) &&
             fread(geo, 8, 4, f) == 4 && fread(sc, 4, 5, f) == 5 &&
             fread(stl, 4, 6, f) == 6 && fread(sta, 4, 6, f) == 6;
    char tzn[40] = "";
    if (ok && fread(tzn, 1, 32, f) != 32) tzn[0] = 0;  // optional tail
    fclose(f);
    if (!ok) { fprintf(stderr, "topopaper: bad meta.bin for '%s'\n", name); return 0; }
    snprintf(path, sizeof path, "%s/%s/terrain.bin", areas_dir, name);
    if (!path_exists(path)) return 0;
    snprintf(p->name, sizeof p->name, "%s", name);
    p->mx0 = geo[0]; p->my0 = geo[1]; p->msx = geo[2]; p->msy = geo[3];
    memcpy(p->tzname, tzn, 32); p->tzname[32] = 0;
    p->tz_when = 0;
    p->elev_lo = sc[0]; p->elev_hi = sc[1]; p->step = sc[2];
    p->focus_u = sc[3]; p->focus_v = sc[4];
    memcpy(p->stL, stl, sizeof stl); memcpy(p->stA, sta, sizeof sta);
    if (!p->gl_ok) p->ter_w = p->ter_h = 1;
    snprintf(path, sizeof path, "%s/%s/meta.bin", areas_dir, name);
    p->meta_mtime = file_mtime(path);
    p->have = 1;
    p->bad = 0;
    return 1;
}

static int cmp_msy(const void *a, const void *b) {
    double ma = packs[*(const int *)a].msy, mb = packs[*(const int *)b].msy;
    return (ma > mb) - (ma < mb);
}

// the ladder: every loadable pack, fine -> coarse
static void build_ladder(void) {
    n_lad = 0;
    for (int i = 0; i < n_packs; i++)
        if (packs[i].have && !packs[i].bad) lad[n_lad++] = i;
    qsort(lad, n_lad, sizeof(int), cmp_msy);
    g_lad_dirty = 0;
}

static int pack_in_use(int i) {
    return i == p_cur || (fly_active && i == fly_tgt) ||
           i == s_slotpk[0] || i == s_slotpk[1] || i == s_slotpk[2];
}

// Idempotent: picks up packs that appeared since the last scan (packs are
// built while we run, and a first run starts with none), notices packs
// rebuilt in place (meta.bin mtime) or deleted, and re-sorts the ladder.
// Builders write a pack's files one after another into its directory, so a
// pack is only taken once its directory and meta.bin have been quiet for a
// few seconds; until then it counts as pending and the caller rescans soon.
// Returns how many packs were added.
static int rescan_packs(void) {
    DIR *d = opendir(areas_dir);
    scan_pending = 0;
    if (!d) return 0;
    time_t now = time(NULL);
    static unsigned char seen[MAXPACKS];
    memset(seen, 0, sizeof seen);
    int added = 0;
    struct dirent *e;
    char path[768];
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.' || strlen(e->d_name) >= sizeof packs[0].name) continue;
        snprintf(path, sizeof path, "%s/%s", areas_dir, e->d_name);
        double dm = file_mtime(path);
        snprintf(path, sizeof path, "%s/%s/meta.bin", areas_dir, e->d_name);
        double mm = file_mtime(path);
        int i = find_pack(e->d_name);
        if (i >= 0) seen[i] = 1;
        if (mm < 0.0) {                      // no meta (yet): a build in progress
            if (dm >= 0.0 && difftime(now, (time_t)dm) < 600.0) scan_pending++;
            continue;
        }
        if (i >= 0 && mm == packs[i].meta_mtime) continue;
        double newest = dm > mm ? dm : mm;
        if (difftime(now, (time_t)newest) < 3.0) { scan_pending++; continue; }
        if (i >= 0) {                        // rebuilt in place
            if (pack_read_meta(&packs[i], e->d_name)) {
                packs[i].stale = packs[i].gl_ok;
                fprintf(stderr, "topopaper: pack '%s' changed on disk\n", e->d_name);
            }
            continue;
        }
        int slot = -1;                       // reuse a removed pack's slot
        for (int k = 0; k < n_packs && slot < 0; k++)
            if (!packs[k].have && !packs[k].gl_ok) slot = k;
        if (slot < 0) {
            if (n_packs >= MAXPACKS) {
                static int warned = 0;
                if (!warned++) fprintf(stderr, "topopaper: more than %d packs, "
                                       "ignoring the rest\n", MAXPACKS);
                continue;
            }
            slot = n_packs;
        }
        memset(&packs[slot], 0, sizeof packs[slot]);
        if (pack_read_meta(&packs[slot], e->d_name)) {
            fprintf(stderr, "topopaper: discovered pack '%s'\n", e->d_name);
            if (slot == n_packs) n_packs++;
            seen[slot] = 1;
            added++;
        }
    }
    closedir(d);
    for (int i = 0; i < n_packs; i++) {
        if (!packs[i].have || seen[i]) continue;
        if (pack_in_use(i)) { scan_pending++; continue; }   // drop it once we leave
        packs[i].have = 0;
        fprintf(stderr, "topopaper: pack '%s' removed\n", packs[i].name);
    }
    build_ladder();
    if (added) area_seen[0] = 0;             // a waiting fly request may be honoured
    return added;
}

static void discover_packs(void) {
    rescan_packs();
    for (int k = 0; k < n_lad; k++)
        fprintf(stderr, "topopaper: ladder[%d] %s (msy %.3e, step %.0fm)\n",
                k, packs[lad[k]].name, packs[lad[k]].msy, packs[lad[k]].step);
}

// Where the camera starts: at launch, where the last session left off (the
// area file), else home; on a first run that started empty, home first (a
// waiting area-file request then flies from there), else the coarsest pack.
static int pick_initial(int late) {
    char af[64] = "";
    FILE *f = fopen(area_path, "r");
    if (f) {
        if (fscanf(f, "%63s", af) != 1) af[0] = 0;
        fclose(f);
    }
    const char *order[2] = { late ? cfg.home_area : af, late ? af : cfg.home_area };
    for (int k = 0; k < 2; k++) {
        int i = order[k][0] ? find_pack(order[k]) : -1;
        if (i >= 0 && !packs[i].bad) return i;
    }
    return n_lad > 0 ? lad[n_lad - 1] : -1;
}

static unsigned char *read_bin(const char *path, const char *magic,
                               unsigned int *w, unsigned int *h) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    char m[8];
    if (fread(m, 1, 8, f) != 8 || memcmp(m, magic, 8) != 0 ||
        fread(w, 4, 1, f) != 1 || fread(h, 4, 1, f) != 1 ||
        *w == 0 || *h == 0 || *w > 8192 || *h > 8192) {
        fprintf(stderr, "topopaper: bad %s\n", path); fclose(f); return NULL;
    }
    size_t nb = (size_t)*w * *h * 2;
    unsigned char *buf = malloc(nb);
    if (!buf || fread(buf, 1, nb, f) != nb) {
        fprintf(stderr, "topopaper: truncated %s\n", path);
        free(buf); fclose(f); return NULL;
    }
    fclose(f);
    return buf;
}

static void upload_la(GLuint tex, int unit, int w, int h,
                      const unsigned char *data, GLint filter) {
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filter);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filter);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE_ALPHA, w, h, 0,
                 GL_LUMINANCE_ALPHA, GL_UNSIGNED_BYTE, data);
}

static void upload_l(GLuint tex, int unit, int w, int h,
                     const unsigned char *data) {
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, w, h, 0,
                 GL_LUMINANCE, GL_UNSIGNED_BYTE, data);
}

static void load_hud(void) {
    if (getenv("TOPA_NOHUD")) return;
    FILE *f = fopen(hud_path, "rb");
    if (!f) return;
    char m[8]; unsigned short n, cw, ch, bo;
    if (fread(m, 1, 8, f) == 8 && !memcmp(m, "TOPOHUD1", 8) &&
        fread(&n, 2, 1, f) == 1 && fread(&cw, 2, 1, f) == 1 &&
        fread(&ch, 2, 1, f) == 1 && fread(&bo, 2, 1, f) == 1 &&
        n > 0 && n < sizeof g_hud_set && cw > 0 && ch > 0 &&
        (int)n * (int)ch <= 8192 &&
        fread(g_hud_set, 1, n, f) == n) {
        size_t nb = (size_t)cw * (size_t)n * ch * 2;
        unsigned char *buf = malloc(nb);
        if (buf && fread(buf, 1, nb, f) == nb) {
            glGenTextures(1, &g_hud_tex);
            upload_la(g_hud_tex, 1, cw, n * ch, buf, GL_LINEAR);
            glActiveTexture(GL_TEXTURE0);
            g_hud_set[n] = 0;
            g_hud_n = n; g_hud_cw = cw; g_hud_ch = ch; g_hud_bo = bo;
            fprintf(stderr, "topopaper: hud atlas %d glyphs, cell %dx%d\n",
                    n, cw, ch);
        }
        free(buf);
    }
    fclose(f);
}

// The clock follows the LOCATION-derived timezone (location.json's "tz",
// written by the session's weather fetcher or any tool via location_file):
// the system tz can be stale on a machine that travels. Absent file ->
// system localtime, unchanged.
static void hud_tz_sync(double t) {
    if (t < tz_next_check) return;
    tz_next_check = t + 20.0;
    FILE *f = fopen(tz_path, "r");
    if (!f) return;
    char buf[512]; size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f); buf[n] = 0;
    char *k = strstr(buf, "\"tz\"");
    if (!k) return;
    k = strchr(k + 4, '"'); if (!k) return;
    char *e = strchr(k + 1, '"');
    if (!e || e == k + 1 || e - k >= (long)sizeof g_tz_cur) return;
    char tz[64]; memcpy(tz, k + 1, (size_t)(e - k - 1)); tz[e - k - 1] = 0;
    long off = LONG_MIN;                     // "utc_offset" (s): for C runtimes without IANA
    char *u = strstr(buf, "\"utc_offset\"");
    if (u && (u = strchr(u, ':'))) off = strtol(u + 1, NULL, 10);
    if (strcmp(tz, g_tz_cur) || off != g_tz_off) {
        if (strcmp(tz, g_tz_cur))
            fprintf(stderr, "topopaper: clock timezone -> %s\n", tz);
        snprintf(g_tz_cur, sizeof g_tz_cur, "%s", tz);
        g_tz_off = off;
        tz_use(tz, off);
    }
}

// Weather line: reads weather.txt (key=value, contract in
// docs/ARCHITECTURE.md), written by the session's fetcher or any tool via
// weather_file — no network here.
// Builds "34° · PARTLY CLOUDY" (latin-1, filtered to baked glyphs + space).
// Missing file or stale ts (>2 h) -> the line fades out.
static void hud_wx_sync(double t) {
    if (t < wx_next_check) return;
    wx_next_check = t + 30.0;
    long ts = 0, neta = 0, afrz = 0;
    char temp[8] = "", cond[28] = "", nraw[44] = "", apack[64] = "";
    FILE *f = fopen(wx_path, "r");
    if (f) {
        char ln[128];
        while (fgets(ln, sizeof ln, f)) {
            if (!strncmp(ln, "ts=", 3)) ts = atol(ln + 3);
            else if (!strncmp(ln, "temp=", 5)) sscanf(ln + 5, "%7[-0-9]", temp);
            else if (!strncmp(ln, "cond=", 5)) {
                snprintf(cond, sizeof cond, "%.*s", (int)sizeof cond - 1, ln + 5);
                char *e = strchr(cond, '\n'); if (e) *e = 0;
            }
            else if (!strncmp(ln, "news=", 5)) {
                snprintf(nraw, sizeof nraw, "%.*s", (int)sizeof nraw - 1, ln + 5);
                char *e = strchr(nraw, '\n'); if (e) *e = 0;
            }
            else if (!strncmp(ln, "neta=", 5)) neta = atol(ln + 5);
            else if (!strncmp(ln, "apack=", 6)) {
                snprintf(apack, sizeof apack, "%.*s", (int)sizeof apack - 1, ln + 6);
                char *e = strchr(apack, '\n'); if (e) *e = 0;
            }
            else if (!strncmp(ln, "afrz=", 5)) afrz = atol(ln + 5);
        }
        fclose(f);
    }
    // snowline: the fetcher looked up the freezing level for the CURRENT pack;
    // trust it only while we're still on that pack (roams outrun the 15-min
    // refresh — better bare than wrongly white)
    g_snow_t = 99999.0f;
    if (afrz > 0 && apack[0] && p_cur >= 0 && !strcmp(apack, packs[p_cur].name)
        && time(NULL) - ts < 7200)
        g_snow_t = (float)afrz - 300.0f;
    g_wx_want = 0;
    if (ts && temp[0] && time(NULL) - ts < 7200) {
        char ns[40];
        int j = snprintf(ns, sizeof ns, "%s\xB0 \xB7 ", temp);
        for (char *p = cond; *p && j < (int)sizeof ns - 1; p++) {
            char c = (*p >= 'a' && *p <= 'z') ? (char)(*p - 32) : *p;
            if (c == ' ' || strchr(g_hud_set, c)) ns[j++] = c;
        }
        ns[j] = 0;
        if (j > 26) ns[26] = 0;              // clamp long NWS labels
        g_wx_want = 1;
        if (strcmp(ns, wx_cur)) {
            snprintf(wx_prev, sizeof wx_prev, "%s", wx_cur);
            snprintf(wx_cur, sizeof wx_cur, "%s", ns);
            wx_flip = wx_prev[0] ? t : -10.0;
        }
    }
    // news line: '@' becomes a live countdown to neta, re-rendered every
    // sync tick so "IN 20 MIN" counts itself down between fetches.
    char nn[44] = "";
    if (nraw[0] == '@') {
        // documented form (docs/ARCHITECTURE.md): a LEADING '@' marks the
        // event, "@RAIN" -> "RAIN IN 12 MIN"; an '@' elsewhere is replaced
        // in place ("RAIN @" reads the same)
        char tmp[44];
        snprintf(tmp, sizeof tmp, "%.38s @", nraw + 1);
        snprintf(nraw, sizeof nraw, "%s", tmp);
    }
    if (g_wx_want && nraw[0]) {
        int k = 0;
        for (char *p = nraw; *p && k < (int)sizeof nn - 12; p++) {
            if (*p == '@') {
                long mins = neta ? (neta - (long)time(NULL) + 59) / 60 : 0;
                if (mins > 120) mins = 120;
                if (mins <= 0) k += snprintf(nn + k, 5, "NOW");
                else k += snprintf(nn + k, 11, "IN %ld MIN", mins);
            } else {
                char c = (*p >= 'a' && *p <= 'z') ? (char)(*p - 32) : *p;
                if (c == ' ' || strchr(g_hud_set, c)) nn[k++] = c;
            }
        }
        nn[k] = 0;
        if (k > 34) nn[34] = 0;
    }
    if (strcmp(nn, wx_ncur)) {
        snprintf(wx_nprev, sizeof wx_nprev, "%s", wx_ncur);
        snprintf(wx_ncur, sizeof wx_ncur, "%s", nn);
        wx_nflip = wx_nprev[0] ? t : -10.0;
    }
}

// The viewed pack's UTC offset in minutes: baked IANA tz name when the pack
// carries one (quarter-hour zones + DST exact, via a cached TZ-swap once an
// hour), longitude/15 solar estimate otherwise (and where the C runtime
// knows no IANA zones).
static int pack_tz_minutes(struct pack *pp) {
    time_t now = time(NULL);
    if (pp->tzname[0]) {
        if (pp->tz_when && now - pp->tz_when < 3600) return pp->tz_min;
        if (tz_offset_min(pp->tzname, now, &pp->tz_min)) {
            pp->tz_when = now;
            return pp->tz_min;
        }
    }
    double plon = (pp->mx0 + 0.5 * pp->msx - 0.5) * 2.0 * M_PI;
    return (int)floor(plon * 12.0 / M_PI + 0.5) * 60;
}

// far-roam context chip: the viewed pack's own clock, whispered, only when
// it's 3+ hours from HERE. Rebuilt every second (event-driven resync on
// arrivals via chip_next=0) so minute flips and pack changes land promptly.
static void chip_sync(double t) {
    if (t < chip_next) return;
    chip_next = t + 1.0;
    char cc[40] = "";
    if (p_cur >= 0 && packs[p_cur].have && packs[p_cur].msx < 0.999) {
        struct pack *pp = &packs[p_cur];
        int offm = pack_tz_minutes(pp);
        time_t nowt = time(NULL);
        int dd = offm - local_offset_min(nowt);
        if (dd > 720) dd -= 1440;
        if (dd < -720) dd += 1440;
        if (dd >= 180 || dd <= -180) {
            time_t pt = nowt + (time_t)offm * 60;
            struct tm pm; utc_tm(pt, &pm);
            int h12 = pm.tm_hour % 12; if (!h12) h12 = 12;
            char nm[20]; int i = 0;
            for (const char *s = pp->name; *s && i < 16; s++) {
                if (!strncmp(s, "-region", 7)) break;
                char c = (*s >= 'a' && *s <= 'z') ? (char)(*s - 32)
                       : (*s == '-') ? ' ' : *s;
                if (c == ' ' || strchr(g_hud_set, c)) nm[i++] = c;
            }
            nm[i] = 0;
            if (cfg.clock_24h)
                snprintf(cc, sizeof cc, "%s \xB7 %02d:%02d", nm, pm.tm_hour, pm.tm_min);
            else
                snprintf(cc, sizeof cc, "%s \xB7 %d:%02d %s", nm, h12, pm.tm_min,
                         pm.tm_hour < 12 ? "AM" : "PM");
        }
    }
    if (strcmp(cc, wx_ccur)) {
        snprintf(wx_cprev, sizeof wx_cprev, "%s", wx_ccur);
        snprintf(wx_ccur, sizeof wx_ccur, "%s", cc);
        wx_cflip = wx_cprev[0] ? t : -10.0;
    }
}

// Load a pack's textures + labels (GL context must be current). Resident once
// loaded — the ladder rebinds freely without re-uploading — until the LRU
// cap (packs_gl_maint) evicts it.
static int ensure_pack_gl(int idx) {
    struct pack *p = &packs[idx];
    if (!p->have || p->bad) return 0;
    p->last_used = now_sec();
    if (p->gl_ok) return 1;
    char path[768];
    snprintf(path, sizeof path, "%s/%s/terrain.bin", areas_dir, p->name);
    unsigned int tw, th;
    unsigned char *ter = read_bin(path, "TOPOTER1", &tw, &th);
    if (!ter) {
        fprintf(stderr, "topopaper: pack '%s' has no usable terrain.bin, skipping it\n",
                p->name);
        p->bad = 1;
        g_lad_dirty = 1;
        return 0;
    }
    glGenTextures(1, &p->ter_tex);
    upload_la(p->ter_tex, UNIT_TER[0], tw, th, ter, GL_NEAREST);
    free(ter);
    p->ter_w = (int)tw; p->ter_h = (int)th;

    glGenTextures(1, &p->feat_tex);
    snprintf(path, sizeof path, "%s/%s/features.bin", areas_dir, p->name);
    unsigned int fw, fh;
    unsigned char *feat = read_bin(path, "TOPORDS1", &fw, &fh);
    if (feat) {
        upload_la(p->feat_tex, UNIT_FEAT[0], fw, fh, feat, GL_LINEAR);
        free(feat);
    } else {
        unsigned char far2[2] = {255, 255};
        upload_la(p->feat_tex, UNIT_FEAT[0], 1, 1, far2, GL_LINEAR);
        p->stL[5] = p->stA[5] = 0.0f;
    }

    // water.bin: optional signed shore-distance (128 = shoreline, <128 water)
    snprintf(path, sizeof path, "%s/%s/water.bin", areas_dir, p->name);
    FILE *wf = fopen(path, "rb");
    if (wf) {
        char wm[8]; unsigned int ww, wh;
        if (fread(wm, 1, 8, wf) == 8 && !memcmp(wm, "TOPOWTR1", 8) &&
            fread(&ww, 4, 1, wf) == 1 && fread(&wh, 4, 1, wf) == 1 &&
            ww > 0 && wh > 0 && ww <= 8192 && wh <= 8192) {
            size_t nb = (size_t)ww * wh;
            unsigned char *wb = malloc(nb);
            if (wb && fread(wb, 1, nb, wf) == nb) {
                glGenTextures(1, &p->water_tex);
                upload_l(p->water_tex, UNIT_WAT[0], (int)ww, (int)wh, wb);
            }
            free(wb);
        }
        fclose(wf);
    }

    p->n_lab = 0;
    snprintf(path, sizeof path, "%s/%s/labels.bin", areas_dir, p->name);
    FILE *f = fopen(path, "rb");
    if (f) {
        char pm[8]; unsigned int cnt, aw, ah; int v2 = 0;
        if (fread(pm, 1, 8, f) == 8 &&
            (!memcmp(pm, "TOPOPKS2", 8) || !memcmp(pm, "TOPOPKS1", 8)) &&
            fread(&cnt, 4, 1, f) == 1 && fread(&aw, 4, 1, f) == 1 &&
            fread(&ah, 4, 1, f) == 1 &&
            cnt > 0 && aw > 0 && ah > 0 && aw <= 4096 && ah <= 16384) {
            v2 = !memcmp(pm, "TOPOPKS2", 8);
            // over-budget packs keep their first PKMAX labels (bake order is
            // peaks, places, water — the ranked head), not zero (a pack
            // baked with 283 labels once drew none at all)
            unsigned int keep = cnt <= PKMAX ? cnt : PKMAX;
            int ok = 1;
            for (unsigned int i = 0; i < cnt && ok; i++) {
                float uv[2], hg[2] = {0.0f, 1e9f}; unsigned short q[6];
                ok = fread(uv, 4, 2, f) == 2 &&
                     (!v2 || fread(hg, 4, 2, f) == 2) &&
                     fread(q, 2, 6, f) == 6;
                if (ok && i < keep) {
                    p->lab_u[i] = uv[0]; p->lab_v[i] = uv[1];
                    p->lab_hmin[i] = hg[0]; p->lab_hmax[i] = hg[1];
                    p->lab_ax[i] = q[0]; p->lab_ay[i] = q[1];
                    p->lab_rx[i] = q[2]; p->lab_ry[i] = q[3];
                    p->lab_rw[i] = q[4]; p->lab_rh[i] = q[5];
                }
            }
            if (ok) {
                size_t nb = (size_t)aw * ah * 2;
                unsigned char *atl = malloc(nb);
                if (atl && fread(atl, 1, nb, f) == nb) {
                    glGenTextures(1, &p->atlas_tex);
                    upload_la(p->atlas_tex, 1, aw, ah, atl, GL_LINEAR);
                    p->atlas_w = (int)aw; p->atlas_h = (int)ah;
                    p->n_lab = (int)keep;
                    if (keep < cnt)
                        fprintf(stderr, "topopaper: '%s' labels capped %u -> %u\n",
                                p->name, cnt, keep);
                }
                free(atl);
            }
        }
        fclose(f);
    }
    if (p->msx > 0.999 && (p->ter_w & (p->ter_w - 1)) == 0) {
        // full-world pack: wrap horizontally so bicubic taps cross the date
        // line instead of clamping into a seam column at 180° (visible now
        // that Pacific departures centre the seam on screen)
        GLuint ts[3] = { p->ter_tex, p->feat_tex, p->water_tex };
        for (int i = 0; i < 3; i++) if (ts[i]) {
            glBindTexture(GL_TEXTURE_2D, ts[i]);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT);
        }
    }
    glActiveTexture(GL_TEXTURE0);
    p->gl_ok = 1;
    fprintf(stderr, "topopaper: resident '%s' %dx%d step %.0fm %d labels\n",
            p->name, p->ter_w, p->ter_h, p->step, p->n_lab);
    return 1;
}

static void pack_gl_free(int idx) {
    struct pack *p = &packs[idx];
    if (!p->gl_ok) return;
    GLuint t[4] = { p->ter_tex, p->feat_tex, p->atlas_tex, p->water_tex };
    for (int i = 0; i < 4; i++) if (t[i]) glDeleteTextures(1, &t[i]);
    p->ter_tex = p->feat_tex = p->atlas_tex = p->water_tex = 0;
    p->n_lab = 0;
    memset(p->lab_vis, 0, sizeof p->lab_vis);
    p->gl_ok = 0;
}

// Once per tick (GL current): re-upload packs rebuilt on disk, release the
// textures of deleted packs, and hold the resident set to MAXRES by evicting
// the least recently slotted packs. Anything on screen, the current pack,
// the flight target and everything preloaded for the current flight stay.
static void packs_gl_maint(void) {
    double now = now_sec();
    int res = 0;
    for (int i = 0; i < n_packs; i++) {
        struct pack *p = &packs[i];
        if (!p->gl_ok) continue;
        if (p->stale && p->have) {
            p->stale = 0;
            pack_gl_free(i);
            if (pack_in_use(i)) ensure_pack_gl(i);
        } else if (!p->have && !pack_in_use(i)) {
            pack_gl_free(i);
        }
        if (p->gl_ok) res++;
    }
    while (res > MAXRES) {
        int lru = -1;
        for (int i = 0; i < n_packs; i++) {
            struct pack *p = &packs[i];
            if (!p->gl_ok || pack_in_use(i) || now - p->last_used < 5.0) continue;
            if (fly_active && p->last_used >= g_fly_start) continue;
            if (lru < 0 || p->last_used < packs[lru].last_used) lru = i;
        }
        if (lru < 0) break;                  // everything is busy: allow overshoot
        fprintf(stderr, "topopaper: evicted '%s' (resident cap %d)\n",
                packs[lru].name, MAXRES);
        pack_gl_free(lru);
        res--;
    }
}

// ---- camera ----------------------------------------------------------------
static void idle_cam(int idx, double *ox, double *oy, double *oh) {
    struct pack *s = &packs[idx];
    if (s->msy > 0.9) {
        // the EARTH: idle is a slow rotation (ph_pa IS the spin — integrated,
        // so it composes with cpu speed and covered-freeze like everything),
        // a gentle latitude drift, and breathing kept inside the full-globe
        // band (uGlobe saturates below ch 0.46).
        double ch = (0.55 + 0.08 * sin(ph_zm)) * s->msy;
        *ox = s->mx0 + s->msx * (ph_pa / (2.0 * M_PI));
        *oy = s->my0 + s->msy * (0.5 + 0.17 * sin(ph_pb));
        *oh = ch;
        return;
    }
    double zt = 0.48 + 0.14 * sin(ph_zm);
    double ch = zt * s->msy;
    double maxh = 0.96 * s->msx / g_ar;
    if (ch > maxh) ch = maxh;
    double hx = ch * g_ar * 0.5, hy = ch * 0.5;
    double fx = s->msx * 0.5 - hx; if (fx < 0) fx = 0;
    double fy = s->msy * 0.5 - hy; if (fy < 0) fy = 0;
    // wander stays within ~half a view of the focus point: packs much larger
    // than their subject (a ski area) must not drift off into empty ridgeline
    if (fx > 0.55 * ch) fx = 0.55 * ch;
    if (fy > 0.55 * ch) fy = 0.55 * ch;
    double cx = s->mx0 + s->focus_u * s->msx + fx * 0.92 * sin(ph_pa);
    double cy = s->my0 + (1.0 - s->focus_v) * s->msy + fy * 0.92 * sin(ph_pb);
    double lo = s->mx0 + hx, hi = s->mx0 + s->msx - hx;
    if (lo > hi) cx = s->mx0 + s->msx * 0.5;
    else { if (cx < lo) cx = lo; if (cx > hi) cx = hi; }
    lo = s->my0 + hy; hi = s->my0 + s->msy - hy;
    if (lo > hi) cy = s->my0 + s->msy * 0.5;
    else { if (cy < lo) cy = lo; if (cy > hi) cy = hi; }
    *ox = cx; *oy = cy; *oh = ch;
}

// van Wijk & Nuij "Smooth and efficient zooming and panning" (2003):
// the optimal pan+zoom path between two camera rects, parameterized by s.
static void start_flight(int tgt) {
    fly_tgt = tgt;
    g_fly_start = now_sec();
    f_c0x = cam_x; f_c0y = cam_y; f_w0 = cam_h;
    if (packs[tgt].msy > 0.9) {
        // zooming out IS staying put on a sphere: seed the spin phases from
        // the departure point so the planet arrives over where you left,
        // then the idle rotation carries on from there
        struct pack *e = &packs[tgt];
        double u = (cam_x - e->mx0) / e->msx;
        ph_pa = 2.0 * M_PI * (u - floor(u));
        double s = ((cam_y - e->my0) / e->msy - 0.5) / 0.17;
        if (s >  1.0) s =  1.0;
        if (s < -1.0) s = -1.0;
        ph_pb = asin(s);
    }
    idle_cam(tgt, &f_c1x, &f_c1y, &f_w1);
    {
        // every rung between here and there that lies along the way renders
        // mid-dive — make those resident up front (the corridor test keeps a
        // big registry from uploading same-scale packs on other continents;
        // anything missed still loads on demand when it enters the view)
        int a = ladder_pos(p_cur), b = ladder_pos(tgt);
        if (a < 0) a = b;
        int klo = a < b ? a : b, khi = a > b ? a : b, n = 0;
        double m = 0.5 * (f_w0 > f_w1 ? f_w0 : f_w1) * g_ar;
        double rx0 = (f_c0x < f_c1x ? f_c0x : f_c1x) - m;
        double rx1 = (f_c0x > f_c1x ? f_c0x : f_c1x) + m;
        double ry0 = (f_c0y < f_c1y ? f_c0y : f_c1y) - m;
        double ry1 = (f_c0y > f_c1y ? f_c0y : f_c1y) + m;
        for (int k = klo; k >= 0 && k <= khi && n < MAXRES - 4; k++) {
            struct pack *p = &packs[lad[k]];
            if (p->mx0 > rx1 || p->mx0 + p->msx < rx0 ||
                p->my0 > ry1 || p->my0 + p->msy < ry0) continue;
            if (ensure_pack_gl(lad[k])) n++;
        }
    }
    double dx = f_c1x - f_c0x, dy = f_c1y - f_c0y;
    f_d = sqrt(dx * dx + dy * dy);
    if (f_d < 1e-12) {
        f_pure = 1;
        f_S = fabs(log(f_w1 / f_w0)) / RHO;
        if (f_S < 1e-6) f_S = 1e-6;
        f_ux = 0; f_uy = 0;
    } else {
        f_pure = 0;
        f_ux = dx / f_d; f_uy = dy / f_d;
        double r4 = RHO * RHO * RHO * RHO;
        double b0 = (f_w1 * f_w1 - f_w0 * f_w0 + r4 * f_d * f_d) /
                    (2.0 * f_w0 * RHO * RHO * f_d);
        double b1 = (f_w1 * f_w1 - f_w0 * f_w0 - r4 * f_d * f_d) /
                    (2.0 * f_w1 * RHO * RHO * f_d);
        double r1;
        f_r0 = log(-b0 + sqrt(b0 * b0 + 1.0));
        r1   = log(-b1 + sqrt(b1 * b1 + 1.0));
        f_S = (r1 - f_r0) / RHO;
    }
    // Duration follows the configured flight length. The van Wijk path effort
    // |f_S| sizes each trip within that: a long globe-to-valley descent takes
    // about the full flight_seconds, shorter hops proportionally less, with a
    // floor so a tiny re-centre never crawls (and never snaps).
    double frac = fabs(f_S) / 2.3;           // 2.3 ~ a globe-scale descent
    if (frac > 1.0) frac = 1.0;
    if (frac < 0.3) frac = 0.3;
    fly_T = cfg.flight_seconds * frac;
    if (fly_T < 1.0) fly_T = 1.0;
    fly_t = 0.0;
    fly_active = 1;
    g_vis = 0.0; roam_next = -1.0;           // roam clock restarts per flight
    fprintf(stderr, "topopaper: flight %s -> %s  S=%.2f T=%.1fs\n",
            packs[p_cur].name, packs[tgt].name, f_S, fly_T);
}

static void flight_cam(double s, double *ox, double *oy, double *oh) {
    if (f_pure) {
        double dir = (f_w1 > f_w0) ? 1.0 : -1.0;
        *oh = f_w0 * exp(dir * RHO * s);
        *ox = f_c0x; *oy = f_c0y;
        return;
    }
    double rs = RHO * s + f_r0;
    double u = (f_w0 / (RHO * RHO)) * (cosh(f_r0) * tanh(rs) - sinh(f_r0));
    *oh = f_w0 * cosh(f_r0) / cosh(rs);
    *ox = f_c0x + f_ux * u;
    *oy = f_c0y + f_uy * u;
}

// ---- auto-roam --------------------------------------------------------------
static void maybe_roam(double dt) {
    if (roam_mean_min <= 0.0 || n_lad < 2 || p_cur < 0) return;
    if (fly_active || pending_area[0]) return;
    g_vis += dt;    // covered time COUNTS: tab back and be somewhere new
    if (roam_next < 0.0) {
        double r = rand() / (double)RAND_MAX;              // 0.6..1.4 x mean
        roam_next = 60.0 * roam_mean_min * (0.6 + 0.8 * r)
                    * (g_on_battery ? 1.6 : 1.0);
    }
    if (g_vis < roam_next) return;
    // pick a destination: adjacent rungs weighted 3x, everything else 1x —
    // then CHASE THE LIGHT: scale by the sun at each pack (golden hour and
    // twilight pull hardest, deep night repels; earth is always a fine stop)
    int pos = ladder_pos(p_cur);
    int cand[MAXPACKS], nc = 0;
    double wt[MAXPACKS], wsum = 0.0;
    for (int k = 0; k < n_lad; k++) {
        if (lad[k] == p_cur) continue;
        struct pack *pp = &packs[lad[k]];
        double b = (k == pos - 1 || k == pos + 1) ? 3.0 : 1.0;
        double sw = 1.5;                         // full-world: always half-day
        if (pp->msx < 0.999) {
            double plat = atan(sinh(M_PI * (1.0 - 2.0 * (pp->my0 + 0.5 * pp->msy))));
            double plon = (pp->mx0 + 0.5 * pp->msx - 0.5) * 2.0 * M_PI;
            double el = asin(sun_elev_sin(plat, plon)) * 180.0 / M_PI;
            sw = el < -8.0 ? 0.35 : el < 0.0 ? 2.2
               : el <= 18.0 ? 3.0 : el <= 40.0 ? 1.4 : 1.0;
        }
        cand[nc] = lad[k]; wt[nc] = b * sw; wsum += wt[nc]; nc++;
    }
    if (!nc || wsum <= 0.0) return;
    double r = (rand() / (double)RAND_MAX) * wsum;
    int tgt = cand[0];
    for (int i = 0; i < nc; i++) { if (r < wt[i]) { tgt = cand[i]; break; } r -= wt[i]; }
    if (g_covered) {
        // nobody's watching: skip the flight, just BE there (the ensure_pack
        // upload also happens behind cover, so the reveal costs nothing)
        if (!ensure_pack_gl(tgt)) return;
        p_cur = tgt;
        idle_cam(tgt, &cam_x, &cam_y, &cam_h);
        s_slotpk[0] = s_slotpk[1] = tgt;
        FILE *f = fopen(area_path, "w");
        if (f) { fprintf(f, "%s\n", packs[tgt].name); fclose(f); }
        fprintf(stderr, "topopaper: roam (covered cut) -> %s\n", packs[tgt].name);
        chip_next = 0.0;                     // context chip resyncs on teleport
    } else {
        FILE *f = fopen(area_path, "w");
        if (f) { fprintf(f, "%s\n", packs[tgt].name); fclose(f); }
        fprintf(stderr, "topopaper: roam -> %s\n", packs[tgt].name);
    }
    g_vis = 0.0; roam_next = -1.0;
}

// ---- power source ------------------------------------------------------------
// compat.c's on_battery() decides (sysfs, IOKit or GetSystemPowerStatus).
static void power_scan(void) {
    int ob = on_battery();
    if (ob != g_on_battery)
        fprintf(stderr, "topopaper: power -> %s\n", ob ? "battery" : "AC");
    g_on_battery = ob;
}

// Update raw targets ~2.5x/sec (cheap CPU-time + file reads). Runs from
// the main loop whether or not anything is drawn, so settings and fly
// requests are seen even with every output deselected.
static void update_targets(double now) {
    if (last_sys >= 0 && now - last_sys < 0.4) return;
    last_sys = now;
    unsigned long long idle, total;
    if (cpu_times(&idle, &total)) {
        unsigned long long di = idle - cpu_prev_idle, dt = total - cpu_prev_total;
        if (cpu_prev_total && dt) {
            float cpu = 1.0f - (float)di / (float)dt;
            if (cpu < 0.0f) cpu = 0.0f;
            if (cpu > 1.0f) cpu = 1.0f;
            cpu_target = cpu;
        }
        cpu_prev_idle = idle; cpu_prev_total = total;
    }
    cfg_poll(0);
    if (!cfg.react_to_cpu) cpu_target = 0.0f;   // g_cpu glides out: no boost, no tint
    read_area_request();
    double rm = cfg.roam_minutes;
    if (rm != roam_mean_min) {               // mean changed -> resample fresh
        roam_mean_min = rm;
        roam_next = -1.0;
        g_vis = 0.0;
    }
    if (now >= scan_next) {                  // slower lane: dir scans
        scan_next = now + 2.0;
        double am = file_mtime(areas_dir);
        if (am != areas_mtime || scan_pending) {
            areas_mtime = am;
            rescan_packs();
        }
        power_scan();
    }
    FILE *cf = g_covered_src >= 0 ? NULL : fopen(covered_path, "r");  // the watcher's
    if (g_covered_src >= 0) {
        g_covered = g_covered_src;           // the backend sees occlusion itself
    } else if (cf) {
        int ch = fgetc(cf);
        g_covered = (ch == '1');
        fclose(cf);
    } else {
        g_covered = 0;                       // no watcher: never assume covered
    }
    double pm = path_mtime(progress_path);  // download meter presence + updates
    if (pm >= 0.0 && difftime(time(NULL), (time_t)pm) < 45.0) {
        double m = floor(pm);
        if (m != g_prog_mseen) { g_prog_mseen = m; g_prog_dirty = 1; }
        g_prog_vis = 1.0f;
    } else {
        g_prog_vis = 0.0f;
    }
}

// ---- live sun --------------------------------------------------------------
// True solar position (subsolar point from UTC, ~0.01 deg class), expressed
// in the view-centre's east/north/up frame — the SAME frame the orthographic
// sphere is drawn in, so the shader's dot(spherePos, uSun) is the exact
// cosine of solar zenith at every fragment (the terminator). Also derives:
// night_target (civil-dusk ramp on true elevation — replaces the old wall-
// clock hour buckets) and the hillshade light. The hillshade azimuth is the
// sun's, MIRRORED into the northern half-plane (south light inverts human
// relief perception); elevation clamped [12,55] deg so dawn rakes long and
// noon flattens without ever killing the relief; a warmth factor peaks at
// golden hour. TOPA_SUN_T=<epoch> pins the sun for rigs/screenshots.
static void sun_update(void) {
    const char *ov = getenv("TOPA_SUN_T");
    double ut = ov ? atof(ov) : (double)time(NULL);
    double n = ut / 86400.0 - 10957.5;              // days since J2000
    double d2r = M_PI / 180.0;
    double ma = fmod(357.528 + 0.9856003 * n, 360.0) * d2r;
    double lam = fmod(280.460 + 0.9856474 * n
                      + 1.915 * sin(ma) + 0.020 * sin(2.0 * ma), 360.0) * d2r;
    double eps = (23.439 - 4.0e-7 * n) * d2r;
    double dec = asin(sin(eps) * sin(lam));
    double ra  = atan2(cos(eps) * sin(lam), cos(lam));
    double gmst = fmod(280.46061837 + 360.98564736629 * n, 360.0) * d2r;
    g_sdec = dec; g_sslon = ra - gmst;      // subsolar point, kept for _at()
    double dlon = (ra - gmst) - g_glon;
    double cs = cos(dec), ss = sin(dec);
    double cl = cos(g_glat), sl = sin(g_glat);
    g_sunE = cs * sin(dlon);
    g_sunN = cl * ss - sl * cs * cos(dlon);
    g_sunU = sl * ss + cl * cs * cos(dlon);
    double su = g_sunU < -1.0 ? -1.0 : (g_sunU > 1.0 ? 1.0 : g_sunU);
    double el = asin(su) / d2r;                     // true elevation, deg
    float nt = (float)((6.0 - el) / 16.0);          // 0 @ +6deg .. 1 @ -10deg
    night_target = nt < 0.0f ? 0.0f : (nt > 1.0f ? 1.0f : nt);
    double hx = g_sunE, hy = g_sunN;
    double hl = sqrt(hx * hx + hy * hy);
    if (hl < 1e-6) { hx = 0.0; hy = 1.0; hl = 1.0; }
    hx /= hl; hy = fabs(hy) / hl;
    double esh = el < 12.0 ? 12.0 : (el > 55.0 ? 55.0 : el);
    double ce = cos(esh * d2r), se = sin(esh * d2r);
    double warm = 1.0 - fabs(el - 2.0) / (el > 2.0 ? 12.0 : 8.0);
    if (warm < 0.0) warm = 0.0;
    g_sunh[0] = (float)(hx * ce); g_sunh[1] = (float)(hy * ce);
    g_sunh[2] = (float)se;        g_sunh[3] = (float)warm;
}

// sine of sun elevation at an arbitrary point (uses the cached subsolar point)
static double sun_elev_sin(double lat, double lon) {
    return sin(g_sdec) * sin(lat)
         + cos(g_sdec) * cos(lat) * cos(g_sslon - lon);
}

// ---- shaders ---------------------------------------------------------------
static const char *VERT =
    "attribute vec2 pos;\n"
    "void main(){ gl_Position = vec4(pos, 0.0, 1.0); }\n";

// All motion arrives as pre-integrated, bounded uniforms — no uTime anywhere.
static const char *FRAG =
    "#extension GL_OES_standard_derivatives : enable\n"
    "precision highp float;\n"
    "uniform vec2 uRes;\n"
    "uniform float uCrawlM, uCpu, uNight;\n"
    "uniform vec2 uFam;\n"    // fractal contour family: base step s (m), band phase t
    "uniform vec3 uW;\n"                     // slot weights (telescoped, sum<=1)
    "uniform float uRadPx;\n"     // ortho sphere radius in px = res.y/(ch*2pi*cos(lat0))
    "uniform vec2 uGlobeC;\n"                // view-centre lon,lat (radians)
    "uniform vec3 uSun;\n"                   // sun dir, view-centre ENU frame
    "uniform vec4 uSunH;\n"                  // hillshade light xyz + golden warmth
    "uniform float uSnow;\n"                 // snowline metres ASL (99999 = off)
    "uniform vec3 uAur;\n"                   // aurora: globe factor, drift phase, on
    // theme palette (engine/themes.h), pushed whenever the theme changes
    "uniform vec3 uBgLo, uBgHi, uLnLo, uLnHi, uWatC, uShoreC, uSnowC, uWarmC;\n"
    "uniform vec3 uRimC, uIceC, uAurA, uAurB;\n"
    "uniform vec3 uWrap;\n"                  // per-slot: 1 = full-world pack, wrap u
    "uniform sampler2D uTer0, uFeat0, uTer1, uFeat1, uWat0, uWat1, uTer2;\n"
    "uniform vec2 uTexel0, uTC0, uTS0;\n"
    "uniform vec2 uTexel1, uTC1, uTS1;\n"
    "uniform vec2 uTexel2, uTC2, uTS2;\n"
    "uniform vec4 uCtr0, uCtr1, uCtr2;\n"   // x levScale, y levOff, z 1/step, w px-per-texel
    "uniform vec3 uFcL0, uFpL0, uFcA0, uFpA0;\n"  // colour rgb | w0,w1,opacity
    "uniform vec3 uFcL1, uFpL1, uFcA1, uFpA1;\n"
    // ---- 16-bit heightmap sampling (hi in .r, lo in .a) --------------------
    "float terS(sampler2D t, vec2 uv){ vec4 s=texture2D(t,uv); return s.r*0.9961090+s.a*0.0038911; }\n"
    // plain bilinear — used only for the hillshade gradient (broad soft signal)
    "float terBil(sampler2D t, vec2 texel, vec2 uv){\n"
    "  vec2 st=uv/texel-0.5; vec2 i=floor(st); vec2 f=st-i;\n"
    "  vec2 b=(i+0.5)*texel;\n"
    "  return mix(mix(terS(t,b),terS(t,b+vec2(texel.x,0.)),f.x),\n"
    "             mix(terS(t,b+vec2(0.,texel.y)),terS(t,b+texel),f.x),f.y); }\n"
    // plain B-spline bicubic on a single .r channel — used for the water
    // SDF when a coarse pack rides far above its native scale (bilinear
    // texel diamonds read as staircase coasts under ~10x magnification)
    "float watBic(sampler2D t, vec2 texel, vec2 uv){\n"
    "  vec2 st=uv/texel-0.5; vec2 ic=floor(st); vec2 f=st-ic;\n"
    "  vec2 f2=f*f; vec2 f3=f2*f;\n"
    "  vec4 wx=vec4(1.0-3.0*f.x+3.0*f2.x-f3.x, 4.0-6.0*f2.x+3.0*f3.x,\n"
    "               1.0+3.0*f.x+3.0*f2.x-3.0*f3.x, f3.x)/6.0;\n"
    "  vec4 wy=vec4(1.0-3.0*f.y+3.0*f2.y-f3.y, 4.0-6.0*f2.y+3.0*f3.y,\n"
    "               1.0+3.0*f.y+3.0*f2.y-3.0*f3.y, f3.y)/6.0;\n"
    "  vec2 b=(ic+0.5)*texel;\n"
    "  float s=0.0;\n"
    "  for(int j=-1;j<=2;j++){\n"
    "    float rv=0.0;\n"
    "    for(int i=-1;i<=2;i++){\n"
    "      float w=(i==-1)?wx.x:((i==0)?wx.y:((i==1)?wx.z:wx.w));\n"
    "      rv+=w*texture2D(t,b+vec2(float(i),float(j))*texel).r;\n"
    "    }\n"
    "    float wj=(j==-1)?wy.x:((j==0)?wy.y:((j==1)?wy.z:wy.w));\n"
    "    s+=wj*rv;\n"
    "  }\n"
    "  return s; }\n"
    // B-spline bicubic (16-tap): C2-continuous, so fwidth(lev) is SMOOTH.
    "float terBic(sampler2D t, vec2 texel, vec2 uv){\n"
    "  vec2 st=uv/texel-0.5; vec2 ic=floor(st); vec2 f=st-ic;\n"
    "  vec2 f2=f*f; vec2 f3=f2*f;\n"
    "  vec4 wx=vec4(1.0-3.0*f.x+3.0*f2.x-f3.x, 4.0-6.0*f2.x+3.0*f3.x,\n"
    "               1.0+3.0*f.x+3.0*f2.x-3.0*f3.x, f3.x)/6.0;\n"
    "  vec4 wy=vec4(1.0-3.0*f.y+3.0*f2.y-f3.y, 4.0-6.0*f2.y+3.0*f3.y,\n"
    "               1.0+3.0*f.y+3.0*f2.y-3.0*f3.y, f3.y)/6.0;\n"
    "  vec2 b=(ic+0.5)*texel;\n"
    "  float s=0.0;\n"
    "  for(int j=-1;j<=2;j++){\n"
    "    float rv=0.0;\n"
    "    for(int i=-1;i<=2;i++){\n"
    "      float w=(i==-1)?wx.x:((i==0)?wx.y:((i==1)?wx.z:wx.w));\n"
    "      rv+=w*terS(t,b+vec2(float(i),float(j))*texel);\n"
    "    }\n"
    "    float wj=(j==-1)?wy.x:((j==0)?wy.y:((j==1)?wy.z:wy.w));\n"
    "    s+=wj*rv;\n"
    "  }\n"
    "  return s; }\n"
    // ONE projection at every height: orthographic unit sphere, scale-matched
    // so the centre ground scale equals the old planar mapping exactly (first
    // order; mercator is conformal, so a sphere seen up close IS the flat
    // map). Curvature is never blended in — it is simply visible or not.
    // Offsets are computed in stable small-forms (w=rr2/(1+z), log of a
    // near-1 ratio): fp32 worst error 0.08 texel at ski-site zoom (validated).
    // coverage: 1 inside the pack, feathering to 0 at the bbox edge over a
    // SCREEN-anchored margin (~80px). A texel-anchored margin is invisible
    // once the view outsizes the pack — the bbox edge then reads as a hard
    // LOD wall against the coarse rung (the tetons west-edge chunk). Clamped
    // low so a deep overhang can never feather a sole cover provider to zero.
    "float slotCov(vec2 uv, vec2 texel){\n"
    "  vec2 m=clamp(fwidth(uv)*80.0, texel*2.5, vec2(0.2));\n"
    "  vec2 f=max(max(m-uv, uv-(1.0-m)), vec2(0.0))/m;\n"
    "  float d=max(f.x, f.y);\n"
    "  return 1.0-smoothstep(0.0, 1.0, d); }\n"
    // one contour family: line intensity at step s for absolute metres hm,
    // with the fwidth anti-moiré damper doubling as the data-quality floor
    // (families too fine for a slot's texels self-suppress)
    "float lineAt(float hm, float s){\n"
    "  float lev=hm/s;\n"
    "  float g=max(fwidth(lev), 1e-4);\n"
    "  float d=abs(fract(lev)-0.5);\n"
    "  float w=0.6*g;\n"
    "  float l=1.0-smoothstep(w, w+g, d);\n"
    "  return l*(1.0-smoothstep(0.18, 0.42, g)); }\n"
    // ---- full real-terrain colour for one slot (uv-space core) -------------
    "vec3 slotRealUV(vec2 uv, sampler2D ter, sampler2D feat, sampler2D wat,\n"
    "                vec2 texel, vec4 ctr,\n"
    "                vec3 fcL, vec3 fpL, vec3 fcA, vec3 fpA){\n"
    "  float h=terBic(ter, texel, uv);\n"
    "  float hm=ctr.x+h*ctr.y+uCrawlM;\n"    // absolute metres + global crawl
    // FRACTAL FAMILIES: fine=s fades with band phase t; mid=5s morphs
    // major->minor as t rises (zoom-out demotes 4/5 of the thick lines);
    // super=25s stays major. Promotions are exact: lines are absolute-
    // elevation anchored — they change rank, never position.
    // NO per-slot quality floor: gating the fine family
    // by each pack's own step turned bbox edges into contour-density WALLS
    // in the height bands where neighbours disagree (rockies 120m vs conus
    // 240m). lineAt's fwidth damper already suppresses unsupported density,
    // and absolute-elevation anchoring keeps cross-pack lines coincident —
    // the fabric is continuous by construction without the floor.
    "  float lF=lineAt(hm, uFam.x);\n"
    "  float lM=lineAt(hm, uFam.x*5.0);\n"
    "  float lS=lineAt(hm, uFam.x*25.0);\n"
    "  float minor=max(lF*(1.0-uFam.y), lM*uFam.y);\n"
    "  float major=max(lM*(1.0-uFam.y), lS);\n"
    // water: signed shore distance (0.502 = shoreline, below = water).
    // contours/index lines stop at the shore; fill + crisp shoreline below.
    "  float wd=(ctr.w>3.0)?watBic(wat, texel, uv):texture2D(wat, uv).r;\n"
    "  float land=smoothstep(0.494, 0.514, wd);\n"
    "  minor*=land; major*=land;\n"
    "  vec2 suvy=gl_FragCoord.xy/uRes.xy;\n"
    "  vec3 base=mix(uBgLo, uBgHi, suvy.y);\n"
    "  vec2 gx=vec2(texel.x*2.4, 0.0);\n"
    "  vec2 gy=vec2(0.0, texel.y*2.4);\n"
    "  vec2 g2=vec2(terBil(ter,texel,uv+gx)-terBil(ter,texel,uv-gx),\n"
    "               terBil(ter,texel,uv+gy)-terBil(ter,texel,uv-gy));\n"
    "  vec3 nrm=normalize(vec3(-g2.x*80.0, -g2.y*80.0, 1.0));\n"
    "  float sh=clamp(dot(nrm, normalize(uSunH.xyz)), 0.0, 1.0);\n"
    "  base*=mix(1.0, 0.64+0.68*sh, 0.50);\n"
    "  base=mix(base, base*vec3(1.26,1.02,0.84), uSunH.w*sh*0.50);\n"
    "  float lineOp=0.75;\n"
    "  vec3 lc=mix(uLnLo, uLnHi, clamp(h*0.85,0.0,1.0));\n"
    "  vec3 col=mix(base, lc, minor*0.45*lineOp);\n"
    "  col=mix(col, lc*1.22, major*0.60*lineOp);\n"
    "  vec3 wcol=mix(base, uWatC, 0.62);\n"
    "  col=mix(wcol, col, land);\n"
    "  float sd=abs(wd-0.502)*31.875*ctr.w;\n"      // texels -> screen px
    "  float shore=1.0-smoothstep(0.7, 1.6, sd);\n"
    "  col=mix(col, uShoreC, shore*0.38);\n"
    // live snowline: whiten land above the real freezing level (true metres,
    // crawl removed); roads/labels draw after, so passes stay plowed
    "  float sn=smoothstep(uSnow, uSnow+140.0, hm-uCrawlM)*land;\n"
    "  col=mix(col, uSnowC, sn*0.42);\n"
    "  vec2 rdt=texture2D(feat, uv).ra;\n"
    "  float dL=rdt.x*31.875*ctr.w;\n"
    "  float dA=rdt.y*31.875*ctr.w;\n"
    "  float sL=1.0-smoothstep(fpL.x, fpL.y, dL);\n"
    "  float sA=1.0-smoothstep(fpA.x, fpA.y, dA);\n"
    "  col=mix(col, fcL, sL*fpL.z);\n"
    "  col=mix(col, fcA, sA*fpA.z);\n"
    "  float line=max(minor, major);\n"
    "  col=mix(col, uWarmC, line*uCpu*0.28);\n"
    "  return col; }\n"
    // GUARANTOR (slot 2): terrain-only backfill for view overhangs — the
    // coverage deficit of slots 0/1 lands here. Contours + hillshade from
    // ter2; water derived from raw elevation (<=0 m: bathymetry carries the
    // oceans, lakes are absent — acceptable for brief edge backfill).
    "vec3 slotG(vec2 uv){\n"
    "  float hh=terBic(uTer2, uTexel2, uv);\n"
    "  float hm=uCtr2.x+hh*uCtr2.y+uCrawlM;\n"
    "  float lF=lineAt(hm, uFam.x);\n"
    "  float lM=lineAt(hm, uFam.x*5.0);\n"
    "  float lS=lineAt(hm, uFam.x*25.0);\n"
    "  float minor=max(lF*(1.0-uFam.y), lM*uFam.y);\n"
    "  float major=max(lM*(1.0-uFam.y), lS);\n"
    "  float em=uCtr2.x+hh*uCtr2.y;\n"
    "  float land=smoothstep(0.0, 6.0, em);\n"
    "  minor*=land; major*=land;\n"
    "  vec2 suvy=gl_FragCoord.xy/uRes.xy;\n"
    "  vec3 base=mix(uBgLo, uBgHi, suvy.y);\n"
    "  vec2 gx=vec2(uTexel2.x*2.4, 0.0);\n"
    "  vec2 gy=vec2(0.0, uTexel2.y*2.4);\n"
    "  vec2 g2=vec2(terBil(uTer2,uTexel2,uv+gx)-terBil(uTer2,uTexel2,uv-gx),\n"
    "               terBil(uTer2,uTexel2,uv+gy)-terBil(uTer2,uTexel2,uv-gy));\n"
    "  vec3 nrm=normalize(vec3(-g2.x*80.0, -g2.y*80.0, 1.0));\n"
    "  float sh=clamp(dot(nrm, normalize(uSunH.xyz)), 0.0, 1.0);\n"
    "  base*=mix(1.0, 0.64+0.68*sh, 0.50);\n"
    "  base=mix(base, base*vec3(1.26,1.02,0.84), uSunH.w*sh*0.50);\n"
    "  vec3 lc=mix(uLnLo, uLnHi, clamp(hh*0.85,0.0,1.0));\n"
    "  vec3 col=mix(base, lc, minor*0.45*0.75);\n"
    "  col=mix(col, lc*1.22, major*0.60*0.75);\n"
    "  vec3 wcol=mix(base, uWatC, 0.62);\n"
    "  col=mix(wcol, col, land);\n"
    "  float sn=smoothstep(uSnow, uSnow+140.0, em)*land;\n"
    "  col=mix(col, uSnowC, sn*0.42);\n"
    "  col=mix(col, uWarmC, max(minor,major)*uCpu*0.28);\n"
    "  return col; }\n"
    "void main(){\n"
    "  vec2 suv=gl_FragCoord.xy/uRes.xy;\n"
    "  vec3 bg=mix(uBgLo, uBgHi, suv.y);\n"
    "  vec2 q=(gl_FragCoord.xy-0.5*uRes.xy)/uRadPx;\n"
    "  float rr=length(q);\n"
    "  float edge=max(fwidth(rr), 1e-4);\n"
    "  float rim=exp(-max(rr-1.0,0.0)/(edge*8.0+0.025))*0.12;\n"
    "  vec3 space=bg+uRimC*rim;\n"
    "  vec3 col=space;\n"
    // solar zenith cosine at this fragment: sphere pos and uSun share the
    // view-centre frame, so this IS the terminator (== centre elevation
    // when zoomed in — the flat night dim and the globe's shadow are one
    // mechanism at different scales)
    "  float zz=sqrt(max(1.0-min(dot(q,q),1.0), 0.0));\n"
    "  float sdot=dot(vec3(q, zz), uSun);\n"
    "  float aurA=0.0; vec3 aurC=vec3(0.0);\n"
    "  if(rr<1.0+edge){\n"
    "    float rr2=min(dot(q,q), 1.0);\n"
    "    float z=zz;\n"
    "    float sla=sin(uGlobeC.y), cla=cos(uGlobeC.y);\n"
    "    float wsm=rr2/(1.0+z);\n"                 // 1-z, stable near 0
    "    float eps=q.y*cla-wsm*sla;\n"             // sin(lat)-sin(lat0)
    "    float snl=clamp(sla+eps, -0.9994, 0.9994);\n"
    "    float one_m=max(cla*cla/(1.0+sla)-eps, 6.0e-4);\n"   // 1-sin(lat)
    "    float csl=sqrt(max((1.0+snl)*one_m, 1.0e-12));\n"
    "    float dlam=atan(q.x, z*cla-q.y*sla);\n"
    "    float dL=log((1.0+snl)*cla/((1.0+sla)*csl));\n"
    "    vec2 D=vec2(dlam, dL);\n"
    // coverage-weighted blend of the ladder pair (slot0 = dominant rung;
    // idle is uW1=0 so slot1 costs one uniform branch)
    // full-world packs: leave u UNWRAPPED and continuous — hardware REPEAT
    // wraps the taps, while a shader-side fract() puts a derivative jump at
    // 180° that spikes every fwidth consumer into a visible seam column
    "    vec2 uv0=uTC0+D*uTS0;\n"
    "    vec2 cl0=clamp(uv0, uTexel0*2.5, 1.0-uTexel0*2.5);\n"
    "    if(uWrap.x>0.5) cl0.x=uv0.x;\n"
    "    vec2 cvv0=uv0; if(uWrap.x>0.5) cvv0.x=0.5;\n"
    "    float cov0=slotCov(cvv0, uTexel0);\n"
    "    vec3 c0=slotRealUV(cl0, uTer0, uFeat0, uWat0, uTexel0, uCtr0,\n"
    "                       uFcL0, uFpL0, uFcA0, uFpA0);\n"
    "    float w0v=uW.x*cov0;\n"
    "    float w1v=0.0; vec3 c1=vec3(0.0);\n"
    "    if (uW.y > 0.001) {\n"
    "      vec2 uv1=uTC1+D*uTS1;\n"
    "      vec2 cl1=clamp(uv1, uTexel1*2.5, 1.0-uTexel1*2.5);\n"
    "      if(uWrap.y>0.5) cl1.x=uv1.x;\n"
    "      vec2 cvv1=uv1; if(uWrap.y>0.5) cvv1.x=0.5;\n"
    "      float cov1=slotCov(cvv1, uTexel1);\n"
    "      c1=slotRealUV(cl1, uTer1, uFeat1, uWat1, uTexel1, uCtr1,\n"
    "                    uFcL1, uFpL1, uFcA1, uFpA1);\n"
    "      w1v=uW.y*cov1;\n"
    "    }\n"
    "    float w2v=0.0; vec3 c2=vec3(0.0);\n"
    "    if (uW.z > 0.001) {\n"
    "      vec2 uv2=uTC2+D*uTS2;\n"
    "      vec2 cl2=clamp(uv2, uTexel2*2.5, 1.0-uTexel2*2.5);\n"
    "      if(uWrap.z>0.5) cl2.x=uv2.x;\n"
    "      vec2 cvv2=uv2; if(uWrap.z>0.5) cvv2.x=0.5;\n"
    "      float cov2=slotCov(cvv2, uTexel2);\n"
    "      c2=slotG(cl2);\n"
    "      w2v=uW.z*cov2;\n"
    "    }\n"
    "    float tot=w0v+w1v+w2v;\n"
    "    vec3 terr=(tot>1e-4)?(c0*w0v+c1*w1v+c2*w2v)/tot:bg;\n"
    "    terr*=0.72+0.28*z;\n"          // limb darkening; ==1.0 up close
    // dusk band: warm cast where the sun grazes the terminator's day side
    "    float band=1.0-smoothstep(0.0, 0.26, abs(sdot-0.02));\n"
    "    terr=mix(terr, terr*vec3(1.18,0.92,0.76), band*0.42);\n"
    "    float lm=smoothstep(1.0+edge, 1.0-edge, rr);\n"
    // aurora: night-side ovals ~15-27deg around the geomagnetic poles,
    // curtains drifting on the integrated phase — globe scales only
    "    if(uAur.x>0.002){\n"
    "      float fl=asin(snl);\n"
    "      float fo=uGlobeC.x+dlam;\n"
    // mercator's >85.05deg hole: the v-clamp smears the edge texel row into
    // a dark disc at each pole — overlay ice derived from the LOCAL terrain
    // tone and sun (an absolute colour becomes a lamp on the night side),
    // blended in over ~82..85.5deg
    // ice with structure, not a sticker (a pale flat disc read as one): mostly
    // local terrain tone, a faint static floe mottling for texture, and a
    // small sun-scaled pale lift — the mercator hole stays hidden but the
    // cap now reads as sea ice instead of a matte overlay
    "      float pcp=smoothstep(1.432, 1.492, abs(fl));\n"
    "      float icd=0.35+0.65*smoothstep(-0.12, 0.10, sdot);\n"
    "      float flo=0.90+0.10*sin(fo*38.0+sin(fl*57.0)*3.0)*sin(fl*71.0+fo*7.0);\n"
    "      terr=mix(terr, (terr*0.75+uIceC)*icd*flo, pcp*0.85);\n"
    "      float cn=cos(fl);\n"
    "      float gdN= snl*0.9869+cn*0.1610*cos(fo+1.2688);\n"
    "      float gdS=-snl*0.9869+cn*0.1610*cos(fo-1.8728);\n"
    // thin auroral crown (a fat scalloped donut didn't read as
    // aurora): narrow ~3deg arc at 20deg geomagnetic colatitude + a faint
    // equatorward drape broken into filament rays (three incommensurate
    // waves, sharpened); green core, mauve only on the poleward fringe
    "      float clt=acos(clamp(max(gdN,gdS),-1.0,1.0));\n"
    "      float dc=(clt-0.349)/0.0524;\n"
    "      float core=exp(-dc*dc*2.2);\n"
    "      float drape=exp(-max(dc,0.0)*0.9)*0.35*smoothstep(-0.3, 0.4, dc);\n"
    "      float rw=sin(fo*29.0+uAur.y)*sin(fo*47.0-uAur.y*1.3)\n"
    "              *sin(fo*11.0+uAur.y*0.55);\n"
    "      float rays=0.35+0.65*smoothstep(-0.2, 0.75, rw);\n"
    "      float nfl=1.0-smoothstep(-0.12, 0.10, sdot);\n"
    "      aurA=(core*(0.75+0.25*rays)+drape*rays)*nfl*uAur.x*uAur.z*lm*0.50;\n"
    "      aurC=mix(uAurA, uAurB,\n"
    "               smoothstep(0.0,-1.5,dc));\n"
    "    }\n"
    "    col=mix(space, terr, lm);\n"
    "  }\n"
    // night per FRAGMENT (was uniform uNight): same dim+desat constants as
    // ever, but driven by sdot — up close it equals the old whole-screen
    // night; on the globe it resolves into the real day/night terminator
    "  float nf=1.0-smoothstep(-0.12, 0.10, sdot);\n"
    "  col *= (1.0 - 0.34*nf);\n"
    "  col = mix(col, vec3(dot(col, vec3(0.333))), 0.30*nf);\n"
    "  col += aurC*aurA;\n"
    "  float vig = 1.0 - 0.28*pow(clamp(length(suv-vec2(0.5,0.42))/0.72,0.0,1.0),1.6);\n"
    "  gl_FragColor = vec4(col*vig, 1.0);\n"
    "}\n";

// ---- city-lights shaders (additive warm points on the globe's night side) --
static const char *VERT_T =
    "attribute vec3 pos;\n"                  // xy NDC, z alpha
    "uniform float uPtS;\n"
    "varying float va;\n"
    "void main(){ va=pos.z; gl_PointSize=uPtS;\n"
    "  gl_Position=vec4(pos.xy, 0.0, 1.0); }\n";
static const char *FRAG_T =
    "precision mediump float;\n"
    "varying float va;\n"
    "uniform vec3 uCity;\n"
    "void main(){ vec2 d=gl_PointCoord-0.5;\n"
    "  float a=va*(1.0-smoothstep(0.10, 0.50, length(d)));\n"
    "  gl_FragColor=vec4(uCity*a, 0.0); }\n";

// ---- label shaders (textured quads from a pack's atlas) --------------------
static const char *VERT_L =
    "attribute vec2 pos;\n"
    "uniform vec4 uRect;\n"       // ndc origin (bottom-left) + size
    "uniform vec4 uUv;\n"         // atlas rect: x,y (from TOP) + w,h (normalized)
    "varying vec2 vUv;\n"
    "void main(){\n"
    "  vec2 p = pos*0.5 + 0.5;\n"
    "  gl_Position = vec4(uRect.xy + p*uRect.zw, 0.0, 1.0);\n"
    "  vUv = vec2(uUv.x + p.x*uUv.z, uUv.y + (1.0-p.y)*uUv.w);\n"
    "}\n";
static const char *FRAG_L =
    "precision mediump float;\n"
    "varying vec2 vUv;\n"
    "uniform sampler2D uAtlas; uniform float uAlpha;\n"
    "uniform vec3 uHalo, uInk;\n"
    "void main(){\n"
    "  vec4 t = texture2D(uAtlas, vUv);\n"   // .r = glyph luminance, .a = halo+glyph
    "  vec3 c = mix(uHalo, uInk, t.r);\n"
    "  gl_FragColor = vec4(c, t.a*uAlpha);\n"
    "}\n";

// The shaders are GLSL ES 1.00. Desktop GL (Windows, macOS) compiles them as
// GLSL 1.20: the same language once the precision statements and the
// derivatives extension (core there) are dropped and the qualifiers defined
// away.
static GLuint compile(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
#ifdef TOPA_DESKTOP_GL
    size_t n = strlen(src);
    char *d = malloc(n + 128), *o = d;
    o += sprintf(o, "#version 120\n#define lowp\n#define mediump\n#define highp\n");
    for (const char *l = src; *l; ) {
        const char *e = strchr(l, '\n');
        size_t len = e ? (size_t)(e - l + 1) : strlen(l);
        if (strncmp(l, "precision ", 10) && strncmp(l, "#extension", 10)) {
            memcpy(o, l, len); o += len;
        }
        l += len;
    }
    *o = 0;
    const char *dsrc = d;
    glShaderSource(s, 1, &dsrc, NULL); glCompileShader(s);
    free(d);
#else
    glShaderSource(s, 1, &src, NULL); glCompileShader(s);
#endif
    GLint ok; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) { char log[4096]; glGetShaderInfoLog(s, 4096, NULL, log);
               fprintf(stderr, "topopaper: shader compile failed:\n%s\n", log); exit(1); }
    return s;
}

// Per-context state. Programs, buffers and textures are shared between the
// contexts of a backend that needs one per display (macOS); this is the rest.
void engine_gl_context(void) {
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
#ifdef TOPA_DESKTOP_GL
    glEnable(0x8642);                       // GL_VERTEX_PROGRAM_POINT_SIZE: gl_PointSize
    glEnable(0x8861);                       // GL_POINT_SPRITE: gl_PointCoord (GL 2.1)
#endif
}

void engine_gl_init(void) {
    fprintf(stderr, "topopaper: GL %s (%s)\n", (const char *)glGetString(GL_VERSION),
            (const char *)glGetString(GL_RENDERER));
    engine_gl_context();
    GLuint v = compile(GL_VERTEX_SHADER, VERT), fsh = compile(GL_FRAGMENT_SHADER, FRAG);
    prog = glCreateProgram();
    glAttachShader(prog, v); glAttachShader(prog, fsh); glLinkProgram(prog);
    GLint ok; glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) { char log[2048]; glGetProgramInfoLog(prog, 2048, NULL, log);
               fprintf(stderr, "topopaper: link failed:\n%s\n", log); exit(1); }
    a_pos     = glGetAttribLocation(prog, "pos");
    u_res     = glGetUniformLocation(prog, "uRes");
    u_crawlm  = glGetUniformLocation(prog, "uCrawlM");
    u_cpu     = glGetUniformLocation(prog, "uCpu");
    u_night   = glGetUniformLocation(prog, "uNight");
    u_w       = glGetUniformLocation(prog, "uW");
    u_texel2  = glGetUniformLocation(prog, "uTexel2");
    u_tc2     = glGetUniformLocation(prog, "uTC2");
    u_ts2     = glGetUniformLocation(prog, "uTS2");
    u_ctr2    = glGetUniformLocation(prog, "uCtr2");
    u_fam     = glGetUniformLocation(prog, "uFam");
    glUniform1i(glGetUniformLocation(prog, "uTer2"), UNIT_TER2);
    u_radpx   = glGetUniformLocation(prog, "uRadPx");
    u_globec  = glGetUniformLocation(prog, "uGlobeC");
    u_sun     = glGetUniformLocation(prog, "uSun");
    u_sunh    = glGetUniformLocation(prog, "uSunH");
    u_snow    = glGetUniformLocation(prog, "uSnow");
    u_aur     = glGetUniformLocation(prog, "uAur");
    u_wrap    = glGetUniformLocation(prog, "uWrap");
    static const char *thn[12] = { "uBgLo", "uBgHi", "uLnLo", "uLnHi", "uWatC",
        "uShoreC", "uSnowC", "uWarmC", "uRimC", "uIceC", "uAurA", "uAurB" };
    for (int i = 0; i < 12; i++) u_th[i] = glGetUniformLocation(prog, thn[i]);
    const char *names[2][8] = {
        {"uTexel0","uTC0","uTS0","uCtr0","uFcL0","uFpL0","uFcA0","uFpA0"},
        {"uTexel1","uTC1","uTS1","uCtr1","uFcL1","uFpL1","uFcA1","uFpA1"}};
    for (int i = 0; i < 2; i++) {
        u_texel[i] = glGetUniformLocation(prog, names[i][0]);
        u_tc[i]    = glGetUniformLocation(prog, names[i][1]);
        u_ts[i]    = glGetUniformLocation(prog, names[i][2]);
        u_ctr[i]   = glGetUniformLocation(prog, names[i][3]);
        u_fcl[i]   = glGetUniformLocation(prog, names[i][4]);
        u_fpl[i]   = glGetUniformLocation(prog, names[i][5]);
        u_fca[i]   = glGetUniformLocation(prog, names[i][6]);
        u_fpa[i]   = glGetUniformLocation(prog, names[i][7]);
    }
    const float quad[] = { -1,-1,  1,-1,  -1,1,  1,1 };
    glGenBuffers(1, &vbo); glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);

    glUseProgram(prog);
    glUniform1i(glGetUniformLocation(prog, "uTer0"),  UNIT_TER[0]);
    glUniform1i(glGetUniformLocation(prog, "uFeat0"), UNIT_FEAT[0]);
    glUniform1i(glGetUniformLocation(prog, "uTer1"),  UNIT_TER[1]);
    glUniform1i(glGetUniformLocation(prog, "uFeat1"), UNIT_FEAT[1]);
    glUniform1i(glGetUniformLocation(prog, "uWat0"),  UNIT_WAT[0]);
    glUniform1i(glGetUniformLocation(prog, "uWat1"),  UNIT_WAT[1]);
    unsigned char flat[2] = {0, 0}, far2[2] = {255, 255}, dry[1] = {255};
    glGenTextures(1, &ph_ter_tex);
    glGenTextures(1, &ph_feat_tex);
    glGenTextures(1, &ph_wat_tex);
    upload_la(ph_ter_tex, UNIT_TER[0], 1, 1, flat, GL_NEAREST);
    upload_la(ph_feat_tex, UNIT_FEAT[0], 1, 1, far2, GL_LINEAR);
    upload_l(ph_wat_tex, UNIT_WAT[0], 1, 1, dry);
    for (int i = 0; i < 2; i++) {           // all units start on placeholders
        glActiveTexture(GL_TEXTURE0 + UNIT_TER[i]);
        glBindTexture(GL_TEXTURE_2D, ph_ter_tex);
        glActiveTexture(GL_TEXTURE0 + UNIT_FEAT[i]);
        glBindTexture(GL_TEXTURE_2D, ph_feat_tex);
        glActiveTexture(GL_TEXTURE0 + UNIT_WAT[i]);
        glBindTexture(GL_TEXTURE_2D, ph_wat_tex);
    }
    glActiveTexture(GL_TEXTURE0);

    // label program (atlas textures live per pack, bound to unit 1 at draw)
    GLuint lv = compile(GL_VERTEX_SHADER, VERT_L);
    GLuint lf = compile(GL_FRAGMENT_SHADER, FRAG_L);
    lprog = glCreateProgram();
    glAttachShader(lprog, lv); glAttachShader(lprog, lf); glLinkProgram(lprog);
    GLint lok; glGetProgramiv(lprog, GL_LINK_STATUS, &lok);
    if (!lok) { fprintf(stderr, "topopaper: label prog link failed\n"); exit(1); }
    la_pos   = glGetAttribLocation(lprog, "pos");
    lu_rect  = glGetUniformLocation(lprog, "uRect");
    lu_uv    = glGetUniformLocation(lprog, "uUv");
    lu_alpha = glGetUniformLocation(lprog, "uAlpha");
    lu_ink   = glGetUniformLocation(lprog, "uInk");
    lu_halo  = glGetUniformLocation(lprog, "uHalo");
    glUseProgram(lprog);
    glUniform1i(glGetUniformLocation(lprog, "uAtlas"), 1);

    // city-lights point program + data (absent lights.bin -> pass skipped)
    GLuint tv = compile(GL_VERTEX_SHADER, VERT_T);
    GLuint tf = compile(GL_FRAGMENT_SHADER, FRAG_T);
    ltprog = glCreateProgram();
    glAttachShader(ltprog, tv); glAttachShader(ltprog, tf); glLinkProgram(ltprog);
    GLint tok; glGetProgramiv(ltprog, GL_LINK_STATUS, &tok);
    if (tok) {
        lta_pos = glGetAttribLocation(ltprog, "pos");
        ltu_pts = glGetUniformLocation(ltprog, "uPtS");
        ltu_city = glGetUniformLocation(ltprog, "uCity");
    } else { ltprog = 0; }
    FILE *lfp = fopen(lights_path, "rb");
    if (lfp) {
        char m[8]; unsigned int n = 0;
        if (fread(m, 1, 8, lfp) == 8 && !memcmp(m, "TOPOLTS1", 8) &&
            fread(&n, 4, 1, lfp) == 1 && n > 0 && n <= MAXLTS) {
            for (unsigned int i = 0; i < n; i++) {
                float v[3];
                if (fread(v, 4, 3, lfp) != 3) { n = i; break; }
                lt_lat[i] = v[0]; lt_lon[i] = v[1]; lt_w[i] = v[2];
            }
            g_lts_n = (int)n;
            fprintf(stderr, "topopaper: %d city lights\n", g_lts_n);
        }
        fclose(lfp);
    }
    glUseProgram(prog);
    load_hud();
}

// ---- theme ------------------------------------------------------------------
// cfg.theme picks one row of theme_rgb (engine/themes.h, generated from
// topopaper/themes.py); an unknown name falls back to row 0 (macchiato).
// Uniforms are program state, so the palette is pushed once per settings
// load, not per frame — a theme change lands on the next frame.
static const float *th(int role) { return theme_rgb[g_theme][role]; }

static void theme_apply(void) {
    if (g_theme_gen == cfg_gen) return;
    g_theme_gen = cfg_gen;
    int t = -1;
    for (int i = 0; i < THEME_COUNT; i++) if (!strcmp(cfg.theme, theme_names[i])) t = i;
    if (t < 0) {
        fprintf(stderr, "topopaper: unknown theme '%s', using %s\n", cfg.theme, theme_names[0]);
        t = 0;
    }
    g_theme = t;
    static const int roles[12] = { TR_BG_LO, TR_BG_HI, TR_LINE_LO, TR_LINE_HI,
        TR_WATER, TR_SHORE, TR_SNOW, TR_WARM, TR_RIM, TR_ICE, TR_AURORA_A, TR_AURORA_B };
    glUseProgram(prog);
    for (int i = 0; i < 12; i++) glUniform3fv(u_th[i], 1, th(roles[i]));
    glUseProgram(lprog);
    glUniform3fv(lu_ink, 1, th(TR_INK));
    glUniform3fv(lu_halo, 1, th(TR_INK_HALO));
    if (ltprog) {
        glUseProgram(ltprog);
        glUniform3fv(ltu_city, 1, th(TR_CITY));
    }
    glUseProgram(prog);
}

// Packs bake their feature colours (meta.bin styleL/styleA) in the macchiato
// palette. A baked colour within 0.02 of one of those keys takes the active
// theme's role instead; anything else (a custom build) is drawn as baked.
// Widths and opacity are never touched.
static void theme_feat(const float *baked, float *out) {
    static const int keys[4] = { TR_ROAD, TR_SKI, TR_LIFT, TR_BORDER };
    for (int k = 0; k < 4; k++) {
        const float *m = theme_rgb[0][keys[k]];
        if (fabsf(baked[0] - m[0]) < 0.02f && fabsf(baked[1] - m[1]) < 0.02f &&
            fabsf(baked[2] - m[2]) < 0.02f) {
            memcpy(out, th(keys[k]), 3 * sizeof(float));
            return;
        }
    }
    memcpy(out, baked, 3 * sizeof(float));
}

// Advance the world by dt. Called once per frame tick, however many outputs
// then draw that frame (each output stepping it ran the world N times fast).
static void step_world(void) {
    double t = now_sec();
    double dt = t - t_prev;
    if (dt < 0.0 || dt > 2.0) dt = 1.0 / 60.0;   // first frame / resume guard
    t_prev = t;

    float kc = 1.0f - expf((float)(-dt / 0.8));   // cpu: ~0.8s glide
    g_cpu   += (cpu_target - g_cpu) * kc;
    float kn = 1.0f - expf((float)(-dt / 8.0));   // night: slow fade
    g_night += (night_target - g_night) * kn;
    // snowline: ease WITHIN terrain range, but snap across the off-sentinel
    // (easing down from 99999 would spend ~40s above the summits — invisible)
    if (g_snow > 90000.0f && g_snow_t < 90000.0f) g_snow = g_snow_t + 1500.0f;
    if (g_snow_t > 90000.0f && g_snow > 12000.0f) g_snow = g_snow_t;
    float ks = 1.0f - expf((float)(-dt / 6.0));   // gentle descent/retreat
    g_snow += (g_snow_t - g_snow) * ks;
    float kp = 1.0f - expf((float)(-dt / 0.25));  // download meter fade
    g_prog_a += (g_prog_vis - g_prog_a) * kp;

    // Integrated phases — speed changes bend the rate, never jump the position.
    // Covered (with pause_when_covered) -> mdt=0: the scene (and any flight)
    // freezes and resumes. animation_speed scales the idle motion (drift,
    // crawl, Ken-Burns, aurora) but never flight timing.
    double mdt = (g_covered && cfg.pause_when_covered) ? 0.0 : dt;
    g_mdt = mdt;                             // label-authority easing shares it
    double speed = (1.0 + (double)g_cpu * 2.5) * cfg.animation_speed;
    // FRACTAL CONTOUR LADDER: global family 12*5^k metres, phase from
    // camera height (gamma tunes density drift; anchored so the 60m-class
    // packs keep today's look). Crawl rate scales with the current step so
    // every line crosses one interval in ~20s AT EVERY ZOOM — the globe
    // drifts as alive as a ski hill. Wrap 37500 = 5 * (12*5^4): index-safe
    // for every family step, so no line snaps at the wraparound.
    {
        double sc = 60.0 * pow(cam_h > 1e-9 ? cam_h / 5.9e-4 : 1.0, 0.5);
        if (sc < FRACTAL_BASE) sc = FRACTAL_BASE;
        if (sc > FRACTAL_BASE * 625.0) sc = FRACTAL_BASE * 625.0;
        g_step_cont = sc;
        double uu = log(sc / FRACTAL_BASE) / log(5.0);
        int k = (int)uu; if (k > 4) k = 4;
        g_fam_s = FRACTAL_BASE * pow(5.0, (double)k);
        g_fam_t = uu - (double)k;
        if (g_fam_t < 0.0) g_fam_t = 0.0;
        if (g_fam_t > 1.0) g_fam_t = 1.0;
    }
    ph_crawlm = fmod(ph_crawlm + mdt * (g_step_cont / 20.0) * speed, 37500.0);
    ph_pa     = fmod(ph_pa     + mdt * 0.0170 * speed, 2.0 * M_PI);
    ph_pb     = fmod(ph_pb     + mdt * 0.0071 * speed, 2.0 * M_PI);
    ph_zm     = fmod(ph_zm     + mdt * 0.0110 * speed, 2.0 * M_PI);
    ph_hud    = fmod(ph_hud    + mdt * 1.05, 2.0 * M_PI);
    ph_aur    = fmod(ph_aur    + mdt * 0.07 * cfg.animation_speed, 20.0 * M_PI);

    // HUD clock: local wall time, "2:05P" (12h; A/P becomes the small
    // meridian pair) or "14:05" (24h). On change, remember the old string
    // and stamp the flip for the right-aligned crossfade; a format switch
    // just snaps.
    if (g_hud_tex) {
        if (g_hud_24 != cfg.clock_24h) {
            g_hud_24 = cfg.clock_24h;
            hud_cur[0] = hud_prev[0] = 0;
            chip_next = 0.0;                 // the context chip follows suit
        }
        hud_tz_sync(t);
        hud_wx_sync(t);
        chip_sync(t);
        g_wx_vis += ((g_wx_want ? 1.0f : 0.0f) - g_wx_vis)
                    * (float)(1.0 - exp(-mdt / 1.2));
        time_t wt = time(NULL); struct tm lt; local_tm(wt, &lt);
        int h12 = lt.tm_hour % 12; if (!h12) h12 = 12;
        char ns[8];
        if (g_hud_24) snprintf(ns, sizeof ns, "%02d:%02d", lt.tm_hour, lt.tm_min);
        else snprintf(ns, sizeof ns, "%d:%02d%c", h12, lt.tm_min,
                      lt.tm_hour < 12 ? 'A' : 'P');
        if (strcmp(ns, hud_cur)) {
            snprintf(hud_prev, sizeof hud_prev, "%s", hud_cur);
            snprintf(hud_cur, sizeof hud_cur, "%s", ns);
            hud_flip = hud_prev[0] ? t : -10.0;
        }
        // hide during flights (out fast, back gently after touchdown)
        float hv = fly_active ? 0.0f : 1.0f;
        double kh = 1.0 - exp(-mdt / (fly_active ? 0.18 : 0.45));
        g_hud_vis += (hv - g_hud_vis) * (float)kh;
    }

    // ---- camera ------------------------------------------------------------
    if (p_cur < 0 || !cam_init) return;
    if (fly_active == 1) {
        fly_t += mdt;
        double tau = fly_t / fly_T;
        double s = f_S * smoother01(tau);
        flight_cam(s, &cam_x, &cam_y, &cam_h);
        // chase the LIVE idle pose: the wander/spin phases advance during
        // the flight, so the pose targeted at start is stale by arrival —
        // landing on it caused a visible correction pan in the landing
        // blend (a quick pan at the end for no reason). Blend the target's
        // drift in with progress: touchdown lands exactly on the current
        // idle pose and the landing blend has nothing left to cover.
        {
            double ix, iy, ih;
            idle_cam(fly_tgt, &ix, &iy, &ih);
            double dk = smoother01(tau);
            cam_x += (ix - f_c1x) * dk;
            cam_y += (iy - f_c1y) * dk;
            cam_h += (ih - f_w1) * dk;
        }
        if (tau >= 1.0) {
            p_cur = fly_tgt;
            fly_active = 2;
            land_t = 0.0;
            land_x = cam_x; land_y = cam_y; land_h = cam_h;
            chip_next = 0.0;                 // context chip resyncs on arrival
        }
    } else if (fly_active == 2) {
        land_t += mdt;
        double k = smooth01(land_t / 1.2);
        double ix, iy, ih;
        idle_cam(p_cur, &ix, &iy, &ih);
        cam_x = land_x + (ix - land_x) * k;
        cam_y = land_y + (iy - land_y) * k;
        cam_h = land_h + (ih - land_h) * k;
        if (k >= 1.0) fly_active = 0;
    } else {
        idle_cam(p_cur, &cam_x, &cam_y, &cam_h);
        maybe_roam(dt);
    }

    // ---- ladder pair: rungs bracketing the camera height -------------------
    // Candidates = packs whose bbox intersects the VIEW rectangle: a sibling
    // pack at the same scale but elsewhere on the map (Tetons vs Wind
    // Rivers) must never blend in by height alone, while a flight corridor
    // naturally picks up each area the camera overflies. When nothing
    // qualifies (a lone remote pack), fall back to the nearest centre.
    // ---- LADDER WEIGHTS: continuous by construction (the law) -------------
    // Every rung's weight is a continuous function of camera state. Drivers:
    //  - SHARPNESS YIELD: a rung hands off only as the NEXT rung's texels
    //    approach 1 screen px — sharp-for-sharp swaps are invisible at any
    //    screen size. (The old arrival-height bands completed while the
    //    coarse rung was still ~1.8x magnified: visible softening, detail
    //    loading out while it was still significant.) The yield is
    //    scaled by the next rung's own view overlap, so a sliver pack
    //    (yellowstone-region clipping a glacier descent) can't pull the
    //    handoff early.
    //  - VIEW OVERLAP: weight scales with the fraction of the view covered —
    //    slivers never claim the blend; packs fade out BEFORE their bbox
    //    exits the view (no candidacy-exit pops).
    // Weights telescope fine->coarse; the leftover coverage deficit flows
    // upward and lands on whatever covers (the guarantor emerges naturally
    // in slot 2, rendered terrain-only).
    double hxv = cam_h * g_ar * 0.5, hyv = cam_h * 0.5;
    double vx0 = cam_x - hxv, vx1 = cam_x + hxv;
    double vy0 = cam_y - hyv, vy1 = cam_y + hyv;
    double varea = (vx1 - vx0) * (vy1 - vy0);
    double ovl[MAXPACKS];
    for (int k = 0; k < n_lad; k++) {
        struct pack *p = &packs[lad[k]];
        double ox0 = p->mx0 > vx0 ? p->mx0 : vx0;
        double oy0 = p->my0 > vy0 ? p->my0 : vy0;
        double ox1 = p->mx0 + p->msx < vx1 ? p->mx0 + p->msx : vx1;
        double oy1 = p->my0 + p->msy < vy1 ? p->my0 + p->msy : vy1;
        ovl[k] = (ox1 > ox0 && oy1 > oy0)
                 ? (ox1 - ox0) * (oy1 - oy0) / varea : 0.0;
    }
    // ---- SIBLING LAW --------------------------------------------------------
    // Rungs of one scale are one LAYER. The ladder is walked in scale GROUPS
    // (msy within 5%); inside a group the members split the group's take by
    // TERRITORY: the member whose centre is nearest the camera (in units of
    // its own size) leads and takes its whole view coverage, the others take
    // only the coverage they ADD past the leaders' edges. Before this, every
    // same-scale pack covering the view took its full coverage in ladder
    // (= qsort) order — isle-royale-region drew level with apostle-islands-
    // region over the Apostles (two rosters, two waters, one screen) — and
    // the sharpness yield consulted lad[k+1], usually a same-scale STRANGER
    // with no view overlap, so the yield was dead and hand-offs fell back
    // to coverage loss. The yield now looks at the next COARSER covering
    // group. Preference is soft (the two leaders blend where territories
    // meet) so every weight stays continuous in camera state. Labels follow
    // territory: a member that only fills a sliver keeps its roster down
    // (label factor = share of its own coverage it won).
    double wsl[MAXPACKS]; int nslw = 0; int idxw[MAXPACKS]; double wlf[MAXPACKS];
    double rem = 1.0;
    int gk = 0;
    while (gk < n_lad && rem > 1e-4) {
        int g0 = gk; double gm = packs[lad[g0]].msy;
        while (gk < n_lad && packs[lad[gk]].msy < gm * 1.05) gk++;   // group [g0,gk)
        int mem[MAXSIB], nm = 0;
        for (int k = g0; k < gk && nm < MAXSIB; k++) if (ovl[k] > 0.0) mem[nm++] = k;
        if (!nm) continue;
        double yield = 0.0;                  // next coarser COVERING group
        for (int h0 = gk; h0 < n_lad; ) {
            double hm = packs[lad[h0]].msy; int h1 = h0, best = -1;
            double bo = 0.0, uni = 0.0;
            while (h1 < n_lad && packs[lad[h1]].msy < hm * 1.05) {
                if (ovl[h1] > bo) { bo = ovl[h1]; best = h1; }
                uni += ovl[h1]; h1++;
            }
            if (best >= 0) {
                struct pack *q = &packs[lad[best]];
                double hs = q->msy * ((double)g_scr_h / (double)(q->ter_h > 0 ? q->ter_h : 2048));
                if (hs > 1e-12) {
                    yield = smooth01(log(cam_h / hs) / log(1.6));
                    if (uni > 1.0) uni = 1.0;
                    double os = uni / 0.5;       // sliver yieldee -> hold on
                    if (os < 1.0) yield *= os;
                }
                break;
            }
            h0 = h1;
        }
        double tt = rem * (1.0 - yield);
        double pref[MAXSIB], gov[MAXSIB], gpo[MAXSIB][MAXSIB];
        for (int i = 0; i < nm; i++) {
            struct pack *p = &packs[lad[mem[i]]];
            double sig = 0.5 * (p->msx < p->msy ? p->msx : p->msy);
            double ddx = cam_x - (p->mx0 + p->msx * 0.5);
            double ddy = cam_y - (p->my0 + p->msy * 0.5);
            if (p->msx > 0.999) { ddx = 0.0; ddy = 0.0; }   // world packs own everything
            pref[i] = exp(-(ddx * ddx + ddy * ddy) / (sig * sig));
        }
        for (int i = 1; i < nm; i++) {       // order by preference (nm is tiny)
            int t = mem[i]; double pt = pref[i]; int j = i - 1;
            while (j >= 0 && pref[j] < pt) { mem[j+1] = mem[j]; pref[j+1] = pref[j]; j--; }
            mem[j+1] = t; pref[j+1] = pt;
        }
        for (int i = 0; i < nm; i++) {
            gov[i] = ovl[mem[i]];
            for (int j = 0; j < nm; j++)
                gpo[i][j] = (i == j) ? gov[i]
                          : rect_ovl3(&packs[lad[mem[i]]], &packs[lad[mem[j]]],
                                      vx0, vy0, vx1, vy1, varea);
        }
        int ord[MAXSIB]; for (int i = 0; i < nm; i++) ord[i] = i;
        double takeA[MAXSIB], takeB[MAXSIB];
        alloc_terr(nm, ord, gov, gpo, tt, takeA);
        double s = 1.0;                      // soft swap of the two leaders
        if (nm >= 2) {
            double x = (pref[0] - pref[1]) / (0.35 * (pref[0] + pref[1]) + 1e-12);
            s = 0.5 + 0.5 * smooth01(x);     // tie -> 50/50, clearly apart -> pure order
            if (s < 0.999) {
                ord[0] = 1; ord[1] = 0;
                alloc_terr(nm, ord, gov, gpo, tt, takeB);
            }
        }
        for (int i = 0; i < nm; i++) {
            double take = (s < 0.999) ? s * takeA[i] + (1.0 - s) * takeB[i] : takeA[i];
            if (take > 1e-4) {
                wsl[nslw] = take; idxw[nslw] = lad[mem[i]];
                double own = tt * gov[i];
                wlf[nslw] = own > 1e-9 ? take / own : 0.0;
                if (wlf[nslw] > 1.0) wlf[nslw] = 1.0;
                nslw++;
            }
            rem -= take;
        }
    }
    if (!nslw) {                             // camera outside everything: nearest
        double best = 1e18; int bi = lad[0];
        for (int k = 0; k < n_lad; k++) {
            struct pack *p = &packs[lad[k]];
            double ddx = cam_x - (p->mx0 + p->msx * 0.5);
            double ddy = cam_y - (p->my0 + p->msy * 0.5);
            if (ddx*ddx + ddy*ddy < best) { best = ddx*ddx + ddy*ddy; bi = lad[k]; }
        }
        wsl[0] = 1.0; idxw[0] = bi; wlf[0] = 1.0; nslw = 1;
    }
    // top-3 by weight -> slots (slot0 = dominant; slot2 = guarantor tail).
    // slot2 renders (w3rd - w4th): as two packs converge on 3rd place the
    // rendered guarantor fades to zero, so its identity crossover happens
    // AT zero weight — the truncation edge stays continuous too.
    int ord[4] = {-1, -1, -1, -1};
    for (int k = 0; k < nslw; k++)
        for (int j = 0; j < 4; j++)
            if (ord[j] < 0 || wsl[k] > wsl[ord[j]]) {
                for (int m = 3; m > j; m--) ord[m] = ord[m - 1];
                ord[j] = k; break;
            }
    int a  = ord[0] >= 0 ? idxw[ord[0]] : -1;
    int b  = ord[1] >= 0 ? idxw[ord[1]] : a;
    int gg = ord[2] >= 0 ? idxw[ord[2]] : -1;
    double w4  = ord[3] >= 0 ? wsl[ord[3]] : 0.0;
    // subtract the 4th weight from ALL rendered slots: every rank crossing
    // then happens at equal rendered weights and pop-ins start at exactly 0;
    // per-pixel normalization rescales, idle (w4=0) is untouched
    double w0v = ord[0] >= 0 ? wsl[ord[0]] - w4 : 1.0;
    double w1v = ord[1] >= 0 ? wsl[ord[1]] - w4 : 0.0;
    double w2v = ord[2] >= 0 ? wsl[ord[2]] - w4 : 0.0;
    if (w0v < 1e-4) w0v = 1e-4;
    if (w1v < 0.0) w1v = 0.0;
    if (w2v < 0.0) w2v = 0.0;
    if (a < 0) a = p_cur >= 0 ? p_cur : lad[0];
    ensure_pack_gl(a);                       // backstop; flights preload ahead
    if (b != a && !ensure_pack_gl(b)) { b = a; w1v = 0.0; }
    if (gg >= 0 && !ensure_pack_gl(gg)) { gg = -1; w2v = 0.0; }
    s_slotpk[0] = a; s_slotpk[1] = b; s_slotpk[2] = gg;
    g_w0v = (float)w0v; g_w1v = (float)w1v; g_w2v = (float)w2v;
    g_lf0 = ord[0] >= 0 ? (float)wlf[ord[0]] : 1.0f;
    g_lf1 = ord[1] >= 0 ? (float)wlf[ord[1]] : 1.0f;
    {   // TOPA_DEBUG=1: per-frame camera/slot trace for jump hunting
        static int dbg = -1;
        if (dbg < 0) dbg = getenv("TOPA_DEBUG") != NULL;
        if (dbg) fprintf(stderr,
            "F h=%.6f x=%.5f y=%.5f s0=%s:%.3f s1=%s:%.3f s2=%s:%.3f\n",
            cam_h, cam_x, cam_y,
            a >= 0 ? packs[a].name : "-", w0v,
            b >= 0 ? packs[b].name : "-", w1v,
            gg >= 0 ? packs[gg].name : "-", w2v);
    }

    // ---- view centre on the sphere (EVERY frame — there is only one
    // projection now; curvature is geometry, not a blend state) ------------
    g_glon = (cam_x - 0.5) * 2.0 * M_PI;
    double glat = atan(sinh(M_PI * (1.0 - 2.0 * cam_y)));
    if (glat >  1.05) glat =  1.05;                         // keep off the poles
    if (glat < -1.05) glat = -1.05;
    g_glat = glat;
}


// ---- HUD layout: ONE geometry truth ---------------------------------------
// The draw (hud_line + the weather rows) and the label occlusion both read
// these rows, so a label can never fade for a box the HUD doesn't occupy, or
// slide unfaded under one it does: peak/town markers fade under the
// time/weather block just as they do at the screen edge — the HUD is an
// edge of the map.
#define HUD_XR  0.9670f      // shared right edge, fraction of screen width
#define HUD_SMF 0.54f        // clock meridian-pair scale
// row 0 clock, 1 weather, 2 news, 3 context chip (the viewed place's clock,
// so it follows show_clock). Rows switched off in the settings are skipped
// and the rest close up under the clock's top edge; rows that are merely
// empty (no weather yet) keep their slot, so data arriving never shifts the
// block.
static int hud_row_on(int row) {
    return row == 1 ? cfg.show_weather : row == 2 ? cfg.show_news : cfg.show_clock;
}
// -> bottom y + glyph height
static void hud_row_geom(int row, int h, float *y, float *ch) {
    float gh = 0.040f * (float)h, chs = 0.020f * (float)h, chn = 0.0155f * (float)h;
    const float size[4] = { gh, chs, chn, chn };
    const float gap[4]  = { 0.38f * gh, 0.55f * chs, 0.62f * chn, 0.0f };  // below row r
    float top = 0.5f * (float)h + 0.5f * gh, pgap = 0.0f;
    int first = 1;
    for (int r = 0; r <= row && r < 4; r++) {
        if (r != row && !hud_row_on(r)) continue;
        if (!first) top -= pgap;
        first = 0;
        if (r == row) break;
        top -= size[r];
        pgap = gap[r];
    }
    *ch = size[row];
    *y = top - size[row];
}
struct hudrow { float x0, y0, x1, y1, a; };
static struct hudrow g_hrow[4];
static int g_nhrow = 0;
// width of an n-cell hud_string row (mono cells + 0.36 tracking)
static float hud_row_w(int n, float ch) {
    if (n <= 0 || g_hud_ch <= 0) return 0.0f;
    float gw = (float)g_hud_cw * ch / (float)g_hud_ch;
    return (float)n * gw + (float)(n - 1) * 0.36f * gw;
}
static int hud_cells(const char *cur, const char *prev, float xf) {
    int lc = (int)strlen(cur), lp = (xf < 1.0f && prev[0]) ? (int)strlen(prev) : 0;
    return lc > lp ? lc : lp;
}
// presence of a row that crossfades between strings: fading in / out rows
// occlude in step with their glyphs
static float hud_presence(const char *cur, const char *prev, float xf) {
    if (cur[0]) return prev[0] ? 1.0f : xf;
    return prev[0] ? 1.0f - xf : 0.0f;
}
// The rows drawn this frame: strings, crossfade, ink factor (times the HUD
// base alpha) and occlusion strength. The draw and the label occlusion both
// come from here, so they can't disagree.
struct hudline { int row; const char *cur, *prev; float xf, ink, occ; };
static int hud_lines(struct hudline *L) {
    int n = 0;
    if (!g_hud_tex || g_hud_vis <= 0.02f || g_hud_ch <= 0) return 0;
    double t = now_sec();
    if (cfg.show_clock && hud_cur[0])
        L[n++] = (struct hudline){ 0, hud_cur, hud_prev,
            (float)smooth01((t - hud_flip) / 0.6), 1.0f, g_hud_vis };
    int wx = g_wx_vis > 0.02f;
    if (cfg.show_weather && wx && wx_cur[0])
        L[n++] = (struct hudline){ 1, wx_cur, wx_prev,
            (float)smooth01((t - wx_flip) / 0.6), 0.82f * g_wx_vis, g_hud_vis * g_wx_vis };
    if (cfg.show_news && wx && (wx_ncur[0] || wx_nprev[0])) {
        // news row: smaller + dimmer still; absent when quiet
        float nf = (float)smooth01((t - wx_nflip) / 0.6);
        L[n++] = (struct hudline){ 2, wx_ncur, wx_nprev, nf, 0.58f * g_wx_vis,
            g_hud_vis * g_wx_vis * hud_presence(wx_ncur, wx_nprev, nf) };
    }
    if (cfg.show_clock && (wx_ccur[0] || wx_cprev[0])) {
        float cf = (float)smooth01((t - wx_cflip) / 0.6);
        L[n++] = (struct hudline){ 3, wx_ccur, wx_cprev, cf, 0.50f,
            g_hud_vis * hud_presence(wx_ccur, wx_cprev, cf) };
    }
    return n;
}
// clock row width on hud_line's grid: big cells (+ the meridian pair in 12h)
static float hud_clock_w(float ch, float xf) {
    float gw = (float)g_hud_cw * ch / (float)g_hud_ch, trk = 0.36f * gw;
    int mer = !g_hud_24;
    int lc = (int)strlen(hud_cur);
    int lp = (xf < 1.0f && hud_prev[0]) ? (int)strlen(hud_prev) : 0;
    int nb = lc - mer, np = lp ? lp - mer : 0, nm = nb > np ? nb : np;
    float wd = nm > 0 ? (float)nm * gw + (float)(nm - 1) * trk : 0.0f;
    if (mer) wd += 2.02f * gw * HUD_SMF + 0.90f * trk;
    return wd;
}
// this frame's HUD boxes (GL px, y up) — a row that isn't drawn occludes
// nothing
static void hud_layout(int w, int h) {
    struct hudline L[4];
    int n = hud_lines(L);
    float xr = HUD_XR * (float)w, y, ch;
    g_nhrow = 0;
    for (int i = 0; i < n; i++) {
        hud_row_geom(L[i].row, h, &y, &ch);
        float wd = L[i].row == 0 ? hud_clock_w(ch, L[i].xf)
                                 : hud_row_w(hud_cells(L[i].cur, L[i].prev, L[i].xf), ch);
        g_hrow[g_nhrow++] = (struct hudrow){ xr - wd, y, xr, y + ch, L[i].occ };
    }
}
// how much of a label (GL px rect) survives the HUD: 0 touching a row,
// easing back to 1 across a band of 5% of the screen height — the same
// smoothstep shape as the screen-edge fade
static float hud_clear(float x0, float y0, float x1, float y1, int h) {
    float m = 0.05f * (float)h, keep = 1.0f;
    for (int i = 0; i < g_nhrow; i++) {
        const struct hudrow *r = &g_hrow[i];
        if (r->a < 0.01f) continue;
        float dx = fmaxf(r->x0 - x1, x0 - r->x1);
        float dy = fmaxf(r->y0 - y1, y0 - r->y1);
        float d = (dx > 0.0f && dy > 0.0f) ? sqrtf(dx * dx + dy * dy) : fmaxf(dx, dy);
        keep *= 1.0f - r->a * (1.0f - (float)smooth01(d / m));
    }
    return keep;
}

// ---- HUD clock draw --------------------------------------------------------
static void hud_glyph(char c, float x, float y, float gw, float gh,
                      float alpha, int w, int h) {
    if (!c || alpha < 0.008f) return;
    const char *p = strchr(g_hud_set, c);
    if (!p) return;
    int idx = (int)(p - g_hud_set);
    float sx = 2.0f / (float)w, sy = 2.0f / (float)h;
    glUniform4f(lu_rect, -1.0f + x * sx, -1.0f + y * sy, gw * sx, gh * sy);
    glUniform4f(lu_uv, 0.0f, (float)idx / (float)g_hud_n,
                1.0f, 1.0f / (float)g_hud_n);
    glUniform1f(lu_alpha, alpha);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

// Generic right-aligned mono line: per-cell RIGHT-ALIGNED crossfade like the
// clock (chars compared from the end so appended/removed cells fade in place).
// Bytes not in the atlas (incl. space) skip the draw but keep their cell.
static void hud_string(const char *cur, const char *prev, float xf, float xr,
                       float y0, float chs, float alpha, int w, int h) {
    float sc = chs / (float)g_hud_ch;
    float gw = (float)g_hud_cw * sc;
    float trk = 0.36f * gw;
    int lc = (int)strlen(cur);
    int lp = (xf < 1.0f && prev[0]) ? (int)strlen(prev) : 0;
    int nmax = lc > lp ? lc : lp;
    float x = xr - gw;
    for (int r = 0; r < nmax; r++) {
        char cc = r < lc ? cur[lc - 1 - r] : 0;
        char pc = r < lp ? prev[lp - 1 - r] : 0;
        if (pc && pc != cc) {
            hud_glyph(pc, x, y0, gw, chs, alpha * (1.0f - xf), w, h);
            hud_glyph(cc, x, y0, gw, chs, alpha * xf, w, h);
        } else {
            hud_glyph(cc, x, y0, gw, chs, alpha, w, h);
        }
        x -= gw + trk;
    }
}

// One right-aligned readout: big "H:MM" + small meridian pair, laid out on a
// fixed mono grid from the right edge inward. cur/prev compared right-aligned
// per cell so any change (a digit, the meridian, even 9:59->10:00 shifting
// the colon) is a per-cell crossfade — geometry never jumps.
static void hud_line(float xf, float base, int w, int h) {
    float ybig, gh;                             // big glyph row (shared geometry)
    hud_row_geom(0, h, &ybig, &gh);
    float sc  = gh / (float)g_hud_ch;
    float gw  = (float)g_hud_cw * sc;
    float trk = 0.36f * gw;                     // airy tracking
    float smf = HUD_SMF;                        // meridian pair scale
    float sgw = gw * smf, sgh = gh * smf;
    // baseline-align the small pair with the big digits
    float ysm = ybig + (float)g_hud_bo * sc * (1.0f - smf);
    float colb = 0.80f + 0.20f * (float)sin(ph_hud);
    int lc = (int)strlen(hud_cur);
    int lp = (xf < 1.0f && hud_prev[0]) ? (int)strlen(hud_prev) : 0;
    int mer = !g_hud_24;                        // 24h: big cells only
    float x = HUD_XR * (float)w;
    if (mer) {
        // meridian pair, dimmer: 'M' static, A/P crossfades at noon/midnight
        x -= sgw;
        hud_glyph('M', x, ysm, sgw, sgh, base * 0.85f, w, h);
        x -= sgw * 1.02f;
        char ca = hud_cur[lc - 1], pa = lp ? hud_prev[lp - 1] : 0;
        if (pa && pa != ca) {
            hud_glyph(pa, x, ysm, sgw, sgh, base * 0.85f * (1.0f - xf), w, h);
            hud_glyph(ca, x, ysm, sgw, sgh, base * 0.85f * xf, w, h);
        } else {
            hud_glyph(ca, x, ysm, sgw, sgh, base * 0.85f, w, h);
        }
        x -= 0.90f * trk;
    }
    // big cells, right to left
    x -= gw;
    int nb = lc - mer, np = lp ? lp - mer : 0;
    int nmax = nb > np ? nb : np;
    for (int r = 0; r < nmax; r++) {
        char cc = r < nb ? hud_cur[nb - 1 - r] : 0;
        char pc = r < np ? hud_prev[np - 1 - r] : 0;
        float ac = base * (cc == ':' ? colb : 1.0f);
        float ap = base * (pc == ':' ? colb : 1.0f);
        if (pc && pc != cc) {
            hud_glyph(pc, x, ybig, gw, gh, ap * (1.0f - xf), w, h);
            hud_glyph(cc, x, ybig, gw, gh, ac * xf, w, h);
        } else {
            hud_glyph(cc, x, ybig, gw, gh, ac, w, h);
        }
        x -= gw + trk;
    }
}

// per-slot uniform + label-projection refresh for this frame
static void set_slot_uniforms(int i) {
    struct pack *s = (s_slotpk[i] >= 0) ? &packs[s_slotpk[i]] : NULL;
    int okp = s && s->gl_ok;
    glActiveTexture(GL_TEXTURE0 + UNIT_TER[i]);
    glBindTexture(GL_TEXTURE_2D, okp ? s->ter_tex : ph_ter_tex);
    glActiveTexture(GL_TEXTURE0 + UNIT_FEAT[i]);
    glBindTexture(GL_TEXTURE_2D, okp ? s->feat_tex : ph_feat_tex);
    glActiveTexture(GL_TEXTURE0 + UNIT_WAT[i]);
    glBindTexture(GL_TEXTURE_2D, (okp && s->water_tex) ? s->water_tex : ph_wat_tex);
    glActiveTexture(GL_TEXTURE0);
    double hx = cam_h * g_ar * 0.5, hy = cam_h * 0.5;
    double cx = okp ? (cam_x - s->mx0) / s->msx : 0.5;
    double cy = okp ? 1.0 - (cam_y - s->my0) / s->msy : 0.5;
    double hxu = okp ? hx / s->msx : 0.5;
    double hyu = okp ? hy / s->msy : 0.5;
    sv_cx[i] = cx; sv_cy[i] = cy; sv_hx[i] = hxu; sv_hy[i] = hyu;
    float texx = okp ? 1.0f / (float)s->ter_w : 1.0f;
    float texy = okp ? 1.0f / (float)s->ter_h : 1.0f;
    glUniform2f(u_texel[i], texx, texy);
    glUniform2f(u_tc[i], (float)cx, (float)cy);
    // sphere-offset scale: local uv = uTC + (dlam, dlogtan) * uSc
    glUniform2f(u_ts[i], okp ? (float)(1.0 / (2.0 * M_PI * s->msx)) : 0.0f,
                         okp ? (float)(1.0 / (2.0 * M_PI * s->msy)) : 0.0f);
    if (okp) {
        float fc[3];
        theme_feat(s->stL, fc);
        glUniform3fv(u_fcl[i], 1, fc);
        glUniform3f(u_fpl[i], s->stL[3], s->stL[4], s->stL[5]);
        theme_feat(s->stA, fc);
        glUniform3fv(u_fca[i], 1, fc);
        glUniform3f(u_fpa[i], s->stA[3], s->stA[4], s->stA[5]);
    } else {
        glUniform3f(u_fpl[i], 1.0f, 2.0f, 0.0f);
        glUniform3f(u_fpa[i], 1.0f, 2.0f, 0.0f);
    }
}

// cross-pack declutter: the ladder pair often carries the SAME feature with
// different baked elevations (region DEM vs leaf DEM) — drawing both reads
// as double vision with contradictory numbers (glacier descent). slot0 (the
// dominant rung) records its label boxes; slot1 skips overlapping ones.
// THE LABEL AUTHORITY (audit task #16): every label from every slot enters
// ONE screen-space arbitration per frame. Ink follows the pack's ABSOLUTE
// weight (ghost packs stop inking) and its native scale (a pack drawn far
// smaller than designed goes quiet — planetary label soup dies here); a
// priority-ordered greedy declutter runs over ALL candidates (within-pack
// included — baked collisions and limb foreshortening both resolve); the
// keep/drop verdict is EASED per label (~0.22s) so arbitration flips
// crossfade instead of popping.
#define MAXCAND (2 * PKMAX)
struct labc { int slot, k, keep;
              float x0, y0, x1, y1, a, pri, ox, oy, qw, qh; };
static struct labc g_lc[MAXCAND];
static int g_lcn = 0;

static void collect_labels(int i, float wabs, int w, int h) {
    struct pack *s = (s_slotpk[i] >= 0) ? &packs[s_slotpk[i]] : NULL;
    if (!s || !s->gl_ok || s->n_lab <= 0 || !s->atlas_tex) return;
    float aw = (float)smooth01(wabs / 0.30);
    float sg = 1.0f;
    if (s->msy < 0.999) {
        double ratio = cam_h / s->msy;
        sg = 1.0f - (float)smooth01((ratio - 2.5) / 3.5);
    }
    if (aw * sg < 0.02f) return;
    float sx = 2.0f / (float)w, sy = 2.0f / (float)h;
    for (int k = 0; k < s->n_lab; k++) {
        // per-label LOD by camera height (log-space soft gates)
        double lf = 1.0;
        if (s->lab_hmax[k] < 8e8f) {
            double x = (log((double)s->lab_hmax[k]) - log(cam_h)) / 0.25;
            lf *= smooth01(x);
        }
        if (s->lab_hmin[k] > 1e-12f) {
            double x = (log(cam_h) - log((double)s->lab_hmin[k])) / 0.25;
            lf *= smooth01(x);
        }
        if (lf < 0.02) continue;
        // one projection, same as the shader: orthographic sphere (in double,
        // so no small-form gymnastics needed; equals planar placement up close)
        double gu = s->mx0 + (double)s->lab_u[k] * s->msx;   // global merc u
        double gy = s->my0 + (1.0 - (double)s->lab_v[k]) * s->msy;
        double lon = (gu - 0.5) * 2.0 * M_PI;
        double lat = atan(sinh(M_PI * (1.0 - 2.0 * gy)));
        double dlon = lon - g_glon;
        double cla = cos(g_glat), sla = sin(g_glat);
        double cosc = sla * sin(lat) + cla * cos(lat) * cos(dlon);
        if (cosc < 0.10) continue;                   // far side of the planet
        double X = cos(lat) * sin(dlon);
        double Y = cla * sin(lat) - sla * cos(lat) * cos(dlon);
        double nx = 0.5 + X * g_radpx / (double)w;
        double ny = 0.5 + Y * g_radpx / (double)h;
        lf *= smooth01((cosc - 0.10) / 0.25);        // fade over the limb
        if (nx < -0.05 || nx > 1.05 || ny < -0.05 || ny > 1.05) continue;
        double fxm = nx < 1.0 - nx ? nx : 1.0 - nx;
        double fym = ny < 1.0 - ny ? ny : 1.0 - ny;
        double fadex = fxm / 0.09; if (fadex > 1) fadex = 1; if (fadex < 0) fadex = 0;
        double fadey = fym / 0.11; if (fadey > 1) fadey = 1; if (fadey < 0) fadey = 0;
        // the HUD block is an edge of the map too: a label under the clock/
        // weather rows fades exactly like one leaving the screen
        float lx0 = (float)(nx * w) - s->lab_ax[k];
        float ly0 = (float)(ny * h) - (s->lab_rh[k] - s->lab_ay[k]);
        float lx1 = lx0 + s->lab_rw[k], ly1 = ly0 + s->lab_rh[k];
        float hf = hud_clear(lx0, ly0, lx1, ly1, h);
        float alpha = (float)(fadex*fadex*(3-2*fadex) * fadey*fadey*(3-2*fadey) * lf)
                      * hf * aw * sg * (1.0f - 0.30f*g_night) * 0.92f;
        if (alpha < 0.015f || g_lcn >= MAXCAND) continue;
        struct labc *c = &g_lc[g_lcn++];
        c->slot = i; c->k = k; c->keep = 0;
        c->x0 = lx0; c->y0 = ly0; c->x1 = lx1; c->y1 = ly1;
        c->a = alpha;
        // bake order = rank head; a label the HUD is hiding yields its space
        // (it must not suppress a visible neighbour it can't be read over)
        c->pri = wabs * (1.0f - 0.005f * (float)k) * hf;
        c->ox = (float)(nx*2.0 - 1.0) - s->lab_ax[k]*sx;
        c->oy = (float)(ny*2.0 - 1.0) - (s->lab_rh[k]-s->lab_ay[k])*sy;
        c->qw = s->lab_rw[k]*sx; c->qh = s->lab_rh[k]*sy;
    }
}

// `ease`: only the primary output advances the per-label gates — they are
// per-pack state, and every output easing them would speed the fades up N x
static void labels_frame(int w, int h, int ease) {
    g_lcn = 0;
    hud_layout(w, h);
    collect_labels(0, g_w0v * g_lf0, w, h);
    if (s_slotpk[1] != s_slotpk[0]) collect_labels(1, g_w1v * g_lf1, w, h);
    for (int a = 1; a < g_lcn; a++) {                // insertion sort by pri
        struct labc t = g_lc[a]; int b = a - 1;
        while (b >= 0 && g_lc[b].pri < t.pri) { g_lc[b+1] = g_lc[b]; b--; }
        g_lc[b+1] = t;
    }
    float kx0[MAXCAND], ky0[MAXCAND], kx1[MAXCAND], ky1[MAXCAND];
    int kn = 0;
    for (int a = 0; a < g_lcn; a++) {
        int hit = 0;
        for (int r = 0; r < kn; r++)
            if (g_lc[a].x0 - 6.0f < kx1[r] && g_lc[a].x1 + 6.0f > kx0[r] &&
                g_lc[a].y0 - 6.0f < ky1[r] && g_lc[a].y1 + 6.0f > ky0[r])
                { hit = 1; break; }
        if (!hit) {
            g_lc[a].keep = 1;
            kx0[kn] = g_lc[a].x0; ky0[kn] = g_lc[a].y0;
            kx1[kn] = g_lc[a].x1; ky1[kn] = g_lc[a].y1; kn++;
        }
    }
    // ease every label's gate toward its verdict (uncollected labels -> 0)
    float ke = ease ? 1.0f - expf((float)(-g_mdt / 0.22)) : 0.0f;
    unsigned char tgt[2][PKMAX] = {{0}};
    for (int a = 0; a < g_lcn; a++)
        if (g_lc[a].keep) tgt[g_lc[a].slot][g_lc[a].k] = 1;
    for (int i = 0; i < 2; i++) {
        if (s_slotpk[i] < 0) continue;
        if (i == 1 && s_slotpk[1] == s_slotpk[0]) continue;
        struct pack *s = &packs[s_slotpk[i]];
        for (int k = 0; k < s->n_lab && k < PKMAX; k++)
            s->lab_vis[k] += ((float)tgt[i][k] - s->lab_vis[k]) * ke;
    }
    for (int i = 0; i < 2; i++) {                    // draw, batched per atlas
        if (s_slotpk[i] < 0) continue;
        if (i == 1 && s_slotpk[1] == s_slotpk[0]) continue;
        struct pack *s = &packs[s_slotpk[i]];
        int bound = 0;
        for (int a = 0; a < g_lcn; a++) {
            if (g_lc[a].slot != i) continue;
            float al = g_lc[a].a * s->lab_vis[g_lc[a].k];
            if (al < 0.015f) continue;
            if (!bound) {
                glActiveTexture(GL_TEXTURE1);
                glBindTexture(GL_TEXTURE_2D, s->atlas_tex);
                glActiveTexture(GL_TEXTURE0);
                bound = 1;
            }
            int k = g_lc[a].k;
            glUniform4f(lu_rect, g_lc[a].ox, g_lc[a].oy, g_lc[a].qw, g_lc[a].qh);
            glUniform4f(lu_uv, (float)s->lab_rx[k]/s->atlas_w,
                               (float)s->lab_ry[k]/s->atlas_h,
                               (float)s->lab_rw[k]/s->atlas_w,
                               (float)s->lab_rh[k]/s->atlas_h);
            glUniform1f(lu_alpha, al);
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        }
    }
}

// Blend state + quad attribute for the label program (labels, meter, HUD).
// Rebinds the quad VBO: the city-light pass leaves ARRAY_BUFFER unbound for
// its client-side points, and a HUD drawn after it read a NULL pointer.
static void label_pass_begin(void) {
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glUseProgram(lprog);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glEnableVertexAttribArray(la_pos);
    glVertexAttribPointer(la_pos, 2, GL_FLOAT, GL_FALSE, 0, 0);
}

// TOPA_SHOT=<file.ppm>: write the primary display's frame once TOPA_SHOT_T
// seconds have passed, then quit (CI renders on every platform with it).
static void maybe_shot(int w, int h) {
    static double due = -1.0;
    const char *path = getenv("TOPA_SHOT");
    if (!path || !*path) return;
    if (due < 0.0) {
        const char *t = getenv("TOPA_SHOT_T");
        due = t ? atof(t) : 8.0;
    }
    if (now_sec() < due || !g_running) return;
    unsigned char *px = malloc((size_t)w * h * 4);
    FILE *f = px ? fopen(path, "wb") : NULL;
    if (f) {
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px);
        fprintf(f, "P6\n%d %d\n255\n", w, h);
        for (int y = h - 1; y >= 0; y--)
            for (int x = 0; x < w; x++) fwrite(px + ((size_t)y * w + x) * 4, 1, 3, f);
        fclose(f);
        fprintf(stderr, "topopaper: wrote %s (%dx%d)\n", path, w, h);
    }
    free(px);
    g_running = 0;
}

// Draw the current world state into the current surface (w x h pixels).
// `primary` = the output whose geometry the world step used (the largest);
// only it eases shared state.
void engine_render(int w, int h, int primary) {
    if (w <= 0 || h <= 0) return;
    glViewport(0, 0, w, h);
    glUseProgram(prog);
    glUniform2f(u_res, (float)w, (float)h);
    glUniform1f(u_crawlm, (float)ph_crawlm);
    glUniform2f(u_fam, (float)g_fam_s, (float)g_fam_t);
    glUniform1f(u_cpu, g_cpu);
    glUniform1f(u_night, g_night);
    glUniform3f(u_w, g_w0v, g_w1v, g_w2v);
    // ortho sphere radius: centre ground scale == old planar mapping exactly
    g_radpx = (double)h / (cam_h * 2.0 * M_PI * cos(g_glat));
    glUniform1f(u_radpx, (float)g_radpx);
    glUniform2f(u_globec, (float)g_glon, (float)g_glat);
    glUniform3f(u_sun, (float)g_sunE, (float)g_sunN, (float)g_sunU);
    glUniform4f(u_sunh, g_sunh[0], g_sunh[1], g_sunh[2], g_sunh[3]);
    glUniform1f(u_snow, g_snow);
    double garr = g_radpx / (double)h;       // globe-scale gate for aurora/lights
    double gav = (2.0 - garr) / 1.3;
    g_aurf = (float)smooth01(gav < 0.0 ? 0.0 : (gav > 1.0 ? 1.0 : gav));
    glUniform3f(u_aur, g_aurf, (float)ph_aur, cfg.aurora ? 1.0f : 0.0f);
    glUniform3f(u_wrap,
        (s_slotpk[0] >= 0 && packs[s_slotpk[0]].msx > 0.999) ? 1.0f : 0.0f,
        (s_slotpk[1] >= 0 && packs[s_slotpk[1]].msx > 0.999) ? 1.0f : 0.0f,
        (s_slotpk[2] >= 0 && packs[s_slotpk[2]].msx > 0.999) ? 1.0f : 0.0f);
    for (int i = 0; i < 2; i++) {
        set_slot_uniforms(i);
        // contour scale + px-per-texel (needs this output's pixel height)
        struct pack *s = (s_slotpk[i] >= 0) ? &packs[s_slotpk[i]] : NULL;
        if (s && s->gl_ok && sv_hy[i] > 1e-12) {
            float ppt = (float)((double)h * (1.0 / (double)s->ter_h) / (2.0 * sv_hy[i]));
            glUniform4f(u_ctr[i], s->elev_lo, s->elev_hi - s->elev_lo, s->step, ppt);
        } else {
            glUniform4f(u_ctr[i], 0.0f, 0.0f, 0.0f, 0.0f);
        }
    }
    {   // guarantor slot: terrain-only uniforms
        struct pack *s = (s_slotpk[2] >= 0) ? &packs[s_slotpk[2]] : NULL;
        int okp = s && s->gl_ok;
        glActiveTexture(GL_TEXTURE0 + UNIT_TER2);
        glBindTexture(GL_TEXTURE_2D, okp ? s->ter_tex : ph_ter_tex);
        glActiveTexture(GL_TEXTURE0);
        double cx = okp ? (cam_x - s->mx0) / s->msx : 0.5;
        double cy = okp ? 1.0 - (cam_y - s->my0) / s->msy : 0.5;
        glUniform2f(u_texel2, okp ? 1.0f / (float)s->ter_w : 1.0f,
                              okp ? 1.0f / (float)s->ter_h : 1.0f);
        glUniform2f(u_tc2, (float)cx, (float)cy);
        glUniform2f(u_ts2, okp ? (float)(1.0 / (2.0 * M_PI * s->msx)) : 0.0f,
                           okp ? (float)(1.0 / (2.0 * M_PI * s->msy)) : 0.0f);
        if (okp) {
            glUniform4f(u_ctr2, s->elev_lo, s->elev_hi - s->elev_lo, 0.0f, 0.0f);
        } else {
            glUniform4f(u_ctr2, 0.0f, 1.0f, 0.0f, 0.0f);
        }
    }
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glEnableVertexAttribArray(a_pos);
    glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 0, 0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);

    // ---- labels: the ladder pair, weighted by the crossfade ----------------
    if (cam_init && cfg.labels) {
        label_pass_begin();
        // one screen-space authority over every label (guarantor unlabeled)
        labels_frame(w, h, primary);
        glDisable(GL_BLEND);
        glUseProgram(prog);
    }

    // ---- download progress meter (bottom-left, fades with build presence) --
    if (g_prog_dirty) {
        g_prog_dirty = 0;
        FILE *pf = fopen(progress_path, "rb");
        if (pf) {
            char m[8]; unsigned short pw, phh;
            if (fread(m, 1, 8, pf) == 8 && !memcmp(m, "TOPOPRG1", 8) &&
                fread(&pw, 2, 1, pf) == 1 && fread(&phh, 2, 1, pf) == 1 &&
                pw > 0 && phh > 0 && pw <= 2048 && phh <= 512) {
                size_t nb = (size_t)pw * phh * 2;
                unsigned char *buf = malloc(nb);
                if (buf && fread(buf, 1, nb, pf) == nb) {
                    if (!g_prog_tex) glGenTextures(1, &g_prog_tex);
                    upload_la(g_prog_tex, 1, pw, phh, buf, GL_LINEAR);
                    glActiveTexture(GL_TEXTURE0);
                    g_prog_w = pw; g_prog_h = phh;
                }
                free(buf);
            }
            fclose(pf);
        }
    }
    if (g_prog_a > 0.01f && g_prog_tex) {
        label_pass_begin();
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, g_prog_tex);
        glActiveTexture(GL_TEXTURE0);
        float sx = 2.0f / (float)w, sy = 2.0f / (float)h;
        glUniform4f(lu_rect, -1.0f + 28.0f * sx, -1.0f + 28.0f * sy,
                    g_prog_w * sx, g_prog_h * sy);
        glUniform4f(lu_uv, 0.0f, 0.0f, 1.0f, 1.0f);
        glUniform1f(lu_alpha, g_prog_a * 0.92f * (1.0f - 0.30f * g_night));
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        glDisable(GL_BLEND);
        glUseProgram(prog);
    }

    // ---- city lights: warm points on the globe's night side ----------------
    if (cfg.city_lights && g_lts_n > 0 && ltprog && g_aurf > 0.02f) {
        static float vb[MAXLTS * 3];
        int nv = 0;
        double sl0 = sin(g_glat), cl0 = cos(g_glat);
        for (int i = 0; i < g_lts_n; i++) {
            double dl = (double)lt_lon[i] - g_glon;
            double cf = cos((double)lt_lat[i]), sf = sin((double)lt_lat[i]);
            double U = sf * sl0 + cf * cl0 * cos(dl);
            if (U < 0.02) continue;                      // far side of the globe
            double px = 0.5 * w + g_radpx * (cf * sin(dl));
            double py = 0.5 * h + g_radpx * (cl0 * sf - sl0 * cf * cos(dl));
            if (px < 0.0 || px >= (double)w || py < 0.0 || py >= (double)h)
                continue;
            double sel = sun_elev_sin((double)lt_lat[i], (double)lt_lon[i]);
            double nfc = (0.10 - sel) / 0.22;            // shader's night ramp
            if (nfc > 1.0) nfc = 1.0;
            if (nfc < 0.05) continue;                    // day side: lights off
            double lf = U / 0.15; if (lf > 1.0) lf = 1.0;
            float a = (float)(lt_w[i] * nfc * lf) * g_aurf * 0.85f;
            vb[nv * 3]     = (float)(px / w * 2.0 - 1.0);
            vb[nv * 3 + 1] = (float)(py / h * 2.0 - 1.0);
            vb[nv * 3 + 2] = a;
            nv++;
        }
        if (nv) {
            glEnable(GL_BLEND);
            glBlendFunc(GL_ONE, GL_ONE);                 // additive glow
            glUseProgram(ltprog);
            glBindBuffer(GL_ARRAY_BUFFER, 0);
            glEnableVertexAttribArray((GLuint)lta_pos);
            glVertexAttribPointer((GLuint)lta_pos, 3, GL_FLOAT, GL_FALSE, 0, vb);
            glUniform1f(ltu_pts, 2.4f * (float)h / 960.0f);
            glDrawArrays(GL_POINTS, 0, nv);
            glDisableVertexAttribArray((GLuint)lta_pos);
            glDisable(GL_BLEND);
            glUseProgram(prog);
        }
    }

    // ---- HUD clock (screen-anchored, right-centre) -------------------------
    struct hudline L[4];
    int nl = hud_lines(L);
    if (nl) {
        label_pass_begin();
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, g_hud_tex);
        glActiveTexture(GL_TEXTURE0);
        float base = 0.80f * g_hud_vis * (1.0f - 0.30f * g_night);
        for (int i = 0; i < nl; i++) {
            if (L[i].row == 0) { hud_line(L[i].xf, base, w, h); continue; }
            // weather, news and the context chip, tucked under on the same edge
            float y0, chs;
            hud_row_geom(L[i].row, h, &y0, &chs);
            hud_string(L[i].cur, L[i].prev, L[i].xf, HUD_XR * (float)w, y0, chs,
                       base * L[i].ink, w, h);
        }
        glDisable(GL_BLEND);
        glUseProgram(prog);
    }
    if (primary) maybe_shot(w, h);
}


// ---- the API the backends drive (platform.h) ----------------------------------

// Tiered frame-rate cap (config.ini): fps_covered while covered and paused;
// flights (and their landing blend) at max(fps, 30) even on battery —
// transitions are the showcase moments; else fps_battery / fps. COVERED beats
// transitioning: a paused flight renders identical frames, and it snaps back
// to speed on the first uncovered frame.
static double frame_min_dt(void) {
    int fps;
    if (g_covered && cfg.pause_when_covered) fps = cfg.fps_covered;
    else if (fly_active)                     fps = cfg.fps > 30 ? cfg.fps : 30;
    else if (g_on_battery)                   fps = cfg.fps_battery;
    else                                     fps = cfg.fps;
    return 1.0 / (double)(fps > 0 ? fps : 1);
}

double engine_now(void) { return now_sec(); }
double engine_next_frame(void) { return t_prev + frame_min_dt(); }
int    engine_layer_background(void) { return cfg.layer_background; }

void engine_poll(void) {
    if (g_hup) { g_hup = 0; cfg_poll(1); }
    if (parent_gone()) {
        fprintf(stderr, "topopaper: the session is gone; exiting\n");
        g_running = 0;
    }
    update_targets(now_sec());
}

// Advance the world once, with a GL context current. The camera's aspect and
// the weight law's pixel height come from the largest display (w x h px);
// every display then draws the same camera at its own size.
void engine_step(int w, int h) {
    g_ar = (double)w / (double)(h > 0 ? h : 1);
    g_scr_h = h;

    // deferred pack work (needs the GL context current)
    theme_apply();
    if (g_lad_dirty) build_ladder();
    packs_gl_maint();
    if (!cam_init) {
        if (p_cur < 0 || packs[p_cur].bad || !packs[p_cur].have) p_cur = pick_initial(1);
        if (p_cur >= 0 && ensure_pack_gl(p_cur)) {
            idle_cam(p_cur, &cam_x, &cam_y, &cam_h);
            s_slotpk[0] = s_slotpk[1] = p_cur;
            s_slotpk[2] = -1;
            cam_init = 1;
            chip_next = 0.0;
            fprintf(stderr, "topopaper: camera at '%s'\n", packs[p_cur].name);
            if (pending_area[0] && !strcmp(pending_area, packs[p_cur].name))
                pending_area[0] = 0;
        } else {
            p_cur = -1;                      // nothing loadable yet: quiet gradient
        }
    }
    if (pending_area[0] && !fly_active && cam_init) {
        int tgt = find_pack(pending_area);
        if (tgt >= 0 && tgt != p_cur && ensure_pack_gl(tgt)) start_flight(tgt);
        else if (tgt != p_cur)
            fprintf(stderr, "topopaper: no such area '%s' (yet)\n", pending_area);
        pending_area[0] = '\0';
    }

    step_world();
    sun_update();                            // live sun follows the camera
    if (!cfg.labels)                         // labels re-enabled later fade in
        for (int i = 0; i < n_packs; i++) memset(packs[i].lab_vis, 0, sizeof packs[i].lab_vis);
}

// cfg.outputs is "all" or a comma-separated list of display names. Displays
// without a name (compositors before wl_output v4) can't be matched, so they
// are always drawn rather than never.
int engine_output_wanted(const char *name) {
    const char *s = cfg.outputs;
    while (*s == ' ') s++;
    if (!*s || !strcasecmp(s, "all") || !name[0]) return 1;
    char list[256];
    snprintf(list, sizeof list, "%s", cfg.outputs);
    for (char *t = strtok(list, ","); t; t = strtok(NULL, ","))
        if (!strcmp(trim(t), name)) return 1;
    return 0;
}

// ---- paths: the same rules as topopaper/paths.py ----------------------------
//   packs   <data>/areas      ($TOPOPAPER_DATA, $TOPOPAPER_DIR)
//   area    <state>/area      ($TOPOPAPER_DIR)
//   config  <conf>/config.ini ($TOPOPAPER_CONFIG)
//   (user_dirs() in compat.c: XDG on Linux/macOS, %LOCALAPPDATA% on Windows)
//   shipped data (hud.bin, lights.bin) next to the binary:
//           $PREFIX/bin/topopaper -> $PREFIX/share/topopaper ($TOPOPAPER_SHARE),
//           or <checkout>/build[/windows]/topopaper -> <checkout>/data
static void resolve_paths(void) {
    static char data[400], state[400], share[400], conf[400], run[400];
    const char *td = getenv("TOPOPAPER_DIR"), *dd = getenv("TOPOPAPER_DATA");
    const char *sd = getenv("TOPOPAPER_SHARE"), *cf = getenv("TOPOPAPER_CONFIG");
    user_dirs(data, state, cache_dir, conf, run, sizeof data);
    if (td && *td) {
        snprintf(data, sizeof data, "%s", td);
        snprintf(state, sizeof state, "%s", td);
    } else if (dd && *dd) {
        snprintf(data, sizeof data, "%s", dd);
    }
    if (cf && *cf) snprintf(cfg_path, sizeof cfg_path, "%s", cf);
    else snprintf(cfg_path, sizeof cfg_path, "%s/config.ini", conf);
    if (sd && *sd) snprintf(share, sizeof share, "%s", sd);
    else {
        // installed, then a checkout's build/ and build/windows/
        static const char *rel[] = { "../share/topopaper", "../data", "../../data" };
        char exe[300], probe[400];
        exe_dir(exe, sizeof exe);
        snprintf(share, sizeof share, "%s/%s", exe, rel[0]);
        for (int i = 0; i < 3; i++) {
            snprintf(probe, sizeof probe, "%s/%s/hud.bin", exe, rel[i]);
            if (path_exists(probe)) { snprintf(share, sizeof share, "%s/%s", exe, rel[i]); break; }
        }
    }
    snprintf(areas_dir, sizeof areas_dir, "%s/areas", data);
    snprintf(area_path, sizeof area_path, "%s/area", state);
    snprintf(hud_path, sizeof hud_path, "%s/hud.bin", share);
    snprintf(lights_path, sizeof lights_path, "%s/lights.bin", data);  // user rebuild wins
    if (!path_exists(lights_path))
        snprintf(lights_path, sizeof lights_path, "%s/lights.bin", share);
    snprintf(covered_path, sizeof covered_path, "%s/topopaper-covered", run);
    snprintf(progress_path, sizeof progress_path, "%s/topopaper-progress.bin", run);
    make_dirs(state);
}

void engine_setup(void) {
    install_signals();
    t_start = mono_sec();
    srand((unsigned)(time(NULL) ^ getpid()));
    resolve_paths();
    cfg_poll(0);
    areas_mtime = file_mtime(areas_dir);
    discover_packs();
    p_cur = pick_initial(0);
    if (p_cur < 0)
        fprintf(stderr, "topopaper: no area packs yet — quiet gradient until one appears in %s\n",
                areas_dir);
}
