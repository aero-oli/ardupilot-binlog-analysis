#!/usr/bin/env bash
set -euo pipefail

log_path="${1:-${REAL_LOG_PATH:-}}"
if [[ -z "$log_path" ]]; then
  echo "usage: tests/real_log_fixture_check.sh /path/to/real/DataFlash.BIN" >&2
  echo "or set REAL_LOG_PATH=/path/to/real/DataFlash.BIN" >&2
  exit 2
fi
if [[ ! -f "$log_path" ]]; then
  echo "real log not found: $log_path" >&2
  exit 2
fi

python_bin="${PYTHON:-python}"
out_dir="$(mktemp -d "${TMPDIR:-/tmp}/ardupilot-real-log-check.XXXXXX")"

"$python_bin" scripts/ap_log_validate.py "$log_path" --json "$out_dir/validate.json" --summary "$out_dir/validate.md"
"$python_bin" scripts/ap_log_index.py "$log_path" --json "$out_dir/index.json" --summary "$out_dir/index.md"
"$python_bin" scripts/ap_log_extract.py "$log_path" --out "$out_dir/tables" --format csv --manifest "$out_dir/manifest.json"
"$python_bin" scripts/ap_log_metrics.py --tables "$out_dir/tables" --json "$out_dir/metrics.json" --summary "$out_dir/metrics.md"
"$python_bin" scripts/ap_log_segments.py --tables "$out_dir/tables" --json "$out_dir/segments.json" --summary "$out_dir/segments.md"
"$python_bin" scripts/ap_log_metrics.py --tables "$out_dir/tables" --window 0:120 --json "$out_dir/metrics-window.json" --summary "$out_dir/metrics-window.md"
"$python_bin" scripts/ap_log_plots.py --tables "$out_dir/tables" --out "$out_dir/plots" --manifest "$out_dir/plots/manifest.json" --events

if [[ -f "$out_dir/tables/GPS.csv" && -f "$out_dir/tables/BARO.csv" ]]; then
  "$python_bin" scripts/ap_log_custom_plot.py \
    --tables "$out_dir/tables" \
    --series GPS.Alt="GPS altitude" \
    --series BARO.Press="Barometric pressure" \
    --series 'GPS.Alt-BARO.Alt=GPS minus baro' \
    --secondary BARO.Press \
    --events \
    --title "GPS altitude and barometric pressure" \
    --out "$out_dir/plots/gps_altitude_pressure.html" \
    --manifest "$out_dir/plots/gps_altitude_pressure.json"
fi

echo "real log fixture check output: $out_dir"
