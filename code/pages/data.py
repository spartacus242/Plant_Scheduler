# pages/data.py -- Connect · Data Files: every input the system reads, at a glance.
#
# Two questions for the planner (Carsten, 2026-09-17: "look at that screen and
# quickly identify if anything is missing or stale"):
#   1. Is anything MISSING or STALE right now?  The status strip and the table
#      at the top — one row per input file, worst first — plus the live data
#      sync's heartbeat and a Sync now button.
#   2. What is this file, who feeds it, what is in it?  One expander per file
#      below, grouped the way the data arrives (plant state, demand, component
#      stock, inbound supply, master data, measured history): preview,
#      download, and upload for the planner-maintained CSVs (every write backs
#      up the old file first).
# States come from helpers.data_health.file_statuses over the registry in
# helpers.data_catalog, judged against the same cadences the Command Center
# uses, so the two pages never disagree about what "stale" means.

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from helpers import data_health as dh
from helpers import live_sync as ls
from helpers.config import load_toml
from helpers.data_catalog import (
    CATALOG,
    CSV_ENCODING,
    GROUP_LABEL,
    GROUP_ORDER,
    GROUPS,
    DataFile,
    missing_columns,
    read_csv,
    read_text_head,
)
from helpers.paths import data_dir
from helpers.safe_io import safe_write_csv
from helpers.theme import chip, render_chips

st.header("Connect — Data Files")
st.caption(
    "Step 1 of the daily loop: get the inputs in and fresh. The table lists "
    "every file Flowstate reads, worst first — red is missing or unreadable, "
    "amber is older than its expected refresh, grey is not applicable here. "
    "Below it: fix capability gaps, then preview or replace any file. The "
    "demand plan is not uploaded: the live sync builds it from the planners' "
    "AZAP workbook. Every write backs up the old file first."
)

dd = data_dir()
cfg = load_toml()
st.caption(f"Data folder: `{dd}`  |  backups: `{dd / '_backups'}`")


def backup(path: Path) -> Path:
    """Copy path into data/_backups/<stem>.<timestamp><suffix>. Returns the backup path."""
    bdir = dd / "_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


# ── Live data sync: the feed's heartbeat + Sync now ─────────────────────────
# scripts/fs-live-pull.py writes data/reference/live_sync.json after every
# pass (folder mode on the planner's PC, GitHub mode on the dev PC). The
# button runs one pass right now — the same call the calendar's "Rebuild
# from plant state" makes first — then the page re-reads every file.
_note = st.session_state.pop("data_sync_note", None)
if _note:
    (st.success if _note[0] else st.warning)(_note[1])

_sync_state = ls.read_state(dd)
_sync_configured = ls.is_configured()
_c1, _c2 = st.columns([6, 1.4], vertical_alignment="center")
with _c1:
    if _sync_state is None:
        st.caption("**Live data sync:** no sync has run on this machine — the feed "
                   "files are whatever was copied in by hand.")
    else:
        try:
            _fin = datetime.fromisoformat(str(_sync_state.get("finished")))
            _fin_age = (datetime.now() - _fin).total_seconds() / 3600.0
            _fin_txt = f"{_fin:%Y-%m-%d %H:%M} ({dh._fmt_age(_fin_age)} ago)"
        except (TypeError, ValueError):
            _fin_txt = "unknown time"
        _mode = str(_sync_state.get("mode") or "?")
        _srcs = [str(x) for x in (_sync_state.get("sources") or [])]
        _where = ("folder " + "; ".join(_srcs)) if _mode == "folder" else "GitHub bridge"
        _upd = [str(x) for x in (_sync_state.get("updated") or [])]
        _prob = [str(x) for x in (_sync_state.get("problems") or [])]
        _line = f"**Live data sync** ({_where}): last pass {_fin_txt}"
        _line += ((" — updated " + ", ".join(_upd[:5]) + ("…" if len(_upd) > 5 else ""))
                  if _upd else " — nothing new")
        if _prob or not _sync_state.get("ok", True):
            st.warning(_line + ". Problems: " + "; ".join(_prob[:3]))
        else:
            st.caption(_line + ".")
with _c2:
    if _sync_configured and st.button(
            "Sync now", type="primary",
            help="Run one live-data pull now (scripts/fs-live-pull.py --once), "
                 "then re-read every file."):
        with st.spinner("Syncing live data…"):
            _out = ls.run_sync(dd)
        st.session_state["data_sync_note"] = (_out.ok, _out.summary)
        st.rerun()


# ── Every input file: state, age, expected refresh — worst first ────────────
_SPEC = {s.key: s for s in CATALOG}
# planner-facing rows only: the legacy start-state file (initial_states.csv)
# is not shown — the staging synthesizes its own work copy and never reads it
_VISIBLE = [s for s in CATALOG if s.planner_visible]
_files = [f for f in dh.file_statuses(dd, cfg) if _SPEC[f.key].planner_visible]
_fs_by_key = {f.key: f for f in _files}
_n_bad = sum(1 for f in _files if f.state in (dh.MISSING, dh.ERROR))
_n_stale = sum(1 for f in _files if f.state == dh.STALE)
_n_ok = sum(1 for f in _files if f.state == dh.OK)
_n_opt = sum(1 for f in _files if f.state == dh.NOT_APPLICABLE)
render_chips([
    chip(f"{_n_bad} missing / unreadable", "bad" if _n_bad else "ok",
         icon="✕" if _n_bad else "●"),
    chip(f"{_n_stale} stale", "warn" if _n_stale else "ok",
         icon="▲" if _n_stale else "●"),
    chip(f"{_n_ok} present and fresh", "ok", icon="●"),
    chip(f"{_n_opt} optional or not applicable here", "neutral", icon="·"),
])

_ICON = {dh.OK: "🟢 OK", dh.STALE: "🟠 STALE", dh.MISSING: "🔴 MISSING",
         dh.ERROR: "🔴 ERROR"}


def _icon(f: dh.FileStatus) -> str:
    """The status cell. NOT_APPLICABLE reads 'optional' for an optional file
    and 'n/a here' for a required one this machine has no source for (the
    AZAP workbook in GitHub mode) — never 'optional' for the demand source."""
    if f.state == dh.NOT_APPLICABLE:
        return "⚪ optional" if _SPEC[f.key].optional else "⚪ n/a here"
    return _ICON.get(f.state, f.state)


_PAGE_ORDER = {s.key: i for i, s in enumerate(CATALOG)}


def _rank(f: dh.FileStatus) -> tuple:
    """Worst first, optional-absent last, then the page's own order."""
    sev = {dh.MISSING: 0, dh.ERROR: 0, dh.STALE: 1, dh.OK: 2,
           dh.NOT_APPLICABLE: 3}.get(f.state, 2)
    return (sev, GROUP_ORDER.get(f.group, 9), _PAGE_ORDER.get(f.key, 99))


_rows = []
for _f in sorted(_files, key=_rank):
    # column order = scan order: state, which file, how old vs how old it may
    # be, then what / where / who — the verdict reads without a scroll
    _rows.append({
        "Status": _icon(_f),
        "File": _f.filename,
        "Age": dh._fmt_age(_f.age_h) if _f.age_h is not None else "—",
        "Expected refresh": f"≤ {_f.cadence_h:g} h" if _f.cadence_h else "static",
        "What it is": _f.name,
        "Group": GROUP_LABEL.get(_f.group, _f.group),
        "Updated": _f.modified or "—",
        "Rows": f"{_f.rows:,}" if _f.rows is not None else "",
        "Source": _f.source,
        "Note": _f.detail,
    })
# Top level, not inside an expander (helpers/st_compat): the grid mounts.
# Tall enough to show every row — scanning must not need a scroll.
st.dataframe(pd.DataFrame(_rows), hide_index=True, use_container_width=True,
             height=min(38 * (len(_rows) + 1) + 4, 1300))
st.caption(
    "🔴 missing or unreadable · 🟠 older than its expected refresh (or, for the "
    "demand chain, behind its source) · 🟢 present and fresh · ⚪ optional file "
    "not present, or not applicable on this machine. Ages are file write times; the "
    "manprg rows use the as-of stamp (when the content was last observed). "
    "Expected refresh per feed: helpers/data_health.py DEFAULT_CADENCE_H, "
    "overridable in flowstate.toml under [health.cadence_h]."
)

st.divider()


# ── Line/SKU capability check (manprg + demand plan vs capabilities) ─────
# Two rules:
#   * manprg is ground truth: if manprg shows an SKU on a line the table
#     doesn't allow, the table is out of date (user decision B).
#   * demand SKUs with NO capable line anywhere silently drop their demand —
#     flag them too; the one-click fix uses plant evidence (historical PDF
#     schedules) to assign a proven line when available.
try:
    from helpers.capability_check import (
        check_capabilities,
        check_demand_capabilities,
        fix_demand_rows,
        fix_rows_for,
        load_capabilities,
        load_manprg_mos,
        load_plan_evidence,
        merge_fixes,
    )
    _caps_df = load_capabilities(dd / "reference" / "capabilities_rates.csv")
    _ds_cfg = cfg.get("datasources", {})
    _mp_paths = [p.strip() for p in str(_ds_cfg.get("manprg_files", "")).split(";")
                 if p.strip()] or [str(dd / "reference" / "manprg.txt"),
                                   str(dd / "reference" / "manprg2.txt")]
    _mos = load_manprg_mos(_mp_paths)
    _cap_res = check_capabilities(_caps_df, _mos)
    _dem_df = None
    _dem_path = dd / "reference" / "demand_plan.csv"
    if _dem_path.exists():
        _dem_df = pd.read_csv(_dem_path, dtype={"sku": str})
        _dem_res = check_demand_capabilities(_caps_df, _dem_df)
    else:
        _dem_res = None
    _all_conflicts = list(_cap_res.conflicts)
    if _dem_res is not None:
        _all_conflicts += _dem_res.conflicts
    _fixable_kinds = {"SKU_MISSING", "LINE_NOT_CAPABLE", "DEMAND_NO_CAPABLE_LINE"}
    if _all_conflicts:
        st.warning(
            f"⚠️ **{len(_all_conflicts)} Line/SKU capability issue(s)** — "
            "manprg SKU/line pairs the table does not allow, and demand SKUs "
            "with no capable line at all. The plant physically ran them / "
            "needs them scheduled, so the table is out of date."
        )
        st.dataframe(pd.DataFrame(
            [{"sku": c.sku, "line_name": c.line_name, "kind": c.kind,
              "mo": c.mo, "detail": c.detail} for c in _all_conflicts]),
            use_container_width=True, hide_index=True)
        _fix = st.button(
            "Add these proven SKU/line pairs to capabilities (one-click fix)",
            type="primary")
        if _fix:
            # default rate for NEW rows: average rate of all SKUs on each
            # proven line (user decision) — existing rows keep their rate and
            # just flip capable=1. Lines come from the changed rows themselves
            # (manprg lines AND demand-evidence lines like P16).
            _changed = fix_rows_for(_caps_df,
                                    [c for c in _cap_res.conflicts
                                     if c.kind in ("SKU_MISSING",
                                                   "LINE_NOT_CAPABLE")],
                                    default_rate=0.0)
            _unfixable: list[str] = []
            if _dem_res is not None and _dem_res.conflicts:
                _evidence = load_plan_evidence(
                    dd / "reference" / "sku_plan_evidence.csv")
                _dem_changed, _unfixable = fix_demand_rows(
                    _caps_df, _dem_res.conflicts,
                    evidence=_evidence, default_rate=0.0)
                _changed = pd.concat([_changed, _dem_changed],
                                     ignore_index=True)
            _default_by_line = {
                ln: float(_caps_df.loc[
                    (_caps_df["line_name"] == ln)
                    & (pd.to_numeric(_caps_df.get("capable", 0),
                                     errors="coerce").fillna(0) == 1),
                    "calc_rate_kgph"].mean() or 0.0)
                for ln in sorted({str(r["line_name"]) for _, r in
                                  _changed.iterrows()
                                  if int(r["capable"]) == 1})
            }
            # apply the per-line average default where fix_rows_for left 0
            _zero_rate = (_changed["capable"].astype(int) == 1) \
                & (_changed["calc_rate_kgph"].astype(float) == 0.0)
            if _zero_rate.any():
                _changed.loc[_zero_rate, "calc_rate_kgph"] = [
                    _default_by_line.get(ln, 0.0)
                    for ln in _changed.loc[_zero_rate, "line_name"]]
            # Merge: flip capable / set rate on the existing rows, append new
            # (sku, line) pairs that were missing entirely.
            _out = merge_fixes(_caps_df, _changed)
            _bdir = dd / "_backups"
            _bdir.mkdir(parents=True, exist_ok=True)
            _dst = _bdir / f"capabilities_rates.{datetime.now():%Y%m%d-%H%M%S}.csv"
            _dst.write_bytes((dd / "reference" / "capabilities_rates.csv").read_bytes())
            _out.to_csv(dd / "reference" / "capabilities_rates.csv", index=False)
            _msg = (f"Updated capabilities_rates.csv ({len(_changed)} rows changed). "
                    "Backup saved. Reloading…")
            if _unfixable:
                _msg = (f"⚠️ {_msg}  Could NOT auto-fix demand SKU(s) "
                        f"{', '.join(_unfixable)} — no plant evidence of a "
                        "capable line (not in manprg or any historical "
                        "schedule). Verify the SKU or add lines manually.")
            st.success(_msg)
            st.rerun()
    else:
        st.caption("✅ Line/SKU capability check: manprg SKU/line pairs all match "
                   "the capabilities table, and every demand SKU has a capable line.")
except Exception as _cap_exc:  # noqa: BLE001
    st.caption(f"Capability check unavailable: {_cap_exc}")


st.divider()


# ── One expander per file, grouped by where the data comes from ────────────
_DOWNLOAD_MAX = 1_000_000  # bytes; bigger files are opened from the folder
_MIME = {".csv": "text/csv", ".txt": "text/plain", ".toml": "text/plain",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12"}


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _render_file(spec: DataFile, fs: dh.FileStatus, *, uploadable: bool) -> None:
    path = fs.path
    exists = fs.state not in (dh.MISSING, dh.NOT_APPLICABLE)
    meta = []
    if fs.rows is not None:
        meta.append(f"{fs.rows:,} " + ("rows" if spec.kind == "csv" else "lines"))
    if fs.modified:
        meta.append(f"updated {fs.modified}")
    if fs.age_h is not None:
        meta.append(f"{dh._fmt_age(fs.age_h)} ago")
    header = f"{_icon(fs)} · {spec.name} — {fs.filename}" \
        + (f"  ({' · '.join(meta)})" if meta else "")

    with st.expander(header):
        st.caption(spec.blurb)
        line = f"Source: {spec.source}." if spec.source else ""
        line += (f" Expected refresh: ≤ {fs.cadence_h:g} h." if fs.cadence_h
                 else " Static reference — age shown, never judged stale.")
        if fs.detail:
            line += f" **{fs.detail}**"
        st.caption(line)
        st.caption(f"Path: `{path}`")
        if spec.key_columns:
            st.caption("Required columns: " + ", ".join(spec.key_columns))
        if uploadable and spec.bridge_synced:
            st.caption(
                "⚠ Also auto-synced by the live data pull — a manual upload "
                "here may be overwritten the next time the plant feed lands."
            )

        if uploadable:
            up = st.file_uploader(
                "Replace this file",
                type=["csv"],
                key=f"up_{spec.key}",
                label_visibility="collapsed",
            )
            if up is not None:
                raw = up.getvalue()
                try:
                    new_df = pd.read_csv(io.BytesIO(raw), encoding=CSV_ENCODING, dtype=str, keep_default_na=False)
                except Exception as exc:
                    new_df = None
                    st.error(f"Not a readable CSV: {type(exc).__name__}: {exc}")
                if new_df is not None:
                    gaps = missing_columns(new_df, spec)
                    if gaps:
                        st.error("Missing required column(s): " + ", ".join(gaps))
                    else:
                        st.success(f"Parsed OK: {len(new_df)} rows, {len(new_df.columns)} columns.")
                        # st.table: st.dataframe never mounts inside an initially-
                        # collapsed expander (helpers/st_compat).
                        st.table(new_df.head(10))
                        if st.button("Overwrite " + spec.filename, key=f"ovr_{spec.key}"):
                            target = spec.path(dd)  # uploads land on the PRIMARY name
                            if target.exists():
                                b = backup(target)
                                st.caption(f"Backed up to `{b.name}`")
                            safe_write_csv(new_df, target)
                            st.success(f"Replaced {spec.rel()}")
                            st.rerun()

        if not exists:
            if fs.state == dh.NOT_APPLICABLE and spec.optional:
                st.info("Optional file — not present. Nothing breaks without it; "
                        "the description above says what it would add.")
            elif fs.state == dh.NOT_APPLICABLE:
                st.info(f"Not applicable on this machine — {fs.detail}")
            elif spec.folder == "drop":
                st.warning("Not in the drop: the planners drop their weekly export "
                           "'New Export AZAP MMDDYY.xlsx' into the fs_manual folder "
                           "named above (the sync reads it in place, it is never "
                           "delivered), then press Sync now.")
            elif uploadable:
                st.warning("File does not exist yet -- upload one above.")
            else:
                st.warning("File does not exist yet — it arrives with the live "
                           "data sync (or Flowstate writes it).")
            return
        if fs.state == dh.ERROR:
            st.error(f"Could not read the file: {fs.detail}")
            return

        # Read-only preview + download. Planner edits happen in Excel and come
        # back through the upload above (every write is backed up first).
        # st.table / st.code, never st.dataframe: the grid does not mount
        # inside an initially-collapsed expander (helpers/st_compat).
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xlsm"):
            st.caption(f"Excel workbook, {_fmt_size(fs.size)} — no in-app preview.")
        elif spec.kind in ("text", "toml") or suffix in (".txt", ".toml"):
            try:
                head = read_text_head(path, 15)
            except OSError as exc:
                st.error(f"Could not read the file: {exc}")
                return
            st.code("\n".join(head), language=None)
            if fs.rows is not None and fs.rows > 15:
                st.caption(f"… first 15 of {fs.rows:,} lines — download to see all.")
        else:
            try:
                df = read_csv(spec, dd, path=path)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read the file: {type(exc).__name__}: {exc}")
                return
            st.table(df.head(20))
            if len(df) > 20:
                st.caption(f"… {len(df) - 20:,} more row(s) — download to see all.")
        if fs.size is not None and fs.size <= _DOWNLOAD_MAX:
            st.download_button(
                "Download",
                data=path.read_bytes(),
                file_name=path.name,
                mime=_MIME.get(suffix, "application/octet-stream"),
                key=f"dl_{spec.key}",
            )
        else:
            st.caption(f"Large file ({_fmt_size(fs.size)}) — open it from the "
                       "path above.")


st.subheader("Every input file, by where it comes from")
st.caption(
    "Open a file for its description, preview and download. Planner-maintained "
    "tables take an upload (the old file is backed up first); feed files are "
    "read-only here — they arrive with the live data sync."
)
for _gkey, _glabel, _gcaption in GROUPS:
    _specs = [s for s in _VISIBLE if s.group == _gkey]
    if not _specs:
        continue
    st.markdown(f"#### {_glabel}")
    _cap = _gcaption
    if _gkey == "demand":
        # the live chain in one line (2026-09-18): which workbook, its export
        # date, whether the summary is its build, when the plan was derived
        try:
            _dp = dh.demand_provenance(dd, cfg, ls.drop_folders())
            _wb = _dp["workbook"]
            _parts = []
            if _wb is not None:
                _parts.append(f"Workbook in use: `{_wb['workbook_name']}` (export "
                              f"{_wb['export_date']:%Y-%m-%d}, weeks W{_wb['weeks'][0]}–W{_wb['weeks'][-1]}) "
                              f"in `{_wb['folder']}`")
                _parts.append("summary built from it: **yes**" if _wb["built_from"] else
                              ("summary: **hand edit newer than every workbook** (kept)" if _wb["hand_edit"]
                               else "summary built from it: **no — the next sync pass rebuilds it**"))
            elif _dp["folders"]:
                _parts.append("**No AZAP workbook** in " + " or ".join(f"`{f}`" for f in _dp["folders"]))
            else:
                _parts.append("No drop folder on this machine (GitHub mode): the summary arrives already built")
            _src = _dp["source"]
            if _src:
                _parts.append(f"demand_plan.csv derived {str(_src.get('imported', ''))[:16].replace('T', ' ')}, "
                              f"anchor W{_src.get('anchor_iso_week')}, {_src.get('rows')} orders"
                              + (" — **summary newer, press Sync now**" if _dp["summary_newer_than_plan"] else ""))
            if _parts:
                _cap += " " + " · ".join(_parts) + "."
        except Exception:  # noqa: BLE001 — a caption, never a failure
            pass
    if _gkey == "stock":
        # the folder the Stock Check actually reads (saved setting → bridge
        # copies in data/reference → bundled dev fixtures)
        try:
            from helpers.reconcile_engine import stock_report_inputs
            _vif, _ = stock_report_inputs(dd)
            _cap += f" Folder in use: `{_vif}`."
            if Path(_vif).name == "dev_vif":
                _cap += (" ⚠ These are the bundled dev fixtures, not live plant "
                         "data — the live VIF exports have not landed.")
        except Exception:  # noqa: BLE001 — a caption, never a failure
            pass
    st.caption(_cap)
    for _spec in _specs:
        _render_file(_spec, _fs_by_key[_spec.key],
                     uploadable=(_spec.managed_by == "user"))
