# install_flowstate.ps1 - one-shot, re-runnable install / update of Flowstate on
# a Windows PC from a clone of this repo (release branch: main).
#
#   git clone --branch main https://github.com/spartacus242/Plant_Scheduler.git "$env:USERPROFILE\Flowstate\Plant_Scheduler"
#   cd "$env:USERPROFILE\Flowstate\Plant_Scheduler"
#   # planner's PC on the work network: live data straight from the ERP's drop folder
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_flowstate.ps1 -FeedDir "\\server\share\erp_out"
#   # a PC off the work network (dev laptop): live data through the private GitHub repo
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_flowstate.ps1 -LiveData
#
# What it does (every step skips what is already in place, so re-running the
# same command later is the update path):
#   1. git fetch + fast-forward the chosen branch (default: main)
#   2. .venv with Python 3.12 (py launcher, else python on PATH) + requirements.txt
#   3. Desktop shortcut "Flowstate" (scripts\install_desktop_shortcut.ps1)
#   4. Live data for THIS machine, one of:
#      -FeedDir   folder mode: scripts\fs-live-pull.py copies the plant files
#                 from the given folder(s) (several: separate with ';') into
#                 data\reference. Read-only towards the folder - no lock,
#                 marker, rename or delete - so the ERP and other people keep
#                 using it untouched. Needs read permission there; no GitHub
#                 login at all (the code repo is public). A UNC path, or a
#                 local folder such as the OneDrive-synced SharePoint library
#                 "VIF Extracts" the ERP exports into (sync it on this PC and
#                 tick "Always keep on this device" first).
#      -LiveData  GitHub mode: pull the private flowstate-live-data repo into
#                 -LiveClone (a sign-in window may open) and copy from there.
#      Both write the git-ignored scripts\fs-live-data.local.json, run one
#      first sync and register the Task Scheduler entry "Flowstate Live Data
#      Pull" every -PullEveryMinutes (default: 5 in folder mode, 30 in GitHub
#      mode). The app's Home page shows the last sync under "Live data sync".
#
# Prerequisites on the PC: Git for Windows, Python 3.12 (python.org, tick
# "Add python.exe to PATH"). No Node needed - the Gantt bundle is committed.

param(
    [string]$Branch = "main",
    [string]$FeedDir = "",
    [switch]$LiveData,
    [string]$LiveClone = (Join-Path $env:USERPROFILE "FlowstateLive"),
    [int]$PullEveryMinutes = 0,
    [switch]$NoShortcut,
    [switch]$NoPip
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

if ($LiveData -and $FeedDir) {
    throw "Use either -FeedDir (shared folder) or -LiveData (GitHub bridge), not both."
}

# ---------------------------------------------------------------- 0. prerequisites
Step "Prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found - install Git for Windows (https://git-scm.com) and re-run."
}
$PyExe = $null
$PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $null = & py -3.12 -c "import sys"
        if ($LASTEXITCODE -eq 0) { $PyExe = "py"; $PyArgs = @("-3.12") }
    } catch { }
}
if (-not $PyExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    try {
        $ok = & python -c "import sys; print(sys.version_info >= (3, 11))"
        if ("$ok".Trim() -eq "True") { $PyExe = "python" }
    } catch { }
}
if (-not $PyExe) {
    throw "Python 3.11+ not found - install Python 3.12 from python.org (tick 'Add python.exe to PATH') and re-run."
}
Write-Host "  git    : $((git --version) -join '')"
Write-Host "  python : $PyExe $($PyArgs -join ' ')"

# ---------------------------------------------------------------- 1. code
Step "Code: branch '$Branch'"
git fetch origin --prune
$cur = (git rev-parse --abbrev-ref HEAD).Trim()
if ($cur -ne $Branch) {
    $dirty = @(git status --porcelain).Count -gt 0
    if ($dirty) {
        throw "Working tree has local changes on '$cur' - commit or discard them before switching to '$Branch'."
    }
    git checkout $Branch
}
git pull --ff-only origin $Branch
Write-Host "  now at: $((git log --oneline -1) -join '')"

# ---------------------------------------------------------------- 2. venv + packages
Step "Python environment (.venv)"
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    & $PyExe @PyArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { throw "venv creation failed" }
    Write-Host "  created $VenvDir"
} else {
    Write-Host "  using existing $VenvDir"
}
if (-not $NoPip) {
    & $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $VenvPy -m pip install -r (Join-Path $RepoRoot "requirements.txt") --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }
    Write-Host "  packages up to date"
}
Write-Host "  $((& $VenvPy --version) -join '')"

# ---------------------------------------------------------------- 3. desktop shortcut
if (-not $NoShortcut) {
    Step "Desktop shortcut"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\install_desktop_shortcut.ps1")
    if ($LASTEXITCODE -ne 0) { throw "install_desktop_shortcut.ps1 failed" }
}

# ---------------------------------------------------------------- 4. live data for this machine
if ($LiveData -or $FeedDir) {
    Step "Live data for this machine"
    $LocalConf = Join-Path $RepoRoot "scripts\fs-live-data.local.json"
    $override = @{ data_reference_dir = (Join-Path $RepoRoot "data\reference") }
    if ($FeedDir) {
        $Mode = "folder"
        $dirs = @($FeedDir -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if ($dirs.Count -eq 0) { throw "-FeedDir is empty." }
        foreach ($d in $dirs) {
            if (Test-Path -LiteralPath $d -PathType Container) {
                Write-Host "  source folder : $d"
            } else {
                Write-Warning "source folder not reachable right now: $d  (the sync keeps retrying; check the path and read permission)"
            }
            if ($d -match '^([A-Za-z]):') {
                # a local folder (a OneDrive-synced SharePoint library, say) is fine;
                # only a MAPPED network drive is per-logon and may be invisible to the task
                $drv = Get-PSDrive -Name $Matches[1] -ErrorAction SilentlyContinue
                if ($drv -and $drv.DisplayRoot -and ("$($drv.DisplayRoot)" -match '^\\\\')) {
                    Write-Host "  note: '$d' is on a mapped network drive ($($drv.DisplayRoot)). Drive letters are per-logon; if the scheduled task cannot see it, use the UNC path instead." -ForegroundColor Yellow
                }
            }
        }
        $override.source_dirs = $dirs
        if ($PullEveryMinutes -le 0) { $PullEveryMinutes = 5 }
    } else {
        $Mode = "github"
        $override.clone_dir_personal = $LiveClone
        if ($PullEveryMinutes -le 0) { $PullEveryMinutes = 30 }
    }
    ($override | ConvertTo-Json) | Set-Content -Path $LocalConf -Encoding utf8
    Write-Host "  wrote $LocalConf ($Mode mode)"

    $PullScript = Join-Path $RepoRoot "scripts\fs-live-pull.py"
    if ($Mode -eq "github") {
        Write-Host "  first pull (a GitHub sign-in window may open: the live-data repo is private) ..."
    } else {
        Write-Host "  first sync from the folder ..."
    }
    & $VenvPy $PullScript --once
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "first sync reported problems (see the line above). Fix the source, then run: .venv\Scripts\python.exe scripts\fs-live-pull.py --once"
    }

    $TaskName = "Flowstate Live Data Pull"
    $Action = New-ScheduledTaskAction -Execute $VenvPy `
        -Argument "`"$PullScript`" --once" -WorkingDirectory $RepoRoot
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $PullEveryMinutes) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    if ($Mode -eq "folder") {
        $What = "the shared folder(s) $($dirs -join '; ')"
    } else {
        $What = "the flowstate-live-data GitHub repo"
    }
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description ("Copies the plant's live data files from $What into Flowstate's data\reference " +
                      "every $PullEveryMinutes min (scripts\fs-live-pull.py --once). Read-only towards the source.") -Force | Out-Null
    Write-Host "  registered Task Scheduler entry '$TaskName' (every $PullEveryMinutes min)"
}

# ---------------------------------------------------------------- done
Step "Done"
Write-Host "  Double-click 'Flowstate' on the Desktop (or run scripts\open_flowstate.bat) -> http://localhost:8501"
Write-Host "  Update later: re-run this same command from this folder."
