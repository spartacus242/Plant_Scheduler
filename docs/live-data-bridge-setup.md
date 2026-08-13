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

## Keeping the data fresh

| Check | Cadence |
|---|---|
| Work push watcher | every 5 min (scheduled task) |
| Personal pull (Hermes cron) | every 30 min |
| Data staleness warning | `data_health` already flags any file older than expected |

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
| Nothing ever pushes | `SRC_DIR` doesn't match where the plant actually drops files. Confirm a file from the `files` list exists there. |
| Token expired | Regenerate a PAT (PART 2c), update the credential manager. |

---

## Files

| File | Role |
|---|---|
| `scripts/fs-live-data.conf.json` | Shared config (repo URL, file list, paths) |
| `scripts/fs-live-push.py` | Work-side watcher → push (run on work computer) |
| `scripts/fs-live-pull.py` | Personal-side pull → copy (run on laptop) |

*Repo-relative paths: `scripts/` lives at the root of the Flowstate repository
(`C:\Users\jbdil\Flowstate\Plant_Scheduler\scripts\` on the laptop).*
