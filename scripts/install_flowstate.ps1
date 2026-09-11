# install_flowstate.ps1 - one-shot, re-runnable install / update of Flowstate on
# a Windows PC from a clone of this repo (release branch: main).
#
#   git clone --branch main https://github.com/spartacus242/Plant_Scheduler.git "$env:USERPROFILE\Flowstate\Plant_Scheduler"
#   cd "$env:USERPROFILE\Flowstate\Plant_Scheduler"
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_flowstate.ps1 -LiveData
#
# What it does (every step skips what is already in place, so re-running the
# same command later is the update path):
#   1. git fetch + fast-forward the chosen branch (default: main)
#   2. .venv with Python 3.12 (py launcher, else python on PATH) + requirements.txt
#   3. Desktop shortcut "Flowstate" (scripts\install_desktop_shortcut.ps1)
#   4. -LiveData: per-machine paths for the live-data pull bridge
#      (scripts\fs-live-data.local.json, git-ignored), one first pull (a GitHub
#      sign-in window may open - the flowstate-live-data repo is private), and
#      a Task Scheduler entry "Flowstate Live Data Pull" every -PullEveryMinutes.
#
# Prerequisites on the PC: Git for Windows, Python 3.12 (python.org, tick
# "Add python.exe to PATH"). No Node needed - the Gantt bundle is committed.

param(
    [string]$Branch = "main",
    [switch]$LiveData,
    [string]$LiveClone = (Join-Path $env:USERPROFILE "FlowstateLive"),
    [int]$PullEveryMinutes = 30,
    [switch]$NoShortcut,
    [switch]$NoPip
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

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

# ---------------------------------------------------------------- 4. live-data pull bridge
if ($LiveData) {
    Step "Live-data pull bridge"
    $LocalConf = Join-Path $RepoRoot "scripts\fs-live-data.local.json"
    $override = @{
        clone_dir_personal = $LiveClone
        data_reference_dir = (Join-Path $RepoRoot "data\reference")
    }
    ($override | ConvertTo-Json) | Set-Content -Path $LocalConf -Encoding utf8
    Write-Host "  wrote $LocalConf"
    Write-Host "  first pull (a GitHub sign-in window may open: the live-data repo is private) ..."
    $PullScript = Join-Path $RepoRoot "scripts\fs-live-pull.py"
    & $VenvPy $PullScript --once
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "first pull failed - fix access to flowstate-live-data, then run: .venv\Scripts\python.exe scripts\fs-live-pull.py --once"
    }
    $TaskName = "Flowstate Live Data Pull"
    $Action = New-ScheduledTaskAction -Execute $VenvPy `
        -Argument "`"$PullScript`" --once" -WorkingDirectory $RepoRoot
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $PullEveryMinutes) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description ("Pulls the plant's live data files into Flowstate's data\reference " +
                      "every $PullEveryMinutes min (scripts\fs-live-pull.py --once).") -Force | Out-Null
    Write-Host "  registered Task Scheduler entry '$TaskName' (every $PullEveryMinutes min)"
}

# ---------------------------------------------------------------- done
Step "Done"
Write-Host "  Double-click 'Flowstate' on the Desktop (or run scripts\open_flowstate.bat) -> http://localhost:8501"
Write-Host "  Update later: re-run this same command from this folder."
