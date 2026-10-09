// topopaper — the seam between the engine core (topopaper.c) and the code
// that is different on every OS.
//
//   topopaper.c   the world, the packs, the HUD and the drawing; knows GL
//                 but no window system
//   compat.c      small OS services: clocks, time zones, paths, CPU load,
//                 power source, signals (Linux, macOS, Windows)
//   wayland.c     Linux: a wlr-layer-shell surface per output, EGL + GLES2
//   win32.c       Windows: a window per monitor behind the desktop icons, WGL
//   macos.m       macOS: a desktop-level window per screen, NSOpenGL
//
// A backend owns main(): it calls engine_setup(), makes a GL context and
// calls engine_gl_init() with it current, then loops: engine_poll(), and
// when engine_next_frame() is due, engine_step() once and engine_render()
// into every surface, swapping each afterwards.
#ifndef TOPOPAPER_PLATFORM_H
#define TOPOPAPER_PLATFORM_H

#include <signal.h>
#include <time.h>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#  include <GL/gl.h>
#  include <GL/glext.h>
#  define TOPA_DESKTOP_GL 1
// opengl32.dll exports GL 1.1 only; everything newer the engine uses is
// fetched from the driver by gl_load() (win32.c) once a context is current.
#  define TOPA_GL_FUNCS(X) \
    X(PFNGLACTIVETEXTUREPROC, glActiveTexture) \
    X(PFNGLATTACHSHADERPROC, glAttachShader) \
    X(PFNGLBINDBUFFERPROC, glBindBuffer) \
    X(PFNGLBUFFERDATAPROC, glBufferData) \
    X(PFNGLCOMPILESHADERPROC, glCompileShader) \
    X(PFNGLCREATEPROGRAMPROC, glCreateProgram) \
    X(PFNGLCREATESHADERPROC, glCreateShader) \
    X(PFNGLDISABLEVERTEXATTRIBARRAYPROC, glDisableVertexAttribArray) \
    X(PFNGLENABLEVERTEXATTRIBARRAYPROC, glEnableVertexAttribArray) \
    X(PFNGLGENBUFFERSPROC, glGenBuffers) \
    X(PFNGLGETATTRIBLOCATIONPROC, glGetAttribLocation) \
    X(PFNGLGETPROGRAMINFOLOGPROC, glGetProgramInfoLog) \
    X(PFNGLGETPROGRAMIVPROC, glGetProgramiv) \
    X(PFNGLGETSHADERINFOLOGPROC, glGetShaderInfoLog) \
    X(PFNGLGETSHADERIVPROC, glGetShaderiv) \
    X(PFNGLGETUNIFORMLOCATIONPROC, glGetUniformLocation) \
    X(PFNGLLINKPROGRAMPROC, glLinkProgram) \
    X(PFNGLSHADERSOURCEPROC, glShaderSource) \
    X(PFNGLUNIFORM1FPROC, glUniform1f) \
    X(PFNGLUNIFORM1IPROC, glUniform1i) \
    X(PFNGLUNIFORM2FPROC, glUniform2f) \
    X(PFNGLUNIFORM3FPROC, glUniform3f) \
    X(PFNGLUNIFORM3FVPROC, glUniform3fv) \
    X(PFNGLUNIFORM4FPROC, glUniform4f) \
    X(PFNGLUNIFORM4FVPROC, glUniform4fv) \
    X(PFNGLUNIFORMMATRIX4FVPROC, glUniformMatrix4fv) \
    X(PFNGLUSEPROGRAMPROC, glUseProgram) \
    X(PFNGLVERTEXATTRIBPOINTERPROC, glVertexAttribPointer)
#  define TOPA_GL_DECL(type, name) extern type topa_##name;
TOPA_GL_FUNCS(TOPA_GL_DECL)
#  undef TOPA_GL_DECL
#  define glActiveTexture            topa_glActiveTexture
#  define glAttachShader             topa_glAttachShader
#  define glBindBuffer               topa_glBindBuffer
#  define glBufferData               topa_glBufferData
#  define glCompileShader            topa_glCompileShader
#  define glCreateProgram            topa_glCreateProgram
#  define glCreateShader             topa_glCreateShader
#  define glDisableVertexAttribArray topa_glDisableVertexAttribArray
#  define glEnableVertexAttribArray  topa_glEnableVertexAttribArray
#  define glGenBuffers               topa_glGenBuffers
#  define glGetAttribLocation        topa_glGetAttribLocation
#  define glGetProgramInfoLog        topa_glGetProgramInfoLog
#  define glGetProgramiv             topa_glGetProgramiv
#  define glGetShaderInfoLog         topa_glGetShaderInfoLog
#  define glGetShaderiv              topa_glGetShaderiv
#  define glGetUniformLocation       topa_glGetUniformLocation
#  define glLinkProgram              topa_glLinkProgram
#  define glShaderSource             topa_glShaderSource
#  define glUniform1f                topa_glUniform1f
#  define glUniform1i                topa_glUniform1i
#  define glUniform2f                topa_glUniform2f
#  define glUniform3f                topa_glUniform3f
#  define glUniform3fv               topa_glUniform3fv
#  define glUniform4f                topa_glUniform4f
#  define glUniform4fv               topa_glUniform4fv
#  define glUniformMatrix4fv         topa_glUniformMatrix4fv
#  define glUseProgram               topa_glUseProgram
#  define glVertexAttribPointer      topa_glVertexAttribPointer
#elif defined(__APPLE__)
#  define GL_SILENCE_DEPRECATION 1
#  include <OpenGL/gl.h>
#  define TOPA_DESKTOP_GL 1
#else
#  include <GLES2/gl2.h>
#endif

// ---- engine core (topopaper.c) ------------------------------------------------
extern volatile sig_atomic_t g_running;     // 0: leave the main loop
extern volatile sig_atomic_t g_hup;         // 1: re-read config.ini now
extern int g_out_dirty;                     // the output set/layer changed: re-select
extern int g_out_retry;                     // ...deliberately: retry refused outputs
extern int g_covered_src;                   // -1: covered flag file, else the backend's 0/1

void   engine_setup(void);                  // paths, settings, pack scan (no GL yet)
void   engine_gl_init(void);                // programs + data, first context current
void   engine_gl_context(void);             // per-context state for an extra shared context
void   engine_poll(void);                   // cheap state (~2.5x/s internally)
int    engine_output_wanted(const char *name);
int    engine_layer_background(void);       // config: layer = background
double engine_now(void);                    // seconds since engine_setup()
double engine_next_frame(void);             // engine_now() the next frame is due at
void   engine_step(int w, int h);           // advance the world once (largest view, px)
void   engine_render(int w, int h, int primary);   // draw into the current surface
int    engine_snapshot_due(void);           // `topopaper-ctl snapshot` is waiting
void   engine_snapshot(int w, int h);       // answer it into the current surface (px)

// ---- OS services (compat.c) ---------------------------------------------------
double mono_sec(void);                      // monotonic seconds
double path_mtime(const char *path);        // -1 when missing
int    path_exists(const char *path);
void   make_dirs(const char *path);
void   exe_dir(char *out, size_t n);        // directory of the running binary
// topopaper's own user directories (conf holds config.ini; run is shared,
// its files are named topopaper-*): XDG on Linux and macOS, %LOCALAPPDATA%
// and %APPDATA% on Windows. The same rules as topopaper/paths.py.
void   user_dirs(char *data, char *state, char *cache, char *conf, char *run, size_t n);
void   install_signals(void);
int    cpu_times(unsigned long long *idle, unsigned long long *total);
int    on_battery(void);
int    parent_gone(void);                   // the supervising session died
// time zones: the HUD clock follows location.json, not the system zone
void   tz_use(const char *name, long utc_offset_s);   // offset: LONG_MIN = unknown
void   local_tm(time_t t, struct tm *out);
int    local_offset_min(time_t t);
int    tz_offset_min(const char *name, time_t t, int *out);  // 0: can't tell
void   utc_tm(time_t t, struct tm *out);

#endif
