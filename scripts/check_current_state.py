"""Verify the current-state builder end-to-end against the REAL exports and
that its output round-trips through save_calendar/load_calendar (to a temp
file, never the planner's real calendar)."""
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from helpers import horizon as hz
from helpers.calendar_io import load_calendar, load_lines, save_calendar
from helpers.config import load_toml
from helpers.current_state import build_current_state

cfg = load_toml()
h = hz.resolve(cfg)
ref = ROOT / "data" / "reference"
cs = build_current_state(
    h,
    manprg_paths=[ref / "manprg.txt", ref / "manprg2.txt"],
    cip_path=ref / "cip_info.csv",
    lines=load_lines(ROOT / "data" / "lines.csv"),
    cfg=cfg,
)
print("counts:", cs.counts)
print("anchor:", h.anchor, "now_h:", round(h.now_h, 2), "end_h:", h.end_h)

prod = cs.blocks[cs.blocks["block_type"] == "production"]
bad = []
for line, grp in prod.groupby("line_name"):
    g = grp.sort_values("start_h")
    rows = list(g.itertuples())
    for a, b in zip(rows, rows[1:]):
        if b.start_h < a.end_h - 1e-6:
            bad.append((line, a.order_id, b.order_id))
print("overlaps:", bad or "none")
print("locked running blocks:", int(prod["locked"].astype(bool).sum()))
print("min start_h:", round(prod["start_h"].min(), 2),
      "max end_h:", round(prod["end_h"].max(), 2))
print("line_id range:", sorted(set(cs.blocks["line_id"]))[:20])

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "calendar_blocks.csv"
    save_calendar(cs.blocks, p)
    back = load_calendar(p)
    assert len(back) == len(cs.blocks), (len(back), len(cs.blocks))
    print("round-trip OK:", len(back), "blocks")

for line in sorted(cs.line_free_h)[:6]:
    print(f"  {line}: first free hour {cs.line_free_h[line]}")
print("running MOs:", [(r["line"], r["mo"], r["completion_pct"]) for r in cs.running])
