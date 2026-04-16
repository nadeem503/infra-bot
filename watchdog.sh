#!/bin/bash
# Infra-bot watchdog — auto-restarts the bot when it exits (handles BrokenPipeError loops)
LOG=/Users/ltadmin/infra-bot/logs/bot.log
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

rapid_exits=0
while true; do
  echo "[watchdog] Starting bot at $(date)" >> "$LOG"
  start_ts=$(date +%s)
  /opt/homebrew/bin/python3 /Users/ltadmin/infra-bot/main.py >> "$LOG" 2>&1
  exit_code=$?
  runtime=$(( $(date +%s) - start_ts ))
  if [ $runtime -lt 30 ]; then
    rapid_exits=$((rapid_exits + 1))
  else
    rapid_exits=0
  fi
  if [ $rapid_exits -ge 3 ]; then
    echo "[watchdog] Rapid-exit loop detected ($rapid_exits exits in <30s each) — sleeping 90s to let Slack WS settle" >> "$LOG"
    sleep 90
    rapid_exits=0
  else
    echo "[watchdog] Bot exited (code=$exit_code, runtime=${runtime}s), restarting in 5s at $(date)" >> "$LOG"
    sleep 5
  fi
done
