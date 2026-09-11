@echo off
rem Flowstate live-data PULL - Windows scheduled task wrapper.
rem Runs scripts\fs-live-pull.py --once with THIS clone's venv and appends the
rem output to scripts\fs-live-pull.log. Paths derive from this file's location,
rem so the same file works in any clone (install_flowstate.ps1 -LiveData
rem registers the venv python directly; this wrapper stays for hand-made tasks).
set "REPO=%~dp0.."
set "PY=%REPO%\.venv\Scripts\python.exe"
set "SCRIPT=%~dp0fs-live-pull.py"
set "LOG=%~dp0fs-live-pull.log"
echo ===== %date% %time% ===== >> "%LOG%"
"%PY%" "%SCRIPT%" --once >> "%LOG%" 2>&1
