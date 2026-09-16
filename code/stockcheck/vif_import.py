# code/stockcheck/vif_import.py — Import VIF text exports into typed frames.
#
# Dialects verified against the real exports (see .hermes/plans/stock-check-design.md
# and the ERP drop of 2026-09-15 under fs_data/fs_vif):
#   ediact.csv                  : ';', cp1252, NO header, 10 cols without the
#                                 flow type, exact duplicate lines (x2-4) ->
#                                 dropped + counted. It does NOT carry the
#                                 semi-finished (HSM/RF) recipes: 24 of the 27
#                                 ediact 4 activities are absent (2026-09-16)
#   ediact 3.csv / ediact 4.csv : ';', cp1252, header row (pre-2026-09 names);
#                                 ediact 4 stays the recipe SUPPLEMENT
#   jestk*.csv                  : ';', cp1252, header row; column ORDER differs
#                                 per report so columns are mapped by header
#                                 name; jestkexp5.csv is '|'-delimited
#   PKG-REC.csv                 : ';', selection-recall preamble + header, then
#                                 receipt-slip rows of exactly 9 fields
#   azapart.csv                 : ';', cp1252, NO header (Column1..Column8)
#   rmpkitems.csv               : ',', cp1252, 7 junk preamble rows, then header
# Every jestk*/ediact export ends with a "Page1/1 " footer row; an empty report
# is blank lines plus "No data corresponds to your selection. Page1/1" (134 B).
#
# Item codes are STRINGS everywhere ('730009-A', 'BT002'). Never numeric.

from __future__ import annotations

import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

EDIACT_COLS = [
    "PF", "Activity", "flow_type", "effective_date", "family",
    "item_type", "item", "designation", "qty_act", "unit",
]

JESTKEXP_COLS = [  # raw materials
    "item", "designation", "scn", "status", "statut", "bbd",
    "batch", "qty", "unit", "depot", "location",
]

JESTKEXP2_COLS = [  # packaging: Batch/BBD before Status
    "item", "designation", "scn", "batch", "bbd", "status", "statut",
    "qty", "unit", "depot", "location",
]

# Every lot export (raw materials, packaging, AMB off-site, QC depots, fresh
# apples, semi-finished WIP) lands in ONE column contract (drop of 2026-09-15)
# so coverage/timeline can treat them alike; 'source' names the file.
LOT_COLS = [
    "item", "designation", "scn", "status", "statut", "bbd", "batch",
    "qty", "unit", "depot", "location", "precaut", "supplier_batch", "source",
]
# Columns only some reports carry (kept when present, after LOT_COLS):
# jestksav.csv adds the apple provenance, jestkexp5.csv the item family.
LOT_EXTRA_COLS = ("variety", "supplier_no", "supplier_name", "apple_receipt",
                  "family")

# Header cell (normalised: lower, whitespace-collapsed) -> LOT column. The
# reports name the same thing differently: Batch/BATCH, 'Qty (MU)'/Quantity,
# Unit/UOM, Status/'Batch status' (jestkexp5), SupplierBatch, and the apple
# report's 'RAW APPLE RECEIVING #'. Unknown headers (jestkex2's RECDate) drop.
_LOT_HEADERS = {
    "item": "item", "designation": "designation", "description": "designation",
    "scn": "scn", "status": "status", "batch status": "status",
    "statut": "statut", "bbd": "bbd", "batch": "batch",
    "qty (mu)": "qty", "quantity": "qty", "qty": "qty",
    "unit": "unit", "uom": "unit", "depot": "depot", "location": "location",
    "precaut": "precaut", "supplierbatch": "supplier_batch",
    "supplier batch": "supplier_batch", "variety": "variety",
    "supplier number": "supplier_no", "supplier name": "supplier_name",
    "raw apple receiving #": "apple_receipt", "family": "family",
}
# jestkexp5's 'Batch status' prints AVA where every other report prints Ava;
# coverage keys are case-sensitive ("AMB|Ava"), so fold the case. OL stays OL.
_STATUS_CASE = {"AVA": "Ava", "LOC": "Loc", "OUT": "Out"}

PKG_REC_COLS = ["receipt_date", "item", "designation", "batch", "qty", "unit",
                "po8", "slip", "user", "source"]

# ---- file names ----------------------------------------------------------
# The BOM comes from the first of BOM_FILES that loads WITH rows: 'ediact.csv'
# (2026-09 export) else the pre-2026-09 'ediact 3.csv' — an empty or
# unreadable ediact.csv no longer hides a good ediact 3 (2026-09-16). Either
# lands in frames["ediact 3.csv"] so every consumer keeps one key.
BOM_FILES = ("ediact.csv", "ediact 3.csv")
# The semi-finished (HSM/RF) recipes. ediact.csv does NOT fold them in (24 of
# the 27 activities are absent from the 2026-09-15 export), so this file is
# the recipe SUPPLEMENT, loaded whenever it exists beside either BOM export;
# when recipes are missing the import says so (missing_semi_recipes, 2026-09-16).
HSM_FILE = "ediact 4.csv"
# House-made intermediate item codes (case-insensitive prefixes). User rule of
# 2026-08-14: such an item never gates a SKU on its own stock — the BOM
# explodes through its recipe, so a missing recipe changes the report.
SEMI_PREFIXES = ("HSM", "RF")
# Exports the stock check cannot work without (2026-09-16): one missing is an
# ERROR "<name>: missing from <folder>" — the pre-drop loader's "[Errno 2]"
# drove the Stock Check import-error chip and the drop loader had gone
# silent. The BOM counts as present when either BOM_FILES name exists,
# jestkexp.csv when its alias does. Every other export (jestkamb, jestkexq,
# jestksav, jestkexp5, PKG-REC, ediact 4, rmpkitems) is optional: absent,
# no error, no note.
REQUIRED_FILES = (BOM_FILES[0], "jestkexp.csv", "jestkexp2.csv", "azapart.csv")
# Lot files in lot_frames() order: rm, pkg, off-site AMB, QC, apples, semi.
LOT_FILES = ("jestkexp.csv", "jestkexp2.csv", "jestkamb.csv", "jestkexq.csv",
             "jestksav.csv", "jestkexp5.csv")
# Alias used only when the canonical file is missing: jestkexp4.csv is a
# byte-identical copy of jestkexp.csv, jestksa.csv = jestksav.csv minus the
# four provenance columns (drop of 2026-09-15).
LOT_FALLBACKS = {"jestkexp.csv": "jestkexp4.csv", "jestksav.csv": "jestksa.csv"}
RECEIPTS_FILE = "PKG-REC.csv"
# Present in the drop but never loaded — each gets a note so the page can
# say why (drop of 2026-09-15).
IGNORED_FILES = {
    "jestkex2.csv": "stale 2025 packaging snapshot (RECDate always blank)",
    "jestkami.csv": "depot AMI report, not used by the stock check",
    "jestkavg.csv": "not used by the stock check",
    "jestktgb.csv": "not used by the stock check",
}
# Every recognised name, old and new, for mtime gating (refresh_vif_snapshot
# and the report cache sign these): a file appearing, vanishing or being
# re-exported must move the signature even when it is only an alias.
VIF_FILES = (BOM_FILES + (HSM_FILE,) + LOT_FILES
             + tuple(LOT_FALLBACKS.values()) + (RECEIPTS_FILE,)
             + ("azapart.csv", "rmpkitems.csv") + tuple(IGNORED_FILES))


@dataclass
class VifSnapshot:
    frames: dict[str, pd.DataFrame]
    source_files: dict[str, str]  # filename -> mtime ISO
    imported_at: str
    errors: list[str] = field(default_factory=list)
    # Non-fatal import remarks (ignored aliases, empty exports, dropped
    # duplicates) — drop of 2026-09-15. Snapshots pickled before then lack
    # the attribute; __setstate__ backfills it, callers may still getattr().
    notes: list[str] = field(default_factory=list)
    # Semi-finished item codes the BOM consumes without a recipe
    # (missing_semi_recipes, 2026-09-16). Deliberately NOT in .errors: the
    # refresh gate re-imports errored files on every call.
    missing_recipes: list[str] = field(default_factory=list)

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__dict__.setdefault("notes", [])
        self.__dict__.setdefault("missing_recipes", [])


_NO_DATA = "No data corresponds to your selection"
_FOOTER_RE = re.compile(r"^\s*Page\s*\d", re.IGNORECASE)   # "Page1/1 "
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _norm(cell) -> str:
    """Header cell key: whitespace collapsed, lower-cased, trimmed."""
    return re.sub(r"\s+", " ", str(cell)).strip().casefold()


def _read_semicolon(path: Path, header: int | None = 0) -> pd.DataFrame:
    return pd.read_csv(path, delimiter=";", encoding="cp1252",
                       dtype=str, keep_default_na=False, header=header)


def _sniff_delimiter(path: Path, default: str = ";") -> str:
    """'|' when the first non-blank line carries more pipes than semicolons
    (jestkexp5.csv, drop of 2026-09-15); ';' otherwise."""
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for line in f:
            if line.strip():
                return "|" if line.count("|") > line.count(";") else default
    return default


def _is_empty_export(path: Path) -> bool:
    """A VIF report with no rows prints only its selection recall and
    'No data corresponds to your selection. Page1/1' (134 bytes). Only small
    files are inspected; a real export is never that short."""
    try:
        if path.stat().st_size > 4096:
            return False
        text = path.read_text(encoding="cp1252", errors="replace")
    except OSError:
        return False
    return _NO_DATA in text or not text.strip()


def _drop_footer(df: pd.DataFrame) -> pd.DataFrame:
    """Remove the 'Page1/1 ' footer, 'No data…' rows and all-blank rows
    (the pre-2026-09 jestkexp ended with ';;;;;;;;;;')."""
    if df.empty:
        return df
    first = df.iloc[:, 0].astype(str).str.strip()
    footer = (first.str.match(_FOOTER_RE.pattern, case=False)
              | first.str.contains(_NO_DATA, regex=False))
    blank = df.apply(lambda c: c.astype(str).str.strip().eq("")).all(axis=1)
    return df[~(footer | blank)]


def _read_table(path: Path, delimiter: str | None = None) -> pd.DataFrame:
    """Raw VIF table: every cell a string, header NOT interpreted (row 0 is
    whatever the file starts with), footer/blank rows gone. An empty export
    -> a 0-column frame, never an error."""
    path = Path(path)
    if _is_empty_export(path):
        return pd.DataFrame()
    delimiter = delimiter or _sniff_delimiter(path)
    try:
        df = pd.read_csv(path, delimiter=delimiter, encoding="cp1252",
                         dtype=str, keep_default_na=False, header=None)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return _drop_footer(df).reset_index(drop=True)


def _looks_like_dates(series: pd.Series) -> bool:
    """>= 80 % of the first 50 non-blank cells look like d/m/yyyy."""
    vals = series.astype(str).str.strip()
    vals = vals[vals.ne("")].head(50)
    if vals.empty:
        return False
    return float(vals.str.match(_DATE_RE.pattern).mean()) >= 0.8


def _num(series: pd.Series) -> pd.Series:
    """VIF numeric column → float. The live export writes thousands with a
    comma ('1,600' cases) which to_numeric refuses — that single quirk made
    8,403 of 15,267 BOM quantities NaN and every explosion UNK (2026-08-14).
    Decimals are dot-separated throughout, so stripping commas is safe.
    Always float64: an all-integer column (the dev jestkexp2 quantities)
    came back int64 before the 2026-09-15 lot contract pinned qty float."""
    return pd.to_numeric(
        series.str.strip().str.replace(",", "", regex=False).replace("", None),
        errors="coerce").astype("float64")


def _dates(series: pd.Series) -> pd.Series:
    """VIF date column → datetime, auto-detecting %d/%m/%Y vs %m/%d/%Y.

    One live export mixes conventions per column (measured 2026-08-14:
    ediact effective_date is day-first, jestkexp BBD is month-first), so the
    convention is chosen per column: whichever format leaves fewer NaT wins;
    ties keep the legacy day-first."""
    s = series.str.strip()
    dayfirst = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")
    monthfirst = pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")
    return monthfirst if monthfirst.isna().sum() < dayfirst.isna().sum() else dayfirst


_EDIACT_NOFLOW = ["PF", "Activity", "effective_date", "family",
                  "item_type", "item", "designation", "qty_act", "unit"]


def _empty_ediact() -> pd.DataFrame:
    df = pd.DataFrame(columns=EDIACT_COLS + ["freinte"]).astype(
        {"qty_act": "float64", "effective_date": "datetime64[ns]",
         "freinte": "float64"})
    df.attrs["dropped_duplicates"] = 0
    df.attrs["empty_export"] = True
    return df


def load_ediact(path: Path) -> pd.DataFrame:
    """BOM activities from either export generation:
      * 'ediact 3.csv' (until 2026-09): header row, flow-type column at
        index 2 (always 'Pull'), sometimes a trailing 'Freinte';
      * 'ediact.csv' (drop of 2026-09-15): NO header, 10 columns WITHOUT the
        flow type — PF;Activity;Effective date;Famille;Item type;Item;
        Designation;Qty;Unit;Freinte — and 8,443 exact duplicate lines
        (x2-4) the old export never had. It does NOT carry the semi-finished
        recipes (24 of the 27 ediact 4 activities absent, verified
        2026-09-16): 'ediact 4.csv' still supplies them.
    Layout: a header is recognised by its first cell 'PF'; the flow-type
    column is absent when the header says 'Effective date' at index 2 or,
    headerless, when the third column parses as dates. Output: EDIACT_COLS
    (+ 'freinte' when the export carries it; flow_type '' when absent).
    Exact duplicate rows are dropped and counted in
    df.attrs['dropped_duplicates']; an empty export -> empty frame."""
    raw = _read_table(path, ";")
    if raw.empty:
        return _empty_ediact()
    first = [_norm(c) for c in raw.iloc[0].tolist()]
    has_header = first[0] == "pf"
    if has_header:
        raw = raw.iloc[1:]
        no_flow = len(first) > 2 and "effective" in first[2]
    else:
        no_flow = raw.shape[1] > 2 and _looks_like_dates(raw.iloc[:, 2])
    before = len(raw)
    raw = raw.drop_duplicates()
    dropped = before - len(raw)
    names = _EDIACT_NOFLOW if no_flow else EDIACT_COLS
    base = len(names)
    if raw.shape[1] < base:
        raise ValueError(f"{Path(path).name}: expected at least {base} "
                         f"columns, found {raw.shape[1]}")
    # Freinte (waste %): named in the header when there is one, else the
    # one column after the base layout in the headerless export.
    fre_idx = None
    if has_header:
        fre_idx = next((i for i, h in enumerate(first)
                        if i >= base and "freinte" in h), None)
    elif raw.shape[1] > base:
        fre_idx = base
    df = raw.iloc[:, :base].copy()
    df.columns = names
    if fre_idx is not None:
        df["freinte"] = _num(raw.iloc[:, fre_idx].astype(str))
    if no_flow:
        df.insert(2, "flow_type", "")
    df["qty_act"] = _num(df["qty_act"].astype(str))
    df["effective_date"] = _dates(df["effective_date"].astype(str))
    for c in ("PF", "Activity", "flow_type", "family", "item_type", "item",
              "unit"):
        df[c] = df[c].astype(str).str.strip()
    cols = EDIACT_COLS + (["freinte"] if "freinte" in df.columns else [])
    df = df[cols].reset_index(drop=True)
    df.attrs["dropped_duplicates"] = int(dropped)
    return df


EDIACT4_COLS = [
    # Same export family as ediact 3 but WITHOUT the flow-type column:
    # PF;Activity;Effective date;Famille;Item type;Item;Designation;Qty;(unit);Freinte
    "PF", "Activity", "effective_date", "family",
    "item_type", "item", "designation", "qty_act", "unit",
]


def load_ediact4(path: Path) -> pd.DataFrame:
    """ediact 4 = the semi-finished (HSM) recipes: Activity is the HSM code,
    Input rows its components per batch, the Output row the batch basis
    (e.g. 1,000 kg of slurry). Feeds BomGraph so HSM inputs explode through
    to their sub-components instead of counting as unstocked leaves.

    Same parser as load_ediact (its header names the no-flow-type layout);
    the output keeps the legacy EDIACT4_COLS shape — no flow_type — plus
    'freinte' when present, so BomGraph's HSM branch is untouched. The
    2026-09-15 export 'ediact.csv' does NOT fold these recipes in (only 3
    of the 27 activities are in it, 21 of the absent codes appear there as
    Input items with no activity — verified 2026-09-16), so this file stays the recipe
    SUPPLEMENT beside either BOM generation; missing_semi_recipes() names
    what is left unexploded without it."""
    df = load_ediact(path)
    attrs = dict(df.attrs)
    cols = EDIACT4_COLS + (["freinte"] if "freinte" in df.columns else [])
    df = df[cols].reset_index(drop=True)
    df.attrs.update(attrs)
    return df


def _empty_lots(source: str) -> pd.DataFrame:
    df = pd.DataFrame(columns=LOT_COLS).astype(
        {"qty": "float64", "bbd": "datetime64[ns]"})
    df["source"] = df["source"].astype(object)
    df.attrs["empty_export"] = True
    df.attrs["source"] = source
    return df


def load_jestkexp(path: Path, packaging: bool = False, *,
                  source: str | None = None) -> pd.DataFrame:
    """Any VIF lot export — jestkexp (raw materials, 13 cols), jestkexp2
    (packaging, 12: Batch/BBD before Status), jestkamb (AMB off-site, 13),
    jestkexq (QC depots, 12), jestksav (fresh apples, 17: + Variety /
    Supplier Number / Supplier Name / RAW APPLE RECEIVING #), jestksa (13),
    jestkexp5 (semi-finished WIP, '|'-delimited: Item|SCN|BATCH|Quantity|
    UOM|Designation|Batch status|Family|Depot|Location|BBD).

    Columns are mapped BY HEADER NAME (drop of 2026-09-15) because the order
    differs per report; when the header is not recognised the legacy
    positional layout applies (JESTKEXP2_COLS if `packaging` else
    JESTKEXP_COLS, first 11 columns). Output: LOT_COLS (+ LOT_EXTRA_COLS the
    report carries), qty float with NaN -> 0.0, bbd datetime, status case
    folded (AVA -> Ava; OL stays), 'source' = the file name. The 'Page1/1'
    footer and blank rows are gone; an empty export -> empty frame.

    BBD is MONTH-FIRST (2026-09-16): every jestk export of the 2026-09-15
    drop writes MM/DD/YYYY (jestkexp: 9,401 of 18,703 second fields > 12,
    none in the first), so a column of only ambiguous dates ('09/02/2026')
    must read 2 Sep, not 9 Feb — the per-column auto-detect tie went
    day-first. Auto-detect remains the fallback when month-first leaves
    values unparsed (the dev fixture's day-first '16/04/2027')."""
    source = source or Path(path).name
    raw = _read_table(path)
    if raw.empty:
        return _empty_lots(source)
    mapped = [_LOT_HEADERS.get(_norm(c)) for c in raw.iloc[0].tolist()]
    if {"item", "qty", "depot"} <= {m for m in mapped if m}:
        cols = mapped
    else:  # legacy: row 0 is still the (unrecognised) header, drop it
        legacy = JESTKEXP2_COLS if packaging else JESTKEXP_COLS
        cols = legacy[:raw.shape[1]]
    body = raw.iloc[1:]
    df = pd.DataFrame(index=body.index)
    for i, name in enumerate(cols):
        if name and name not in df.columns:
            df[name] = body.iloc[:, i].astype(str)
    for c in LOT_COLS:
        if c not in df.columns:
            df[c] = ""
    df["qty"] = _num(df["qty"].astype(str)).fillna(0.0)
    df["bbd"] = _dates_us(df["bbd"].astype(str))   # month-first (2026-09-16)
    extras = [c for c in LOT_EXTRA_COLS if c in df.columns]
    for c in ("item", "scn", "status", "statut", "batch", "unit", "depot",
              "location", "precaut", "supplier_batch", *extras):
        df[c] = df[c].astype(str).str.strip()
    df["status"] = df["status"].map(lambda s: _STATUS_CASE.get(s.upper(), s))
    df["source"] = source
    return df[LOT_COLS + extras].reset_index(drop=True)


def load_azapart(path: Path) -> pd.DataFrame:
    # header=None: the live export has NO header row — reading with a header
    # silently ate the first SKU (030480). The dev fixture's "Column1;..."
    # junk header becomes a data row instead, dropped by the filter below.
    df = _drop_footer(_read_semicolon(path, header=None))
    df = df.iloc[:, :8]
    df.columns = ["sku", "description", "cas_unit", "kg_unit",
                  "cas_conv", "cnt_conv", "kg_per_case", "format"]
    df = df[df["sku"].str.strip().ne("") & df["sku"].ne("Column1")]
    df["sku"] = df["sku"].str.strip()
    df["kg_per_case"] = pd.to_numeric(df["kg_per_case"].str.strip(),
                                      errors="coerce")
    df["format"] = df["format"].str.strip()
    # duplicate sku rows exist (one with blank format): prefer non-blank format
    df = (df.sort_values("format", key=lambda s: s.str.len())
            .groupby("sku", as_index=False).last())
    return df.reset_index(drop=True)


def load_rmpkitems(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, delimiter=",", encoding="cp1252", dtype=str,
                     keep_default_na=False, skiprows=7)
    df = df.iloc[:, :2]
    df.columns = ["item", "designation"]
    df["item"] = df["item"].str.strip()
    df["designation"] = df["designation"].str.strip()
    keep = df["item"].ne("") & ~df["item"].str.match(_FOOTER_RE.pattern,
                                                     case=False)
    return df[keep].reset_index(drop=True)


def _dates_us(series: pd.Series) -> pd.Series:
    """MM/DD/YYYY first (the receipt slips print US dates: '09/13/2026', and
    so does every jestk lot export's BBD — used there since 2026-09-16);
    the per-column auto-detect only when that convention leaves gaps and
    the auto choice parses more."""
    s = series.astype(str).str.strip()
    us = pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")
    if (us.isna() & s.ne("")).any():
        auto = _dates(s)
        if auto.isna().sum() < us.isna().sum():
            return auto
    return us


def load_pkg_rec(path: Path, *, source: str | None = None) -> pd.DataFrame:
    """Packaging receipt slips (PKG-REC.csv, drop of 2026-09-15): a rolling
    ~1-week window of what the dock booked into stock, one row per
    (slip, batch). The file opens with a selection-recall preamble whose
    lines split into 1-4 ';' fields ('Recall of the selection',
    'Prechrono :;Receipt slips;...', 'Receipt Date :;09/12/2026 ->
    09/18/2026;...'), then a header and the data rows of exactly 9 fields:
    receipt date (MM/DD/YYYY); item; designation; batch; qty; unit;
    PO number (8 digits); receipt slip number; user. Only 9-field lines are
    kept and the header row is dropped by name. po8 = the same join key the
    PO feed and the dock sheet use (po_import.po8: last 8 digits)."""
    from .po_import import po8   # lazy: keeps this module import-light
    source = source or Path(path).name
    text = Path(path).read_text(encoding="cp1252", errors="replace")
    rows = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) != 9:
            continue
        parts = [p.strip() for p in parts]
        if _norm(parts[0]).startswith("receipt") or _FOOTER_RE.match(parts[0]):
            continue
        rows.append(parts)
    df = pd.DataFrame(rows, columns=["receipt_date", "item", "designation",
                                     "batch", "qty", "unit", "po", "slip",
                                     "user"], dtype=str)
    df["receipt_date"] = _dates_us(df["receipt_date"])
    df["qty"] = _num(df["qty"].astype(str))
    df["po8"] = df["po"].map(po8)
    df["source"] = source
    df = df[PKG_REC_COLS].reset_index(drop=True)
    if df.empty:
        df.attrs["empty_export"] = True
    return df


def find_bom_file(folder: str | Path) -> Path | None:
    """The BOM export present in `folder`: the first of BOM_FILES that
    exists ('ediact.csv' wins over the legacy 'ediact 3.csv'), else None.
    A presence anchor only (data health, page gating): import_vif_folder
    reads past an empty or unreadable ediact.csv to a good 'ediact 3.csv'
    (2026-09-16), so the file that supplies the BOM frame may differ."""
    folder = Path(folder)
    for name in BOM_FILES:
        p = folder / name
        if p.is_file():
            return p
    return None


def lot_frames(snap: VifSnapshot) -> list[tuple[str, pd.DataFrame]]:
    """(file name, frame) for every lot export the snapshot holds, in
    LOT_FILES order (rm, pkg, AMB, QC, apples, semi). Every frame carries
    LOT_COLS; a fallback alias (jestkexp4 / jestksa) sits under its
    canonical key with the real name in the 'source' column."""
    return [(name, snap.frames[name]) for name in LOT_FILES
            if name in snap.frames]


def receipts_frame(snap: VifSnapshot) -> pd.DataFrame | None:
    """The PKG-REC receipt slips (PKG_REC_COLS) or None when the drop has
    no receipt export."""
    return snap.frames.get(RECEIPTS_FILE)


def missing_semi_recipes(frames: dict[str, pd.DataFrame]) -> list[str]:
    """Semi-finished item codes the BOM consumes but no recipe defines
    (2026-09-16), sorted and unique: Input items of frames["ediact 3.csv"]
    and of frames.get("ediact 4.csv") that carry a quantity, are no
    Activity in either frame and look semi-finished (SEMI_PREFIXES HSM / RF,
    case-insensitive). BomGraph cannot explode through such an item, so the
    SKUs above it lose their sub-component checks (with ediact.csv alone on
    the 2026-09-15 drop: schedule blocks OK 150 -> 121, AT_RISK 13 -> 40).

    Only qty-bearing Input rows count, because BomGraph recurses only
    through those: a blank-qty Input is an ALTERNATE (pooled for stock,
    never exploded), so its recipe changes no number. Verified 2026-09-16:
    RF750005 'RF ORG BLACKCURRANT IQF' is a blank-qty alternate with no
    recipe in any export generation (dev fixture, data/reference, the drop
    + ediact 4) — counting it would pin a note no export can clear. The
    drop's ediact.csv alone -> 17 codes; with 'ediact 4.csv' -> none."""
    parts = [frames.get(k) for k in ("ediact 3.csv", HSM_FILE)]
    parts = [df for df in parts if df is not None and not df.empty
             and {"Activity", "item_type", "item"} <= set(df.columns)]
    activities: set[str] = set()
    inputs: set[str] = set()
    for df in parts:
        activities.update(df["Activity"].astype(str).str.strip())
        is_input = df["item_type"].astype(str).str.strip().str.casefold().eq("input")
        if "qty_act" in df.columns:
            is_input &= pd.to_numeric(df["qty_act"], errors="coerce").gt(0)
        inputs.update(df.loc[is_input, "item"].astype(str).str.strip())
    prefixes = tuple(p.upper() for p in SEMI_PREFIXES)
    return sorted(i for i in inputs - activities
                  if i and i.upper().startswith(prefixes))


# A physical lot: the same (item, scn, batch, depot, location) in two exports
# is ONE lot listed twice (2026-09-16).
LOT_KEY = ("item", "scn", "batch", "depot", "location")


def _drop_cross_file_lots(frames: dict[str, pd.DataFrame],
                          notes: list[str]) -> None:
    """Keep each physical lot in the FIRST lot frame (LOT_FILES order) that
    lists it; drop its rows from later frames, one note per frame. Within-
    file repeats are left alone, and a row with neither scn nor batch never
    matches (no lot identity to compare).

    What it changes (corrected 2026-09-16): the drop of 2026-09-15 lists the
    same 9 QCR lots in jestkexq.csv (status Out) and jestkexp5.csv (status
    OL). No stock number moved: jestkexp5 is never counted
    (api._NEVER_COUNTED_FRAMES) and QCR/QCP are off for every status by
    default, so available stock and tracked items are identical with or
    without this step (checked on the real drop). The visible effect is lot
    detail (pages/stock_check._lot_rows), which listed each of those lots
    twice and now shows it once, as the jestkexq QCR row; the jestkexp5 'OL'
    copy is dropped on purpose (same qty and BBD; neither 'Out' nor 'OL'
    counts by default). The rule also
    keeps a future overlap between COUNTED frames (jestkexp, jestkexp2,
    jestkamb, jestkexq, jestksav) from being summed twice."""
    seen: dict[tuple, str] = {}
    for name in LOT_FILES:
        df = frames.get(name)
        if df is None or df.empty or not set(LOT_KEY) <= set(df.columns):
            continue
        keys = list(zip(*(df[c].astype(str) for c in LOT_KEY)))
        ident = (df["scn"].astype(str).ne("") | df["batch"].astype(str).ne("")).tolist()
        hits = [seen.get(k) if ok else None for k, ok in zip(keys, ident)]
        for k, h, ok in zip(keys, hits, ident):
            if ok and h is None:
                seen.setdefault(k, name)
        dup = [h is not None for h in hits]
        if any(dup):
            earlier = sorted({h for h in hits if h is not None}, key=LOT_FILES.index)
            attrs = dict(df.attrs)
            kept = df[[not d for d in dup]].reset_index(drop=True)
            kept.attrs.update(attrs)
            frames[name] = kept
            notes.append(f"{name}: dropped {sum(dup)} lots already listed in "
                         f"{', '.join(earlier)}")


def import_vif_folder(folder: str | Path) -> VifSnapshot:
    """Load every VIF export in `folder` (drop of 2026-09-15 layout and the
    pre-2026-09 names alike). Frame keys follow the CONTRACT: the BOM sits
    under "ediact 3.csv" whichever file it came from, "ediact 4.csv" only
    when that file exists, lot frames under their canonical LOT_FILES names
    (a fallback alias loads under the canonical key), receipts under
    "PKG-REC.csv", azapart/rmpkitems unchanged.

    Errors (.errors, "<name>: <why>", the Stock Check import-error chip):
    an unreadable file (no mtime recorded, so the refresh gate retries it)
    and, since 2026-09-16, a REQUIRED export missing ("<name>: missing from
    <folder>"; REQUIRED_FILES: a BOM, jestkexp.csv or its alias,
    jestkexp2.csv, azapart.csv). An optional export missing is simply absent.
    An unreadable BOM file stays an error when the fallback found another
    one: "ediact.csv: <exc> (BOM taken from ediact 3.csv)" (2026-09-16).

    Notes (.notes, never errors): ignored aliases and generations, empty
    exports, dropped duplicate lines, and since 2026-09-16
      * the BOM fallback — the first of BOM_FILES that loads WITH rows
        supplies the frame; a skipped EMPTY ediact.csv reads
        "ediact.csv: empty export - BOM taken from ediact 3.csv" (when no
        BOM file has rows the first readable one is kept);
      * "semi-finished recipes missing: <n> - <first 8 codes>" plus
        "(ediact 4.csv not in the folder)" when that file is absent — the
        codes also sit on .missing_recipes (missing_semi_recipes);
      * a lot listed by two exports kept in the first only
        ("jestkexp5.csv: dropped 9 lots already listed in jestkexq.csv").
    .source_files holds the mtime of every VIF_FILES name present so the
    refresh gate sees any re-export, alias or ignored file included; a lot
    frame loaded from its alias is stamped under the canonical name too
    (the report's per-frame export stamp reads that key, 2026-09-16)."""
    folder = Path(folder)
    frames: dict[str, pd.DataFrame] = {}
    mtimes: dict[str, str] = {}
    errors: list[str] = []
    notes: list[str] = []

    def stamp(name: str) -> None:
        p = folder / name
        if p.is_file():
            mtimes[name] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))

    def put(key: str, name: str, df: pd.DataFrame) -> None:
        frames[key] = df
        stamp(name)
        if df.attrs.get("empty_export"):
            notes.append(f"{name}: empty export (no rows)")
        dropped = int(df.attrs.get("dropped_duplicates", 0) or 0)
        if dropped:
            notes.append(f"{name}: dropped {dropped} exact duplicate rows")

    def load(key: str, name: str, loader) -> bool:
        p = folder / name
        if not p.is_file():
            return False
        try:
            df = loader(p)
        except Exception as exc:  # noqa: BLE001 - import must never die
            errors.append(f"{name}: {exc}")
            return False
        put(key, name, df)
        return True

    def missing(name: str) -> None:
        errors.append(f"{name}: missing from {folder}")

    # -- BOM: the first of BOM_FILES that loads WITH rows (2026-09-16). The
    # ERP's 134-byte "No data corresponds" report or a garbled ediact.csv
    # used to win on existence alone and hide a good 'ediact 3.csv'.
    present = [n for n in BOM_FILES if (folder / n).is_file()]
    if not present:
        missing(BOM_FILES[0])
    read: dict[str, pd.DataFrame] = {}
    failed: dict[str, Exception] = {}
    for name in present:
        try:
            read[name] = load_ediact(folder / name)
        except Exception as exc:  # noqa: BLE001 - import must never die
            failed[name] = exc
            continue
        if len(read[name]):
            break
    full = next((n for n, df in read.items() if len(df)), None)
    chosen = full or next(iter(read), None)
    for name in present:
        if name == chosen:
            put("ediact 3.csv", name, read[name])
        elif name not in read and name not in failed:   # after the chosen one
            notes.append(f"{name}: ignored ({chosen} is the BOM export)")
            stamp(name)
        elif name in failed:
            # An unreadable BOM file is an ERROR and gets no mtime, even when
            # another file supplied the frame (2026-09-16): the refresh gate
            # (api.refresh_vif_snapshot) retries only the files .errors names.
            # As a stamped note, an ediact.csv locked by Excel or a OneDrive
            # hiccup left the older ediact 3.csv BOM in the saved snapshot
            # until the next export moved ediact.csv's mtime.
            errors.append(f"{name}: {failed[name]}" + (
                f" (BOM taken from {chosen})" if full is not None else ""))
        elif full is not None:                  # empty, skipped before it
            notes.append(f"{name}: empty export - BOM taken from {chosen}")
            stamp(name)
        else:                         # a second empty export
            notes.append(f"{name}: empty export (no rows)")
            stamp(name)
    load(HSM_FILE, HSM_FILE, load_ediact4)

    # -- semi-finished recipes (2026-09-16): ediact.csv lacks most of them,
    # ediact 4.csv supplies them; say so instead of degrading silently.
    missing_recipes = missing_semi_recipes(frames)
    if missing_recipes:
        codes = ", ".join(missing_recipes[:8])
        if len(missing_recipes) > 8:
            codes += f" +{len(missing_recipes) - 8} more"
        hint = ("" if (folder / HSM_FILE).is_file()
                else f" ({HSM_FILE} not in the folder)")
        notes.append(f"semi-finished recipes missing: {len(missing_recipes)}"
                     f" - {codes}{hint}")

    for name in LOT_FILES:
        packaging = name in ("jestkexp2.csv", "jestkexq.csv")
        alias = LOT_FALLBACKS.get(name)
        if (folder / name).is_file():
            load(name, name,
                 lambda p, pk=packaging: load_jestkexp(p, packaging=pk))
            if alias and (folder / alias).is_file():
                notes.append(f"{alias}: ignored ({name} present, same lots)")
                stamp(alias)
        elif alias and (folder / alias).is_file():
            if load(name, alias, lambda p, pk=packaging, s=alias:
                    load_jestkexp(p, packaging=pk, source=s)):
                notes.append(f"{name} missing: loaded {alias} in its place")
                # the report's export stamp for this frame reads the
                # canonical key (2026-09-16); the alias keeps its own entry
                if alias in mtimes:
                    mtimes[name] = mtimes[alias]
        elif name in REQUIRED_FILES:
            missing(name)
    _drop_cross_file_lots(frames, notes)

    load(RECEIPTS_FILE, RECEIPTS_FILE, load_pkg_rec)
    if not load("azapart.csv", "azapart.csv", load_azapart) \
            and not (folder / "azapart.csv").is_file():
        missing("azapart.csv")
    load("rmpkitems.csv", "rmpkitems.csv", load_rmpkitems)

    for name, why in IGNORED_FILES.items():
        p = folder / name
        if p.is_file():
            notes.append(f"{name}: empty export" if _is_empty_export(p)
                         else f"{name}: ignored ({why})")
            stamp(name)

    return VifSnapshot(frames=frames, source_files=mtimes,
                       imported_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                       errors=errors, notes=notes,
                       missing_recipes=missing_recipes)


def save_snapshot(snap: VifSnapshot, snapshots_dir: str | Path) -> Path:
    """Versioned snapshot; writes then moves a 'latest' pointer. A failed
    import (empty frames) never clobbers last-good."""
    if not snap.frames:
        raise ValueError("refusing to save empty snapshot")
    d = Path(snapshots_dir) / snap.imported_at.replace(":", "-").replace(" ", "_")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "snapshot.pkl", "wb") as f:
        pickle.dump(snap, f)
    (Path(snapshots_dir) / "latest").write_text(d.name, encoding="utf-8")
    return d


def load_latest_snapshot(snapshots_dir: str | Path) -> VifSnapshot | None:
    d = Path(snapshots_dir)
    ptr = d / "latest"
    if not ptr.exists():
        return None
    pkl = d / ptr.read_text(encoding="utf-8").strip() / "snapshot.pkl"
    if not pkl.exists():
        return None
    with open(pkl, "rb") as f:
        return pickle.load(f)
