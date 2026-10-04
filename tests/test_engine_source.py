"""Static contracts between the C engine and the Python side."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from topopaper import config, themes  # noqa: E402

ENGINE = (ROOT / "engine" / "topopaper.c").read_text(encoding="utf-8")


def _cfg_set_body():
    start = ENGINE.index("static void cfg_set(")
    return ENGINE[start:ENGINE.index("\n}\n", start)]


def test_engine_reads_only_schema_keys():
    body = _cfg_set_body()
    keys = set(re.findall(r"\b(?:STR|BOOL)\((\w+)\)", body))
    keys |= set(re.findall(r'strcmp\(k, "(\w+)"\)', body))
    keys.discard("name")                     # the macro parameter itself
    assert keys, "cfg_set parse failed"
    unknown = keys - set(config.BY_NAME)
    assert not unknown, f"engine reads keys missing from config.SCHEMA: {unknown}"


def test_themes_header_in_sync():
    header = (ROOT / "engine" / "themes.h").read_text(encoding="utf-8")
    assert header == themes.c_header(), "regenerate: python3 -m topopaper.themes > engine/themes.h"


def test_engine_theme_choices_match_schema():
    assert tuple(themes.THEMES) == config.BY_NAME["theme"].choices


def test_engine_has_no_stale_script_references():
    banned = ["make-area.py", "topo-search", "area-fly", "make-hud.py",
              "topopaper-watch", "build.sh"]
    found = [b for b in banned if b.lower() in ENGINE.lower()]
    assert not found, f"engine still mentions {found}"
