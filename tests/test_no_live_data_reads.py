# Guard — tests must never read the live data/reference dir.
#
# data/reference/ is refreshed every 30 minutes by the GitHub file-drop bridge
# (scripts/fs-live-*.py). Tests that pin values against it break on every
# refresh. Live-data tests use the pinned snapshots instead:
#   data/test_fixtures/live_2026-08-13/   (manprg / cip_info / capabilities)
#   data/test_fixtures/solver_tiny/       (a complete solver data dir)
#
# Two layers (fix T-4, audit tests-6, 2026-09-03):
#   1. RUNTIME guard -- tests/conftest.py wraps builtins.open / io.open for
#      every test and fails a test that READS a file under data/reference
#      unless it carries @pytest.mark.live_data or its module is listed in
#      conftest.LIVE_READ_ALLOWED_MODULES (known debt, one line of reason
#      each). This is the actual guard: it sees pandas.read_csv, Path.read_text,
#      shutil.copy, tomllib -- every path-based read -- whatever the path was
#      built from. The self-test below proves it fires.
#   2. SOURCE guard (this file, kept) -- a static scan for the literal
#      'data/reference' AND for the segment join  "data" / "reference"  that
#      the old regex could not see (six modules built the path that way and
#      the guard stayed green). Static hits in an allowlisted module are
#      reported as debt, never as a pass.

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import conftest as cft  # noqa: E402

# test_changeover_cache.py stages a copy into tmp and asserts nothing about
# live values; this guard file itself only globs test files (and opens ONE
# live path on purpose in the runtime self-test); conftest.py IS the runtime
# guard and names the live dir to recognise it.
EXEMPT = {"test_changeover_cache.py", "test_no_live_data_reads.py", "conftest.py"}
LIVE_REF = re.compile(r"data[\\/]reference")
# ROOT / "data" / "reference", Path("data") / "reference", ("data", "reference")
SEGMENT_JOIN = re.compile(r"""["']data["']\s*(?:/|,)\s*["']reference["']""")


def _is_code(line: str) -> bool:
    """True when the line is code, not a comment or docstring line."""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False
    if stripped.startswith(('"""', "'''")):
        return False
    return True


def _code_lines(p: Path):
    in_string = False  # inside a multi-line triple-quoted string body
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
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
        yield i, line


def _live_data_marked_ranges(p: Path) -> list[tuple[int, int]]:
    """(first, last) line ranges of functions decorated @pytest.mark.live_data
    -- the explicit opt-in; a live path built inside one is not a hit."""
    import ast
    out = []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if "live_data" in ast.unparse(dec):
                out.append((node.lineno, node.end_lineno or node.lineno))
    return out


def _static_hits(pattern: re.Pattern) -> list[tuple[str, int, str]]:
    hits = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name in EXEMPT:
            continue
        marked = _live_data_marked_ranges(p)
        for i, line in _code_lines(p):
            if pattern.search(line) and not any(a <= i <= b for a, b in marked):
                hits.append((p.name, i, line.strip()))
    return hits


def test_no_live_data_reads_in_tests():
    """The original literal scan: 'data/reference' spelled out in test code."""
    hits = _static_hits(LIVE_REF)
    assert not hits, "tests read live data/reference:\n" + "\n".join(
        f"{n}:{i}: {s}" for n, i, s in hits)


def test_no_segment_joined_live_paths_outside_the_allowlist():
    """The path built as  ROOT / "data" / "reference"  was invisible to the
    literal scan (audit tests-6: six modules). Such a build is allowed ONLY in
    a module conftest.LIVE_READ_ALLOWED_MODULES names -- the list is the debt
    record, and every hit outside it is a new offender."""
    hits = _static_hits(SEGMENT_JOIN)
    allowed_files = set(cft.LIVE_READ_ALLOWED_MODULES) | {
        k.split("::")[0] for k in cft.LIVE_READ_ALLOWED_TESTS}
    new = [(n, i, s) for n, i, s in hits if n not in allowed_files]
    assert not new, ("tests build the live data/reference path (segment join) outside "
                     "the conftest allowlist:\n" + "\n".join(f"{n}:{i}: {s}" for n, i, s in new))
    # honest debt: the allowlisted builders must still exist, else the list is stale
    listed = {n for n, _i, _s in hits}
    stale = [m for m, why in cft.LIVE_READ_ALLOWED_MODULES.items()
             if "tests-6" in why and m not in listed]
    assert not stale, ("allowlisted modules no longer build the live path -- remove them "
                       f"from conftest.LIVE_READ_ALLOWED_MODULES: {stale}")


def test_runtime_guard_records_a_live_read(_guard_live_data_reads):
    """Self-test of the conftest hook: opening a live path (existing or not)
    through the builtin is recorded. This module is allowlisted, so the
    recorded hit does not fail the test at teardown -- the point is that the
    hook SAW it. A missing file still counts (the read was attempted)."""
    hits = _guard_live_data_reads
    live = ROOT / "data" / "reference" / "__guard_probe_does_not_exist__.csv"
    with pytest.raises(FileNotFoundError):
        open(live, "r", encoding="utf-8").close()
    assert any("__guard_probe_does_not_exist__" in h for h in hits), hits
    # pandas goes through the same builtin
    import pandas as pd
    with pytest.raises(FileNotFoundError):
        pd.read_csv(live)
    assert sum("__guard_probe_does_not_exist__" in h for h in hits) >= 2
    # a pinned fixture is not live data
    pd.read_csv(ROOT / "data" / "test_fixtures" / "solver_tiny" / "demand_plan.csv")
    assert all("test_fixtures" not in h for h in hits)


def test_runtime_guard_module_allowlist_is_the_only_exemption_besides_the_marker():
    """The hook exempts exactly two things: the live_data marker and the
    module allowlist. Pin the mechanism so a future 'temporary' broadening
    shows up in a diff of this test."""
    import inspect
    src = inspect.getsource(cft._guard_live_data_reads)
    assert 'get_closest_marker("live_data")' in src
    assert "LIVE_READ_ALLOWED_MODULES" in src
    assert "pytest.fail(" in src
