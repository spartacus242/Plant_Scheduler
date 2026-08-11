# code/helpers/capability_check.py — manprg vs capabilities table validation.
#
# Rule (user, 2026-08-10): *if an SKU is in manprg, it is capable on that
# line* — the plant physically ran it. A manprg SKU/line pair missing from
# `capabilities_rates.csv` (or marked not-capable) means the table is out of
# date, not the plant. This module finds those conflicts so the UI can flag
# them and offer a one-click fix.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass
class CapabilityConflict:
    sku: str
    line_name: str
    mo: str
    kind: str  # SKU_MISSING | LINE_NOT_CAPABLE
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}: {self.sku} on {self.line_name} (MO {self.mo})"


@dataclass
class CapabilityCheckResult:
    conflicts: list[CapabilityConflict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.conflicts)

    def by_kind(self) -> dict[str, list[CapabilityConflict]]:
        out: dict[str, list[CapabilityConflict]] = {}
        for c in self.conflicts:
            out.setdefault(c.kind, []).append(c)
        return out

    def as_frame(self) -> pd.DataFrame:
        if not self.conflicts:
            return pd.DataFrame(
                columns=["sku", "line_name", "mo", "kind", "detail"])
        return pd.DataFrame(
            [{"sku": c.sku, "line_name": c.line_name, "mo": c.mo,
              "kind": c.kind, "detail": c.detail} for c in self.conflicts])


def load_capabilities(path: str | Path) -> pd.DataFrame:
    """Read capabilities_rates.csv with sku/line_name kept as strings.

    Tolerates a missing line_name column (older fixtures / tests): derive it
    from line_id (0 -> P09, 1 -> P10, ...).
    """
    df = pd.read_csv(path, dtype={"sku": str})
    df["sku"] = df["sku"].astype(str).str.strip()
    if "line_name" not in df.columns:
        df["line_name"] = df["line_id"].apply(
            lambda i: f"P{9 + int(i):02d}")
    df["line_name"] = df["line_name"].astype(str).str.strip().str.upper()
    return df


def load_manprg_mos(
    manprg_paths: Iterable[str | Path],
) -> list[tuple[str, str, str]]:
    """Return [(sku, line_name_upper, mo)] for production MOs only.

    CIP/TRIALS pseudo-MOs are not SKU capability checks. Lines are mapped
    from the manprg 'LMH-P09' form to 'P09'.
    """
    from helpers.manprg_import import read_manprg

    res = read_manprg(list(manprg_paths))
    out: list[tuple[str, str, str]] = []
    for mo, m in res.by_mo.items():
        item = str(m.item or "").strip()
        if not item or item.upper() in ("CIP", "TRIALS"):
            continue
        line = str(m.line or "").strip().upper().replace("LMH-", "")
        out.append((item, line, mo))
    return out


def check_capabilities(
    capabilities: pd.DataFrame,
    manprg_mos: Iterable[tuple[str, str, str]],
) -> CapabilityCheckResult:
    """Compare manprg MOs against the capabilities table."""
    res = CapabilityCheckResult()
    # capable lookup: sku -> set of capable line names
    capable_by_sku: dict[str, set[str]] = {}
    known_skus: set[str] = set()
    for _, r in capabilities.iterrows():
        sku = str(r["sku"]).strip()
        line = str(r["line_name"]).strip().upper()
        known_skus.add(sku)
        try:
            capable_val = int(r.get("capable", 0) or 0)
        except (TypeError, ValueError):
            capable_val = 0
        if capable_val == 1:
            capable_by_sku.setdefault(sku, set()).add(line)

    seen: set[tuple[str, str, str]] = set()
    for sku, line, mo in manprg_mos:
        sku = str(sku).strip()
        line = str(line).strip().upper().replace("LMH-", "")
        if not sku or sku.upper() in ("CIP", "TRIALS"):
            continue
        key = (sku, line, mo)
        if key in seen:
            continue
        seen.add(key)
        if sku not in known_skus:
            res.conflicts.append(CapabilityConflict(
                sku=sku, line_name=line, mo=mo, kind="SKU_MISSING",
                detail="no row in capabilities_rates.csv at all"))
        elif line not in capable_by_sku.get(sku, set()):
            res.conflicts.append(CapabilityConflict(
                sku=sku, line_name=line, mo=mo, kind="LINE_NOT_CAPABLE",
                detail=f"capable lines: {sorted(capable_by_sku.get(sku, set()))}"))
    return res


def fix_rows_for(
    capabilities: pd.DataFrame,
    conflicts: Iterable[CapabilityConflict],
    *,
    default_rate: float,
) -> pd.DataFrame:
    """Build the capabilities rows that would resolve the conflicts.

    For SKU_MISSING: append a full 14-line block with the new SKU, capable=1
    only on the manprg-proven line(s), 0 elsewhere.
    For LINE_NOT_CAPABLE: flip the existing row(s) for that sku+line to 1
    (and fill rate if it was 0).

    `default_rate` is the fallback calc_rate_kgph for new rows (user
    decision: average rate of all SKUs on that line, manual entry option).
    Returns only the changed rows; caller merges into the full table.
    """
    from collections import defaultdict

    out_rows: list[dict] = []
    table_lines = set(capabilities["line_name"].unique())
    lines = sorted(table_lines)
    # group conflicts by sku -> set of proven lines
    proven: dict[str, set[str]] = defaultdict(set)
    for c in conflicts:
        proven[c.sku].add(c.line_name)

    for sku, lines_ok in proven.items():
        # a proven line absent from the table is still a real physical line —
        # append its row too (defensive; the full table normally has all 14)
        existing = capabilities[capabilities["sku"] == sku]
        if existing.empty:
            # SKU_MISSING: the whole line block is missing -> emit all lines
            all_lines = sorted(table_lines | lines_ok)
            for ln in all_lines:
                capable = 1 if ln in lines_ok else 0
                out_rows.append({
                    "sku": sku, "line_name": ln, "capable": capable,
                    "calc_rate_kgph": default_rate if capable else 0,
                })
        else:
            # LINE_NOT_CAPABLE: the SKU has rows; only flip the proven lines
            for ln in sorted(lines_ok):
                row = existing[existing["line_name"] == ln]
                if row.empty:
                    out_rows.append({
                        "sku": sku, "line_name": ln, "capable": 1,
                        "calc_rate_kgph": default_rate,
                    })
                else:
                    r = row.iloc[0]
                    rate = float(r.get("calc_rate_kgph") or 0) or default_rate
                    out_rows.append({
                        "sku": sku, "line_name": ln, "capable": 1,
                        "calc_rate_kgph": rate,
                    })
    if not out_rows:
        return pd.DataFrame(columns=["sku", "line_name", "capable", "calc_rate_kgph"])
    return pd.DataFrame(out_rows)


def average_rate_for_line(
    capabilities: pd.DataFrame,
    line_name: str,
) -> float:
    """Mean calc_rate_kgph of capable SKUs on the line (0 if none)."""
    df = capabilities[(capabilities["line_name"] == line_name.upper())
                      & (capabilities["capable"] == 1)]
    rates = pd.to_numeric(df["calc_rate_kgph"], errors="coerce").dropna()
    return float(rates.mean()) if len(rates) else 0.0
