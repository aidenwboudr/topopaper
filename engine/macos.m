// topopaper — the macOS backend (platform.h): one borderless window per
// screen at the desktop window level (above the system wallpaper, below the
// Finder's desktop icons and every app), on every Space, click-through,
// drawn with NSOpenGL (legacy 2.1 profile: deprecated by Apple, still
// shipped). One context per screen, all sharing the first one's objects.
//
// No Dock icon (accessory app). Screen changes rebuild the windows; the
// windows' occlusion state tells the engine when apps cover the wallpaper.
#define GL_SILENCE_DEPRECATION 1
#import <Cocoa/Cocoa.h>
#include "platform.h"
#include <math.h>

// never key or main (it can't be activated), and free to cover the menu bar
@interface TopoWindow : NSWindow
@end
@implementation TopoWindow
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
- (NSRect)constrainFrameRect:(NSRect)r toScreen:(NSScreen *)s { (void)s; return r; }
@end

@interface TopoView : NSView
@end
@implementation TopoView
- (BOOL)isOpaque { return YES; }
- (NSView *)hitTest:(NSPoint)p { (void)p; return nil; }
@end

@interface TopoScreen : NSObject
@property (strong) TopoWindow *window;
@property (strong) TopoView *view;
@property (strong) NSOpenGLContext *ctx;
@property (copy) NSString *name;
@property int w, h;
@end
@implementation TopoScreen
@end

static NSMutableArray<TopoScreen *> *screens;
static NSOpenGLPixelFormat *pixfmt;
static NSOpenGLContext *shared;     // owns the programs/textures; never destroyed
static int rebuild = 1;

static void destroy_screens(void) {
    [NSOpenGLContext clearCurrentContext];
    for (TopoScreen *s in screens) {
        [s.ctx clearDrawable];
        [s.window orderOut:nil];
        [s.window close];
    }
    [screens removeAllObjects];
}

static void size_screen(TopoScreen *s) {
    NSSize px = [s.view convertRectToBacking:s.view.bounds].size;
    s.w = (int)lround(px.width);
    s.h = (int)lround(px.height);
}

static void build_screens(void) {
    destroy_screens();
    for (NSScreen *scr in [NSScreen screens]) {
        NSString *name = @"display";
        if (@available(macOS 10.15, *)) name = scr.localizedName;
        if (!engine_output_wanted(name.UTF8String)) continue;
        NSRect fr = scr.frame;
        TopoWindow *w = [[TopoWindow alloc] initWithContentRect:fr
                                                  styleMask:NSWindowStyleMaskBorderless
                                                    backing:NSBackingStoreBuffered
                                                      defer:NO];
        [w setFrame:fr display:NO];
        // one above the system wallpaper (same base level), well below the
        // Finder's icons (+20)
        w.level = CGWindowLevelForKey(kCGDesktopWindowLevelKey) + 1;
        w.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                               NSWindowCollectionBehaviorStationary |
                               NSWindowCollectionBehaviorIgnoresCycle;
        w.ignoresMouseEvents = YES;
        w.opaque = YES;
        w.hasShadow = NO;
        w.releasedWhenClosed = NO;
        w.canHide = NO;                      // Cmd-H elsewhere must not take it away
        w.hidesOnDeactivate = NO;
        w.restorable = NO;
        w.animationBehavior = NSWindowAnimationBehaviorNone;
        w.backgroundColor = NSColor.blackColor;
        TopoView *v = [[TopoView alloc] initWithFrame:NSMakeRect(0, 0, fr.size.width, fr.size.height)];
        v.wantsBestResolutionOpenGLSurface = YES;
        w.contentView = v;
        [w orderFrontRegardless];

        NSOpenGLContext *ctx = [[NSOpenGLContext alloc] initWithFormat:pixfmt shareContext:shared];
        if (!ctx) { fprintf(stderr, "topopaper: no OpenGL context for %s\n", name.UTF8String); continue; }
        GLint zero = 0;                      // the engine paces itself
        [ctx setValues:&zero forParameter:NSOpenGLContextParameterSwapInterval];
        ctx.view = v;
        [ctx makeCurrentContext];
        engine_gl_context();

        TopoScreen *s = [TopoScreen new];
        s.window = w; s.view = v; s.ctx = ctx; s.name = name;
        size_screen(s);
        [screens addObject:s];
        fprintf(stderr, "topopaper: drawing on %s (%dx%d px)\n", name.UTF8String, s.w, s.h);
    }
}

static void frame(void) {
    TopoScreen *prim = nil;
    long best = -1;
    for (TopoScreen *s in screens) {
        long px = (long)s.w * s.h;
        if (px > best) { best = px; prim = s; }
    }
    if (!prim) return;
    [prim.ctx makeCurrentContext];
    engine_step(prim.w, prim.h);
    NSMutableArray *order = [NSMutableArray arrayWithObject:prim];   // primary first
    for (TopoScreen *s in screens) if (s != prim) [order addObject:s];
    for (TopoScreen *s in order) {
        [s.ctx makeCurrentContext];
        engine_render(s.w, s.h, s == prim);
        [s.ctx flushBuffer];
    }
}

// covered = no part of any of our windows is visible
static int all_covered(void) {
    if (!screens.count) return 0;
    for (TopoScreen *s in screens)
        if (s.window.occlusionState & NSWindowOcclusionStateVisible) return 0;
    return 1;
}

int main(void) {
    @autoreleasepool {
        engine_setup();
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [NSApp finishLaunching];
        screens = [NSMutableArray array];

        NSOpenGLPixelFormatAttribute attrs[] = {
            NSOpenGLPFADoubleBuffer, NSOpenGLPFAColorSize, 24, NSOpenGLPFAAlphaSize, 8,
            NSOpenGLPFAOpenGLProfile, NSOpenGLProfileVersionLegacy,
            NSOpenGLPFAAllowOfflineRenderers, 0 };
        pixfmt = [[NSOpenGLPixelFormat alloc] initWithAttributes:attrs];
        shared = pixfmt ? [[NSOpenGLContext alloc] initWithFormat:pixfmt shareContext:nil] : nil;
        if (!shared) { fprintf(stderr, "topopaper: no OpenGL on this Mac\n"); return 1; }
        [shared makeCurrentContext];
        engine_gl_init();

        NSNotificationCenter *nc = NSNotificationCenter.defaultCenter;
        [nc addObserverForName:NSApplicationDidChangeScreenParametersNotification object:nil
                         queue:nil usingBlock:^(NSNotification *n) { (void)n; rebuild = 1; }];
        // after wake and unlock the window server can drop the all-Spaces
        // membership and the occlusion state goes stale: start over
        NSNotificationCenter *wc = NSWorkspace.sharedWorkspace.notificationCenter;
        for (NSString *what in @[NSWorkspaceDidWakeNotification,
                                 NSWorkspaceSessionDidBecomeActiveNotification,
                                 NSWorkspaceScreensDidWakeNotification])
            [wc addObserverForName:what object:nil queue:nil
                        usingBlock:^(NSNotification *n) { (void)n; rebuild = 1; }];
        g_covered_src = 0;

        double next_tick = 0.0, next_check = 0.0;
        while (g_running) {
            @autoreleasepool {
                engine_poll();
                if (g_out_dirty) { g_out_dirty = 0; rebuild = 1; }
                if (rebuild) {
                    rebuild = 0;
                    build_screens();
                    next_tick = 0.0;
                }
                double now = engine_now();
                if (now >= next_check) {
                    next_check = now + 1.0;
                    g_covered_src = all_covered();
                    for (TopoScreen *s in screens) {         // backing scale changes
                        int ow = s.w, oh = s.h;
                        size_screen(s);
                        if (s.w != ow || s.h != oh) [s.ctx update];
                    }
                }
                if (engine_now() >= next_tick) {
                    frame();
                    next_tick = engine_next_frame();
                }
                double wait = next_tick - engine_now();
                if (wait > 0.4) wait = 0.4;
                if (wait < 0.0) wait = 0.0;
                NSDate *until = [NSDate dateWithTimeIntervalSinceNow:wait];
                NSEvent *ev;
                while ((ev = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:until
                                                   inMode:NSDefaultRunLoopMode dequeue:YES])) {
                    [NSApp sendEvent:ev];
                    until = [NSDate distantPast];
                }
            }
        }
        destroy_screens();
    }
    return 0;
}
