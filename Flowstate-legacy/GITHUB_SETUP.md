# Connect to GitHub (Plant_Scheduler)

Your project is ready to push to [https://github.com/spartacus242/Plant_Scheduler](https://github.com/spartacus242/Plant_Scheduler).

## 1. Install Git (if needed)

If `git` is not in your PATH:

- **Windows:** Download from [https://git-scm.com/download/win](https://git-scm.com/download/win) and install. Restart your terminal after install.
- Or use **GitHub Desktop**: [https://desktop.github.com](https://desktop.github.com) — you can connect the repo via the GUI.

## 2. Open terminal in project folder

```powershell
cd C:\Users\jolen\Scheduler\Flowstate
```

## 3. Initialize and connect to GitHub

```powershell
# Initialize git (if not already)
git init

# Add remote
git remote add origin https://github.com/spartacus242/Plant_Scheduler.git

# Stage all files (.gitignore excludes .venv, __pycache__, etc.)
git add .

# Check what will be committed
git status

# First commit
git commit -m "Initial commit: Flowstate 2-week scheduler with two-phase, Week-1 InitialStates, CIP-in-gaps, Gantt viewer"

# Push to main (repo is empty; this creates main)
git branch -M main
git push -u origin main
```

## 4. If you need to sign in

- **HTTPS:** Git will prompt for GitHub username and a Personal Access Token (not password).
  - Create a token: GitHub → Settings → Developer settings → Personal access tokens
- **SSH:** Use `git@github.com:spartacus242/Plant_Scheduler.git` as remote and ensure SSH keys are set up.

## 5. Optional: exclude output files from repo

If you prefer not to track generated outputs, uncomment the relevant lines in `.gitignore`:

```
# data/schedule_phase2.csv
# data/produced_vs_bounds.csv
# ...
```

Then run `git add .` and `git commit -m "..."` again.
