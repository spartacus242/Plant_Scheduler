#!/usr/bin/env bash
# A/B proof for task-list item 10 (CP-SAT warm start).
#
# Three real single-phase solves, identical inputs:
#   A  seed  300s cold  -> produces the "previous schedule" used as the hint
#   B  cold  120s       -> baseline at a deliberately tight budget
#   C  warm  120s       -> same budget, hinted from A
#
# A short budget is the honest test: pitfall 9 records that single-phase at
# tl=120 produces a much worse schedule than tl=300 with identical constraints,
# purely because the search runs out of time. If hinting is worth anything, it
# shows up exactly there.
set -u

ROOT="/c/Users/jbdil/Flowstate/Plant_Scheduler"
PY='C:\Users\jbdil\Flowstate\Plant_Scheduler\.venv\Scripts\python.exe'
SCHED='C:\Users\jbdil\Flowstate\Plant_Scheduler\code\solver\phase2_scheduler.py'
cd "$ROOT" || exit 1

OUT="$ROOT/data/_ab_warmstart"
rm -rf "$OUT"; mkdir -p "$OUT"

stage() {  # stage <dir>
  local w="$1"
  mkdir -p "$w"
  for f in changeovers.csv downtimes.csv demand_plan.csv capabilities_rates.csv \
           line_cip_hrs.csv trials.csv sku_info.csv initial_states.csv current_mo.csv; do
    [ -f "data/reference/$f" ] && cp "data/reference/$f" "$w/$f"
  done
  cp flowstate.toml "$w"/
}

run() {  # run <dir> <time_limit> <extra flags>
  local w="$1"; local tl="$2"; shift 2
  local ww; ww=$(cygpath -w "$w")
  powershell -NoProfile -Command "\$env:PYTHONPATH=''; & '$PY' '$SCHED' --data-dir '$ww' --time-limit $tl --config '$ww\\flowstate.toml' $* 2>&1 | Out-File -Encoding utf8 '$ww\\run.log'; Write-Output \"EXIT=\$LASTEXITCODE\""
}

report() {  # report <label> <dir>
  local label="$1"; local w="$2"
  echo "===== $label ====="
  if [ -f "$w/schedule_phase2.csv" ]; then
    powershell -NoProfile -Command "\$env:PYTHONPATH=''; & '$PY' -c \"
import json,sys,csv
w=r'$(cygpath -w "$w")'
rows=list(csv.DictReader(open(w+r'\schedule_phase2.csv')))
print('blocks           :', len(rows))
print('lines used       :', len({r['line_id'] for r in rows}))
print('max end hour     :', max(int(float(r['end_hour'])) for r in rows))
print('total run hours  :', sum(int(float(r['run_hours'])) for r in rows))
try:
    fr=json.load(open(w+r'\feasibility_report.json'))
    print('status           :', fr.get('status'))
    print('relax level      :', fr.get('relax_level'))
    print('short of qmin    :', len(fr.get('orders_short_of_qmin') or []))
    print('late orders      :', len(fr.get('late_orders') or []))
except Exception as e:
    print('report           : unreadable', e)
\""
  else
    echo "NO SCHEDULE PRODUCED"
  fi
  echo "--- warm-start + objective lines ---"
  grep -E "warm-start|SOLVER level=|best_objective|Objective" "$w/run.log" 2>/dev/null | head -20
  echo
}

echo "### A: seed run (300s, cold) - generates the hint source"
stage "$OUT/seed"
run "$OUT/seed" 300 --no-warm-start
report "A seed 300s cold" "$OUT/seed"

echo "### B: baseline (120s, cold)"
stage "$OUT/cold"
run "$OUT/cold" 120 --no-warm-start
report "B cold 120s" "$OUT/cold"

echo "### C: warm (120s, hinted from A)"
stage "$OUT/warm"
cp "$OUT/seed/schedule_phase2.csv" "$OUT/warm/prev_schedule.csv" 2>/dev/null \
  && echo "hint source installed: $(wc -l < "$OUT/warm/prev_schedule.csv") lines" \
  || echo "WARNING: no hint source - A produced no schedule"
run "$OUT/warm" 120
report "C warm 120s" "$OUT/warm"

echo "### DONE"
