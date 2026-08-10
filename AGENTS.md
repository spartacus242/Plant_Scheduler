# Flowstate

Streamlit-based manufacturing schedule optimization / decision-support tool. See `README.md` for the product overview and the canonical run commands.

## Cursor Cloud specific instructions

### Services
Flowstate is effectively a **single runtime service**: the Streamlit app (`code/app.py`, default port `8501`). There is no database or external API — all state lives in flat CSV/JSON files under `data/`.

- **Phase 0 (Scorecard)**, **Phase 1 (Plant Calendar / Digital Twin)**, and **Stock Check** run entirely inside that one Streamlit process.
- **Phase 2 (Generate Scenarios)** spawns the Google OR-Tools CP-SAT solver as an on-demand subprocess via `code/solver/phase2_scheduler.py` (using the same Python interpreter). The solver reads its inputs from a scratch work dir under `data/_scenario_work/` that is rebuilt from `data/reference/` on every run — it must NOT depend on `Flowstate-legacy/`.

### Repository layout (current)
- `code/app.py` — Streamlit entry (8 nav groups, 10 pages).
- `code/pages/` — home (Command Center), data, scorecard, stock_check, calendar, compare, generate, lines, settings.
- `code/helpers/` — config, horizon, calendar_io, scorecard_engine, scenario_runner, current_state, data_health (freshness/health engine), process_flow (pipeline model), importers (demand summary, PDF, manual, manprg, cip), stock-check API.
- `code/solver/` — the live CP-SAT solver (moved out of `Flowstate-legacy/` 2026-08-10). Flat modules: `phase2_scheduler.py` (CLI), `model_builder.py`, `data_loader.py`, `diagnostics.py`, `validate_schedule.py`, `solver_progress.py`.
- `code/components/gantt/` — React/TS Gantt (prebuilt `dist/` committed; no Node build needed to run).
- `code/stockcheck/` — VIF BOM explosion + component coverage engine.
- `data/reference/` — the 10 input CSVs + live feeds (manprg, cip_info) + demand samples.
- `data/seed/` — bundled developer seed fixtures (schedule_phase2.csv + cip_windows.csv) used by the scorecard page's first-run import.
- `Flowstate-legacy/` — **deprecated archive only**; nothing in `code/` may import from it. Scheduled for deletion once the moved solver is confirmed on a real run.
- `tests/` — pytest suite (see Testing below).

### Running
- Use `python3`, not `python` — this environment has no `python` alias. The README's `python -m streamlit ...` will fail; run `python3 -m streamlit run code/app.py --server.headless true` instead.
- Streamlit is installed to `~/.local/bin` (not on `PATH`). Invoke it as a module (`python3 -m streamlit ...`) rather than the bare `streamlit` command.
- The React/Vite Gantt component in `code/components/gantt/frontend/` ships a prebuilt `dist/` that is committed to the repo, so the app runs without a Node build. Only rebuild (`npm run build` in that dir) if you change the frontend source. If `dist/` is ever missing, the component falls back to a Vite dev server at `http://localhost:5173`.
- Quick open: `./scripts/open_flowstate.sh` (Linux/macOS/Cloud). On Windows, use `scripts/open_flowstate.bat` or `scripts/install_desktop_shortcut.ps1` (see README).

### Testing / lint
- There **is** a pytest suite in `tests/` (8 files, 84 tests as of 2026-08-10): horizon, current-state, current-state overlay, AZAP import, live imports, PDF import, stock-check engine, stock-check receiving, and the data-health engine. Run it from the repo root:
  ```bash
  python3 -m pytest -q
  ```
  (On the Windows dev box: `PYTHONPATH=code .venv/Scripts/python.exe -m pytest -q` — scrub `PYTHONPATH` first if pandas crashes on import.)
- No configured linter (no ruff/ESLint config). Validate changes by running the suite plus `python3 -m py_compile` for a quick syntax check; UI changes must be browser-verified (see the `flowstate-app-qa` Hermes skill).
- Solver changes: the browser cannot see solver internals — verify with real CLI runs (`scripts/check_solver_current_state.py`, `scripts/diag_available_from.py`) and check `feasibility_report.json` in the work dir.
