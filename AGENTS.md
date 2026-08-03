# Flowstate

Streamlit-based manufacturing schedule optimization / decision-support tool. See `README.md` for the product overview and the canonical run commands.

## Cursor Cloud specific instructions

### Services
Flowstate is effectively a **single runtime service**: the Streamlit app (`code/app.py`, default port `8501`). There is no database or external API — all state lives in flat CSV/JSON files under `data/`.

- **Phase 0 (Scorecard)** and **Phase 1 (Plant Calendar / Digital Twin)** run entirely inside that one Streamlit process.
- **Phase 2 (Generate Scenarios)** spawns the Google OR-Tools CP-SAT solver as an on-demand subprocess (via `Flowstate-legacy/code/phase2_scheduler.py`, using the same Python interpreter). It is not a separate long-running service, but the `Flowstate-legacy/` tree and its data must remain present.

### Running
- Use `python3`, not `python` — this environment has no `python` alias. The README's `python -m streamlit ...` will fail; run `python3 -m streamlit run code/app.py --server.headless true` instead.
- Streamlit is installed to `~/.local/bin` (not on `PATH`). Invoke it as a module (`python3 -m streamlit ...`) rather than the bare `streamlit` command.
- The React/Vite Gantt component in `code/components/gantt/frontend/` ships a prebuilt `dist/` that is committed to the repo, so the app runs without a Node build. Only rebuild (`npm run build` in that dir) if you change the frontend source. If `dist/` is ever missing, the component falls back to a Vite dev server at `http://localhost:5173`.
- Quick open: `./scripts/open_flowstate.sh` (Linux/macOS/Cloud). On Windows, use `scripts/open_flowstate.bat` or `scripts/install_desktop_shortcut.ps1` (see README).

### Testing / lint
There is **no** automated test suite and no configured linter in this repo (no pytest/unittest, no ESLint/ruff config). Validate changes by running the app and exercising the relevant page, plus `python3 -m py_compile` for a quick syntax check and `npm run build` (in the frontend dir) to type-check/build the Gantt component.
