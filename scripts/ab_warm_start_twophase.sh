#!/usr/bin/env bash
# A/B proof for task-list item 30 (warm start on the two-phase Week-0 solve).
#
# Two real two-phase solves, identical inputs (8 reference CSVs + flowstate.toml):
#   COLD  --two-phase --no-warm-start   -> baseline, also produces prev_schedule.csv
#   WARM  --two-phase                  -> hinted from COLD's combined schedule
#
# The two-phase Week-0 model runs on a 168h horizon. build_hint_plan range-checks
# every prev row against that horizon, so the combined schedule's Week-1 rows
# (absolute hours 168+) are dropped and only Week-0 placements seed the hint.
# Proof = "[warm-start] hinted" appears in WARM's log (and is absent/"disabled"
# in COLD's), and the warm schedule is FEASIBLE with comparable blocks/short.
set -u

ROOT="/c/Users/jbdil/Flowstate/Plant_Scheduler"
PY='C:\Users\jbdil\Flowstate\Plant_Scheduler\.venv\Scripts\python.exe'
SCHED='C:\Users\jbdil\Flowstate\Plant_Scheduler\code\solver\phase2_scheduler.py'
cd "$ROOT" || exit 1

OUT="$ROOT/data/_ab_warmstart_twophase"
rm -rf "$OUT"; mkdir -p "$OUT"

stage() {  # stage <dir>
  local w="$1"
  mkdir -p "$w"
  for f in changeovers.csv downtimes.csv demand_plan.csv capabilities_rates.csv \
           line_cip_hrs.csv trials.csv sku_info.csv initial_states.csv; do
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
rows=list(csv.DictReader(open(w+r'\\schedule_phase2.csv')))
print('blocks           :', len(rows))
print('lines used       :', len({r['line_id'] for r in rows}))
print('max end hour     :', max(int(float(r['end_hour'])) for r in rows))
print('total run hours  :', sum(int(float(r['run_hours'])) for r in rows))
try:
    fr=json.load(open(w+r'\\feasibility_report.json'))
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

echo "### COLD: two-phase (120s/phase, no warm start) - generates the hint source"
stage "$OUT/cold"
run "$OUT/cold" 120 --two-phase --no-warm-start
report "COLD two-phase 120s (no hint)" "$OUT/cold"

echo "### WARM: two-phase (120s/phase, hinted from COLD)"
stage "$OUT/warm"
cp "$OUT/cold/schedule_phase2.csv" "$OUT/warm/prev_schedule.csv" 2>/dev/null \
  && echo "hint source installed: $(wc -l < "$OUT/warm/prev_schedule.csv") lines" \
  || echo "WARNING: no hint source - COLD produced no schedule"
run "$OUT/warm" 120 --two-phase
report "WARM two-phase 120s (hinted)" "$OUT/warm"

echo "### DONE"
