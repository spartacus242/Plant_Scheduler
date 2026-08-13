# Guard — tests must never read the live data/reference dir.

# data/reference/ is refreshed daily by the GitHub file-drop bridge
# (scripts/fs-live-*.py). Tests that pin values against it break on every
# refresh. Live-data tests use the pinned snapshot instead:
#   data/test_fixtures/live_2026-08-13/
# test_changeover_cache.py is exempt: it stages a copy into tmp and asserts
# nothing about live values.

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
EXEMPT = {"test_changeover_cache.py"}
LIVE_REF = re.compile(r"data\W+reference")


def test_no_live_data_reads_in_tests():
    hits = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name in EXEMPT:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8"), 1):
            if LIVE_REF.search(line):
                hits.append(f"{p.name}:{i}: {line.strip()}")
    assert not hits, "tests read live data/reference:\n" + "\n".join(hits)
