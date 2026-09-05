@echo off
rem Flowstate live-data PULL — Windows scheduled task wrapper.
rem Runs scripts\fs-live-pull.py --once and appends output to a log for review.
set "PY=C:\Users\jbdil\Flowstate\Plant_Scheduler\.venv\Scripts\python.exe"
set "SCRIPT=C:\Users\jbdil\Flowstate\Plant_Scheduler\scripts\fs-live-pull.py"
set "LOG=C:\Users\jbdil\Flowstate\Plant_Scheduler\scripts\fs-live-pull.log"
echo ===== %date% %time% ===== >> "%LOG%"
"%PY%" "%SCRIPT%" --once >> "%LOG%" 2>&1