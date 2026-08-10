# code/pages/settings.py — data-source connections (Carsten's machine).
#
# Writes the [datasources] section of flowstate.toml. Every importer reads its
# path/connection from here, so pointing Flowstate at a new file/SQL is a UI
# action, not a code edit.

from __future__ import annotations

import re
import sys
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers.config import datasources_config, load_toml  # noqa: E402
from helpers.paths import toml_path  # noqa: E402

st.header("Settings — Data Sources")
st.caption(
    "Point Flowstate at the files and SQL connections on this machine. "
    "Saved to `flowstate.toml` under `[datasources]`."
)

cfg = load_toml()
ds = datasources_config(cfg)

vif = st.text_input("VIF export folder (ediact/jestkexp/azapart/rmpkitems)",
                    value=ds["vif_folder"])
manprg = st.text_input("manprg report files (two paths, ';'-separated)",
                       value=ds["manprg_files"],
                       help="e.g. C:\\...\\manprg.txt;C:\\...\\manprg2.txt")
pdf = st.text_input("Production-schedule PDF folder", value=ds["schedule_pdf_folder"])
cip = st.text_input("CIP info CSV", value=ds["cip_info_csv"])

st.divider()
st.subheader("Live SQL (optional)")
st.caption(
    "Direct connection to VIF (NPA) for auto-refreshing MO progress and the CIP "
    "schedule. Leave disabled to use the file imports above.")
sql_enabled = st.checkbox("Enable SQL live refresh", value=bool(ds["sql_enabled"]))
sql_dsn = st.text_input("ODBC connection string (DSN)", value=ds["sql_dsn"],
                        type="password",
                        help="e.g. DRIVER={ODBC Driver 17 for SQL Server};SERVER=NPA-PMUTSQL3901.bel.com,62272;DATABASE=NPA;Trusted_Connection=yes;")


def _set_toml_section(txt: str, section: str, values: dict[str, str | bool]) -> str:
    """Upsert keys into a [section] of TOML text (add section if absent)."""
    lines = [f"[{section}]"]
    for k, v in values.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        else:
            lines.append(f'{k} = "{v}"')
    block = "\n".join(lines)
    if re.search(rf"^\[{re.escape(section)}\]", txt, flags=re.M):
        # replace existing section body
        return re.sub(
            rf"^\[{re.escape(section)}\].*?(?=^\[|\Z)",
            block + "\n\n", txt, flags=re.M | re.S)
    return txt.rstrip() + "\n\n" + block + "\n"


if st.button("Save data sources", type="primary"):
    tp = toml_path()
    txt = tp.read_text(encoding="utf-8") if tp.exists() else ""
    txt = _set_toml_section(txt, "datasources", {
        "vif_folder": vif,
        "manprg_files": manprg,
        "schedule_pdf_folder": pdf,
        "cip_info_csv": cip,
        "sql_enabled": sql_enabled,
        "sql_dsn": sql_dsn,
    })
    tp.write_text(txt, encoding="utf-8")
    st.success(f"Saved to `{tp.name}`. Reload the app for importers to pick it up.")
    st.rerun()

# status: which sources currently resolve
st.divider()
st.subheader("Source status")
checks = {
    "VIF folder": vif,
    "AZAP CSV": azap,
    "manprg files": manprg,
    "CIP info CSV": cip,
}
rows = []
for name, val in checks.items():
    if not val:
        rows.append((name, "—", "not set (using dev fixtures)"))
        continue
    paths = [p.strip() for p in val.split(";") if p.strip()]
    ok = all(Path(p).exists() for p in paths)
    rows.append((name, val[:50], "OK" if ok else "NOT FOUND"))
st.dataframe({"Source": [r[0] for r in rows],
              "Path": [r[1] for r in rows],
              "Status": [r[2] for r in rows]},
             use_container_width=True, hide_index=True)
