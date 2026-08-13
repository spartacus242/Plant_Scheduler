# live_2026-08-13 — pinned test snapshot

Contents: `manprg.txt`, `manprg2.txt`, `cip_info.csv`, `capabilities_rates.csv` —
byte-identical to the last committed baseline (`git 6103535^:data/reference/…`).
Read by `tests/test_live_imports.py` and `tests/test_capability_check.py`.

**Why:** `data/reference/` is refreshed daily by the live-data bridge and must
never be read by tests (`tests/test_no_live_data_reads.py` enforces this).

**Re-pin** after a deliberate baseline change:

```bash
git show <commit>:data/reference/<file> > data/test_fixtures/live_2026-08-13/<file>
```

then update the expected values in the tests to match the new snapshot.
