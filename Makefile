# topopaper — build and install.
#
#   make                      build the engine into build/topopaper (Linux, macOS)
#   make windows              cross-build build/windows/topopaper.exe (mingw-w64)
#   make dist-windows         ...and the release zip install.ps1 downloads
#   make install              install for this user (PREFIX=~/.local)
#   sudo make install PREFIX=/usr/local
#   make uninstall            remove the installed files (never your maps/settings)
#   make check                lint + unit tests
#
# Packagers: DESTDIR and PREFIX are honoured; nothing writes outside them.

PREFIX   ?= $(HOME)/.local
DESTDIR  ?=
BINDIR    = $(PREFIX)/bin
SHAREDIR  = $(PREFIX)/share/topopaper
APPDIR    = $(PREFIX)/share/applications
ICONDIR   = $(PREFIX)/share/icons/hicolor/scalable/apps
METAINFO  = $(PREFIX)/share/metainfo

CC       ?= cc
CFLAGS   ?= -O2
override CFLAGS += -Wall -Wextra -Iengine
LAUNCHERS  = topopaper-ctl topopaper-session topopaper-settings
PYTHON    ?= python3
CORE_SRC   = engine/topopaper.c engine/compat.c

ifeq ($(shell uname -s),Darwin)
# macOS: Cocoa + the (deprecated, still shipped) OpenGL framework
ENGINE_SRC = $(CORE_SRC) engine/macos.m
override CFLAGS += -fobjc-arc -mmacosx-version-min=10.15
LDLIBS    += -framework Cocoa -framework OpenGL -framework IOKit -framework CoreFoundation
else
PKGS       = wayland-client wayland-egl egl glesv2
ENGINE_SRC = $(CORE_SRC) engine/wayland.c engine/wlr-layer-shell-protocol.c engine/xdg-shell-protocol.c
override CFLAGS += $(shell pkg-config --cflags $(PKGS))
LDLIBS    += $(shell pkg-config --libs $(PKGS)) -lm
endif

# Windows: cross-compile with mingw-w64 (or build in MSYS2 UCRT64), see
# docs/PLATFORMS.md. `make windows` -> build/windows/topopaper.exe
WIN_CC    ?= x86_64-w64-mingw32-gcc
WIN_RC    ?= x86_64-w64-mingw32-windres
WIN_SRC    = $(CORE_SRC) engine/win32.c

# install MODE SRC DST, making DST's directory (BSD install on macOS has no -D)
INST = sh -c 'mkdir -p "$$(dirname "$$3")" && cp "$$2" "$$3" && chmod "$$1" "$$3"' inst

.PHONY: all install uninstall clean check protocols windows dist-windows

all: build/topopaper

build/topopaper: $(ENGINE_SRC) $(wildcard engine/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) $(ENGINE_SRC) -o $@ $(LDFLAGS) $(LDLIBS)

windows: build/windows/topopaper.exe

WIN_CFLAGS ?= -O2
build/windows/topopaper.exe: $(WIN_SRC) $(wildcard engine/*.h) engine/win32.rc engine/topopaper.manifest
	@mkdir -p build/windows
	$(WIN_RC) -O coff engine/win32.rc -o build/windows/res.o
	$(WIN_CC) $(WIN_CFLAGS) -Wall -Wextra -Iengine $(WIN_SRC) build/windows/res.o -o $@ \
	    -mwindows -static -lopengl32 -lgdi32 -luser32 -ldwmapi

# The release zip install.ps1 downloads: topopaper\{bin,share\topopaper} plus
# uninstall.ps1, laid out like $(PREFIX) (the engine finds its data the same way).
WIN_DIST = dist/windows/topopaper
dist-windows: build/windows/topopaper.exe
	rm -rf dist/windows && mkdir -p $(WIN_DIST)/bin $(WIN_DIST)/share/topopaper
	cp build/windows/topopaper.exe windows/*.cmd $(WIN_DIST)/bin/
	cp uninstall.ps1 LICENSE README.md $(WIN_DIST)/
	find topopaper -name '*.py' -o -name '*.css' \
	    | while read f; do $(INST) 644 "$$f" "$(WIN_DIST)/share/topopaper/$$f"; done
	for f in hud.bin lights.bin fonts/JetBrainsMono-Bold.ttf fonts/OFL.txt icons/topopaper.ico; do \
	    $(INST) 644 "data/$$f" "$(WIN_DIST)/share/topopaper/$$f"; done
	cd dist/windows && $(PYTHON) -c "import shutil; shutil.make_archive('../topopaper-windows-x64', 'zip', '.', 'topopaper')"
	cd dist && sha256sum topopaper-windows-x64.zip > topopaper-windows-x64.zip.sha256
	@echo "dist/topopaper-windows-x64.zip"

# Regenerate the committed protocol code (needs wayland-scanner + wayland-protocols).
XDG_XML = $(shell pkg-config --variable=pkgdatadir wayland-protocols)/stable/xdg-shell/xdg-shell.xml
LS_XML  = engine/protocols/wlr-layer-shell-unstable-v1.xml
protocols:
	wayland-scanner private-code  $(LS_XML)  engine/wlr-layer-shell-protocol.c
	wayland-scanner client-header $(LS_XML)  engine/wlr-layer-shell-client-protocol.h
	wayland-scanner private-code  $(XDG_XML) engine/xdg-shell-protocol.c
	wayland-scanner client-header $(XDG_XML) engine/xdg-shell-client-protocol.h

install: build/topopaper
	$(INST) 755 build/topopaper $(DESTDIR)$(BINDIR)/topopaper
	for l in $(LAUNCHERS); do $(INST) 755 bin/$$l $(DESTDIR)$(BINDIR)/$$l; done
	rm -rf $(DESTDIR)$(SHAREDIR)/topopaper
	mkdir -p $(DESTDIR)$(SHAREDIR)
	cd . && find topopaper -name '*.py' -o -name '*.css' -o -name '*.ui' -o -name '*.js' \
	    | while read f; do $(INST) 644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	find data -type f ! -name '*.in' | while read f; do \
	    $(INST) 644 "$$f" "$(DESTDIR)$(SHAREDIR)/$${f#data/}"; done
	@if [ "$$(uname -s)" != Darwin ]; then \
	    $(INST) 644 data/topopaper-settings.desktop \
	        $(DESTDIR)$(APPDIR)/io.github.aidenwboudr.topopaper.desktop && \
	    $(INST) 644 data/icons/topopaper.svg \
	        $(DESTDIR)$(ICONDIR)/io.github.aidenwboudr.topopaper.svg && \
	    $(INST) 644 data/io.github.aidenwboudr.topopaper.metainfo.xml \
	        $(DESTDIR)$(METAINFO)/io.github.aidenwboudr.topopaper.metainfo.xml; fi
	@echo "installed to $(DESTDIR)$(PREFIX)"

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/topopaper
	for l in $(LAUNCHERS); do rm -f $(DESTDIR)$(BINDIR)/$$l; done
	rm -rf $(DESTDIR)$(SHAREDIR)
	rm -f $(DESTDIR)$(APPDIR)/io.github.aidenwboudr.topopaper.desktop
	rm -f $(DESTDIR)$(ICONDIR)/io.github.aidenwboudr.topopaper.svg
	rm -f $(DESTDIR)$(METAINFO)/io.github.aidenwboudr.topopaper.metainfo.xml

check:
	$(PYTHON) -m compileall -q topopaper
	$(PYTHON) -m pytest -q tests

clean:
	rm -rf build
