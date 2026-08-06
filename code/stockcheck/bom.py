# code/stockcheck/bom.py — ediact BOM graph + recursive explosion.
#
# Structure (verified against real ediact 3 data):
#   FG activity (out: CAS)  -> IN intermediate items (SLV sleeves, packaging)
#   SU activity (out: SLV)  -> IN POU pouches, sleeve blanks (EA)
#   POU activity (out: POU) -> IN film (M2), caps (EA), slurries (Kg)
#   R  activity (out: Kg)   -> IN purchased raw items (73xxxx) etc.
#
# Normalization is PER ACTIVITY: need(input) = need(output) * qty_in / qty_out.
# Blank-qty Input rows are ALTERNATES for a qty-bearing sibling (user-confirmed).
# 'xxxSCN' self-rows and 'of cost' rows are excluded. Cycles terminate branches.

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class RequirementGroup:
    """One logical requirement: a primary item plus acceptable alternates."""
    primary_item: str
    designation: str
    need_qty: float
    unit: str
    alternates: list[dict] = field(default_factory=list)  # [{item, designation}]
    paths: list[list[str]] = field(default_factory=list)  # activity chains


@dataclass
class ExplosionResult:
    sku: str
    qty_cas: float
    requirements: list[RequirementGroup] = field(default_factory=list)
    unk_items: list[dict] = field(default_factory=list)   # [{item, reason, path}]
    cycles: list[list[str]] = field(default_factory=list)
    status: str = "OK"  # OK | NO_BOM | UNK_PARTIAL


class BomGraph:
    def __init__(self, ediact: pd.DataFrame):
        df = ediact[ediact["item_type"].isin(("Output", "Input"))].copy()
        df = df[~df["item"].str.endswith("SCN")]
        self.df = df
        self._by_act = {a: g for a, g in df.groupby("Activity")}
        self._by_pf = {p: set(g["Activity"]) for p, g in df.groupby("PF")}

    def has_bom(self, sku: str) -> bool:
        return sku in self._by_pf

    @staticmethod
    def _resolve(act_df: pd.DataFrame, as_of) -> pd.DataFrame:
        """Latest effective-date row <= as_of wins per (item_type, item)."""
        if as_of is None:
            as_of = pd.Timestamp.today().normalize()
        cutoff = pd.Timestamp(as_of)
        dated = act_df.copy()
        dated["_eff"] = dated["effective_date"].fillna(pd.Timestamp.min)
        dated = dated[dated["_eff"] <= cutoff]
        if dated.empty:
            dated = act_df.copy()
            dated["_eff"] = dated["effective_date"].fillna(pd.Timestamp.min)
        dated = dated.sort_values("_eff")
        return dated.groupby(["item_type", "item"], as_index=False).tail(1)

    def explode(self, sku: str, qty_cas: float, as_of=None) -> ExplosionResult:
        res = ExplosionResult(sku=sku, qty_cas=qty_cas)
        if not self.has_bom(sku):
            res.status = "NO_BOM"
            return res
        req: dict[str, RequirementGroup] = {}
        # ediact lists ALL sub-activities under the PF section. Only the FG
        # activity (code == PF, or PF-variant like '280358-A') is the entry
        # point — the rest are reachable by recursion from it.
        entries = sorted(a for a in self._by_pf[sku]
                         if a == sku or a.startswith(sku + "-"))
        if not entries:  # no self-activity: fall back to FG-family activity
            entries = sorted(
                a for a in self._by_pf[sku]
                if (self._by_act.get(a) is not None
                    and (self._by_act[a]["family"] == "FG").any()))
        for act in entries:
            self._walk(act, qty_cas, [], res, req, as_of)
        # Intermediates are produced in-line, never purchased/stocked: their
        # own "need" rows are meaningless once exploded through. Drop them.
        req = {k: g for k, g in req.items() if g.primary_item not in self._by_act}
        res.requirements = sorted(req.values(), key=lambda r: r.primary_item)
        if res.unk_items:
            res.status = "UNK_PARTIAL"
        return res

    def _add_need(self, req: dict, item: str, designation: str, qty: float,
                  unit: str, path: list[str]) -> RequirementGroup:
        key = f"{item}|{unit}"
        if key in req:
            g = req[key]
            g.need_qty += qty
            if path not in g.paths:
                g.paths.append(path)
        else:
            g = RequirementGroup(primary_item=item,
                                 designation=designation,
                                 need_qty=qty, unit=unit,
                                 paths=[path])
            req[key] = g
        return g

    def _walk(self, act_code: str, need_out: float, path: list[str],
              res: ExplosionResult, req: dict, as_of) -> None:
        """need_out = required quantity of this activity's output, in its own
        output unit (CAS for FG, SLV, POU, Kg for R)."""
        if act_code in path:
            res.cycles.append(path + [act_code])
            return
        act_df = self._by_act.get(act_code)
        if act_df is None:
            return
        act_df = self._resolve(act_df, as_of)
        path = path + [act_code]

        outs = act_df[act_df["item_type"] == "Output"]
        ins = act_df[act_df["item_type"] == "Input"]

        out_row = None
        for _, r in outs.iterrows():
            if r["item"] == act_code:
                out_row = r
                break
        if out_row is None and not outs.empty:
            out_row = outs.iloc[0]
        out_qty = out_row["qty_act"] if out_row is not None else None
        run_scale = (need_out / out_qty) if (out_qty and out_qty > 0) else None

        qty_ins = ins[ins["qty_act"].notna() & (ins["qty_act"] > 0)]
        blank_ins = ins[ins["qty_act"].isna() | (ins["qty_act"] <= 0)]

        recurse: list[tuple[str, float]] = []
        made: list[RequirementGroup] = []

        for _, r in qty_ins.iterrows():
            item = r["item"]
            if run_scale is None:
                res.unk_items.append({
                    "item": item,
                    "reason": f"activity {act_code} lacks output qty",
                    "path": path})
                continue
            need = run_scale * r["qty_act"]
            g = self._add_need(req, item, str(r["designation"]).strip(),
                               need, r["unit"], path)
            made.append((g, r["qty_act"]))
            if item in self._by_act:
                recurse.append((item, need))

        # Blank-qty rows are ALTERNATES (user-confirmed). In slurry activities
        # they cluster BEFORE the primaries (730009 before BT001), so attach
        # each blank row to the LARGEST same-unit primary of this activity —
        # that is the base ingredient it substitutes for.
        largest_by_unit: dict[str, RequirementGroup] = {}
        for g, raw_qty in made:
            cur = largest_by_unit.get(g.unit)
            if cur is None or raw_qty > cur[1]:
                largest_by_unit[g.unit] = (g, raw_qty)
        for _, r in blank_ins.iterrows():
            item = r["item"]
            tgt = largest_by_unit.get(r["unit"])
            if tgt is not None:
                g = tgt[0]
                if all(a["item"] != item for a in g.alternates):
                    g.alternates.append(
                        {"item": item,
                         "designation": str(r["designation"]).strip()})
            else:
                res.unk_items.append({
                    "item": item,
                    "reason": f"alternate without primary in {act_code}",
                    "path": path})

        for item, need in recurse:
            self._walk(item, need, path, res, req, as_of)
