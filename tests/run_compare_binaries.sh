#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
One-shot baseline vs modified binary comparison.

Usage:
  bash run_compare_binaries.sh \
    [--password 0333] \
    [--config /usr/local/mysql-8.0.34/tests/config.yaml] \
    [--workload mixed_with_delete] \
    [--threads 10] \
    [--operations 300000]

Default binaries:
  Baseline:  /usr/local/mysql-8.0.34/binaries/mysqld.baseline
  Modified:  /usr/local/mysql-8.0.34/build_debug/runtime_output_directory/mysqld

To override binaries:
  bash run_compare_binaries.sh \
    --baseline-mysqld /path/to/baseline/mysqld \
    --modified-mysqld /path/to/modified/mysqld \
    --password 0333

Optional parameters:
  [--config /path/to/config.yaml] \
    [--baseline-basedir /usr/local/mysql] \
    [--modified-basedir /usr/local/mysql] \
    [--datadir /rds/mysql/data] \
    [--socket /rds/mysql/tmp/mysql.sock] \
    [--port 3306] \
    [--host 127.0.0.1] \
    [--user root] \
    [--runs-dir out/runs] \
    [--reports-dir out/reports]

What it does:
  1) Stops any existing server on the given socket/port (best-effort)
  2) Starts baseline mysqld -> runs tests/test_framework.py once -> stops server
  3) Starts modified mysqld -> runs tests/test_framework.py once -> stops server
  4) Runs tests/compare.py between the two runs -> writes report into reports dir

Important notes:
  - This script reuses the SAME datadir for both runs.
  - Root password must already be valid for that datadir.
  - It does NOT initialize or modify the datadir schema; it only starts/stops mysqld.

EOF
}

BASELINE_MYSQLD=/usr/local/mysql-8.0.34/binaries/mysqld.baseline
MODIFIED_MYSQLD=/usr/local/mysql-8.0.34/build_debug/runtime_output_directory/mysqld
BASELINE_BASEDIR=/usr/local/mysql
MODIFIED_BASEDIR=/usr/local/mysql
DATADIR=/rds/mysql/data
SOCKET=/rds/mysql/tmp/mysql.sock
PORT=3306
HOST=127.0.0.1
USER=root
PASSWORD=0333
WORKLOAD=insert
THREADS=10
OPERATIONS=1000
RUNS_DIR=out/runs
REPORTS_DIR=out/reports
CONFIG_FILE=

while [[ $# -gt 0 ]]; do
  case "$1" in
    --baseline-mysqld) BASELINE_MYSQLD="$2"; shift 2;;
    --modified-mysqld) MODIFIED_MYSQLD="$2"; shift 2;;
    --baseline-basedir) BASELINE_BASEDIR="$2"; shift 2;;
    --modified-basedir) MODIFIED_BASEDIR="$2"; shift 2;;
    --datadir) DATADIR="$2"; shift 2;;
    --socket) SOCKET="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --host) HOST="$2"; shift 2;;
    --user) USER="$2"; shift 2;;
    --password) PASSWORD="$2"; shift 2;;
    --config) CONFIG_FILE="$2"; shift 2;;
    --workload) WORKLOAD="$2"; shift 2;;
    --threads) THREADS="$2"; shift 2;;
    --operations) OPERATIONS="$2"; shift 2;;
    --runs-dir) RUNS_DIR="$2"; shift 2;;
    --reports-dir) REPORTS_DIR="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "[ERROR] Unknown arg: $1"; usage; exit 2;;
  esac
done

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
TESTS_DIR="$ROOT_DIR/tests"

if [[ -z "$CONFIG_FILE" ]]; then
  if [[ -f "$TESTS_DIR/config.yaml" ]]; then
    CONFIG_FILE="$TESTS_DIR/config.yaml"
  fi
fi

if [[ -z "$PASSWORD" ]]; then
  echo "[ERROR] Missing required args: --password"
  usage
  exit 2
fi

# 检查默认的 mysqld 路径是否存在
if [[ ! -x "$BASELINE_MYSQLD" ]]; then
  echo "[ERROR] Baseline mysqld not found or not executable: $BASELINE_MYSQLD"
  exit 2
fi

if [[ ! -x "$MODIFIED_MYSQLD" ]]; then
  echo "[ERROR] Modified mysqld not found or not executable: $MODIFIED_MYSQLD"
  exit 2
fi

MYSQL_BIN=/usr/local/mysql/bin/mysql
MYSQLADMIN_BIN=/usr/local/mysql/bin/mysqladmin
if [[ ! -x "$MYSQL_BIN" || ! -x "$MYSQLADMIN_BIN" ]]; then
  echo "[ERROR] Expected mysql/mysqladmin under /usr/local/mysql/bin"
  echo "        Missing: $MYSQL_BIN or $MYSQLADMIN_BIN"
  exit 2
fi

if [[ -n "$CONFIG_FILE" && ! -f "$CONFIG_FILE" ]]; then
  echo "[ERROR] config file not found: $CONFIG_FILE"
  exit 2
fi
mkdir -p "$TESTS_DIR/$RUNS_DIR" "$TESTS_DIR/$REPORTS_DIR"

PID_FILE="$(dirname "$SOCKET")/mysqld_${PORT}.pid"
ERR_LOG="$(dirname "$SOCKET")/error_${PORT}.log"

shutdown_server() {
  # Prefer clean shutdown via socket, fall back to pid kill.
  MYSQL_PWD="$PASSWORD" "$MYSQLADMIN_BIN" --no-defaults \
    --protocol=socket --socket="$SOCKET" -u"$USER" shutdown >/dev/null 2>&1 || true

  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      for _ in $(seq 1 40); do
        kill -0 "$pid" >/dev/null 2>&1 || return 0
        sleep 0.25
      done
    fi
  fi
}

start_server() {
  local mysqld_bin="$1"
  local basedir="$2"

  rm -f "$SOCKET" "$PID_FILE"

  "$mysqld_bin" --no-defaults \
    --basedir="$basedir" \
    --datadir="$DATADIR" \
    --socket="$SOCKET" \
    --port="$PORT" \
    --pid-file="$PID_FILE" \
    --log-error="$ERR_LOG" \
    --performance_schema=ON \
    --user=mysql \
    --daemonize

  for _ in $(seq 1 80); do
    if [[ -S "$SOCKET" ]] && MYSQL_PWD="$PASSWORD" "$MYSQL_BIN" --no-defaults \
        --protocol=socket --socket="$SOCKET" -u"$USER" -e "SELECT 1" >/dev/null 2>&1; then
      # Self-check: confirm which mysqld is actually running.
      if [[ -f "$PID_FILE" ]]; then
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
      else
        pid=""
      fi

      echo "[INFO] mysqld bin (requested): $mysqld_bin"
      "$mysqld_bin" --version 2>/dev/null | head -n 1 || true
      if [[ -n "$pid" ]]; then
        echo "[INFO] mysqld pid: $pid"
        if [[ -e "/proc/$pid/exe" ]]; then
          echo "[INFO] mysqld exe (actual): $(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
        fi
      fi

      MYSQL_PWD="$PASSWORD" "$MYSQL_BIN" --no-defaults \
        --protocol=socket --socket="$SOCKET" -u"$USER" -Nse \
        "SELECT CONCAT('@@version=',@@version,' @@version_comment=',@@version_comment,' @@basedir=',@@basedir,' @@datadir=',@@datadir)" \
        2>/dev/null | sed 's/^/[INFO] /' || true
      return 0
    fi
    sleep 0.25
  done

  echo "[ERROR] mysqld did not become ready. Check: $ERR_LOG"
  return 1
}

run_once() {
  local variant="$1"
  echo "[INFO] Running: variant=$variant workload=$WORKLOAD threads=$THREADS ops=$OPERATIONS"
  (cd "$TESTS_DIR" && python3 test_framework.py \
    --variant="$variant" \
    ${CONFIG_FILE:+--config="$CONFIG_FILE"} \
    --workload="$WORKLOAD" \
    --threads="$THREADS" \
    --operations="$OPERATIONS" \
    --host="$HOST" --port="$PORT" \
    --user="$USER" --password="$PASSWORD" \
    --output_dir="$RUNS_DIR")
}

latest_run_dir() {
  ls -dt "$TESTS_DIR/$RUNS_DIR"/* 2>/dev/null | head -n 1 || true
}

echo "[INFO] Stopping any existing mysqld (best-effort)"
shutdown_server

# Baseline run
before=$(latest_run_dir)
echo "[INFO] Starting baseline mysqld: $BASELINE_MYSQLD (basedir=$BASELINE_BASEDIR)"
start_server "$BASELINE_MYSQLD" "$BASELINE_BASEDIR"
run_once baseline
shutdown_server
base_run=$(latest_run_dir)
if [[ -z "$base_run" || "$base_run" == "$before" ]]; then
  echo "[ERROR] Baseline run directory not found under $TESTS_DIR/$RUNS_DIR"
  exit 1
fi

# Modified run
before2=$(latest_run_dir)
echo "[INFO] Starting modified mysqld: $MODIFIED_MYSQLD (basedir=$MODIFIED_BASEDIR)"
start_server "$MODIFIED_MYSQLD" "$MODIFIED_BASEDIR"
run_once optimized
shutdown_server
mod_run=$(latest_run_dir)
if [[ -z "$mod_run" || "$mod_run" == "$before2" ]]; then
  echo "[ERROR] Modified run directory not found under $TESTS_DIR/$RUNS_DIR"
  exit 1
fi

echo "[INFO] Generating compare report"
(cd "$TESTS_DIR" && python3 compare.py \
  --baseline "$base_run/result.csv" \
  --optimized "$mod_run/result.csv" \
  --output "$REPORTS_DIR")

echo "[INFO] Latest report files:"
ls -dt "$TESTS_DIR/$REPORTS_DIR"/*.html 2>/dev/null | head -n 1 || true
ls -dt "$TESTS_DIR/$REPORTS_DIR"/*_charts.png 2>/dev/null | head -n 1 || true
