# Flowstate

**Operational truth → Digital twin → Optimizer**

The hard part is not generating a Gantt chart. It is building a model that can say:

> "This schedule costs us 47.2 hours of manufacturing opportunity."

Once that exists, optimization is a mathematically solvable problem. Planners will not accept "the computer says do this," but they will accept:

> "Current schedule scores 62. Proposed balanced schedule scores 84 - show me why."

## Phases

| Phase | Question | Status in this repo |
| --- | --- | --- |
| **0 — Schedule Scorecard** | How good is this week's schedule? | Primary home page |
| **1 — Digital Twin** | If I move X, what happens to the score? | Plant Calendar (DnD) |
| **2 — Optimizer** | Which scenarios beat the current schedule, and why? | Generate Scenarios |

CIP remains first-class. The solver is a **scenario generator**, not the home screen.

## Local setup (Windows)

```powershell
git clone https://github.com/spartacus242/Plant_Scheduler.git
cd Plant_Scheduler
git checkout main; git pull

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# One-time: silent Desktop shortcut (no console window) that starts/opens http://localhost:8501
powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop_shortcut.ps1
```

Double-click **Flowstate** on your Desktop. The shortcut runs `scripts\open_flowstate.vbs`, which silently launches `scripts\open_flowstate.bat` — no console window. The launcher starts Streamlit from this clone if needed, then opens the browser.

## Install on a planner's PC (Windows, from `main`)

Prerequisites: Git for Windows and Python 3.12 (python.org, tick "Add python.exe to PATH"). No Node - the Gantt bundle is committed. No GitHub account - the code repo is public.

```powershell
git clone --branch main https://github.com/spartacus242/Plant_Scheduler.git "$env:USERPROFILE\Flowstate\Plant_Scheduler"
cd "$env:USERPROFILE\Flowstate\Plant_Scheduler"
powershell -ExecutionPolicy Bypass -File .\scripts\install_flowstate.ps1 -FeedDir "C:\Users\<user>\Flowstate\fs_data"
```

The script creates `.venv`, installs `requirements.txt`, puts the **Flowstate** shortcut on the Desktop and sets up the live data for this PC, one of:

- **`-FeedDir <folder>`** - a PC on the work network. The plant files are copied from the drop folders into `data\reference` every 5 min (Task Scheduler entry "Flowstate Live Data Pull"). Give it the drop **root** `fs_data` (one synced folder): since 2026-09-15 the drop holds two subfolders, `fs_vif` (the ERP's own exports) and `fs_manual` (files people maintain) - see `docs/vif_exports.md` for every file - and since 2026-09-17 the sync reads a configured folder that holds them as those subfolders (the root's own files are ignored; a subfolder created later is picked up). Spelling the subfolders out, separated with `;` (`…\fs_data\fs_vif;…\fs_data\fs_manual`), still works and gives the same result, and a flat folder holding the files themselves is read as it is; a reachable folder with none of the plant files makes the pass report `no plant files found` instead of a quiet "nothing new". The folders are only ever **read** - no lock, marker, rename or delete - so the ERP and other people keep using them untouched; read permission is all the account needs. The one exception is `fs_manual\demand_plan_summary.csv`, which the sync rebuilds from the weekly AZAP workbook (`New Export AZAP MMDDYY.xlsx`) unless the summary was built from that workbook or is a hand edit newer than every AZAP workbook in the folder (a workbook that yields 0 rows, or under half the current rows, is refused and the previous summary kept; rebuild by hand with `.venv\Scripts\python.exe scripts\azap_demand_summary.py`, which reads the folders from this install's `source_dirs`, or with `--folder <fs_manual>` anywhere else). It can be a file share (use the UNC path `\\server\share\...`, not a mapped drive letter) or a SharePoint library synced by OneDrive: sync the library on the planner's PC, right-click it and tick *Always keep on this device*, then point `-FeedDir` at the local folder, e.g. `C:\Users\<planner>\GROUPE BEL\NPA_ContinuousImprovement - Documents\VIF Extracts` (the ERP's exports land there every evening).
- **`-LiveData`** - a PC off the work network (the dev laptop). The same files come through the private `flowstate-live-data` GitHub repo every 30 min; the first pull opens a GitHub sign-in.

Per-machine settings land in the git-ignored `scripts\fs-live-data.local.json`. Home shows the last pass under **Live data sync**, and the calendar's **Rebuild from plant state** syncs first. Re-run the same command later to update to the latest `main`.

When editing frontend Gantt source, rebuild:

```powershell
cd code\components\gantt\frontend
npm install
npm run build
cd ..\..\..\..
```

A prebuilt `dist/` is already committed, so rebuild is only required after TypeScript/React changes.

## Run (Linux / macOS / Cloud)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional: ./scripts/open_flowstate.sh
python3 -m streamlit run code/app.py --server.headless true
```

Open http://localhost:8501

## Push when done

```powershell
git checkout -b your-feature-branch
git add -A
git commit -m "Describe the change"
git push -u origin HEAD
```

Then open a PR into `develop` on GitHub.

## Look & feel

The app is a light workbench (see [`docs/ui-redesign-2026-09.md`](docs/ui-redesign-2026-09.md)). It opens on the **Plant Calendar**; the design tokens live in `.streamlit/config.toml`, `code/helpers/theme.py` and the Gantt's `src/utils/theme.ts` — change all three together.

## Data

- `data/calendar_blocks.csv` — unified plant calendar (production, CIP, maintenance, trial, contractor, line_down)
- `data/reference/` — changeovers, demand, CIP intervals, capabilities (scoring inputs)
- `data/scorecards/` — weekly score history
- `data/versions/` — named options with scorecard + pros/cons

## Legacy

The previous solver-first app lives in [`Flowstate-legacy/`](Flowstate-legacy/DEPRECATED.md) for reference and for lifting CP-SAT / Gantt code.
