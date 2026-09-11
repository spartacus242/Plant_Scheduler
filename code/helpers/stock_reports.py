# helpers/stock_reports.py — planner-facing views over the ONE stock report.
#
# stockcheck.api.stock_check_report() answers "will this block run?" and
# "should this SKU be scheduled?" block by block and SKU by SKU. The planner
# also needs the report the other way round — by COMPONENT: which purchased
# item is short, how much is on hand against what the board and the demand
# plan need, which runs it blocks and when the first one starts. That is
# the list Carsten chases with purchasing, so it is also what the Excel
# export carries. Pure functions over the report dict (no Streamlit, no
# disk) so every table here is unit-testable and identical on the page and
# in the workbook.

from __future__ import annotations

import math
from io import BytesIO
from typing import Any, Callable, Iterable

import pandas as pd

RISK_STATUSES = ("AT_RISK", "TIGHT", "NOT_TRACKED")
STATUS_RANK = {
    "DO_NOT_SCHEDULE": 0, "AT_RISK": 1, "TIGHT": 2, "NOT_TRACKED": 3,
    "UNK": 4, "NO_BOM": 5, "OK": 6,
}
STATUS_TEXT = {
    "OK": "OK", "TIGHT": "TIGHT", "AT_RISK": "AT RISK",
    "DO_NOT_SCHEDULE": "DO NOT SCHEDULE", "NO_BOM": "NO BOM", "UNK": "UNK",
    "NOT_TRACKED": "NOT TRACKED", "UNK_PARTIAL": "UNK",
}


def status_text(status: str | None) -> str:
    s = str(status or "")
    return STATUS_TEXT.get(s, s.replace("_", " "))


def _num(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return None


def fmt_qty(x: Any, unit: str = "") -> str:
    v = _num(x)
    if v is None:
        return "—"
    txt = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"
    return f"{txt} {unit}".strip()


def fmt_pct(ratio: Any) -> str:
    v = _num(ratio)
    if v is None:
        return "—"
    return f"{v:.0%}" if v >= 0.1 else f"{v:.1%}"


def _worst(statuses: Iterable[str]) -> str:
    best = "OK"
    for s in statuses:
        if STATUS_RANK.get(s, 9) < STATUS_RANK.get(best, 9):
            best = s
    return best


def constraint_text(items: Iterable[dict], limit: int = 3) -> str:
    """'730009 84% (252,140 / 300,000 Kg) · 752744 …' — the worst items
    first, those without a need or with infinite coverage last."""
    rows = [i for i in items if _num(i.get("need"))]
    rows.sort(key=lambda i: (STATUS_RANK.get(str(i.get("status")), 9),
                             _num(i.get("ratio")) if _num(i.get("ratio")) is not None else 9e9))
    out = []
    for i in rows[:limit]:
        st_ = str(i.get("status"))
        if st_ == "OK":
            continue
        if st_ == "NOT_TRACKED":
            out.append(f"{i.get('item')} not in stock export")
            continue
        out.append(
            f"{i.get('item')} {fmt_pct(i.get('ratio'))} "
            f"({fmt_qty(i.get('available_total'))} / {fmt_qty(i.get('need'))} {i.get('unit', '')})".strip())
    return " · ".join(out)


# ---------------------------------------------------------------------------
# Board (schedule view) rows
# ---------------------------------------------------------------------------

def block_rows(report: dict, stamp: Callable[[float], str],
               only_risk: bool = False) -> list[dict]:
    """One row per production block of the board, earliest first."""
    rows: list[dict] = []
    for b in report.get("schedule_view") or []:
        st_ = str(b.get("status") or "")
        if only_risk and st_ not in RISK_STATUSES:
            continue
        items = b.get("items") or []
        short = [i for i in items if str(i.get("status")) in RISK_STATUSES and _num(i.get("need"))]
        rows.append({
            "Status": status_text(st_),
            "SKU": str(b.get("sku") or ""),
            "Line": str(b.get("line_name") or ""),
            "Start": stamp(float(b.get("start_h") or 0)),
            "End": stamp(float(b.get("end_h") or 0)),
            "Cases": round(float(b.get("cases") or 0)),
            "Items flagged": len(short),
            "Constraints": constraint_text(items),
            "_status": st_,
            "_start_h": float(b.get("start_h") or 0),
        })
    rows.sort(key=lambda r: (STATUS_RANK.get(r["_status"], 9), r["_start_h"]))
    return rows


# ---------------------------------------------------------------------------
# Demand plan rows
# ---------------------------------------------------------------------------

def demand_rows(report: dict, week_index: int | None,
                week_label: Callable[[int], str]) -> list[dict]:
    rows: list[dict] = []
    for d in report.get("demand_view") or []:
        if week_index is not None and d.get("week_index") != week_index:
            continue
        c = (d.get("constraining") or [{}])[0] if d.get("constraining") else {}
        st_ = str(d.get("status") or "")
        rows.append({
            "Status": status_text(st_),
            "SKU": str(d.get("sku") or ""),
            "Week": week_label(int(d.get("week_index") or 0)),
            "Target kg": round(float(d.get("target_kg") or 0)),
            "Coverage": fmt_pct(d.get("achievable_ratio")),
            "Top constraint": (f"{c.get('item', '')} {fmt_pct(c.get('ratio'))} "
                               f"({fmt_qty(c.get('available_total'))} / "
                               f"{fmt_qty(c.get('need'))} {c.get('unit', '')})".strip()
                               if c else ""),
            "_status": st_,
            "_ratio": _num(d.get("achievable_ratio")) if _num(d.get("achievable_ratio")) is not None else 9e9,
        })
    rows.sort(key=lambda r: (STATUS_RANK.get(r["_status"], 9), r["_ratio"]))
    return rows


# ---------------------------------------------------------------------------
# Component shortages — the purchasing list
# ---------------------------------------------------------------------------

def component_shortages(report: dict, week_index: int | None = None,
                        stamp: Callable[[float], str] | None = None,
                        week_label: Callable[[int], str] | None = None
                        ) -> list[dict]:
    """Per purchased component: what is on hand vs what the board and the
    demand plan need, which blocks it puts at risk and when the first one
    starts. Only items that are short somewhere (board block not OK, or
    demand need above availability) are listed; worst first.

    Board need sums every block's need for the item (the engine checks each
    block against TOTAL availability, so the sum is the honest "what the
    whole board wants"). Demand need comes from item_reverse (every item of
    every demand SKU, week-filtered), primaries only — an alternate's need
    is the primary's need restated, never additional."""
    stamp = stamp or (lambda h: f"h{h:.0f}")
    week_label = week_label or (lambda w: f"W{w}")
    agg: dict[str, dict] = {}

    def _slot(item: str) -> dict:
        return agg.setdefault(item, {
            "item": item, "designation": "", "unit": "", "available": None,
            "board_need": 0.0, "demand_need": 0.0, "board_blocks": 0,
            "blocks_short": 0, "first_short_h": None, "first_short_line": "",
            "skus": set(), "statuses": [], "alternates": set(), "note": "",
        })

    for b in report.get("schedule_view") or []:
        for i in b.get("items") or []:
            need = _num(i.get("need"))
            if not need:
                continue
            s = _slot(str(i.get("item")))
            s["designation"] = s["designation"] or str(i.get("designation") or "")
            s["unit"] = s["unit"] or str(i.get("unit") or "")
            av = _num(i.get("available_total"))
            if av is not None:
                s["available"] = av if s["available"] is None else max(s["available"], av)
            s["board_need"] += need
            s["board_blocks"] += 1
            s["skus"].add(str(b.get("sku") or ""))
            s["statuses"].append(str(i.get("status") or "OK"))
            s["note"] = s["note"] or str(i.get("note") or "")
            for a in i.get("alternates") or []:
                s["alternates"].add(str(a.get("item")))
            if str(i.get("status")) in RISK_STATUSES:
                s["blocks_short"] += 1
                start = float(b.get("start_h") or 0)
                if s["first_short_h"] is None or start < s["first_short_h"]:
                    s["first_short_h"] = start
                    s["first_short_line"] = str(b.get("line_name") or "")

    for item, entries in (report.get("item_reverse") or {}).items():
        for e in entries or []:
            if e.get("as_alternate_of"):
                continue
            if week_index is not None and e.get("week_index") != week_index:
                continue
            need = _num(e.get("need"))
            if not need:
                continue
            s = _slot(str(item))
            s["unit"] = s["unit"] or str(e.get("unit") or "")
            s["demand_need"] += need
            s["skus"].add(str(e.get("sku") or ""))

    # availability for demand-only items comes from the demand view's
    # constraining lists (the only place the engine serializes it)
    for d in report.get("demand_view") or []:
        if week_index is not None and d.get("week_index") != week_index:
            continue
        for c in d.get("constraining") or []:
            item = str(c.get("item"))
            if item in agg:
                s = agg[item]
                s["designation"] = s["designation"] or str(c.get("designation") or "")
                av = _num(c.get("available_total"))
                if av is not None and s["available"] is None:
                    s["available"] = av
                if str(c.get("status")) in RISK_STATUSES:
                    s["statuses"].append(str(c.get("status")))

    rows: list[dict] = []
    for s in agg.values():
        board_status = _worst(s["statuses"]) if s["statuses"] else "OK"
        av = s["available"]
        demand_short = (av is not None and s["demand_need"] > 0
                        and av < s["demand_need"] * 0.95)
        if board_status == "OK" and not demand_short:
            continue
        cov_board = (av / s["board_need"]) if (av is not None and s["board_need"] > 0) else None
        cov_demand = (av / s["demand_need"]) if (av is not None and s["demand_need"] > 0) else None
        status = board_status if board_status != "OK" else "AT_RISK"
        rows.append({
            "Status": status_text(status),
            "Item": s["item"],
            "Designation": s["designation"],
            "Unit": s["unit"],
            "Available": None if av is None else round(av, 1),
            "Board need": round(s["board_need"], 1),
            "Board coverage": fmt_pct(cov_board),
            "Demand need": round(s["demand_need"], 1),
            "Demand coverage": fmt_pct(cov_demand),
            "Blocks at risk": s["blocks_short"],
            "First run at risk": (f"{s['first_short_line']} {stamp(s['first_short_h'])}"
                                  if s["first_short_h"] is not None else ""),
            "SKUs": ", ".join(sorted(k for k in s["skus"] if k)),
            "Alternates": ", ".join(sorted(s["alternates"])),
            "Note": s["note"],
            "_status": status,
            "_cov": cov_board if cov_board is not None else (cov_demand if cov_demand is not None else 9e9),
        })
    rows.sort(key=lambda r: (STATUS_RANK.get(r["_status"], 9), r["_cov"]))
    return rows


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------

def _public(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])


def _autosize(ws) -> None:
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)


def excel_report(*, summary: list[tuple[str, Any]], board: list[dict],
                 demand: list[dict], shortages: list[dict],
                 appointments: list[dict], no_bom: list[str],
                 unk: list[dict]) -> bytes:
    """Multi-sheet workbook: Summary · Board · Demand plan · Component
    shortages · Receiving · Data quality. Every sheet is the same table the
    page shows, so what the planner prints is what they saw."""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(summary, columns=["Metric", "Value"]).to_excel(
            xw, sheet_name="Summary", index=False)
        for name, rows in (("Board", board), ("Demand plan", demand),
                           ("Component shortages", shortages)):
            df = _public(rows)
            if df.empty:
                df = pd.DataFrame({"note": ["nothing to report"]})
            df.to_excel(xw, sheet_name=name, index=False)
        recv = pd.DataFrame(appointments) if appointments else pd.DataFrame({"note": ["no receiving file"]})
        recv.to_excel(xw, sheet_name="Receiving", index=False)
        dq_rows = [{"kind": "NO_BOM", "sku": s, "detail": "no recipe in ediact 3"} for s in no_bom]
        dq_rows += [{"kind": "UNK", "sku": u.get("sku", ""), "detail": str({k: v for k, v in u.items() if k != "sku"})}
                    for u in unk]
        (pd.DataFrame(dq_rows) if dq_rows else pd.DataFrame({"note": ["clean"]})).to_excel(
            xw, sheet_name="Data quality", index=False)
        for ws in xw.book.worksheets:
            _autosize(ws)
            ws.freeze_panes = "A2"
    return buf.getvalue()
