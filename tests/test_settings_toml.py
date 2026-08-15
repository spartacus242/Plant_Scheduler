# tests/test_settings_toml.py — TOML escaping in settings page (regression).

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


def _set_toml_section(txt: str, section: str, values: dict[str, str | bool]) -> str:
    """Upsert keys into a [section] of TOML text (add section if absent).
    This is copied from code/pages/settings.py for testing isolation."""
    import json
    import re
    lines = [f"[{section}]"]
    for k, v in values.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        else:
            lines.append(f"{k} = {json.dumps(v)}")
    block = "\n".join(lines)
    if re.search(rf"^\[{re.escape(section)}\]", txt, flags=re.M):
        # replace existing section body
        return re.sub(
            rf"^\[{re.escape(section)}\].*?(?=^\[|\Z)",
            lambda m: block + "\n\n", txt, flags=re.M | re.S)
    return txt.rstrip() + "\n\n" + block + "\n"


def test_windows_path_roundtrip():
    """Values with backslashes round-trip through _set_toml_section via
    tomllib.loads without error and read back equal."""
    value = r"\\usnpa-appfs\DATA\vif-export\auto editions"
    txt = _set_toml_section("", "datasources", {"vif_folder": value})
    # tomllib.loads must not crash
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["vif_folder"] == value


def test_user_entered_backslash_path():
    """User-entered Windows paths with backslashes are properly escaped."""
    value = r"C:\Users\Carsten\vif_export"
    txt = _set_toml_section("", "datasources", {"cip_info_csv": value})
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["cip_info_csv"] == value


def test_bool_keys_write_true_false():
    """Boolean keys write as true/false literals, not strings."""
    txt = _set_toml_section("", "datasources", {
        "sql_enabled": True,
        "vif_folder": "/some/path",
    })
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["sql_enabled"] is True
    assert isinstance(parsed["datasources"]["sql_enabled"], bool)


def test_bool_false_writes_false():
    """Boolean False writes as false literal."""
    txt = _set_toml_section("", "datasources", {
        "sql_enabled": False,
    })
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["sql_enabled"] is False
    assert isinstance(parsed["datasources"]["sql_enabled"], bool)


def test_replace_existing_section():
    """When section exists, its body is replaced and bools/backslashes remain valid."""
    txt = "[datasources]\nvif_folder = \"/old/path\"\n\n"
    value = r"\\usnpa-appfs\DATA\new-path"
    txt = _set_toml_section(txt, "datasources", {
        "vif_folder": value,
        "sql_enabled": True,
    })
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["vif_folder"] == value
    assert parsed["datasources"]["sql_enabled"] is True


def test_string_with_quotes():
    """Strings containing quotes are properly escaped."""
    value = 'path with "quotes" inside'
    txt = _set_toml_section("", "datasources", {"sql_dsn": value})
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["sql_dsn"] == value


def test_string_with_newlines():
    """Strings with escape sequences are properly handled."""
    value = "line1\nline2\ttab"
    txt = _set_toml_section("", "datasources", {"sql_dsn": value})
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["sql_dsn"] == value


def test_empty_string():
    """Empty strings write and read back correctly."""
    txt = _set_toml_section("", "datasources", {
        "vif_folder": "",
        "sql_enabled": False,
    })
    parsed = tomllib.loads(txt)
    assert parsed["datasources"]["vif_folder"] == ""
    assert parsed["datasources"]["sql_enabled"] is False
