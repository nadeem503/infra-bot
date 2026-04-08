# Infra-Bot Test Playbook

Run `python3 scripts/health_check.py` first — all checks must pass before testing.

---

## Step 1 — Start the bot

```bash
cd infra-bot
python3 main.py
```

You should see:
```
Starting Infra-Bot...
Redis connected: localhost:6379/0
Infra-Bot is running in Socket Mode
```

---

## Step 2 — Basic ping (sanity check)

In any Slack channel where bot is invited, type:

| You type | Expected bot reply |
|----------|--------------------|
| `@infra-bot hello` | Friendly greeting (Gemini-generated) |
| `@infra-bot what can you do?` | Capabilities summary |

✅ **Pass:** Bot replies in thread  
❌ **Fail:** No reply → check logs for errors

---

## Step 3 — Confidence gating

| You type | Expected |
|----------|----------|
| `@infra-bot xyz blah foo` | A/B/C clarification card with 3 options |

Click any option → bot executes that choice.

---

## Step 4 — Infra issue detection

| You type | Expected |
|----------|----------|
| `@infra-bot device 10.151.12.34 is down` | Approval card with ✅ Approve / 🚀 Execute Now / ❌ Deny buttons |
| `@infra-bot reboot 10.100.5.20` | Approval card, region = Dublin |
| `@infra-bot ADB offline on device abc123` | Approval card, action = adb_restart |
| `@infra-bot MISMATCH: DB=11, Device=device not found udid=abc123` | device_disconnected approval card with retry-once dry-run |

Click **✅ Approve** → only your Slack ID (U04UTG30V9A) can approve.

**Dry-run preview** should appear above the buttons showing the exact command.

---

## Step 5 — Duplicate deduplication

1. Report same device twice within 15 min:
   ```
   @infra-bot device 10.151.12.34 is down
   @infra-bot device 10.151.12.34 is down   ← second time
   ```
2. Second message should reply:  
   `:repeat: Already tracking device_down for 10.151.12.34 — action pending (~15m cooldown)`

---

## Step 6 — Root cause grouping

Send 3+ different issue types in the same channel within 10 min:
```
@infra-bot device 10.151.12.34 down
@infra-bot ADB offline on 10.151.12.35
@infra-bot Jenkins build failing in AP
```
After the 3rd message, bot should post a grouped root cause analysis block.

---

## Step 7 — Rate limiting

Trigger 6 infra issues rapidly:
```
@infra-bot device 10.151.12.34 down
@infra-bot device 10.151.12.35 down
@infra-bot device 10.151.12.36 down
@infra-bot device 10.151.12.37 down
@infra-bot device 10.151.12.38 down
@infra-bot device 10.151.12.39 down   ← 6th
```
6th should reply: `:traffic_light: You've triggered 5 actions in 10 min — slow down`

---

## Step 8 — Jira ticket creation

```
@infra-bot create a jira task: Device 10.151.12.34 keeps going offline
@infra-bot create a bug ticket for ADB not working on AP devices
```
Expected: `Done ✅ Created TE-XXX — Device 10.151.12.34...`  
Check Jira to confirm ticket was actually created.

---

## Step 9 — Jira ticket assignment

```
@infra-bot assign TE-123 to @Nadeem Khan
```
Expected: `Done ✅ TE-123 assigned to @Nadeem Khan`

---

## Step 10 — Thread follow-up (conversation memory)

```
@infra-bot device 10.151.12.34 is down
  → (bot replies with approval card)
@infra-bot also reboot it        ← follow-up in same thread
```
Bot should understand the device from thread context without you repeating the IP.

---

## Step 11 — Reaction-based approval

1. Bot posts an approval card
2. React to the card with ✅ emoji (from your account = U04UTG30V9A)
3. Bot should start executing without clicking the button

---

## Step 12 — Slash commands

| Command | Expected |
|---------|----------|
| `/infra pending` | List of pending approval actions (or "No pending actions") |
| `/infra status 10.151.12.34` | Device health output |
| `/infra history device=10.151.12.34 last=24h` | List with 🔁 Replay buttons |
| `/infra faulty count` | Count of offline devices from DB |

---

## Step 13 — Home Tab dashboard

1. Click the bot's name in Slack sidebar
2. Click the **Home** tab
3. Should show: pending approvals, today's stats, learned fix patterns, token expiry warnings

---

## Step 14 — Circuit breaker

```bash
# Simulate 3 failures on same host by manually setting in Redis:
redis-cli
> SET infra:circuit:fail:10.151.12.34 3
> EXPIRE infra:circuit:fail:10.151.12.34 900
```
Then try to approve an action for that host — bot should reply:
`:zap: Circuit breaker tripped for 10.151.12.34 — actions paused`

---

## Step 15 — Logs

All actions are written to `logs/actions.jsonl`:

```bash
tail -f logs/actions.jsonl | python3 -m json.tool
```

Sample entry:
```json
{
  "timestamp": 1712345678.0,
  "action_type": "ssh_reboot",
  "triggered_by": "U04UTG30V9A",
  "channel": "C06TFLLMR5G",
  "devices": ["10.151.12.34"],
  "region": "ap",
  "status": "completed"
}
```

---

## Quick Debug Commands

```bash
# Check Redis keys
redis-cli KEYS "infra:*"

# Check pending approvals
redis-cli KEYS "infra:approval:*"

# Check dedup keys (active cooldowns)
redis-cli KEYS "infra:dedup:*"

# Check learning store
redis-cli KEYS "infra:learn:*"

# Check circuit breakers
redis-cli KEYS "infra:circuit:*"

# Watch bot logs live
python3 main.py 2>&1 | grep -E "INFO|ERROR|WARNING"

# Clear all infra-bot Redis keys (reset state)
redis-cli KEYS "infra:*" | xargs redis-cli DEL
```

---

## Common Issues

| Error | Cause | Fix |
|-------|-------|-----|
| `BoltError: SLACK_BOT_TOKEN required` | Token in `.env.example` not `.env` | `cp .env.example .env` |
| `RuntimeError: GEMINI_API_KEY not set` | Missing Gemini key | Add to `.env` from aistudio.google.com/apikey |
| `Redis connection refused` | Redis not running | `redis-server` or `docker-compose up redis` |
| `Action not found` | Redis key expired (>30 min) | Normal — re-request the action |
| `lock: not authorized to approve` | Wrong user clicking Approve | Only U04UTG30V9A can approve |
| Bot doesn't respond to mentions | Bot not in channel | `/invite @infra-bot` in the channel |
