"""AI brain for infra-bot.

Flow: local_classifier → Claude CLI → Gemini fallback.
Claude CLI uses the ltadmin Claude Code subscription (zero API cost).
Claude acts as smart router: classifies OR replies directly for complex questions.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from utils.activity_log import log_claude_call, log_bot_session
from utils.logger import get_logger

logger = get_logger(__name__)

_QUOTA_MSG = (
    ":hourglass: AI is unavailable right now. Try again in a moment."
)

# Claude CLI path on the bot host
_CLAUDE_BIN   = "/opt/homebrew/bin/claude"
_KEYCHAIN_DB  = "/Users/ltadmin/Library/Keychains/login.keychain-db"
# Use Sonnet for classify (fast, cheap) — Opus is overkill for JSON routing
_CLASSIFY_MODEL = "claude-sonnet-4-6"

_keychain_unlocked = False   # unlocked once per process lifetime
_keychain_failed  = False   # permanently skip after first failure (avoids per-call overhead)
_keychain_lock = __import__("threading").Lock()


def _ensure_keychain_unlocked() -> None:
    """Unlock the login keychain so Claude CLI can read its OAuth token.

    Idempotent — runs at most once per process. Required when bot starts
    as a background process (nohup/SSH) where keychain is locked.
    Password is passed via stdin (not -p flag) to avoid exposure in ps aux.
    After a permanent failure the flag is set so we skip the attempt on
    every subsequent call rather than retrying and adding latency.
    """
    global _keychain_unlocked, _keychain_failed
    with _keychain_lock:
        if _keychain_unlocked or _keychain_failed:
            return
        try:
            passwd = settings.HOST_PASS or ""
            result = subprocess.run(
                ["security", "unlock-keychain", _KEYCHAIN_DB],
                input=passwd + "\n",
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                logger.info("Keychain unlocked for Claude CLI")
                _keychain_unlocked = True
            else:
                logger.warning("Keychain unlock failed (will not retry): %s",
                               result.stderr.strip()[:100])
                _keychain_failed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Keychain unlock error (will not retry): %s", exc)
            _keychain_failed = True

MODEL = "gemini-2.0-flash"

# Cache TTL: 5 min for classify, 10 min for root cause
_CLASSIFY_CACHE_TTL = 300
_ROOT_CAUSE_CACHE_TTL = 600

# ---------------------------------------------------------------------------
# Claude router prompt — Claude decides: classify OR reply directly.
# ---------------------------------------------------------------------------

CLAUDE_ROUTER_SYSTEM = """
You are Infra-Bot, a Slack assistant for LambdaTest's Real Device Cloud infrastructure team.

Read the Slack message and choose ONE of four actions:

ACTION 1 — classify: The message maps clearly to a single structured bot action.
Use this for device checks, service restarts, Jira tasks, ADB issues, reboots, etc.
Only classify when you are confident about the single intended action.

ACTION 2 — direct: Reply conversationally. Use for explanations, summaries, greetings,
capability questions, monitoring requests, or anything that is NOT a single clear action.
Also use this when the message is AMBIGUOUS — ask a short clarification question instead
of guessing. Example: "Did you want me to *reboot* the device, or just *check* if it's connected?"

ACTION 3 — AMBIGUITY RULE (most important): When in doubt, use action=direct.
If you are not sure whether the user wants you to DO something or is just sharing context,
providing a status update, or acknowledging something — respond naturally with action=direct.
NEVER force a classification when the intent is unclear. Trust your own judgment.
Examples:
- "reboot and check if up" → ask: reboot first, then check? or just check current state?
- "monitor and let me know when back" → monitoring isn't supported; use direct to explain
- "fix the device" → ask: what specifically? reboot, check connectivity, restart LRR?
- "removed passcode" / "device fixed" / "done" / "resolved it" → user is sharing an update,
  not asking for an action — acknowledge conversationally, offer to check if they'd like

ACTION 4 — multi: Use ONLY when the message EXPLICITLY requests 2+ actions in clear sequence
with an "and"/"then"/"also"/"proceed with" connector AND the actions are unambiguous.
Examples that qualify:
- "create a jira and dispose the device" → create_jira then device_dispose
- "raise a ticket and proceed with migration" → create_jira then device_migrate
- "create ticket and mark device disposed" → create_jira then device_dispose
- "create jira for same, proceed with same ticket and mark disposed" → create_jira then device_dispose

Format for action=multi:
{"action":"multi","actions":[
  {"intent":"create_jira","params":{"title":"...","description":"..."}},
  {"intent":"infra_issue","issue_category":"device_dispose","params":{"host_udid_pairs":"...","environment":"prod","jira":"__from_jira__"}}
]}
Use "__from_jira__" as the jira param value when a later action needs the ticket key that will
be created by an earlier create_jira step. The executor will substitute it automatically.
Supported sequences:
- create_jira → device_dispose  (jira="__from_jira__")
- create_jira → device_migrate  (jira="__from_jira__")
- create_jira → infra_issue (any) (jira="__from_jira__" if needed)
Do NOT use action=multi for ambiguous chains — use action=direct to clarify first.

== AVAILABLE ACTIONS ==
Read the user's message semantically and pick the action whose PURPOSE best matches what
the user is trying to accomplish. Do NOT pattern-match phrases — understand intent.
When two actions could fit, use action=direct and ask one short clarifying question.

-- JIRA (intent=create_jira) --
Purpose: User wants to open, raise, create, log, or file a ticket/task for tracking.
Always classify as create_jira regardless of thread context (device/host data in thread doesn't change this).
Params: title (strip @mentions; synthesize from thread if missing: device UDID + host + issue), description, issue_type (default "Task")

-- DEVICE CHECK (intent=device_check) --
Purpose: User wants to verify if a device is online, reachable, connected, or healthy — no other action implied.
Use host and udid from thread context if not in message.
Params: host (IP), udid, devices=[host,udid]

-- DEVICE REBOOT (intent=infra_issue, issue_category=reboot) --
Purpose: User wants to power-cycle or reboot the physical device itself.
This is for the device, not services — never use for LRR/container/service restarts.
Params: host (IP), udid

-- SERVICE RESTART (intent=infra_issue) --
Purpose: User wants to restart or fix a background service on the host.
Use judgment to map to the correct issue_category:
  lrr_down         → LRR (LambdaRemoteRunner) on macOS iOS hosts
  resigner_down    → Resigner service (port 6789)
  ihm_down         → IHM (iOS Host Manager)
  lrp_down         → LRP (LambdaRemoteProvider)
  reconciler_down  → Reconciler service
  rmdm_down        → RMDM on Ubuntu Android hosts
  rdtsa_down       → RDTSA on Ubuntu Android hosts
  android_container_down → adbd Docker container for a specific Android device
IMPORTANT: "check logs / show logs / tail logs / look at logs / what's in the log" for ANY
service → DO NOT classify as a service restart. Use action=direct and share the relevant
log path from SERVICE LOG PATHS below. The user wants to READ, not restart.
Params: host (IP address only — NEVER put UDID in host field), udid

-- HOST STATUS (intent=infra_issue, issue_category=host_service_status) --
Purpose: User wants an overall status check of all services on a host.
Params: host (IP), udid

-- ADB / ANDROID ISSUES (intent=infra_issue) --
Purpose: User reports an Android device problem or remark-based issue.
Use the ANDROID REMARK → ISSUE CATEGORY MAPPING section to pick the right issue_category.
Params: host (IP), udid

-- JENKINS JOB (intent=infra_issue, issue_category=jenkins_trigger) --
Purpose: User wants to run or trigger a Jenkins job.
Params:
  job_name: the user's description of the job (e.g. "ubuntu host setup", "device reboot", "sanity check").
            Do NOT try to match to a fixed list — pass the user's words exactly. The bot will search Jenkins.
  host_ips: space-separated IPs from message/thread
  environment: "stage" | "prod" (default "stage"; infer from context)
  job_params: JSON object with any extra params mentioned (HOST_IP, ENV, Tags, UDID, etc.)

-- DATABASE QUERY (intent=infra_issue, issue_category=db_query) --
Purpose: User wants to look up device data from the database.
Params:
  query: valid SELECT against lambda_lmds.device_host (ALWAYS use exactly this table name)
    Schema: udid, device_id, host_ip, name, os, os_version, status, dedicated_org,
            cleanup, manual, automation, features, remark, region, meta_data, adb_port, updated_at
    status values: active, busy, cleanup, faulty, maintenance, inactive, disposed
    os values: android, ios, fireos, tvos, roku, androidtv
    cleanup values: full, dedicated, adaptive
    region values: us-west-1, us-west-2, eu-west-1, ap-south-1, ap-south-2
    dedicated_org: NULL = public cloud, else org ID integer
    Default SELECT columns: udid, host_ip, status, remark, dedicated_org, cleanup, region, updated_at
    Always LIMIT 50 unless aggregate (COUNT/GROUP BY). NEVER INSERT/UPDATE/DELETE/DROP.
    CRITICAL — always use WHERE udid = '<udid>' when a device serial is present in the message.
      Device serial = any alphanumeric token (6-40 chars, may contain non-hex letters like J,K,L,M,N) that is NOT a common English word or IP address. Examples: 09191FDD4000FJ, R58M31YBKAE.
      NEVER add WHERE status = '...' when user says "check status" — they want to READ the status, not filter by it.
  If no clear filter (UDID/IP/org/status) → action=direct, ask what to look up.

-- DEVICE DISPOSE (intent=infra_issue, issue_category=device_dispose) --
Purpose: User wants to permanently retire/decommission/dispose a device.
Params:
  host_udid_pairs: "ip,udid ip,udid ..." (space-separated, one pair per device — build from thread if needed)
  jira: ticket ID (any format: TE-XXXXX, TTN-XXXXX, TPI-XXXXX) — scan current message AND full thread.
        REQUIRED: if missing, use action=direct — confirm what you understood (device count,
        environment, reason), then ask for Jira. Never send a bare "please provide Jira" sentence.
  environment: "prod" for production/live, "stage" for staging (default "stage")
  status: "disposed" | "inactive" (default "disposed")
  remark: one of ["Device battery bloated","Device screen is not working","Device needs to be repaired","Device is deprecated","others"]
  where_status: space-separated filter (default "active faulty maintenance")
  FOLLOW-UP: if thread shows bot already asked for Jira AND current message looks like a Jira ID →
  extract it + re-extract all other params from thread. Do NOT ask again.

-- DEVICE MIGRATE (intent=infra_issue, issue_category=device_migrate) --
Purpose: User wants to move devices between orgs or between private/public cloud.
"move to public cloud" / "remove from org" / "remove from private" → dedicated_org="NULL"
Params:
  udids: space-separated UDID list
  host_ips: space-separated IP list
  jira: ticket ID — REQUIRED, same ask-if-missing rule as device_dispose above.
  environment: "prod" | "stage" (default "stage")
  dedicated_org: org ID string or "NULL"
  status, cleanup, remark, where_status, manual, automation, features (leave "" if not specified)
  FOLLOW-UP: same as device_dispose above.

-- NOTE PATTERN (intent=note_pattern) --
Purpose: User wants to record a fix, solution, or pattern for future reference.
Params: udid, host, device_name, issue_type (e.g. "WDAstatus_failed"), pattern (one sentence),
        steps (list of fix steps), fixed (true/false), region

-- MONITORING --
Purpose: User asks to continuously watch/monitor/alert for a device.
→ action=direct: "I don't support continuous monitoring yet — once the device is back,
  mention me with `check now` and I'll verify. :eyes:"

== DC INFRASTRUCTURE ==
- macOS hosts: iOS devices — services: LRR, Resigner (port 6789), IHM, LRP, Reconciler (launchctl)
- Ubuntu hosts: Android devices in Docker (adbd_<UDID>) — services: RMDM, RDTSA, LRP, Reconciler (systemctl)
- AP=10.151.x.x  Dublin=10.100.x.x  US=10.146.x.x
- UDIDs/serials: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX, Android=any alphanumeric 6-40 char token (may include non-hex letters like J,K,L,M,N,P,R,S,T). Any standalone uppercase/alphanumeric token that is NOT a common English word or IP address is a device serial.
- Each host (PC Mini=Android, Mac Mini=iOS) manages up to 8 devices.

== DEVICE STATUSES ==
Active=ready | Busy=in-use | Cleanup=post-test clearing | Faulty=needs fix | Diagnosis=investigating

== ANDROID REMARK → ISSUE CATEGORY MAPPING ==
- "failed device sanity" → issue_category=adb_issue (run sanity Jenkins job)
- "device not found" / "No such container: adbd_*" → issue_category=device_disconnected (USB disconnected, restart container)
- "power_stayon is off" → issue_category=reboot (reboot device)
- "cleanup was not completed" → issue_category=adb_issue (reboot or check host reachability)
- "HttpProxy is set" / wifi not working → issue_category=network_issue (reset proxy job)
- "app install limit reached" / "failed to checkAppInstallAndReboot" → issue_category=app_crash (run gnirehtet install job)
- "exit status 255" → issue_category=android_container_down (container restart failed)
- "device ip mis-match" → issue_category=device_disconnected (MAC randomization not set to Phone MAC)
- "getdeviceip_failure" → issue_category=device_disconnected
- "K4S Health check Failed" / deployment → issue_category=jenkins_failure
- "PhysicalDensity/PhysicalSize not present" / meta column wrong → issue_category=db_mismatch
- "error in getting io.appium.uiautomator2.server" → issue_category=app_crash (automator app uninstalled)
- "screen_off_timeout is not 1800000" → issue_category=adb_issue (device locked)
- No devices on host (go-adb shows 0) → issue_category=device_disconnected (run resetusb.sh)

== SERVICE LOG PATHS ==
macOS:  LRR=/Users/ltadmin/Documents/LambdaRemoteRunner/lamda-remote-runner-<UDID>.log
        IHM=/Users/ltadmin/ios-host-manager/com.lambda.ihm.stdout
        LRP=/Users/ltadmin/Documents/LambdaRemoteProvider/lambda-remote-provider.log
        Reconciler=/Users/ltadmin/reconciler/com.lambda.reconciler.stdout
Ubuntu: RMDM=/home/ltadmin/rdtsa/logs/rdtsa.log
        LRP=/home/ltadmin/Documents/LambdaRemoteProvider/lambda-remote-provider.log
        Reconciler=/home/ltadmin/reconciler/runner.log

== KEY FIX STEPS (use in direct replies) ==
Reboot Android: `docker exec -it adbd_<UDID> adb -s <UDID> reboot`
Check container: `docker exec -it adbd_<UDID> adb devices`
Check host devices: `/usr/bin/go-adb listdevices | jq -r '.devicelist[].SerialNumber'`
Reset USB (0 devices on host): `cd Documents/devops_scripts/ && ./resetusb.sh`
Clear proxy: run Jenkins job `realdevice-reset-proxy` or set Wi-Fi proxy to None
Reboot iOS: `idevicediagnostics -u <UDID> restart` then reload plist
iOS WDA failed: reboot device → wait 30s → `reload_remoterunner_plist.sh <UDID>`
Pixel black screen: `adb shell am force-stop com.google.android.apps.nexuslauncher`
Check connectivity: run Jenkins job `realdevice-device-check` with `host_ip,UDID`
Sanity check: Jenkins job `realdevice-run-devops-sanity` with `host_ip,udid`
Restart container: Jenkins job `realdevice-restart-android-container`

== JENKINS JOBS ==
Do NOT hardcode job names. Pass the user's description as job_name — the bot searches Jenkins automatically.

== DEVICE SETUP CHECKLIST (for direct replies about faulty device) ==
1. Wi-Fi MAC → Phone MAC (not random)  2. USB mode = File Transfer  3. Enable: Stay Awake, USB Debugging, Wireless Debugging
4. Disable: ADB Auth Timeout, Verify Apps over USB  5. Chinese devices: disable Permission Monitoring
6. Xiaomi/Redmi: enable Install via USB + USB Debugging (Security Settings) — needs SIM
7. MDM: confirm 4 profiles (MITM Proxy, LT LittleProxy cert, Android LT Certificate, Android Restrictions)

For ACTION 1 (classify):
{"action":"classify","intent":"<intent>","confidence":0.0-1.0,"params":{"title":"","issue_type":"Task","assignee":"","cc":[],"ticket_key":"","issue_category":"","host":"","udid":"","hosts":[],"udids":[],"devices":[],"region":null,"host_type":null,"log_lines":20,"device_name":"","pattern":"","steps":[],"fixed":false,"query":""}}

log_lines: number of log lines to tail. Default 20. Extract from message if user says "last 100 lines", "show 200 lines", "tail 30", etc.
query: for issue_category=db_query ONLY — full SELECT SQL to run against device_host. When a device serial is present in the message, ALWAYS use WHERE udid='<serial>'. Never filter by status unless the user explicitly states a status value.

IMPORTANT — all list fields must contain plain strings only, never objects/dicts.
For device_check: set "host"="10.x.x.x", "udid"="<serial>", "devices":["10.x.x.x","<serial>"].
For multiple devices: "hosts":["10.x.x.1","10.x.x.2"], "udids":["serial1","serial2"] (parallel arrays).
For note_pattern: set "pattern"="one-sentence description", "steps":["step1","step2"], "fixed":true/false.

Valid intents: create_jira | assign_ticket | send_invite | infra_issue | device_check | note_pattern | unknown
Valid issue_categories: device_down | reboot | adb_issue | network_issue | db_mismatch | db_query |
jenkins_failure | app_crash | storage_issue | device_disconnected | lrr_down | resigner_down |
ihm_down | reconciler_down | lrp_down | rmdm_down | rdtsa_down | android_container_down |
cert_expired | host_service_status | device_dispose | device_migrate

For ACTION 2 (direct):
{"action":"direct","reply":"<slack-formatted response, *bold*, bullet points, max 8 lines>"}

Rules:
- Slack user IDs: <@U04UTG30V9A> → extract U... part (9-12 chars starting with U)
- IP prefix → region: 10.151→ap, 10.100→dublin, 10.146→us
- MISMATCH / device not found → intent=infra_issue, issue_category=device_disconnected
- Reply ONLY with valid JSON. No markdown fences, no explanation outside the JSON.
"""

# Gemini-only classification prompt (used as fallback when Claude CLI fails)
CLASSIFY_SYSTEM = """
You are Infra-Bot for LambdaTest's Real Device Cloud. Classify the intent of a Slack message.

Host context:
- macOS: iOS devices — services: LRR, Resigner (port 6789), IHM, LRP, Reconciler (launchctl plists)
- Ubuntu: Android devices in Docker (adbd_<UDID>) — services: RMDM, RDTSA, LRP, Reconciler (systemctl)
- AP=10.151.x.x, Dublin=10.100.x.x, US=10.146.x.x

Remark → issue_category mapping:
- "failed device sanity" → adb_issue
- "device not found" / "No such container" → device_disconnected
- "power_stayon is off" → reboot
- "cleanup was not completed" → adb_issue
- "HttpProxy is set" / wifi not working → network_issue
- "app install limit" / "failed to checkAppInstallAndReboot" → app_crash
- "exit status 255" → android_container_down
- "device ip mis-match" → device_disconnected
- "getdeviceip_failure" → device_disconnected
- "Health check Failed" / deployment stuck → jenkins_failure
- "PhysicalDensity/PhysicalSize not present" / meta column wrong → db_mismatch
- "io.appium.uiautomator2.server" → app_crash
- "screen_off_timeout" → adb_issue

Intents: create_jira | assign_ticket | send_invite | infra_issue | device_check | unknown

issue_categories: device_down|reboot|adb_issue|network_issue|db_mismatch|jenkins_failure|
app_crash|storage_issue|device_disconnected|lrr_down|resigner_down|ihm_down|reconciler_down|
lrp_down|rmdm_down|rdtsa_down|android_container_down|cert_expired|host_service_status

device_check intent: use when user says "check", "is it connected", "check now", "is it up".
  params must include: "host":"10.x.x.x", "udid":"<serial>", "devices":["10.x.x.x","<serial>"]

Return ONLY JSON:
{"intent":"...","confidence":0.0-1.0,"params":{"title":"","issue_type":"Task","assignee":"","cc":[],
"ticket_key":"","issue_category":"","host":"","udid":"","devices":[],"region":null,"host_type":null,
"hosts":[],"udids":[],"log_lines":20}}

Rules:
- UDIDs/serials: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX, Android=alphanumeric 6-20 chars (may include non-hex letters like J,K,L,M,N etc). ANY standalone uppercase/alphanumeric token that is not a common English word or IP is a device serial. IPs: 10.151→ap, 10.100→dublin, 10.146→us
- MISMATCH/device not found → device_disconnected
- Slack IDs: <@U...> format, 11 chars starting with U
- Use thread context for follow-up messages missing device info
- For device_check: extract host IP and UDID/serial from message or thread context
"""

# Root cause: only called for 3+ correlated signals
ROOT_CAUSE_SYSTEM = """
Infra-Bot root cause analysis. Multiple DC issues in same channel.
Output concise Slack-formatted hypothesis (max 6 lines):
:mag: *Root Cause Analysis*
• *Likely cause:* <one line>
• *Evidence:* <signals>
• *Recommended action:* <next step>
Use :rotating_light: for network/rack-level incidents.
"""


_INFRA_BOT_DIR = str(__import__("pathlib").Path(__file__).parent.parent)


def _extract_first_json(text: str) -> dict | None:
    """Return the first valid JSON object found in text, or None.

    Uses json.JSONDecoder.raw_decode so it stops at the end of the first
    complete object rather than greedily matching to the last closing brace.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None

# Atlassian MCP tools available after OAuth auth
_ATLASSIAN_MCP_TOOLS = [
    "mcp__atlassian__createJiraIssue",
    "mcp__atlassian__getVisibleJiraProjects",
    "mcp__atlassian__lookupJiraAccountId",
    "mcp__atlassian__getJiraIssue",
    "mcp__atlassian__editJiraIssue",
    "mcp__atlassian__searchJiraIssuesUsingJql",
]


def _call_claude_cli(
    prompt: str,
    timeout: int = 30,
    _log_action: str = "",
    allowed_tools: list[str] | None = None,
    model: str | None = None,
) -> str:
    """Run claude -p <prompt> as subprocess. Returns stdout text or raises.

    allowed_tools: list of MCP/tool names to pass via --allowedTools.
                   If None, no --allowedTools flag is added (default behaviour).
    model: Claude model ID to pass via --model. If None, CLI default is used.
    """
    import os
    _ensure_keychain_unlocked()
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/Users/ltadmin"),
        "USER": os.environ.get("USER", "ltadmin"),
        "LOGNAME": os.environ.get("LOGNAME", "ltadmin"),
    }
    cmd = [_CLAUDE_BIN, "-p", prompt]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=_INFRA_BOT_DIR,
        )
        duration = int((time.time() - t0) * 1000)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log_claude_call(prompt[:120], "", duration, False,
                            action=_log_action, error=err[:200])
            raise RuntimeError(f"claude CLI exited {result.returncode}: {err[:200]}")
        output = result.stdout.strip()
        log_claude_call(prompt[:120], output[:200], duration, True, action=_log_action)
        return output
    except subprocess.TimeoutExpired:
        duration = int((time.time() - t0) * 1000)
        log_claude_call(prompt[:120], "", duration, False,
                        action=_log_action, error="timeout")
        raise


def create_jira_via_mcp(
    title: str,
    description: str = "",
    assignee_account_id: str = "642196d2b05b4e3e7dab5355",
    project_key: str = "TE",
    priority: str = "Medium",
) -> dict:
    """Create a Jira ticket via Atlassian MCP (claude -p + --allowedTools).

    Returns dict with keys: success, key, url, error.
    Raises RuntimeError if claude CLI is not authenticated with Atlassian MCP.
    """
    prompt = (
        f"Create a Jira issue with these exact details using the mcp__atlassian__createJiraIssue tool:\n"
        f"- project_key: {project_key}\n"
        f"- summary: {title}\n"
        f"- issue_type: Simple Task\n"
        f"- assignee_id: {assignee_account_id}\n"
        f"- priority: {priority}\n"
        f"- description: {description or title}\n"
        f"- team field customfield_10001: b79a27b6-de36-4381-8d60-0b0c3e6477a7\n\n"
        f"After creating, reply with ONLY a JSON object: "
        f'{{\"key\": \"TE-XXX\", \"url\": \"https://lambdatest.atlassian.net/browse/TE-XXX\", \"success\": true}}'
    )
    try:
        raw = _call_claude_cli(
            prompt,
            timeout=60,
            _log_action="jira_mcp_create",
            allowed_tools=_ATLASSIAN_MCP_TOOLS,
        )
        # Extract first JSON object from response
        data = _extract_first_json(raw)
        if data and data.get("key"):
            key = data["key"]
            url = data.get("url", f"https://lambdatest.atlassian.net/browse/{key}")
            return {"success": True, "key": key, "url": url}
        # If no JSON, check for error signals
        if "not logged in" in raw.lower() or "please run /login" in raw.lower():
            raise RuntimeError("Atlassian MCP not authenticated — run `claude` interactively and authenticate via /mcp")
        logger.warning("create_jira_via_mcp unexpected output: %s", raw[:200])
        return {"success": False, "error": f"Unexpected response: {raw[:150]}"}
    except RuntimeError:
        raise
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timed out waiting for Jira ticket creation"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)[:200]}


class AIBrain:
    """Claude CLI primary (smart router) → Gemini fallback — used when local classifier fails."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        # Cache is Redis-backed — thread-safe, survives restarts, shared across workers

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _cache_get(self, key: str):
        from bot.memory.redis_client import get_redis  # noqa: PLC0415
        try:
            raw = get_redis().get(f"brain:cache:{key}")
            if raw:
                logger.debug("Brain cache hit: %s", key[:20])
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Brain cache get error (non-fatal): %s", exc)
        return None

    def _cache_set(self, key: str, value, ttl: int) -> None:
        from bot.memory.redis_client import get_redis  # noqa: PLC0415
        try:
            get_redis().setex(f"brain:cache:{key}", ttl, json.dumps(value))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Brain cache set error (non-fatal): %s", exc)

    def _cache_key(self, prefix: str, text: str, extra: str = "") -> str:
        return hashlib.md5(f"{prefix}:{text}:{extra}".encode()).hexdigest()

    def _build_contents(
        self, text: str, thread_history: list[dict] | None = None
    ) -> list[types.Content]:
        contents: list[types.Content] = []
        for msg in (thread_history or [])[-10:]:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=msg["content"])])
            )
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        return contents

    def _call_with_retry(self, fn, retries: int = 1):
        """Call fn(). On 429 quota errors, fail fast — do NOT sleep.

        Sleeping 60s on the Slack event handler thread blocks the entire
        thread-pool slot and causes backpressure under load. Quota errors
        reset on Gemini's own schedule; retrying immediately won't help.
        """
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
                if is_quota:
                    raise  # surface immediately as _quota_exceeded
                if attempt < retries - 1:
                    continue
                raise
        return None

    def classify(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Classify intent + extract params, or get a direct reply from Claude.

        Flow: Claude CLI (smart router) → Gemini fallback.
        Claude CLI can either classify (return structured params) or reply directly.
        Results cached 5 min.
        """
        # Hash actual thread content (not just length) to avoid collisions
        thread_ctx = ""
        for msg in (thread_history or [])[-10:]:
            role = "User" if msg.get("role") == "user" else "Bot"
            thread_ctx += f"{role}: {msg['content']}\n"
        cache_key = self._cache_key("classify", text[:200], thread_ctx[-200:])
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug("Classify cache hit")
            return cached

        # ── Claude CLI (primary — free, uses ltadmin subscription) ───────────
        try:
            thread_header = ("Thread context:\n" + thread_ctx) if thread_ctx else ""
            prompt = (
                f"{CLAUDE_ROUTER_SYSTEM}\n\n"
                f"{thread_header}"
                f"Message: {text}\n\n"
                f"Reply with ONLY valid JSON."
            )
            raw = _call_claude_cli(prompt, timeout=30, _log_action="router",
                                   model=_CLASSIFY_MODEL)

            # Extract first valid JSON object from response
            parsed = _extract_first_json(raw)
            if parsed is None:
                logger.warning("Claude returned invalid JSON, falling back to Gemini: %.200s", raw)
                return self.classify_gemini(text, thread_history)

            action = parsed.get("action", "classify")

            if action == "direct":
                # Claude is handling this directly — wrap as _direct_reply intent
                reply = parsed.get("reply", "")
                if reply:
                    result = {
                        "intent": "_direct_reply",
                        "confidence": 1.0,
                        "params": {"reply": reply},
                        "_source": "claude",
                    }
                    log_claude_call(prompt[:120], reply[:200], 0, True,
                                    action="direct", intent="_direct_reply")
                    logger.info("Claude CLI direct reply (len=%d)", len(reply))
                    self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                    return result

            elif action == "multi":
                # Claude identified multiple sequential actions
                actions_list = parsed.get("actions", [])
                if actions_list:
                    result = {
                        "intent": "_multi_action",
                        "confidence": 1.0,
                        "params": {"actions": actions_list},
                        "_source": "claude",
                    }
                    log_claude_call(prompt[:120], str(actions_list)[:200], 0, True,
                                    action="multi", intent="_multi_action")
                    logger.info("Claude CLI multi-action: %d steps", len(actions_list))
                    self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                    return result

            elif action == "classify":
                # Claude classified — return as standard classification dict
                intent_val = parsed.get("intent", "unknown")
                result = {
                    "intent": intent_val,
                    "confidence": parsed.get("confidence", 0.7),
                    "params": parsed.get("params", {}),
                    "_source": "claude",
                }
                log_claude_call(prompt[:120], raw[:200], 0, True,
                                action="classify", intent=intent_val)
                logger.info("Claude CLI classified: intent=%s confidence=%.2f",
                            result["intent"], result["confidence"])
                self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                return result

        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI timed out — falling back to Gemini")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude CLI classify failed: %s — falling back to Gemini", exc)

        # ── Gemini fallback (Claude failed entirely) ─────────────────────────
        return self.classify_gemini(text, thread_history)

    def classify_gemini(self, text: str, thread_history: list[dict] | None = None) -> dict:
        """Gemini-only classification — called as last-resort fallback."""
        # Fix #2: hash actual thread content, not just length — avoids cache collisions
        # between different threads that happen to have the same number of messages
        gemini_ctx = ""
        for msg in (thread_history or [])[-10:]:
            role = "User" if msg.get("role") == "user" else "Bot"
            gemini_ctx += f"{role}: {msg['content']}\n"
        cache_key = self._cache_key("classify_gemini", text[:200], gemini_ctx[-200:])
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            contents = self._build_contents(text, thread_history)

            def _call():
                resp = self.client.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=CLASSIFY_SYSTEM,
                        response_mime_type="application/json",
                        max_output_tokens=1024,
                    ),
                )
                return json.loads(resp.text)

            result = self._call_with_retry(_call)
            if result:
                result["_source"] = "gemini"
                logger.info("Gemini classified: intent=%s confidence=%.2f",
                            result.get("intent"), result.get("confidence", 0))
                self._cache_set(cache_key, result, _CLASSIFY_CACHE_TTL)
                return result
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned invalid JSON: %s", exc)
        except Exception as exc:  # noqa: BLE001
            is_quota = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if is_quota:
                logger.warning("Gemini quota exhausted")
                return {"intent": "_quota_exceeded", "params": {}, "confidence": 0.0,
                        "_source": "gemini"}
            logger.error("Gemini classify error: %s", exc)

        return {"intent": "unknown", "params": {}, "confidence": 0.0, "_source": "gemini"}

    def analyze_root_cause(self, signals_text: str) -> str:
        """Produce root cause diagnosis from correlated signals.

        Tries Claude CLI first, falls back to Gemini.
        Cached 10 min.
        """
        cache_key = self._cache_key("rca", signals_text)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # ── Claude CLI ────────────────────────────────────────────────────────
        try:
            prompt = f"{ROOT_CAUSE_SYSTEM}\n\nSignals:\n{signals_text}"
            result = _call_claude_cli(prompt, timeout=30, _log_action="rca")
            if result:
                self._cache_set(cache_key, result, _ROOT_CAUSE_CACHE_TTL)
                return result
        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI RCA timed out — falling back to Gemini")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude CLI RCA failed: %s — falling back to Gemini", exc)

        # ── Gemini fallback ──────────────────────────────────────────────────
        try:
            def _call():
                resp = self.client.models.generate_content(
                    model=MODEL,
                    contents=f"Signals:\n{signals_text}",
                    config=types.GenerateContentConfig(
                        system_instruction=ROOT_CAUSE_SYSTEM,
                        max_output_tokens=200,
                    ),
                )
                return resp.text.strip()

            result = self._call_with_retry(_call)
            if result:
                self._cache_set(cache_key, result, _ROOT_CAUSE_CACHE_TTL)
                return result
        except Exception as exc:  # noqa: BLE001
            logger.error("analyze_root_cause error: %s", exc)

        return ":mag: *Root Cause Analysis*\n• Multiple correlated signals — manual investigation recommended"

    def generate_unauthorized_greeting(self, user_message: str) -> str:
        """Return a friendly, context-aware reply for unauthorized users.

        Uses Claude CLI to craft a warm response that acknowledges what the
        user asked and explains the bot is in restricted access — without
        being cold or robotic.  Falls back to a static reply if Claude fails.
        """
        prompt = (
            "You are Infra-bot, a friendly infrastructure assistant for the LambdaTest mobile "
            "infra team. A team member who is NOT yet on the authorized-users list has just "
            "tagged you with the message below.\n\n"
            "Write a short, warm Slack reply (2-3 sentences max) that:\n"
            "1. Greets them by acknowledging what they asked / said.\n"
            "2. Explains you're currently in restricted early access for the mobile-infra team.\n"
            "3. Tells them you'll be rolling out to more of the team soon.\n"
            "Use a friendly, slightly casual tone. Use 1-2 relevant emojis. "
            "Do NOT mention 'unauthorized'. Do NOT use bullet points.\n\n"
            f"User's message: {user_message}\n\n"
            "Reply with ONLY the Slack message text, no JSON, no explanation."
        )
        try:
            result = _call_claude_cli(prompt, timeout=20, _log_action="unauthorized_greeting")
            if result and len(result) > 10:
                return result.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_unauthorized_greeting failed: %s", exc)

        # Static fallback
        return (
            ":robot_face: *Hey there!* I'm still in early access — not fully available to everyone just yet.\n"
            "I'll be rolling out to the wider team soon. Stay tuned! :rocket:"
        )


# ---------------------------------------------------------------------------
# Template responses — replaces generate_response() for known action outcomes
# ---------------------------------------------------------------------------

def _jira_created_reply(result: dict) -> str:
    if not result.get("success"):
        err = result.get("error", "unknown error")
        return f":x: Failed to create Jira ticket — {err}"
    # jira_client returns ticket_key; support both field names
    key   = result.get("ticket_key") or result.get("key") or "?"
    url   = result.get("url", "")
    title = result.get("title", "")
    link  = f"<{url}|{key}>" if url else key
    reply = f"Done :white_check_mark: Created {link}"
    if title:
        reply += f" — _{title}_"
    # Only show assignee if it looks like a Slack user ID (starts with U, 9-11 chars)
    slack_assignee = result.get("slack_assignee_id", "")
    if slack_assignee and slack_assignee.startswith("U") and 8 <= len(slack_assignee) <= 12:
        reply += f"\nAssigned to <@{slack_assignee}>"
    return reply


def _jira_assigned_reply(result: dict) -> str:
    if not result.get("success"):
        err = result.get("error", "unknown error")
        return f":x: Failed to assign ticket — {err}"
    key      = result.get("key", "?")
    assignee = result.get("assignee_id", "")
    reply = f"Done :white_check_mark: {key} assigned"
    if assignee:
        reply += f" to <@{assignee}>"
    return reply


def _unclear_reply(text: str) -> str:
    return (
        ":thinking_face: Not sure what you mean. Try:\n"
        "• `@infra-bot device 10.151.x.x is down`\n"
        "• `@infra-bot LRR down on 10.151.x.x`\n"
        "• `@infra-bot create jira: <title>`\n"
        "• `@infra-bot what can you do`"
    )


def _invite_reply(params: dict) -> str:
    return (
        ":calendar: Got it — calendar invite feature coming soon. "
        "For now, please create a Google Calendar event manually."
    )


# Module-level singleton
brain = AIBrain()
