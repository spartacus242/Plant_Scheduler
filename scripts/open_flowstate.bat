@echo off
REM Start Flowstate (Streamlit) if needed, then open http://localhost:8501.
REM Idempotent — safe to double-click repeatedly.
setlocal EnableExtensions

set "PORT=8501"
if not "%FLOWSTATE_PORT%"=="" set "PORT=%FLOWSTATE_PORT%"
set "URL=http://localhost:%PORT%"

REM Repo root = parent of scripts\
set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%" >nul
set "REPO_ROOT=%CD%"
popd >nul

set "LOG=%TEMP%\flowstate-streamlit.log"

REM Scrub PYTHONPATH: a leaked site-packages (e.g. from an agent/IDE shell)
REM can shadow the venv's numpy/pandas with an incompatible build and crash
REM the app on import. The venv is self-contained, so drop PYTHONPATH entirely.
set "PYTHONPATH="

REM Prefer repo venv, then py launcher, then python on PATH
set "PY_CMD="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY_CMD="%REPO_ROOT%\.venv\Scripts\python.exe""
if not defined PY_CMD (
  where py >nul 2>&1 && set "PY_CMD=py -3"
)
if not defined PY_CMD (
  where python >nul 2>&1 && set "PY_CMD=python"
)
if not defined PY_CMD (
  echo Python not found. Install Python 3 and/or create .venv in the repo.
  pause
  exit /b 1
)

REM Health check via PowerShell
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/_stcore/health' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %ERRORLEVEL%==0 goto :open

echo Starting Flowstate on port %PORT%...
cd /d "%REPO_ROOT%"
start "Flowstate" /MIN cmd /c %PY_CMD% -m streamlit run code\app.py --server.headless true --server.port %PORT% ^> "%LOG%" 2^>^&1

set /a ATTEMPTS=0
:waitloop
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/_stcore/health' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %ERRORLEVEL%==0 goto :open
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 60 (
  echo Timed out waiting for Flowstate at %URL%.
  echo Check log: %LOG%
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto :waitloop

:open
start "" "%URL%"
endlocal
exit /b 0
