# Flowstate Live-Data Bridge — Setup Guide

Connect the plant's live data to the Flowstate laptop using a **private GitHub
repo as a file drop**. No work-linked OneDrive, no desktop client, no AI on the
work network.

```
[ WORK computer ]            [ GitHub (private) ]              [ PERSONAL laptop ]
plant system ─┐  fs-live-push.py                        fs-live-pull.py ─┐
  manprg ─────┼─►  watch + git push  ───►  flowstate-live-data  ──►  clone + copy ──►  data/reference/
  cip_info ───┘                                        (auto-sync both ways)
  demand ────
```

**How it works**
- **Work side** (`fs-live-push.py`): watches the plant files, copies any that
  changed into a local clone of the repo, and `git push`es. It runs **only
  git** — never AI — so it respects the "AI calls blocked on work network" rule.
- **Cloud:** the private repo holds the current plant data plus full history.
- **Personal side** (`fs-live-pull.py`): pulls the latest, copies changed files
  into `data/reference/` for Hermes to ingest.

> **One-way data flow.** Files travel plant → GitHub → laptop. Nothing is ever
> pushed *back* to the plant. The plant network is only ever written to by the
> watcher copy step, never read back.

---

## PART 1 — Create the GitHub repo (one time, any computer, ~3 min)

1. Go to https://github.com/new
2. **Repository name:** `flowstate-live-data`
3. **Visibility:** **Private**
4. Leave "Initialize with README" **unchecked** (the scripts create the first
   commit).
5. Click **Create repository**.

That's it. Make a note of the account name (e.g. `spartacus242`) — you'll need
it for the clone URL.

---

## PART 2 — Work computer: install git + set up auth (~10 min)

### 2a. Install Git for Windows
If not already installed: download from https://git-scm.com, install with
defaults. Verify in a command prompt:

```
git --version
```

### 2b. Set your git identity (once)
Open **Command Prompt** (not PowerShell):

```
git config --global user.name "your name"
git config --global user.email "you@yourdomain.com"
```

### 2c. Create a Personal Access Token (PAT)
This is how the push script authenticates without typing a password each time.

1. GitHub → top-right avatar → **Settings** → **Developer settings** (bottom of
   left menu) → **Personal access tokens** → **Tokens (classic)** → **Generate
   new token (classic)**.
2. Give it a name like `flowstate-push`.
3. **Expiration:** 90 days (or longer if your IT allows).
4. **Scopes:** check **`repo`** (this grants full control of your repos — the
   minimum needed to push). Nothing else.
5. Click **Generate token**.
6. **Copy the token now** (starts with `ghp_...`). GitHub shows it only once.
   Store it somewhere safe (e.g. a password manager).

> ⚠️ A token is like a password. Do not share it, do not commit it to any repo.

---

## PART 3 — Work computer: configure + run the push script (~5 min)

1. Copy the `scripts/` folder from Flowstate onto the work computer (e.g. via
   a USB drive or the work OneDrive), OR clone this repo there if you prefer.
2. Open `scripts/fs-live-data.conf.json` and confirm:
   - `repo_url` matches your account:
     `https://github.com/<YOUR_ACCOUNT>/flowstate-live-data.git`
3. Open `scripts/fs-live-push.py` in a text editor and set these at the top:

   ```python
   SRC_DIR = Path(r"C:\PlantData")   # folder where the plant drops raw files
   ```

   Change `C:\PlantData` to the actual location of the plant files on the work
   machine.

4. **Test it once** (from the `scripts/` folder):

   ```
   python fs-live-push.py --once
   ```

   - First run clones the repo and pushes whatever files exist in `SRC_DIR`
     that are in the config's `files` list.
   - It will prompt for GitHub username + the PAT when cloning. Enter your
     account name and paste the `ghp_...` token for the password.
   - If it prints `pushed: <names>` — it worked.

> **Tip:** after the first successful clone, git caches your credentials, so
> later runs won't re-prompt. If it re-prompts, add the token to the credential
> manager with:
> ```
> git config --global credential.helper manager
> ```

---

## PART 4 — Work computer: schedule the watcher to run automatically

The push script must run **reliably, even after reboot/logout**, or the data
goes stale. The best way on Windows is a **Scheduled Task**.

### 4a. Create the scheduled task (one time)
1. Press **Win+R**, type `taskschd.msc`, Enter.
2. Right-click **Task Scheduler Library** → **Create Task**.
3. **General tab:**
   - Name: `Flowstate Live Data Push`
   - Check **"Run whether user is logged on or not"** (so it runs at logon/lock).
   - Check **"Run with highest privileges"** if IT requires it (not usually).
4. **Triggers tab** → **New**:
   - **Begin the task:** On a schedule
   - **Daily**, start `12:00 AM`, **Repeat every:** `5 minutes`, for a duration
     of `Indefinitely`.
   - Check **Enabled**.
5. **Actions tab** → **New**:
   - **Action:** Start a program
   - **Program/script:** `C:\...\scripts\fs-live-push.py`
   - **Add arguments:** `--once`
   - **Start in:** `C:\...\scripts`
6. **Conditions tab:** UNCHECK **"Start the task only if the computer is on AC
   power"** (so it runs on battery too).
7. **Settings tab:** check **"If the task fails, restart every 1 minute"** and
   **"Allow task to be run on demand."**
8. Click **OK**. Windows may ask for your account password once.

### 4b. Verify it runs
In **Task Scheduler**, select the task and click **Run** (right side). After a
few seconds it should show **"Last Run Result: (0x0)"** = success.

Now every 5 minutes the work computer checks the plant files and pushes any
changes to GitHub automatically. **You no longer copy-paste anything manually.**

---

## PART 5 — Personal laptop (Hermes): pull the data

The pull side is **already written and committed** to the Flowstate repo
(`scripts/fs-live-pull.py`). Once the GitHub repo exists and the work side has
pushed at least once, tell Hermes (Jace's AI) *"the repo is ready"* and it will:

1. Clone `flowstate-live-data` to a local folder.
2. Pull the latest and copy changed files into `data/reference/`.
3. Set up a recurring cron job to pull automatically.

The config file already points at the standard location:
- `clone_dir`: `C:\Users\jbdil\FlowstateLive`
- `data_reference_dir`: `C:\Users\jbdil\Flowstate\Plant_Scheduler\data\reference`

**To pull manually, from the Flowstate `scripts/` folder:**

```
python fs-live-pull.py --once
```

---

## PART 6 — Planner's PC on the work network: folder mode (no GitHub)

Added 2026-09-14. A PC that can see the share the ERP exports to does not need
the GitHub hop at all: the same `fs-live-pull.py` copies straight from that
folder into `data\reference`.

```
[ ERP exports ]  ──► fs_data\fs_vif     ─┐
[ planners ]     ──► fs_data\fs_manual  ─┴► fs-live-pull.py (every 5 min) ──► data\reference\
                                          │
                                          └──► fs-live-push.py on the work PC ──► GitHub ──► dev laptop (PART 5)
```

**The `fs_data` layout (drop of 2026-09-15).** The plant data comes as one
drop with two folders, and the sync watches both:

| Folder | Contents | Written by |
|---|---|---|
| `fs_data\fs_vif` | the ERP's own exports: schedule (`manprg*.txt`), BOM (`ediact.csv`), item masters, every `jestk*` stock-lot report, packaging receipt slips (`PKG-REC.csv`), PO lines (`order_npa.csv`) | the ERP, every evening |
| `fs_data\fs_manual` | files people maintain: `cip_info.csv`, the weekly AZAP demand workbook `New Export AZAP MMDDYY.xlsx`, the dock schedule workbook, `demand_plan_summary.csv` | planners — and the sync, for one file (below) |

Every file, its format and what the app does with it is in
`docs/vif_exports.md`.

**Set it up** (the installer does all of this):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_flowstate.ps1 -FeedDir "C:\Users\<planner>\Flowstate\fs_data\fs_vif;C:\Users\<planner>\Flowstate\fs_data\fs_manual"
```

which writes the git-ignored `scripts\fs-live-data.local.json`:

```json
{
  "data_reference_dir": "C:\\Users\\<planner>\\Flowstate\\Plant_Scheduler\\data\\reference",
  "source_dirs": ["C:\\Users\\<planner>\\Flowstate\\fs_data\\fs_vif", "C:\\Users\\<planner>\\Flowstate\\fs_data\\fs_manual"]
}
```

runs one first sync and registers the Task Scheduler entry **Flowstate Live
Data Pull** (every 5 min). Folders are separated with `;` and listed in
priority order — a file present in more than one is taken from wherever it
is newest. A single folder (`-FeedDir "\\server\share\erp_out"`) still
works. Use UNC paths for a share: a mapped drive letter is per-logon and the
scheduled task may not see it.

**What the sync does to the folders — and does not:**

- Reads only the files on the conf list (`files` in `fs-live-data.conf.json`,
  dated names through the `<glob> -> <name>` entries). Everything else in
  the folders is ignored.
- Never writes into `fs_vif`: no lock, no marker file, no rename, no archive
  move, no delete. Read permission is all the account needs. Other people
  and the ERP keep using the folder exactly as before.
- `fs_manual` is the **one** source folder the sync writes **one** file
  into: `demand_plan_summary.csv`, rebuilt from the newest
  `New Export AZAP MMDDYY.xlsx|xlsm` (recipe in `docs/vif_exports.md`) and
  skipped only while the summary is up to date: it carries that workbook's
  modified time (the rebuild stamps it so — it was built from it), or it is
  newer than **every** AZAP workbook in the folder (a hand edit). A hand
  edit therefore survives until any AZAP workbook in the folder is saved
  after it, and a summary stamped by last week's workbook never hides this
  week's export, whatever its modified time (2026-09-16). The same
  rebuild by hand: `.venv\Scripts\python.exe scripts\azap_demand_summary.py`
  (`--help` for the options) — with no `--folder` it uses the first of the
  `source_dirs` above that holds an AZAP workbook, else it stops with
  "no AZAP workbook in the configured source folders; pass --folder".
- The rebuild never replaces a good summary with a bad one (2026-09-16): a
  workbook that yields **0 rows** (a wrong Factory value, a year typo in the
  name) or **fewer than half the rows** of the current summary is refused,
  the previous `demand_plan_summary.csv` stays, and every pass reports the
  refusal under problems until a good workbook lands (a smaller plan that is
  real: run the CLI once with `--force`). "Newest workbook" means the latest
  export date in the name, then the latest modified time, so an AutoSave on
  last week's file does not beat this week's export; `New Export AZAP 091126
  (1).xlsx` and OneDrive conflict copies keep their date. A name dated after
  the day the file was saved cannot be the export date (a typo such as
  `091827`): that file ranks by its modified date. A workbook the ranking
  passes over although it was saved after the one in use is named on every
  pass: under **problems** when the date in its name is more than four weeks
  before its modified date (a year typo — `091825` for `091826`, `010126` for
  `010127` — or an old export saved again: rename it or move it out), as a
  `(NOTE: ...)` entry otherwise (an AutoSave on last week's export). A name
  with no `MMDDYY` builds from the file's modified date and the pass notes a
  **WARNING** next to the "built from" entry — and again on every later
  pass, as "(unchanged, from ...; WARNING: ...)", while that summary is in
  use. A summary open in Excel is retried briefly and never leaves a
  `demand_plan_summary.csv.tmp` behind.
- Holds a file open only for the milliseconds it takes to read it.
- A file modified less than `settle_seconds` ago (default 60) is left for
  the next pass — an export may still be being written — and so is a file
  whose size or mtime moves while it is read. One busy or unreadable file
  costs only itself; the rest of the pass continues.
- Copies land under a temp name and are swapped in, so the app never reads
  a half-copied file. The source mtime is kept (the health page ages files
  by it); the manprg as-of stamp is the ERP's write time of the newest
  manprg file.
- `demand_plan.csv` is re-derived whenever the summary changed, exactly as
  in GitHub mode.

**Heartbeat.** Every pass — including a failed one — writes
`data\reference\live_sync.json` (mode, sources, finished, updated, skipped,
problems). Home shows it as **Live data sync**: OK, STALE when the last pass
is older than 1 h (`[health] cadence_h.live_sync` in flowstate.toml) or
reported problems, never blocking on its own. The calendar's **Rebuild from
plant state** runs a sync first (bounded to 90 s) so a rebuild never trails
the schedule. The script exits 1 on problems, so the Task Scheduler "Last Run
Result" shows them too.

**A SharePoint library instead of a file share (IT, 2026-09-15).** The
ERP's exports land in the library **VIF Extracts** of the
NPA_ContinuousImprovement SharePoint site (Shared Documents), same file
name every day, no archive copy. OneDrive syncs it to a local folder —
`C:\Users\<user>\GROUPE BEL\NPA_ContinuousImprovement - Documents\VIF Extracts` —
and that local folder is what `-FeedDir` points at. Two things to set on
the planner's PC: sync the library (SharePoint → *Sync*), and right-click
the synced folder → *Always keep on this device*, so every file is a real
local file and not a cloud placeholder that has to download on first read.
OneDrive keeps the SharePoint modified time on the local copy, so the
as-of the sync records is the time the ERP wrote the file (about 19:10,
landing by 19:25).

**Feeding the same folders to GitHub.** On the work PC `"source_dirs_work"`
in `fs-live-data.conf.json` lists the same two folders (`fs_vif`,
`fs_manual`; the single `"source_dir_work"` is the fallback when the list
is empty) and `fs-live-push.py` watches them all — the same folders then
feed the planner's PC directly and the dev laptop through GitHub, from one
file list. (Copy the updated conf next to the hand-copied push script on
the work PC.)

**Where the AZAP summary is rebuilt (2026-09-16).** `demand_plan_summary.csv`
is cut from the weekly `New Export AZAP MMDDYY.xlsx` in three places, all
running the same helper (`code/helpers/azap_demand.py`) with the same
freshness rule and write guards:

1. **Folder-mode sync** (`fs-live-pull.py` on the planner's PC) — on every
   pass, before the copy step.
2. **The push script** (`fs-live-push.py` on the work PC) — on every pass,
   before it resolves the file list, **only when it runs from a repo
   checkout** (it imports the helper from `code/` next to `scripts/`, and
   needs pandas + openpyxl). A hand-copied single script cannot: it prints
   `AZAP rebuild skipped: <reason> - run scripts/azap_demand_summary.py`
   and pushes whatever summary sits in `fs_manual`, exactly as before.
3. **By hand** — `python scripts\azap_demand_summary.py --folder "<fs_manual>"`
   on any machine with a checkout (`--folder` can be left out where the
   local conf lists `source_dirs`, i.e. a folder-mode install).

GitHub mode on the dev laptop (`fs-live-pull.py` from the clone) never
rebuilds: it copies the summary the work PC pushed.

---

## Keeping the data fresh

| Check | Cadence |
|---|---|
| Work push watcher | every 5 min (scheduled task) |
| Personal pull (GitHub mode, dev laptop) | every 30 min |
| Folder sync (planner's PC, PART 6) | every 5 min (installer default) |
| Data staleness warning | `data_health` already flags any file older than expected; **Live data sync** flags a silent or failing sync |

If `data_health` reports a file as STALE, the push watcher on the work side
isn't running (or the plant file stopped updating) — check the scheduled task
result first.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `clone failed` on first push | Repo not created yet, or `repo_url` wrong. Check PART 1. |
| Push prompts for username/password | Add PAT to credential manager (PART 3 tip). |
| Scheduled task result `(0x1)` | The `python` command isn't found. Use full path to `python.exe` in the Action, or `py fs-live-push.py --once`. |
| Task result `(0x2)` | Wrong path to the script. Check **Start in** matches the `scripts/` folder. |
| Nothing ever pushes | `source_dirs_work` (or `source_dir_work` / `SRC_DIR`) doesn't match where the plant actually drops files. Confirm a file from the `files` list exists there; an unreachable folder is printed as "Source folder not reachable, skipped". |
| Home says **Live data sync** reported `source folder not reachable` | The share is down, the UNC path has a typo, or the account lacks read permission. Open the path in Explorer as that user; then run `.venv\Scripts\python.exe scripts\fs-live-pull.py --once`. |
| A file keeps being "left for next pass (settling)" | Its mtime is always within `settle_seconds` of now — the exporter rewrites it continuously, or the file server's clock is off. Lower `settle_seconds` in `fs-live-data.local.json` (0 disables the wait). |
| Task Scheduler "Last Run Result" is `0x1` | A pass reported problems; read `data\reference\live_sync.json` or the Home row. |
| Token expired | Regenerate a PAT (PART 2c), update the credential manager. |

---

## Files

| File | Role |
|---|---|
| `scripts/fs-live-data.conf.json` | Shared config (repo URL, file list, paths; `source_dirs_work` = the folders the work PC watches) |
| `scripts/fs-live-push.py` | Work-side watcher → push (run on work computer); watches every folder in `source_dirs_work` |
| `scripts/fs-live-pull.py` | Sync → copy into `data/reference` (GitHub clone or shared folders; laptop AND planner's PC) |
| `scripts/fs-live-data.local.json` | Per-machine override (git-ignored): `data_reference_dir`, `source_dirs` or `clone_dir_personal`; written by the installer |
| `scripts/install_flowstate.ps1` | One-command install / update; `-FeedDir` (folder mode, `;`-separated folders) or `-LiveData` (GitHub mode) |
| `scripts/azap_demand_summary.py` | AZAP workbook (`fs_manual\New Export AZAP MMDDYY.xlsx`) → `demand_plan_summary.csv` by hand (`--folder`, else the conf's `source_dirs`); the folder-mode sync and a repo-checkout push script run the same rebuild |
| `docs/vif_exports.md` | Reference for every file in the `fs_data` drop: format, row counts, what the app does with it, the ignored files and the AZAP recipe |
| `data/reference/live_sync.json` | Heartbeat of the last pass (git-ignored); read by Home's **Live data sync** row and the calendar's rebuild |

*Repo-relative paths: `scripts/` lives at the root of the Flowstate repository
(`C:\Users\jbdil\Flowstate\Plant_Scheduler\scripts\` on the laptop).*
