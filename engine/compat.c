// topopaper — the small OS services the engine core needs (platform.h), for
// Linux, macOS and Windows. Everything that reads /proc, /sys, the registry
// of power sources or the time-zone database lives here.
#if !defined(_WIN32) && !defined(__APPLE__)
#  define _GNU_SOURCE
#endif
#include "platform.h"
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#if defined(_WIN32)
#  include <direct.h>
#else
#  include <dirent.h>
#  include <unistd.h>
#endif
#if defined(__APPLE__)
#  include <mach/mach.h>
#  include <mach-o/dyld.h>
#  include <CoreFoundation/CoreFoundation.h>
#  include <IOKit/ps/IOPowerSources.h>
#  include <IOKit/ps/IOPSKeys.h>
#endif

// ---- clocks + files ------------------------------------------------------------
double mono_sec(void) {
#if defined(_WIN32)
    static LARGE_INTEGER f;
    LARGE_INTEGER c;
    if (!f.QuadPart) QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
#endif
}

double path_mtime(const char *path) {
#if defined(_WIN32)
    // FILETIME: 100 ns ticks since 1601 (a directory's changes when its
    // entries do, which the pack scan relies on)
    WIN32_FILE_ATTRIBUTE_DATA a;
    if (!GetFileAttributesExA(path, GetFileExInfoStandard, &a)) return -1.0;
    unsigned long long t = ((unsigned long long)a.ftLastWriteTime.dwHighDateTime << 32)
                         | a.ftLastWriteTime.dwLowDateTime;
    return (double)t / 1e7 - 11644473600.0;
#else
    struct stat st;
    if (stat(path, &st) != 0) return -1.0;
#  if defined(__APPLE__)
    return (double)st.st_mtimespec.tv_sec + st.st_mtimespec.tv_nsec / 1e9;
#  else
    return (double)st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
#  endif
#endif
}

int path_exists(const char *path) {
    struct stat st;
    return stat(path, &st) == 0;
}

static int is_sep(char c) {
#if defined(_WIN32)
    return c == '/' || c == '\\';
#else
    return c == '/';
#endif
}

static void mkdir1(const char *p) {
#if defined(_WIN32)
    _mkdir(p);
#else
    mkdir(p, 0755);
#endif
}

void make_dirs(const char *p) {
    char b[512];
    snprintf(b, sizeof b, "%s", p);
    for (char *c = b + 1; *c; c++)
        if (is_sep(*c) && c[-1] != ':') { char s = *c; *c = 0; mkdir1(b); *c = s; }
    mkdir1(b);
}

void exe_dir(char *out, size_t n) {
    char exe[1024] = "";
#if defined(_WIN32)
    DWORD k = GetModuleFileNameA(NULL, exe, sizeof exe - 1);
    exe[k < sizeof exe ? k : 0] = 0;
    for (char *c = exe; *c; c++) if (*c == '\\') *c = '/';
#elif defined(__APPLE__)
    char raw[1024];
    uint32_t sz = sizeof raw;
    if (_NSGetExecutablePath(raw, &sz) != 0 || !realpath(raw, exe)) exe[0] = 0;
#else
    ssize_t k = readlink("/proc/self/exe", exe, sizeof exe - 1);
    exe[k > 0 ? k : 0] = 0;
#endif
    char *sl = strrchr(exe, '/');
    if (sl) *sl = 0;
    snprintf(out, n, "%s", exe[0] ? exe : ".");
}

// ---- user directories (the same rules as topopaper/paths.py) ---------------------
#if !defined(_WIN32)
static void xdg(char *out, size_t n, const char *var, const char *fallback) {
    const char *v = getenv(var), *home = getenv("HOME");
    if (v && v[0] == '/') snprintf(out, n, "%s/topopaper", v);
    else snprintf(out, n, "%s/%s/topopaper", home ? home : ".", fallback);
}
#endif

void user_dirs(char *data, char *state, char *cache, char *conf, char *run, size_t n) {
#if defined(_WIN32)
    // %LOCALAPPDATA%\topopaper holds the packs, state, caches and the run
    // dir; config.ini roams in %APPDATA%\topopaper
    const char *la = getenv("LOCALAPPDATA"), *ra = getenv("APPDATA");
    char base[400];
    snprintf(base, sizeof base, "%s/topopaper", la ? la : ".");
    snprintf(data, n, "%s", base);
    snprintf(state, n, "%s/state", base);
    snprintf(cache, n, "%s/cache", base);
    snprintf(run, n, "%s/run", base);
    snprintf(conf, n, "%s/topopaper", ra ? ra : base);
#else
    xdg(data, n, "XDG_DATA_HOME", ".local/share");
    xdg(state, n, "XDG_STATE_HOME", ".local/state");
    xdg(cache, n, "XDG_CACHE_HOME", ".cache");
    xdg(conf, n, "XDG_CONFIG_HOME", ".config");
    const char *rt = getenv("XDG_RUNTIME_DIR");
    snprintf(run, n, "%s", rt && *rt ? rt : "/tmp");
#  if defined(__APPLE__)
    if (!rt || !*rt) {                       // per user, the same for launchd and a shell
        const char *home = getenv("HOME");
        snprintf(run, n, "%s/Library/Caches/topopaper/run", home ? home : "/tmp");
    }
#  endif
#endif
}

// ---- signals --------------------------------------------------------------------
#if defined(_WIN32)
static BOOL WINAPI on_ctrl(DWORD ev) {
    (void)ev;
    g_running = 0;
    return TRUE;
}
#else
static void on_signal(int s) { if (s == SIGHUP) g_hup = 1; else g_running = 0; }
#endif

void install_signals(void) {
#if defined(_WIN32)
    SetConsoleCtrlHandler(on_ctrl, TRUE);
#else
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;               // no SA_RESTART: poll() wakes at once
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);            // reload config.ini now
#endif
}

// The session that started us (TOPOPAPER_PARENT) is gone: a killed session
// must never leave an orphan engine drawing. Linux also has PDEATHSIG.
int parent_gone(void) {
    static int init = 0;
    static long pid = 0;
#if defined(_WIN32)
    static HANDLE h = NULL;
#endif
    if (!init) {
        init = 1;
        const char *v = getenv("TOPOPAPER_PARENT");
        pid = v ? atol(v) : 0;
#if defined(_WIN32)
        if (pid > 0) h = OpenProcess(SYNCHRONIZE, FALSE, (DWORD)pid);
#endif
    }
    if (pid <= 0) return 0;
#if defined(_WIN32)
    return h && WaitForSingleObject(h, 0) == WAIT_OBJECT_0;
#else
    return (long)getppid() != pid;
#endif
}

// ---- CPU load ---------------------------------------------------------------------
int cpu_times(unsigned long long *idle, unsigned long long *total) {
#if defined(_WIN32)
    FILETIME fi, fk, fu;                     // kernel time includes idle time
    if (!GetSystemTimes(&fi, &fk, &fu)) return 0;
#  define FT(f) (((unsigned long long)(f).dwHighDateTime << 32) | (f).dwLowDateTime)
    *idle = FT(fi);
    *total = FT(fk) + FT(fu);
#  undef FT
    return 1;
#elif defined(__APPLE__)
    host_cpu_load_info_data_t c;
    mach_msg_type_number_t k = HOST_CPU_LOAD_INFO_COUNT;
    if (host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, (host_info_t)&c, &k) != KERN_SUCCESS)
        return 0;
    *idle = c.cpu_ticks[CPU_STATE_IDLE];
    *total = (unsigned long long)c.cpu_ticks[CPU_STATE_USER] + c.cpu_ticks[CPU_STATE_SYSTEM]
           + c.cpu_ticks[CPU_STATE_NICE] + c.cpu_ticks[CPU_STATE_IDLE];
    return 1;
#else
    FILE *f = fopen("/proc/stat", "r");
    if (!f) return 0;
    unsigned long long u, n, s, i, io, irq, sirq, st;
    int ok = fscanf(f, "cpu %llu %llu %llu %llu %llu %llu %llu %llu",
                    &u, &n, &s, &i, &io, &irq, &sirq, &st) == 8;
    fclose(f);
    if (!ok) return 0;
    *idle = i + io;
    *total = u + n + s + i + io + irq + sirq + st;
    return 1;
#endif
}

// ---- power source -----------------------------------------------------------------
// On battery = the machine has a system battery and no adapter is online.
#if !defined(_WIN32) && !defined(__APPLE__)
// sysfs: an adapter (type Mains or USB, online=1); without any adapter entry,
// the battery reporting Discharging. Peripheral batteries (mice, headsets:
// scope=Device) are ignored; a desktop with no battery is never on battery.
static int sysfs_word(const char *dir, const char *name, char *out, size_t n) {
    char path[512];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "r");
    out[0] = 0;
    if (!f) return 0;
    int ok = fgets(out, (int)n, f) != NULL;
    fclose(f);
    char *e = strchr(out, '\n'); if (e) *e = 0;
    return ok;
}
#endif

int on_battery(void) {
#if defined(_WIN32)
    SYSTEM_POWER_STATUS ps;
    if (!GetSystemPowerStatus(&ps)) return 0;
    return ps.ACLineStatus == 0 && ps.BatteryFlag != 128 && ps.BatteryFlag != 255;
#elif defined(__APPLE__)
    CFTypeRef info = IOPSCopyPowerSourcesInfo();
    if (!info) return 0;
    CFStringRef src = IOPSGetProvidingPowerSourceType(info);
    int ob = src && CFStringCompare(src, CFSTR(kIOPSBatteryPowerValue), 0) == kCFCompareEqualTo;
    CFRelease(info);
    return ob;
#else
    const char *base = getenv("TOPA_POWER_DIR");     // rigs: a fake sysfs tree
    if (!base || !*base) base = "/sys/class/power_supply";
    DIR *d = opendir(base);
    if (!d) return 0;
    int bat = 0, dis = 0, ac = 0, ac_on = 0;
    struct dirent *e;
    char dir[400], v[32];
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        snprintf(dir, sizeof dir, "%s/%s", base, e->d_name);
        if (sysfs_word(dir, "scope", v, sizeof v) && !strcmp(v, "Device")) continue;
        if (!sysfs_word(dir, "type", v, sizeof v)) continue;
        if (!strcmp(v, "Battery")) {
            bat = 1;
            if (sysfs_word(dir, "status", v, sizeof v) && !strcmp(v, "Discharging")) dis = 1;
        } else if (!strcmp(v, "Mains") || !strcmp(v, "USB")) {
            ac = 1;
            if (sysfs_word(dir, "online", v, sizeof v) && v[0] == '1') ac_on = 1;
        }
    }
    closedir(d);
    return bat && (ac ? !ac_on : dis);
#endif
}

// ---- time zones ---------------------------------------------------------------------
// POSIX: the clock's zone is an IANA name swapped in through TZ. Windows'
// C runtime knows no IANA names, so there the clock uses the UTC offset the
// weather fetcher writes next to the name (refreshed every 15 minutes, so a
// DST change lands within that), or the system zone without one.
#if defined(_WIN32)
static long g_off = LONG_MIN;

void tz_use(const char *name, long utc_offset_s) {
    (void)name;
    g_off = utc_offset_s;
}

void utc_tm(time_t t, struct tm *out) { gmtime_s(out, &t); }

void local_tm(time_t t, struct tm *out) {
    if (g_off != LONG_MIN) utc_tm(t + (time_t)g_off, out);
    else localtime_s(out, &t);
}

int local_offset_min(time_t t) {
    if (g_off != LONG_MIN) return (int)(g_off / 60);
    struct tm l;
    localtime_s(&l, &t);
    return (int)((_mkgmtime(&l) - t) / 60);
}

int tz_offset_min(const char *name, time_t t, int *out) {
    (void)name; (void)t; (void)out;
    return 0;
}
#else
void tz_use(const char *name, long utc_offset_s) {
    (void)utc_offset_s;
    setenv("TZ", name, 1);
    tzset();
}

void utc_tm(time_t t, struct tm *out) { gmtime_r(&t, out); }
void local_tm(time_t t, struct tm *out) { localtime_r(&t, out); }

int local_offset_min(time_t t) {
    struct tm l;
    localtime_r(&t, &l);
    return (int)(l.tm_gmtoff / 60);
}

// a zone's offset by a TZ swap; the engine's own zone is restored after
int tz_offset_min(const char *name, time_t t, int *out) {
    char save[128] = "";
    const char *old = getenv("TZ");
    if (old) snprintf(save, sizeof save, "%s", old);
    setenv("TZ", name, 1); tzset();
    *out = local_offset_min(t);
    if (old) setenv("TZ", save, 1); else unsetenv("TZ");
    tzset();
    return 1;
}
#endif
