"""gettext setup. English strings live in the code; po/*.po hold translations.

Compiled catalogs are looked up in, in order:
  $GUITAR_STATION_LOCALEDIR
  <checkout>/locale            (`make` builds them there)
  <prefix>/share/locale        (`make install`)
Without any catalog the UI is simply English.
"""

import gettext
import locale
import os
from pathlib import Path

DOMAIN = "guitar-station"


def _localedir() -> str | None:
    here = Path(__file__).resolve().parent
    candidates = [os.environ.get("GUITAR_STATION_LOCALEDIR"),
                  here.parent / "locale",                 # checkout
                  here.parent.parent / "locale"]          # <prefix>/share/guitar-station/..
    for c in candidates:
        if c and Path(c).is_dir():
            return str(c)
    return None


try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

_t = gettext.translation(DOMAIN, localedir=_localedir(), fallback=True)
_ = _t.gettext
ngettext = _t.ngettext
