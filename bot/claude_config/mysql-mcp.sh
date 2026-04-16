#!/bin/bash
# MySQL MCP server for ltadmin bot host (10.151.2.248)
# Reads credentials from infra-bot .env — no macOS Keychain needed on Linux

# Load bot .env
set -a
for envfile in /Users/ltadmin/infra-bot/.env ~/infra-bot/.env; do
  [ -f "$envfile" ] && source "$envfile" && break
done
set +a

# Kill any existing tunnel on port 3307
lsof -ti:3307 | xargs kill -9 2>/dev/null || true

TUNNEL_PID=""
if [ -n "$DB_TUNNEL_HOST" ] && [ -n "$DB_TUNNEL_USER" ]; then
  # SSH tunnel needed (DB not directly reachable)
  sshpass -p "${DB_TUNNEL_PASS}" ssh -N \
    -L 3307:${DB_HOST}:${DB_PORT:-3306} \
    -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=30 \
    "${DB_TUNNEL_USER}@${DB_TUNNEL_HOST}" &
  TUNNEL_PID=$!
  sleep 3
  CONNECT_HOST=127.0.0.1
  CONNECT_PORT=3307
else
  # Direct connection (bot host is on internal network)
  CONNECT_HOST="${DB_HOST:-127.0.0.1}"
  CONNECT_PORT="${DB_PORT:-3306}"
fi

MYSQL_HOST=$CONNECT_HOST \
MYSQL_PORT=$CONNECT_PORT \
MYSQL_USER="${DB_USER:-read_only_user}" \
MYSQL_PASS="${DB_PASSWORD}" \
MYSQL_DB="${DB_NAME:-}" \
npx -y @benborla29/mcp-server-mysql

[ -n "$TUNNEL_PID" ] && kill $TUNNEL_PID 2>/dev/null || true
