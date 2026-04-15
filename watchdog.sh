#!/bin/bash
# Infra-bot watchdog — auto-restarts the bot when it exits (handles BrokenPipeError loops)
LOG=/Users/ltadmin/infra-bot/logs/bot.log
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

while true; do
  echo "[watchdog] Starting bot at $(date)" >> "$LOG"
  /opt/homebrew/bin/python3 /Users/ltadmin/infra-bot/main.py >> "$LOG" 2>&1
  echo "[watchdog] Bot exited (code=$?), restarting in 5s at $(date)" >> "$LOG"
  sleep 5
done
