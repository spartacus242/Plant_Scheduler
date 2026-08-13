# Guard — tests must never read the live data/reference dir.

# data/reference/ is refreshed daily by the GitHub file-drop bridge
# (scripts/fs-live-*.py). Tests that pin values against it break on every
# refresh. Live-data tests use the pinned snapshot instead:
#   data/test_fixtures/live_2026-08-13/
# test_changeover_cache.py is exempt: it stages a copy into tmp and asserts
# nothing about live values. This guard file itself is exempt: it only globs
# test files and never touches data.

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
EXEMPT = {"test_changeover_cache.py", "test_no_live_data_reads.py"}
LIVE_REF = re.compile(r"data[\\/]reference")


def _is_code(line: str) -> bool:
    """True when the line is code, not a comment or docstring line."""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False
    if stripped.startswith(('"""', "'''")):
        return False
    return True


def test_no_live_data_reads_in_tests():
    hits = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name in EXEMPT:
            continue
        in_string = False  # inside a multi-line triple-quoted string body
        for i, line in enumerate(
            p.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.lstrip()
            if in_string:
                # Closing triple-quote (alone on the line, or trailing) ends
                # the body; a line with only the opener keeps it open.
                if stripped.startswith(('"""', "'''")) or stripped.endswith(
                    ('"""', "'''")
                ):
                    in_string = False
                continue
            if stripped.startswith(('"""', "'''")) and not (
                stripped.count('"""') >= 2 or stripped.count("'''") >= 2
            ):
                in_string = True
                continue
            if not _is_code(line):
                continue
            if LIVE_REF.search(line):
                hits.append(f"{p.name}:{i}: {line.strip()}")
    assert not hits, "tests read live data/reference:\n" + "\n".join(hits)
