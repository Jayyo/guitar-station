# Installs into ~/.local by default:  make install / make uninstall
PREFIX  ?= $(HOME)/.local
DESTDIR ?=
PYTHON  ?= /usr/bin/python3

APP_ID   = io.github.jayyo.GuitarStation
BINDIR   = $(PREFIX)/bin
DATADIR  = $(PREFIX)/share
PKGDIR   = $(DATADIR)/guitar-station
LANGS    = $(basename $(notdir $(wildcard po/*.po)))
MO       = $(foreach l,$(LANGS),locale/$(l)/LC_MESSAGES/guitar-station.mo)

.PHONY: all check pot install uninstall clean

all: $(MO)

locale/%/LC_MESSAGES/guitar-station.mo: po/%.po
	@mkdir -p $(dir $@)
	msgfmt --check -o $@ $<

check:
	$(PYTHON) -m pytest -q

pot:
	xgettext --language=Python --keyword=_ --from-code=UTF-8 --no-location \
	  --sort-output --package-name=guitar-station -o po/guitar-station.pot guitarstation/*.py

install: all
	install -d $(DESTDIR)$(PKGDIR)/guitarstation $(DESTDIR)$(BINDIR)
	install -m 644 guitarstation/*.py $(DESTDIR)$(PKGDIR)/guitarstation/
	install -m 755 bin/guitar-station bin/guitar-monitor $(DESTDIR)$(BINDIR)/
	sed -i '1s|.*|#!$(PYTHON)|' $(DESTDIR)$(BINDIR)/guitar-station $(DESTDIR)$(BINDIR)/guitar-monitor
	install -d $(DESTDIR)$(DATADIR)/applications $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps
	sed 's|@BINDIR@|$(BINDIR)|' data/$(APP_ID).desktop.in > $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop
	install -m 644 data/$(APP_ID).svg $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/
	$(foreach l,$(LANGS),install -D -m 644 locale/$(l)/LC_MESSAGES/guitar-station.mo \
	  $(DESTDIR)$(DATADIR)/locale/$(l)/LC_MESSAGES/guitar-station.mo;)

uninstall:
	rm -rf $(DESTDIR)$(PKGDIR)
	rm -f $(DESTDIR)$(BINDIR)/guitar-station $(DESTDIR)$(BINDIR)/guitar-monitor
	rm -f $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop
	rm -f $(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	$(foreach l,$(LANGS),rm -f $(DESTDIR)$(DATADIR)/locale/$(l)/LC_MESSAGES/guitar-station.mo;)

clean:
	rm -rf locale .pytest_cache
