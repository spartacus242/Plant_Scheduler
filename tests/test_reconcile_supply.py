# tests/test_reconcile_supply.py — supply_findings (contract 2026-09-01 §6).
#
# Pure transform of a stock_check_report dict carrying the additive supply
# keys (supply_meta / inbound / schedule_view[i].supply). Every report here
# is synthetic; the one "real engine" test builds its Supply dicts with
# stockcheck.timeline on tiny in-memory inputs. assess_plan wiring is
# smoke-tested against a self-contained tmp_path dir.

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))

from helpers.reconcile_engine import (  # noqa: E402
    BLOCKING,
    DATA,
    INFO,
    STOCK,
    WARN,
    Finding,
    assess_plan,
    summary,
    supply_findings,
)
from helpers.timefmt import hour_to_iso  # noqa: E402
from stockcheck.timeline import verdict_text  # noqa: E402

ANCHOR = datetime(2026, 8, 31)          # Monday, hour 0
TODAY = date(2026, 9, 1)
BINDING = {"po8": "30043543", "qty": 5000.0, "ready_h": 40.0,
           "receipt_date": "2026-09-01",
           "label": "PO 30043543 · 754751 · 5,000 EA · 2026-09-01 · ERP date"}


def _supply(verdict: str, *, minor=False, binding=True, action="move",
            item="754751") -> dict:
    """A Supply dict shaped like stockcheck.timeline.evaluate_block's."""
    sup = {
        "key": "b1@100.00", "verdict": verdict, "backed": False,
        "minor": minor, "mid_run": False, "item": item,
        "covered_frac": 0.39, "depletion_h": 95.3,
        "lead_h": 60.0 if binding else None,
        "safe_from_h": 136.0 if binding else None,
        "binding": dict(BINDING) if binding else None,
        "action": action, "lead_from": "block_start", "items": [],
        "untracked": [], "co_consumers": {}, "text": "",
    }
    sup["text"] = verdict_text(sup)
    return sup


def _block(block_id="b1", sku="280351", line="P09", start_h=100.0,
           supply=None, **extra) -> dict:
    end_h = start_h + 10.0 if isinstance(start_h, float) else start_h
    row = {"block_id": block_id, "sku": sku, "line_name": line,
           "start_h": start_h, "end_h": end_h, "qty_kg": 12000.0,
           "status": "OK", "items": []}
    if supply is not None:
        row["supply"] = supply
    row.update(extra)
    return row


def _inbound(state="ok", max_receipt_date="2026-09-30", **extra) -> dict:
    d = {"state": state, "source_path": "C:/live/open_pos.xlsx",
         "source_mtime": "2026-09-01 06:10:00", "as_of": None,
         "max_receipt_date": max_receipt_date, "n_rows": 42, "errors": [],
         "lines": [], "receipts": {},
         "join": {"used": 30, "received": 5, "overdue": 4, "landed": 1,
                  "unjoinable": 2, "unit_mismatch": 0,
                  "offsite_no_transfer": 0},
         "appt_join": {"matched": 3, "total": 30}}
    d.update(extra)
    return d


def _report(blocks, *, feed_state="ok", inbound="default", anchor=None
            ) -> dict:
    rep = {
        "schedule_view": list(blocks), "demand_view": [], "item_reverse": {},
        "supply_meta": {
            "rules": {}, "snapshot_h": {}, "feed_state": feed_state,
            "snapshot_stamp": {"rm": "2026-09-01 14:46:00",
                               "pkg": "2026-09-01 14:47:00"},
            "receipts_window_end_h": 800.0, "opening": {}, "tracked": [],
            "in_house": [], "units": {}, "designations": {}},
    }
    if inbound == "default":
        inbound = _inbound(state=feed_state if feed_state != "ok" else "ok")
    if inbound is not None:
        rep["inbound"] = inbound
    if anchor is not None:
        rep["anchor"] = anchor
    return rep


def _blocks(findings):
    return [f for f in findings if f.key.startswith("supply:")]


def _feed(findings) -> Finding:
    hits = [f for f in findings if f.key == "supply_feed"]
    assert len(hits) == 1, [f.key for f in findings]
    return hits[0]


# ---------------------------------------------------------------------------
# per-block severity paths
# ---------------------------------------------------------------------------

def test_short_on_fresh_feed_is_blocking_with_calendar_deep_link():
    sup = _supply("SHORT")
    f = supply_findings(_report([_block(supply=sup)]), ANCHOR, today=TODAY)
    blk = _blocks(f)
    assert len(blk) == 1
    x = blk[0]
    assert x.severity == BLOCKING and x.category == STOCK
    assert x.key == f"supply:b1@{hour_to_iso(100.0, ANCHOR)}"
    assert x.key == "supply:b1@2026-09-04 04:00"
    # message = the timeline's own sentence, restamped in the anchor frame
    assert x.title == verdict_text(sup, ANCHOR)
    assert x.title.startswith("⛔ 754751")
    assert "h95.3" not in x.title and "Thu 9/3 23:18" in x.title
    assert x.page == "pages/calendar.py"
    assert x.context["focus"] == "b1"
    for k in ("block_id", "sku", "line", "line_name", "start_h", "verdict"):
        assert k in x.context
    # "line" is the contract §6 key; "line_name" mirrors stock_findings
    assert x.context["sku"] == "280351" and x.context["line"] == "P09"
    assert x.context["line_name"] == x.context["line"]
    assert x.context["start_h"] == 100.0 and x.context["verdict"] == "SHORT"
    assert "280351 on P09" in x.detail and "12,000 kg" in x.detail


def test_short_on_non_ok_feed_is_only_a_warning():
    for state in ("missing", "stale", "empty"):
        rep = _report([_block(supply=_supply("SHORT"))], feed_state=state)
        blk = _blocks(supply_findings(rep, ANCHOR, today=TODAY))
        assert [x.severity for x in blk] == [WARN], state


def test_dependent_warns_minor_ok_no_data_and_unknown_are_silent():
    rows = [
        _block("d1", supply=_supply("DEPENDENT")),
        _block("d2", supply=_supply("DEPENDENT", minor=True)),
        _block("o1", supply=_supply("OK")),
        _block("n1", supply=_supply("NO_DATA")),
        _block("z1", supply=_supply("SOMETHING_NEW")),   # forward-compatible
        _block("x1"),                                    # no supply at all
        _block("x2", supply="not-a-dict"),
    ]
    blk = _blocks(supply_findings(_report(rows), ANCHOR, today=TODAY))
    assert [(x.context["block_id"], x.severity) for x in blk] == [("d1", WARN)]
    assert blk[0].title.startswith("🚚 754751")
    assert "safe from" in blk[0].title


def test_locked_block_action_says_chase_the_po():
    rows = [_block("l1", supply=_supply("SHORT", action="chase_po")),
            _block("m1", supply=_supply("SHORT", action="move"))]
    blk = _blocks(supply_findings(_report(rows), ANCHOR, today=TODAY))
    by = {x.context["block_id"]: x for x in blk}
    assert "locked/running" in by["l1"].detail
    assert "PO 30043543" in by["l1"].action
    assert "movable" in by["m1"].detail
    assert "Move or shrink" in by["m1"].action


def test_short_without_binding_still_reads():
    sup = _supply("SHORT", binding=False)
    blk = _blocks(supply_findings(_report([_block(supply=sup)]), ANCHOR,
                                  today=TODAY))
    assert len(blk) == 1 and "no inbound counted" in blk[0].title
    assert blk[0].context["po8"] is None


# ---------------------------------------------------------------------------
# old cache / error reports
# ---------------------------------------------------------------------------

def test_report_without_supply_meta_yields_no_supply_findings():
    old = {"schedule_view": [_block(supply=_supply("SHORT"))],
           "demand_view": [], "item_reverse": {}}
    assert supply_findings(old, ANCHOR, today=TODAY) == []
    assert supply_findings({}, ANCHOR, today=TODAY) == []
    assert supply_findings(None, ANCHOR, today=TODAY) == []
    assert supply_findings({"error": "ediact missing",
                            "supply_meta": {"feed_state": "ok"}},
                           ANCHOR, today=TODAY) == []


def test_supply_meta_alone_still_reports_the_feed():
    rep = _report([], inbound=None)          # meta but no inbound block
    f = supply_findings(rep, ANCHOR, today=TODAY)
    assert [x.key for x in f] == ["supply_feed"]
    assert _feed(f).context["state"] == "ok"


# ---------------------------------------------------------------------------
# key stability across anchor rolls
# ---------------------------------------------------------------------------

def test_key_is_stable_when_the_anchor_rolls_but_the_start_iso_stays():
    sup = _supply("SHORT")
    before = supply_findings(_report([_block(start_h=100.0, supply=sup)]),
                             ANCHOR, today=TODAY)
    rolled = ANCHOR + timedelta(days=7)     # rebase: every hour shifts -168
    after = supply_findings(_report([_block(start_h=100.0 - 168.0,
                                            supply=sup)]),
                            rolled, today=TODAY)
    assert _blocks(before)[0].key == _blocks(after)[0].key
    assert _blocks(before)[0].key == "supply:b1@2026-09-04 04:00"
    # a different start hour is a different key (split MOs share block_id)
    other = supply_findings(_report([_block(start_h=124.0, supply=sup)]),
                            ANCHOR, today=TODAY)
    assert _blocks(other)[0].key != _blocks(before)[0].key


def test_report_anchor_frame_wins_over_the_argument():
    # The report's hours were computed against ITS anchor; a caller passing
    # a newer toml anchor must not re-stamp them into the wrong week.
    sup = _supply("SHORT")
    rep = _report([_block(start_h=100.0, supply=sup)],
                  anchor="2026-08-31 00:00:00")
    f = supply_findings(rep, ANCHOR + timedelta(days=7), today=TODAY)
    assert _blocks(f)[0].key == "supply:b1@2026-09-04 04:00"


def test_bad_start_hour_never_raises():
    sup = _supply("SHORT")
    rows = [_block("n", start_h=None, supply=sup),
            _block("s", start_h="abc", supply=sup),
            _block("f", start_h=float("nan"), supply=sup)]
    blk = _blocks(supply_findings(_report(rows), ANCHOR, today=TODAY))
    assert len(blk) == 3
    assert all(x.context["start_h"] == 0.0 for x in blk)


# ---------------------------------------------------------------------------
# the supply_feed DATA finding
# ---------------------------------------------------------------------------

def test_feed_ok_with_a_wide_window_is_info_and_quotes_the_stamps():
    f = _feed(supply_findings(_report([]), ANCHOR, today=TODAY))
    assert f.category == DATA and f.severity == INFO
    assert "42 lines" in f.title and "30 counted" in f.title
    assert "2026-09-30" in f.title
    assert "POs 2026-09-01 06:10:00" in f.detail
    assert "stock rm 2026-09-01 14:46:00" in f.detail
    assert "stock pkg 2026-09-01 14:47:00" in f.detail
    assert f.page == "pages/stock_check.py"
    assert f.context["max_receipt_date"] == "2026-09-30"


def test_feed_window_shorter_than_seven_days_warns():
    rep = _report([], inbound=_inbound(max_receipt_date="2026-09-05"))
    f = _feed(supply_findings(rep, ANCHOR, today=TODAY))
    assert f.severity == WARN
    assert "PO window ends" in f.title and "wider export" in f.title
    assert "POs 2026-09-01 06:10:00" in f.detail
    # exactly 7 days out is still fine
    rep = _report([], inbound=_inbound(max_receipt_date="2026-09-08"))
    assert _feed(supply_findings(rep, ANCHOR, today=TODAY)).severity == INFO


def test_feed_window_check_honours_a_datetime_today():
    rep = _report([], inbound=_inbound(max_receipt_date="2026-09-05"))
    f = _feed(supply_findings(rep, ANCHOR,
                              today=datetime(2026, 8, 20, 9, 30)))
    assert f.severity == INFO      # 16 days ahead of that "today"


def test_feed_missing_stale_empty_and_unknown_states_warn():
    cases = {
        "missing": ("missing", "Ask IT"),
        "stale": ("stale", "fresh"),
        "empty": ("no lines", "Re-export"),
        "weird": ("state 'weird'", "Inbound tab"),
    }
    for state, (in_title, in_action) in cases.items():
        rep = _report([], feed_state=state,
                      inbound=_inbound(state=state,
                                       max_receipt_date="2026-08-20"))
        f = _feed(supply_findings(rep, ANCHOR, today=TODAY))
        assert f.severity == WARN, state
        assert in_title in f.title, (state, f.title)
        assert in_action in f.action, (state, f.action)
        assert f.context["state"] == state


def test_feed_missing_names_the_configured_path():
    rep = _report([], feed_state="missing",
                  inbound=_inbound(state="missing",
                                   source_path="Z:/nope/open_pos.xlsx",
                                   max_receipt_date=None, n_rows=0))
    f = _feed(supply_findings(rep, ANCHOR, today=TODAY))
    assert "Z:/nope/open_pos.xlsx" in f.detail
    rep = _report([], feed_state="missing",
                  inbound=_inbound(state="missing", source_path="",
                                   max_receipt_date=None, n_rows=0))
    f = _feed(supply_findings(rep, ANCHOR, today=TODAY))
    assert "po_report_path" in f.detail


def test_feed_as_of_beats_mtime_in_the_stamp():
    rep = _report([], inbound=_inbound(as_of="2026-09-01"))
    f = _feed(supply_findings(rep, ANCHOR, today=TODAY))
    assert "POs 2026-09-01 ·" in f.detail or f.detail.endswith("POs 2026-09-01)")
    assert "06:10:00" not in f.detail


# ---------------------------------------------------------------------------
# real engine output flows through unchanged
# ---------------------------------------------------------------------------

def _engine_report(receipts: dict, feed_state="ok") -> dict:
    from stockcheck.timeline import build_timelines, evaluate_board
    blocks = [{"block_id": "b1", "sku": "S1", "line_name": "P09",
               "start_h": 100.0, "end_h": 110.0, "cases": 100.0,
               "locked": False, "running": False, "cases_left": None}]
    needs = {"S1": {"kg_per_case": 10.0, "items": [
        {"item": "754751", "per_case": 1.0, "unit": "EA", "alts": []}]}}
    tl = build_timelines(blocks, needs, {"754751": 50.0}, receipts, {},
                         tracked={"754751"}, in_house=set())
    board = evaluate_board(blocks, tl, {}, feed_state, 800.0)
    # Agent K (stock-14, 2026-09-03): stockcheck.timeline.block_key is now
    # `<block_id>|<JS String(start_h)>` (100.0 -> "100") so Python and the
    # Gantt's stockRisk.ts agree on one key; the old "@%.2f" form was the
    # parity bug. Only the engine lookup changes; `_block` keeps its literal.
    sup = board["b1|100"]
    return _report([_block("b1", sku="S1", start_h=100.0, supply=sup)],
                   feed_state=feed_state), sup


def test_engine_dependent_and_short_verdicts_become_findings():
    rep, sup = _engine_report({"754751": [dict(BINDING, ready_h=60.0,
                                              qty=100.0)]})
    assert sup["verdict"] == "DEPENDENT" and not sup["minor"]
    f = _blocks(supply_findings(rep, ANCHOR, today=TODAY))
    assert [x.severity for x in f] == [WARN]
    assert f[0].title == verdict_text(sup, ANCHOR)
    assert "PO 30043543" in f[0].title

    rep, sup = _engine_report({})
    assert sup["verdict"] == "SHORT"
    f = _blocks(supply_findings(rep, ANCHOR, today=TODAY))
    assert [x.severity for x in f] == [BLOCKING]

    # the engine itself downgrades to NO_DATA on a bad feed -> silent
    rep, sup = _engine_report({}, feed_state="missing")
    assert sup["verdict"] == "NO_DATA"
    assert _blocks(supply_findings(rep, ANCHOR, today=TODAY)) == []


# ---------------------------------------------------------------------------
# assess_plan wiring (Home counts + Reconcile page read the same list)
# ---------------------------------------------------------------------------

def _seed_dir(tmp_path: Path) -> Path:
    dd = tmp_path / "data"
    (dd / "reference").mkdir(parents=True)
    (dd / "calendar_blocks.csv").write_text(
        "block_id,block_type,line_id,line_name,start_h,end_h,label,"
        "order_id,sku,sku_description,qty_kg,locked,attrs\n",
        encoding="utf-8")
    return dd


CFG = {"scheduler": {"horizon_weeks": 3,
                     "planning_start_date": "2026-08-31 00:00:00"}}


def test_assess_plan_emits_supply_findings_from_the_report(tmp_path):
    dd = _seed_dir(tmp_path)
    rep = _report([_block("b1", supply=_supply("SHORT")),
                   _block("b2", supply=_supply("DEPENDENT"))],
                  inbound=_inbound(max_receipt_date="2099-01-01"))
    findings = assess_plan(dd, CFG, stock_report=rep)
    keys = {f.key for f in findings}
    assert "supply:b1@2026-09-04 04:00" in keys
    assert "supply:b2@2026-09-04 04:00" in keys
    assert "supply_feed" in keys
    assert not any(k.startswith("rule_error:") for k in keys)
    by = {f.key: f for f in findings}
    assert by["supply:b1@2026-09-04 04:00"].severity == BLOCKING
    assert by["supply:b2@2026-09-04 04:00"].severity == WARN
    assert by["supply_feed"].severity == INFO
    # Home's _reconcile_counts is summary(assess_plan(...)): BLOCKING counts
    assert summary(findings)[BLOCKING] >= 1
    # sorted: the blocking STOCK finding leads the list
    assert findings[0].severity == BLOCKING and findings[0].category == STOCK


def test_assess_plan_with_an_old_cache_report_adds_nothing(tmp_path):
    dd = _seed_dir(tmp_path)
    old = {"schedule_view": [_block("b1", supply=_supply("SHORT"))],
           "demand_view": [], "item_reverse": {}}
    findings = assess_plan(dd, CFG, stock_report=old)
    keys = {f.key for f in findings}
    assert not any(k.startswith("supply") for k in keys)
    assert not any(k.startswith("rule_error:") for k in keys)


def test_assess_plan_uses_the_report_anchor_for_stamps(tmp_path):
    dd = _seed_dir(tmp_path)
    rep = _report([_block("b1", supply=_supply("SHORT"))],
                  anchor="2026-09-07 00:00:00",
                  inbound=_inbound(max_receipt_date="2099-01-01"))
    findings = assess_plan(dd, CFG, stock_report=rep)   # toml anchor 08-31
    assert "supply:b1@2026-09-11 04:00" in {f.key for f in findings}


# ---------------------------------------------------------------------------
# robustness regressions (verifier round 1)
# ---------------------------------------------------------------------------

def _rotten_rows(*, junk=True) -> list:
    """Shapes the real api never emits but a hand-edited cache might: a
    string `binding`, a non-numeric `covered_frac`, a non-list `items`, and
    (junk=True) rows that are not dicts at all — the latter also trip the
    untouched flat stock rule, so the assess_plan test leaves them out."""
    rows = [
        _block("s1", supply=dict(_supply("SHORT"), binding="30043543")),
        _block("s2", supply=dict(_supply("SHORT", binding=False),
                                 covered_frac="x")),
        _block("d1", start_h=110.0,
               supply=dict(_supply("DEPENDENT"), items="oops")),
    ]
    if junk:
        rows += [None, "row", 42]
    rows.append(_block("ok", supply=_supply("OK")))
    return rows


def test_malformed_supply_rows_never_raise_and_short_still_surfaces():
    # Before: verdict_text/binding.get raised -> assess_plan turned the whole
    # rule into rule_error:supply and EVERY short block vanished from Reconcile.
    blk = _blocks(supply_findings(_report(_rotten_rows()), ANCHOR, today=TODAY))
    by = {x.context["block_id"]: x for x in blk}
    assert set(by) == {"s1", "s2", "d1"}
    assert by["s1"].severity == BLOCKING and by["s2"].severity == BLOCKING
    assert by["d1"].severity == WARN
    # the sentence falls back to a plain headline, the finding itself stays
    for x in blk:
        assert "detail unreadable" in x.title, x.title
        assert x.title.split()[1].startswith("754751")
    assert by["s1"].title.startswith("⛔") and by["d1"].title.startswith("🚚")
    assert by["s1"].context["po8"] is None           # string binding: no PO
    assert by["s1"].key == "supply:s1@2026-09-04 04:00"
    assert by["d1"].key == "supply:d1@2026-09-04 14:00"
    assert "the inbound" in by["s1"].action


def test_assess_plan_survives_malformed_rows_without_a_rule_error(tmp_path):
    dd = _seed_dir(tmp_path)
    rep = _report(_rotten_rows(junk=False),
                  inbound=_inbound(max_receipt_date="2099-01-01"))
    findings = assess_plan(dd, CFG, stock_report=rep)
    keys = {f.key for f in findings}
    # Before: the whole rule collapsed into this one WARN and no supply:* key
    assert "rule_error:supply" not in keys, keys
    assert not any(k.startswith("rule_error:") for k in keys), keys
    assert {"supply:s1@2026-09-04 04:00", "supply:s2@2026-09-04 04:00",
            "supply:d1@2026-09-04 14:00", "supply_feed"} <= keys
    assert summary(findings)[BLOCKING] >= 2


def test_garbage_report_anchor_falls_back_to_the_argument():
    # Before: parse_anchor silently fell back to DEFAULT_ANCHOR (2026-02-15)
    # and the key read 'supply:b1@2026-02-19 04:00' — a frame nobody uses.
    sup = _supply("SHORT")
    for bad in ("garbage!!", "", None, "31/31/2026", "nan"):
        rep = _report([_block(supply=sup)], anchor=bad)
        f = _blocks(supply_findings(rep, ANCHOR, today=TODAY))
        assert f[0].key == "supply:b1@2026-09-04 04:00", (bad, f[0].key)
        assert "2026-02" not in f[0].key
        assert "Thu 9/3 23:18" in f[0].title, (bad, f[0].title)
    # every anchor spelling the report might carry lands in the same frame
    for good in ("2026-08-31", "2026-08-31 00:00:00", "2026-08-31T00:00:00",
                 "2026-08-31 00:00", datetime(2026, 8, 31), date(2026, 8, 31)):
        rep = _report([_block(supply=sup)], anchor=good)
        f = _blocks(supply_findings(rep, ANCHOR + timedelta(days=7),
                                    today=TODAY))
        assert f[0].key == "supply:b1@2026-09-04 04:00", (good, f[0].key)
