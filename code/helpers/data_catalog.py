# helpers/data_catalog.py -- registry of EVERY input file Flowstate reads.
#
# One place that names each input — plant state feeds, the demand baseline,
# the VIF stock exports, the ERP's PO export, the dock workbook, planner
# master data, the measured-history tables — what it means in plain English,
# who produces it, how fresh it should be, which file stands in when the
# primary is absent, and (for the planner-maintained CSVs) which columns a
# replacement upload must carry.
#
# Used by pages/data.py (the Data Files page: status table + upload/preview),
# helpers/data_health.py (Command Center rows and the per-file statuses the
# Data Files page shows) and tests/test_data_files_catalog.py, which fails
# the build when a file on the bridge's delivery list or in the stock
# check's loader has no row here — the 2026-09-15 drop had added a dozen
# inputs the page never showed.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from helpers.paths import data_dir, reference_dir

# Files whose first line may carry a UTF-8 BOM; read everything as utf-8-sig.
CSV_ENCODING = "utf-8-sig"
# The plant's own exports (manprg, VIF / Sage X3) are Windows-1252 text.
PLANT_ENCODING = "cp1252"

# Section order on the Data Files page: (group key, label, one-line caption).
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("plant", "Plant state",
     "What the plant is doing right now and the schedule of record built on "
     "it. The manprg exports and cip_info arrive with the live data sync."),
    ("demand", "Demand",
     "The corporate demand plan: the weekly AZAP baseline and the derived "
     "order file every planning surface reads."),
    ("stock", "Component stock (VIF exports)",
     "The ERP's stock, BOM and receipt exports the Stock Check and the "
     "Supply Timeline read. Refreshed by the ERP every evening (~19:10)."),
    ("supply", "Inbound supply",
     "Open purchase orders and dock appointments — when components arrive."),
    ("reference", "Master data",
     "Planner-maintained reference tables. Upload here to replace one "
     "(the old file is backed up first)."),
    ("history", "Measured history",
     "Tables derived from the 8-year run log; measured kg/h overlays the "
     "plant's catalog rates. Rebuilt by scripts, not edited."),
)
GROUP_LABEL: dict[str, str] = {g[0]: g[1] for g in GROUPS}
GROUP_ORDER: dict[str, int] = {g[0]: i for i, g in enumerate(GROUPS)}

KINDS = ("csv", "text", "excel", "toml")
FOLDERS = ("data", "vif")


@dataclass(frozen=True)
class DataFile:
    key: str
    name: str
    subdir: str  # "" for data/, "reference" for data/reference/, "reference/historical" ...
    filename: str
    blurb: str
    key_columns: tuple = field(default_factory=tuple)
    # Who OWNS the file's content (drives whether Data Files offers upload):
    #   "user"    — planner-maintained; upload/replace is the normal flow
    #   "bridge"  — delivered by the live data sync (folder or GitHub mode);
    #               manual uploads would be overwritten on the next pass
    #   "app"     — written by Flowstate itself (Save / Promote / downtime strip)
    #   "derived" — rebuilt by an importer / script from another source file
    managed_by: str = "user"
    # True when the live pull ALSO syncs this file: planner edits are
    # legitimate (capability fixes, downtime editor) but may be overwritten.
    bridge_synced: bool = False
    # ── Data Files page metadata (2026-09-17) ───────────────────────────
    # Page section (a key of GROUPS).
    group: str = "reference"
    # How to read / preview it: "csv" (comma, utf-8-sig — the app's own
    # tables), "text" (a delimited plant export in cp1252: manprg, VIF, the
    # PO export), "excel" (xlsx / xlsm workbook), "toml".
    kind: str = "csv"
    # Who produces the file, in plain words, for the status table.
    source: str = ""
    # Key of helpers.data_health.DEFAULT_CADENCE_H that sets the expected
    # refresh (a [health] cadence_h entry overrides it). None = a static
    # reference file: age is shown but never judged stale.
    cadence_key: Optional[str] = None
    # An optional file's absence is informational (grey), never MISSING.
    optional: bool = False
    # Fallback file names, first existing wins — the same rules the loaders
    # apply (BOM: ediact.csv else "ediact 3.csv"; PO feed: order_npa.csv
    # else the legacy open_pos.xlsx / open_pos.csv; lot aliases).
    alternates: tuple = field(default_factory=tuple)
    # [datasources] key whose non-empty value REPLACES the path (Settings
    # page). Authoritative even when that path does not exist (cip_info
    # precedent): the row then says "missing at <configured path>".
    config_key: Optional[str] = None
    # Index into a ';'-separated multi-path key (manprg_files holds two).
    config_index: int = 0
    # "data": subdir under the data folder. "vif": the folder the stock
    # check actually reads (saved Stock Check setting → data/reference when
    # a BOM export sits there → bundled dev fixtures), so the page shows the
    # files the report is built from.
    folder: str = "data"
    # False: a dedicated data_health rule (manprg, cip_info, the demand
    # baseline, the VIF set, the PO feed, the receiving workbook) or a
    # semantic rule (line_rates ↔ rate_mode) judges it on the Command
    # Center, so the generic catalog loop must not add a second row.
    generic_health: bool = True

    # ── paths ────────────────────────────────────────────────────────────
    def base_dir(self, dd: Optional[Path] = None) -> Path:
        base = data_dir() if dd is None else Path(dd)
        if self.folder == "vif":
            # lazy: reconcile_engine is a heavy module and only the VIF
            # entries need the resolver
            from helpers.reconcile_engine import stock_report_inputs
            return Path(stock_report_inputs(base)[0])
        if self.subdir == "reference":
            return reference_dir(base)
        return base / self.subdir if self.subdir else base

    def path(self, dd: Optional[Path] = None) -> Path:
        """The PRIMARY path (where an upload lands), ignoring fallbacks."""
        return self.base_dir(dd) / self.filename

    def configured_path(self, cfg: Optional[dict] = None) -> Optional[Path]:
        """The [datasources] override for this file, or None."""
        if not self.config_key:
            return None
        from helpers.config import datasources_config, load_toml
        ds = datasources_config(cfg if cfg is not None else load_toml())
        raw = str(ds.get(self.config_key, "") or "")
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        if len(parts) <= self.config_index:
            return None
        return Path(parts[self.config_index])

    def candidates(self, dd: Optional[Path] = None,
                   cfg: Optional[dict] = None) -> list[Path]:
        """Paths considered, in order: the configured override alone when
        set, else the primary file followed by its fallbacks."""
        override = self.configured_path(cfg)
        if override is not None:
            return [override]
        base = self.base_dir(dd)
        return [base / self.filename] + [base / a for a in self.alternates]

    def resolve(self, dd: Optional[Path] = None,
                cfg: Optional[dict] = None) -> Path:
        """The file actually in use: the first existing candidate, else the
        first candidate (so a MISSING row still names the expected file)."""
        cands = self.candidates(dd, cfg)
        for p in cands:
            if p.is_file():
                return p
        return cands[0]

    def rel(self) -> str:
        if self.folder == "vif":
            return f"<VIF folder>/{self.filename}"
        return f"data/{self.subdir}/{self.filename}" if self.subdir else f"data/{self.filename}"

    def all_names(self) -> tuple[str, ...]:
        return (self.filename,) + tuple(self.alternates)


# Source labels shared by many rows.
_ERP = "ERP export (VIF / Sage X3), delivered by the live data sync"
_PLANNER_FEED = "Planner-maintained file (fs_manual), delivered by the live data sync"
_PLANNER = "Planner (upload here)"
_FLOWSTATE = "Written by Flowstate"

CATALOG: tuple = (
    # ── Plant state ──────────────────────────────────────────────────────
    DataFile(
        key="manprg",
        name="Live MO progress — lines P09–P18 (manprg)",
        subdir="reference",
        filename="manprg.txt",
        blurb="The plant's schedule of manufacturing orders for lines P09–P18: "
        "running and queued MOs, start, forecast and made quantities. The "
        "current-state source — committed blocks, fixed line-time in "
        "Scenario F, made kg for demand netting. Its as-of time is the "
        "manprg.asof.json stamp the sync writes when the content changes.",
        managed_by="bridge",
        group="plant", kind="text", source=_ERP,
        cadence_key="manprg", config_key="manprg_files", config_index=0,
        generic_health=False,
    ),
    DataFile(
        key="manprg2",
        name="Live MO progress — lines P19–P22 (manprg2)",
        subdir="reference",
        filename="manprg2.txt",
        blurb="The same MO progress export for lines P19–P22 (the Bossar "
        "lines). Read together with manprg.txt; one as-of stamp covers both.",
        managed_by="bridge",
        group="plant", kind="text", source=_ERP,
        cadence_key="manprg", config_key="manprg_files", config_index=1,
        generic_health=False,
    ),
    DataFile(
        key="cip_info",
        name="CIP schedule (cip_info)",
        subdir="reference",
        filename="cip_info.csv",
        blurb="Per-line CIP state: PreviousCIP (end of the last CIP), "
        "MaxHoursBetweenCIP (food-safety limit), ScheduledCIP (planned next, "
        "optional), Notes. Drives CIP projection and the per-line interval "
        "limit. Live plant feed — not a planner input.",
        key_columns=("LineEquipment", "PreviousCIP", "MaxHoursBetweenCIP"),
        managed_by="bridge",
        group="plant", source=_PLANNER_FEED,
        cadence_key="cip_info", config_key="cip_info_csv",
        generic_health=False,
    ),
    DataFile(
        key="initial_states",
        name="Initial line states",
        subdir="reference",
        filename="initial_states.csv",
        blurb="Where each line starts: current SKU, available-from hour, CIP "
        "carryover. Legacy start-state input (the current-state MOs come "
        "from manprg since Scenario E); auto-synced, not a planner input.",
        key_columns=("line_id", "initial_sku", "available_from_hour"),
        managed_by="bridge",
        group="plant", source="Plant data, delivered by the live data sync",
    ),
    DataFile(
        key="downtimes",
        name="Lines down (downtimes)",
        subdir="reference",
        filename="downtimes.csv",
        blurb="Planned line-down windows: maintenance, contractors, outages. "
        "Absolute wall-clock start/end (rolling the calendar never moves "
        "them). Edited ONLY in the Plant Calendar's Start-of-day downtime "
        "strip — deliberately excluded from the live data sync so the "
        "bridge can't clobber it.",
        key_columns=("line_id", "line_name", "start_datetime", "end_datetime"),
        managed_by="app",
        group="plant", source="Planner, via the Plant Calendar downtime strip",
    ),
    DataFile(
        key="calendar_blocks",
        name="Current schedule (calendar blocks)",
        subdir="",
        filename="calendar_blocks.csv",
        blurb="The plant's own line schedule: what runs on which line, when. "
        "Written by Flowstate itself (Save / Promote) — edit it on the Plant "
        "Calendar, not here.",
        key_columns=("block_id", "block_type", "line_id", "start_h", "end_h"),
        managed_by="app",
        group="plant", source=_FLOWSTATE + " (Plant Calendar Save / Promote)",
    ),
    # ── Demand ───────────────────────────────────────────────────────────
    DataFile(
        key="demand_summary",
        name="Demand plan summary (AZAP baseline)",
        subdir="reference",
        filename="demand_plan_summary.csv",
        blurb="The weekly AZAP demand baseline: Week (ISO), Product (SKU), "
        "Tons. In folder mode the live sync rebuilds it from the planners' "
        "newest 'New Export AZAP MMDDYY.xlsx' workbook, then derives "
        "demand_plan.csv from it. Upload a different summary in the import "
        "section above.",
        key_columns=("Week", "Product"),
        managed_by="bridge",
        group="demand", source=_PLANNER_FEED + " (rebuilt from the AZAP workbook)",
        cadence_key="demand_summary", config_key="demand_summary_csv",
        generic_health=False,
    ),
    DataFile(
        key="demand_plan",
        name="Demand plan (AZAP, derived)",
        subdir="reference",
        filename="demand_plan.csv",
        blurb="AZAP demand the scorecard/calendar/solver read — REBUILT from "
        "demand_plan_summary.csv by the import (never hand-edited): which SKU, "
        "how many kg, which week. It does not assign lines. Provenance in "
        "demand_plan.source.json.",
        key_columns=("order_id", "sku", "qty_target"),
        managed_by="derived",
        group="demand", source="Derived from demand_plan_summary.csv (import / sync)",
        cadence_key="demand_plan",
    ),
    # ── Component stock: the VIF exports (folder = what the stock check reads)
    DataFile(
        key="vif_bom",
        name="Bill of materials (ediact)",
        subdir="reference",
        filename="ediact.csv",
        blurb="Every production activity with its inputs and outputs per "
        "finished product — the BOM the stock check explodes each block "
        "through. Headerless since the 2026-09-15 drop; the legacy "
        "'ediact 3.csv' stands in when it is absent. The stock check cannot "
        "run without a BOM.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", alternates=("ediact 3.csv",), folder="vif",
        generic_health=False,
    ),
    DataFile(
        key="vif_hsm_recipes",
        name="Semi-finished recipes (ediact 4)",
        subdir="reference",
        filename="ediact 4.csv",
        blurb="Recipes of the house-made intermediates (HSM / RF codes): "
        "their components per batch. ediact.csv does NOT carry them, so "
        "without this file the stock check treats 17+ intermediates as "
        "untracked and never checks their ingredients. Not in the ERP's "
        "daily drop (open question to IT) — keep the copy in place.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP + " — supplement, not refreshed daily",
        folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_rm_lots",
        name="Raw-material lots (jestkexp)",
        subdir="reference",
        filename="jestkexp.csv",
        blurb="Every raw-material lot on hand: item, batch, status "
        "(Ava / Loc / Out), quantity, depot, location. Only Ava lots count "
        "by default. Alias jestkexp4.csv is read when this file is absent.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", alternates=("jestkexp4.csv",), folder="vif",
        generic_health=False,
    ),
    DataFile(
        key="vif_pkg_lots",
        name="Packaging lots (jestkexp2)",
        subdir="reference",
        filename="jestkexp2.csv",
        blurb="Every packaging lot on hand (films, sleeves, cases): item, "
        "batch, status, quantity, depot. Batch dates near a PO's receipt "
        "date back the Supply Timeline's 'landed' rule.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_amb_lots",
        name="Off-site lots at Americold Burley (jestkamb)",
        subdir="reference",
        filename="jestkamb.csv",
        blurb="Lots held at the AMB ambient store off site. Ava counts by "
        "default, Loc does not. Optional: without it AMB stock is simply "
        "not counted.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", optional=True, folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_qc_lots",
        name="Quality-control depot lots (jestkexq)",
        subdir="reference",
        filename="jestkexq.csv",
        blurb="Lots in the QC depots QCR / QCP — stock under quality hold. "
        "Every status is OFF by default (not available until released). "
        "Optional.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", optional=True, folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_apple_lots",
        name="Fresh-apple lots (jestksav)",
        subdir="reference",
        filename="jestksav.csv",
        blurb="Fresh-apple lots at the apple depots SA1 / SA2 with variety and "
        "supplier. Counted on hand; land only same-day lines. Alias "
        "jestksa.csv (four columns fewer) is read when this file is absent. "
        "Optional.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", optional=True, alternates=("jestksa.csv",),
        folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_semi_lots",
        name="Semi-finished / WIP lots (jestkexp5)",
        subdir="reference",
        filename="jestkexp5.csv",
        blurb="Work-in-progress and semi-finished lots (M12, QCR, M01, SC1). "
        "Shown on the Stock Check, never counted against a SKU (plant rule "
        "2026-08-14: an intermediate's own stock does not gate a SKU). "
        "Pipe-delimited. Optional.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", optional=True, folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_pkg_receipts",
        name="Packaging receipt slips (PKG-REC)",
        subdir="reference",
        filename="PKG-REC.csv",
        blurb="Receipt slips of the last ~week: which PO line landed, when, "
        "in which batch. The Supply Timeline uses them to mark PO lines as "
        "landed before the ERP closes them. Optional.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP,
        cadence_key="vif", optional=True, folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_azapart",
        name="Finished-product master (azapart)",
        subdir="reference",
        filename="azapart.csv",
        blurb="Finished-product master with pack conversions: case ↔ kg, "
        "pallet text, AZAP product group. Converts block cases to kg for the "
        "stock check. Stable master data — its age is not judged.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP + " — master data",
        folder="vif", generic_health=False,
    ),
    DataFile(
        key="vif_rmpkitems",
        name="Raw-material & packaging item list (rmpkitems)",
        subdir="reference",
        filename="rmpkitems.csv",
        blurb="The tracked-item universe: every raw-material and packaging "
        "item code with its designation (families PK1, PK3, RM1). Stable "
        "master data. Optional.",
        managed_by="bridge",
        group="stock", kind="text", source=_ERP + " — master data",
        optional=True, folder="vif", generic_health=False,
    ),
    # ── Inbound supply ───────────────────────────────────────────────────
    DataFile(
        key="open_pos",
        name="Open purchase orders (order_npa)",
        subdir="reference",
        filename="order_npa.csv",
        blurb="The ERP's own PO-line export: every open purchase order line "
        "with item, remaining quantity, receipt date, arrival area and "
        "status (20 receivable / 60 archived / 70 deleted). The inbound feed "
        "of the Supply Timeline. Leaves the ERP ~19:10 daily. The legacy "
        "open_pos.xlsx / open_pos.csv workbook stands in when it is absent. "
        "Line comments from the inventory specialists (comment / "
        "comment_external, cut at 50 characters by the ERP) show on the "
        "Stock Check Inbound tab, the Excel export and the Gantt supply "
        "details.",
        managed_by="bridge",
        group="supply", kind="text", source=_ERP + " (Sage X3 PO lines)",
        cadence_key="open_pos", config_key="po_report_path",
        alternates=("open_pos.xlsx", "open_pos.csv"),
        generic_health=False,
    ),
    DataFile(
        key="receiving",
        name="Receiving schedule (dock appointments)",
        subdir="reference",
        filename="Shipping Receiving Schedule NPA - 2024.xlsm",
        blurb="The dock's appointment book. Appointments are joined to PO "
        "lines on the PO number to time receipts; SL3 (off-site) lines need "
        "a transfer appointment to count. Weekly. A bundled dev fixture "
        "keeps the Receiving tab usable when it is absent.",
        managed_by="bridge",
        group="supply", kind="excel", source=_PLANNER_FEED + " (weekly workbook)",
        cadence_key="receiving", generic_health=False,
    ),
    # ── Master data ──────────────────────────────────────────────────────
    DataFile(
        key="lines",
        name="Lines",
        subdir="",
        filename="lines.csv",
        blurb="The production lines shown as Gantt rows.",
        key_columns=("line_id", "line_name"),
        group="reference", source=_PLANNER,
    ),
    DataFile(
        key="capabilities_rates",
        name="Line capabilities + rates",
        subdir="reference",
        filename="capabilities_rates.csv",
        blurb="Which SKUs each line can run, and how fast (kg/hr). Measured "
        "kg/h from the run log is overlaid on top where the evidence is "
        "strong (see Measured history). The capability check above fixes "
        "gaps with one click.",
        key_columns=("line_id", "sku", "capable"),
        bridge_synced=True,
        group="reference", source=_PLANNER + "; also synced by the live pull",
    ),
    DataFile(
        key="changeovers",
        name="Changeovers",
        subdir="reference",
        filename="changeovers.csv",
        blurb=("Setup hours and change flags for every SKU-to-SKU transition. "
               "Optional cip_req_after column: 1 = a CIP is required between "
               "these SKUs (protein hygiene). Refreshed quarterly / yearly."),
        key_columns=("from_sku", "to_sku", "setup_hours"),
        bridge_synced=True,
        group="reference", source=_PLANNER + "; also synced by the live pull",
    ),
    DataFile(
        key="line_cip_hrs",
        name="CIP limits",
        subdir="reference",
        filename="line_cip_hrs.csv",
        blurb="Maximum run hours per line before a CIP is required.",
        key_columns=("line_id", "max_cip_hrs"),
        bridge_synced=True,
        group="reference", source=_PLANNER + "; also synced by the live pull",
    ),
    DataFile(
        key="sku_info",
        name="SKU info",
        subdir="reference",
        filename="sku_info.csv",
        blurb="SKU master: description/designation, format, organic / flavor attributes.",
        key_columns=("sku", "designation"),
        bridge_synced=True,
        group="reference", source=_PLANNER + "; also synced by the live pull",
    ),
    DataFile(
        key="line_rates",
        name="Line average rates",
        subdir="reference",
        filename="line_rates.csv",
        blurb="Flat per-line average rates (kg/hr) — the rate source when "
        "use_sku_rates is off; prices forfeited-CIP kg in the scorecard.",
        key_columns=(),
        managed_by="bridge",
        group="reference", source="Plant data, delivered by the live data sync",
        generic_health=False,
    ),
    DataFile(
        key="sku_plan_evidence",
        name="SKU plan evidence (historical schedules)",
        subdir="reference",
        filename="sku_plan_evidence.csv",
        blurb="Every SKU/line pair seen in the plant's historical PDF "
        "schedules. Evidence the capability one-click fix uses to assign a "
        "proven line to a demand SKU the capabilities table lacks. Static; "
        "optional.",
        key_columns=("sku", "line_name"),
        group="reference", source=_PLANNER + " (built from the PDF schedules)",
        optional=True,
    ),
    # ── Measured history ─────────────────────────────────────────────────
    DataFile(
        key="rate_rules",
        name="Run-log rules (historical_run_log.rules.toml)",
        subdir="reference/historical",
        filename="historical_run_log.rules.toml",
        blurb="Every threshold behind the measured-rate overlay: nominal "
        "kg/h per pouch weight, usability rules, and the [effective_rates] "
        "policy (recent runs ≥ 3, else all-time ≥ 3, else the plant's calc "
        "rate). Nothing is hard-coded; this file is the audit trail. "
        "Optional — without it the solver uses catalog rates.",
        managed_by="derived",
        group="history", kind="toml",
        source="scripts/ingest_historical_run_log.py (analyst-maintained)",
        optional=True,
    ),
    DataFile(
        key="measured_rates",
        name="Measured rates by line × SKU",
        subdir="reference/historical",
        filename="rates_by_line_sku.csv",
        blurb="Hour-weighted measured kg/h per line and SKU from the 8-year "
        "run log (recent window and all-time, run counts, OEE). Overlaid on "
        "capabilities_rates.csv at solver staging and in every UI reader. "
        "Optional — without it every rate is the plant's calc rate.",
        key_columns=("line", "sku", "rate_kgph"),
        managed_by="derived",
        group="history",
        source="scripts/derive_historical_tables.py (from the run log)",
        optional=True,
    ),
)


def by_key(key: str) -> Optional[DataFile]:
    for spec in CATALOG:
        if spec.key == key:
            return spec
    return None


def covered_names() -> set[str]:
    """Every file name the catalog knows, primaries and fallbacks alike."""
    out: set[str] = set()
    for spec in CATALOG:
        out.update(spec.all_names())
    return out


def read_csv(spec: DataFile, dd: Optional[Path] = None,
             path: Optional[Path] = None) -> pd.DataFrame:
    p = path if path is not None else spec.path(dd)
    return pd.read_csv(p, encoding=CSV_ENCODING, dtype=str, keep_default_na=False)


def read_text_head(path: Path, n: int = 15) -> list[str]:
    """First `n` lines of a plant text export (cp1252, undecodable bytes
    replaced) — a raw preview that works for every dialect the ERP uses
    (';' with or without header, '|', preambles, footers)."""
    lines: list[str] = []
    with open(path, "r", encoding=PLANT_ENCODING, errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= n:
                break
            lines.append(line.rstrip("\r\n"))
    return lines


def _count_lines(path: Path) -> int:
    """Non-blank lines of a text export — the plant's files carry no
    reliable header convention (ediact.csv has none, PKG-REC a preamble),
    so the count is 'lines', not 'rows'."""
    n = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if raw.strip():
                n += 1
    return n


def status(spec: DataFile, dd: Optional[Path] = None,
           cfg: Optional[dict] = None) -> dict:
    """Existence / size / row-count summary for one catalog entry, on the
    file actually in use (configured override or first existing fallback).
    Never raises."""
    p = spec.resolve(dd, cfg)
    info = {
        "key": spec.key,
        "name": spec.name,
        "rel": spec.rel(),
        "path": p,
        "filename": p.name,
        "alternate_in_use": p.name != spec.filename,
        "configured": spec.configured_path(cfg) is not None,
        "exists": p.is_file(),
        "rows": None,
        "modified": None,
        "age_h": None,
        "size": None,
        "error": None,
    }
    if not info["exists"]:
        return info
    try:
        st_ = p.stat()
        mtime = datetime.fromtimestamp(st_.st_mtime)
        info["modified"] = mtime.strftime("%Y-%m-%d %H:%M")
        info["age_h"] = (datetime.now() - mtime).total_seconds() / 3600.0
        info["size"] = int(st_.st_size)
    except OSError as exc:
        info["error"] = str(exc)
        return info
    try:
        if p.suffix.lower() in (".xlsx", ".xlsm", ".toml"):
            info["rows"] = None  # workbooks / config: presence and age only
        elif spec.kind == "text" or p.suffix.lower() == ".txt":
            info["rows"] = _count_lines(p)
        else:
            info["rows"] = int(len(read_csv(spec, dd, path=p)))
    except Exception as exc:  # a corrupt file should degrade, not crash the page
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def missing_columns(df: pd.DataFrame, spec: DataFile) -> list:
    cols = {str(c).strip().lstrip("﻿").lower() for c in df.columns}
    return [c for c in spec.key_columns if c.lower() not in cols]
