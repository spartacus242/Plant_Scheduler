#!/usr/bin/env bash
# A/B the solver horizon fix: same inputs, horizon_hours=336 (old behaviour)
# vs 504 (the app's real 3-week rolling horizon). Prints how many week-2
# orders (due_start_hour >= 336) each run actually schedules.
set -u
cd /c/Users/jbdil/Flowstate/Plant_Scheduler
PY='C:\Users\jbdil\Flowstate\Plant_Scheduler\.venv\Scripts\python.exe'
SOLVER='C:\Users\jbdil\Flowstate\Plant_Scheduler\code\solver\phase2_scheduler.py'
TL="${TL:-300}"

stage () {
  local dir="$1" hz="$2"
  rm -rf "$dir"; mkdir -p "$dir"
  for f in changeovers.csv downtimes.csv demand_plan.csv capabilities_rates.csv \
           line_cip_hrs.csv trials.csv sku_info.csv initial_states.csv; do
    [ -f "data/reference/$f" ] && cp "data/reference/$f" "$dir/$f"
  done
  sed -e "s/^horizon_hours = .*/horizon_hours = $hz/" \
      -e "s/^time_limit = .*/time_limit = $TL/" flowstate.toml > "$dir/flowstate.toml"
}

run () {
  local dir="$1"
  local win; win=$(cygpath -w "$dir")
  powershell -NoProfile -Command "\$env:PYTHONPATH=''; & '$PY' '$SOLVER' --data-dir '$win' --config '$win\\flowstate.toml' 2>&1 | Out-File -Encoding utf8 '$win\\run.log'; Write-Output \"EXIT=\$LASTEXITCODE\""
}

report () {
  local dir="$1" tag="$2"
  echo "===== $tag ====="
  grep -i "status\|Blocks\|utilization" "$dir/solver_kpis.txt" 2>/dev/null | head -8
  python - "$dir" <<'PY' 2>/dev/null || true
PY
  awk -F, 'NR>1 && $7+0 >= 336 {print $1}' data/reference/demand_plan.csv | sort -u > /tmp/w2_ids.txt
  if [ -f "$dir/schedule_phase2.csv" ]; then
    head -1 "$dir/schedule_phase2.csv"
    total=$(( $(wc -l < "$dir/schedule_phase2.csv") - 1 ))
    w2=$(grep -c -F -f /tmp/w2_ids.txt "$dir/schedule_phase2.csv" || true)
    echo "blocks=$total  week2_order_blocks=$w2  (week2 order ids=$(wc -l < /tmp/w2_ids.txt))"
  else
    echo "no schedule_phase2.csv"
  fi
}

BASE=/c/Users/jbdil/AppData/Local/Temp/fs_hz
stage "$BASE/off" 336
stage "$BASE/on" 504
echo "--- running OFF (336h) ---"; run "$BASE/off"
echo "--- running ON  (504h) ---"; run "$BASE/on"
report "$BASE/off" "OFF horizon=336"
report "$BASE/on"  "ON  horizon=504"
