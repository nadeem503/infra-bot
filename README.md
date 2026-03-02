# Infra-Bot: Autonomous DC Infrastructure Assistant

A production-ready Slack bot that monitors Device Cloud (DC) infrastructure channels,
detects issues via keywords and @mentions, proposes remediation actions, and executes
them **only after explicit approval** from an authorized user.

---

## Features

| Feature | Detail |
|---------|--------|
| Trigger | @mention only — no passive channel scanning |
| Issue detection | Keyword-based (8 categories, YAML-configured) |
| Region detection | India / US / Dublin / APAC via regex patterns |
| Device parsing | UDID (40-char hex), IPv4, short hostname |
| Approval workflow | Block Kit buttons with 30-minute TTL |
| Audit logging | JSON Lines → `logs/actions.jsonl` |
| Deployment | Docker + docker-compose |

---

## Safety Rules (enforced in code)

1. **DBAction** — raises `PermissionError` on any non-`SELECT` statement
2. **SSHAction** — explicit allowlist of permitted commands only
3. **Approval** — only `APPROVER_SLACK_ID` can approve actions
4. **No credentials** — raw secrets never appear in Slack messages
5. **Audit trail** — every action logged before _and_ after execution

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/nadeem503/infra-bot.git
cd infra-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

### 4. Configure Slack App

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. **Socket Mode** → Enable → generate App-Level Token with `connections:write` scope → set as `SLACK_APP_TOKEN`
3. **Event Subscriptions** → Enable → Subscribe to bot event: `app_mention`
4. **Interactivity & Shortcuts** → Enable (required for approval buttons)
5. **OAuth & Permissions** → Add scopes: `app_mentions:read`, `chat:write`, `channels:history`
6. Install app → copy Bot OAuth Token → set as `SLACK_BOT_TOKEN`

### 5. Update DC owner config

Edit `config/dc_owners.yaml` — replace `U_TODO_*` placeholders with real Slack user IDs.

### 6. Run

```bash
# Direct
python main.py

# Docker
docker-compose up -d
```

---

## Usage

Mention the bot in any channel:

```
@infra-bot device down UDID a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 India region
```

The bot will:

1. Detect the issue type (`device_down`)
2. Identify the region (`India`)
3. Extract the UDID
4. Post an analysis summary with ✅ Approve / ❌ Deny buttons
5. Execute **only** after approval
6. Post the result in the thread

**Response format:**
```
🚨 Infra AI Response
• Issue Detected: device_down
• Region: India
• Devices: `a1b2c3...`
• DC Owner: @india-dc-team (India DC Team)
• Action Plan:
  • `device_status` for device_down (severity: high)
• Executing: Awaiting approval ⏳
```

---

## Repository Structure

```
infra-bot/
├── main.py                      # Entry point (Socket Mode)
├── config.py                    # Env var loader
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── config/
│   ├── dc_owners.yaml           # Region → Slack user/team mapping
│   ├── keywords.yaml            # Issue keyword → category mapping
│   └── regions.yaml             # Region detection regex patterns
├── bot/
│   ├── listeners/
│   │   ├── message_listener.py  # @mention handler + analysis pipeline
│   │   └── action_listener.py   # Approve/deny button handler
│   ├── analyzers/
│   │   ├── issue_detector.py    # Keyword-based issue classification
│   │   ├── region_detector.py   # Regex region detection
│   │   └── device_extractor.py  # UDID / IP / hostname extraction
│   ├── actions/
│   │   ├── base_action.py       # Abstract base with audit logging
│   │   ├── ssh_action.py        # SSH via bastion (command allowlist)
│   │   ├── adb_action.py        # ADB command execution
│   │   ├── jenkins_action.py    # Jenkins job trigger
│   │   ├── github_action.py     # GitHub workflow dispatch
│   │   ├── db_action.py         # Read-only MySQL queries
│   │   ├── jira_action.py       # Jira ticket creation (TE project)
│   │   └── device_status.py     # Device health / build info
│   ├── approval/
│   │   └── approval_manager.py  # Pending action store (30-min TTL)
│   └── formatters/
│       └── slack_formatter.py   # Block Kit message builder
└── utils/
    ├── logger.py                # JSON audit logger
    └── config_loader.py         # YAML loader with LRU cache
```

---

## Action Types

| Action type | Trigger keywords | Description |
|-------------|-----------------|-------------|
| `ssh_reboot` | reboot, hung, frozen | SSH reboot via bastion |
| `device_status` | device down, offline | Fetch health / build info |
| `adb_restart` | adb, no adb, adb offline | ADB kill-server + restart |
| `db_query` | db mismatch, build mismatch | Read-only MySQL SELECT |
| `jenkins_trigger` | jenkins, build failed | Trigger Jenkins job |
| `github_workflow` | — | GitHub Actions workflow dispatch |
| `jira_ticket` | any | Create Jira ticket (TE project) |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-level token for Socket Mode (`xapp-...`) |
| `APPROVER_SLACK_ID` | Slack user ID permitted to approve actions |
| `BASTION_HOST` | SSH bastion hostname |
| `BASTION_USER` | SSH username |
| `BASTION_KEY_PATH` | Path to SSH private key |
| `JENKINS_URL` | Jenkins base URL |
| `JENKINS_USER` | Jenkins username |
| `JENKINS_API_TOKEN` | Jenkins API token |
| `GITHUB_TOKEN` | GitHub personal access token |
| `DB_HOST` | MySQL hostname |
| `DB_USER` | MySQL username |
| `DB_PASSWORD` | MySQL password |
| `JIRA_EMAIL` | Atlassian account email |
| `JIRA_API_TOKEN` | Jira API token |

---

## Verification

```bash
# 1. Start the bot (requires .env)
python main.py

# 2. Verify DB safety block
python -c "
from bot.actions.db_action import DBAction
action = DBAction(params={'query': 'DROP TABLE devices'}, triggered_by='test', channel='test')
try:
    action.execute()
except PermissionError as e:
    print('PASS — Safety check blocked:', e)
"

# 3. Verify SELECT is allowed
python -c "
from bot.actions.db_action import DBAction
action = DBAction(params={'query': 'SELECT 1'}, triggered_by='test', channel='test')
result = action.execute()
print('SELECT result:', result['message'])
"

# 4. Inspect audit log
cat logs/actions.jsonl
```

---

## Extending

- **New keyword category**: edit `config/keywords.yaml`
- **New region**: edit `config/regions.yaml`
- **New action**: extend `BaseAction`, wire into `ISSUE_TO_ACTION` (message_listener) and `_get_action_handler` (action_listener)
- **Scale approval store**: swap the in-memory dict in `ApprovalManager` for a Redis client
