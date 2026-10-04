# topopaper — build and install.
#
#   make                      build the engine into build/topopaper
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
PKGS      = wayland-client wayland-egl egl glesv2
CFLAGS   ?= -O2
CFLAGS   += -Wall -Wextra -Iengine $(shell pkg-config --cflags $(PKGS))
LDLIBS   += $(shell pkg-config --libs $(PKGS)) -lm

ENGINE_SRC = engine/topopaper.c engine/wlr-layer-shell-protocol.c engine/xdg-shell-protocol.c
LAUNCHERS  = topopaper-ctl topopaper-session topopaper-settings
PYTHON    ?= python3

.PHONY: all install uninstall clean check protocols

all: build/topopaper

build/topopaper: $(ENGINE_SRC) $(wildcard engine/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) $(ENGINE_SRC) -o $@ $(LDFLAGS) $(LDLIBS)

# Regenerate the committed protocol code (needs wayland-scanner + wayland-protocols).
XDG_XML = $(shell pkg-config --variable=pkgdatadir wayland-protocols)/stable/xdg-shell/xdg-shell.xml
LS_XML  = engine/protocols/wlr-layer-shell-unstable-v1.xml
protocols:
	wayland-scanner private-code  $(LS_XML)  engine/wlr-layer-shell-protocol.c
	wayland-scanner client-header $(LS_XML)  engine/wlr-layer-shell-client-protocol.h
	wayland-scanner private-code  $(XDG_XML) engine/xdg-shell-protocol.c
	wayland-scanner client-header $(XDG_XML) engine/xdg-shell-client-protocol.h

install: build/topopaper
	install -Dm755 build/topopaper $(DESTDIR)$(BINDIR)/topopaper
	for l in $(LAUNCHERS); do install -Dm755 bin/$$l $(DESTDIR)$(BINDIR)/$$l; done
	rm -rf $(DESTDIR)$(SHAREDIR)/topopaper
	mkdir -p $(DESTDIR)$(SHAREDIR)
	cd . && find topopaper -name '*.py' -o -name '*.css' -o -name '*.ui' -o -name '*.js' \
	    | while read f; do install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$$f"; done
	find data -type f ! -name '*.in' | while read f; do \
	    install -Dm644 "$$f" "$(DESTDIR)$(SHAREDIR)/$${f#data/}"; done
	@if [ -f data/topopaper-settings.desktop ]; then \
	    install -Dm644 data/topopaper-settings.desktop \
	        $(DESTDIR)$(APPDIR)/io.github.aidenwboudr.topopaper.desktop; fi
	@if [ -f data/icons/topopaper.svg ]; then \
	    install -Dm644 data/icons/topopaper.svg \
	        $(DESTDIR)$(ICONDIR)/io.github.aidenwboudr.topopaper.svg; fi
	@if [ -f data/io.github.aidenwboudr.topopaper.metainfo.xml ]; then \
	    install -Dm644 data/io.github.aidenwboudr.topopaper.metainfo.xml \
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
