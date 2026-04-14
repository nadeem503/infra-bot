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

Read the Slack message and choose ONE of three actions:

ACTION 1 — classify: The message maps clearly to a single structured bot action.
Use this for device checks, service restarts, Jira tasks, ADB issues, reboots, etc.
Only classify when you are confident about the single intended action.

ACTION 2 — direct: Reply conversationally. Use for explanations, summaries, greetings,
capability questions, monitoring requests, or anything that is NOT a single clear action.
Also use this when the message is AMBIGUOUS — ask a short clarification question instead
of guessing. Example: "Did you want me to *reboot* the device, or just *check* if it's connected?"

ACTION 3 — AMBIGUITY RULE (most important): When a message could mean more than one action,
or you are not sure which action the user wants, ALWAYS use action=direct to ask first.
NEVER guess and run the wrong action. Examples of ambiguous messages:
- "reboot and check if up" → ask: reboot first, then check? or just check current state?
- "monitor and let me know when back" → monitoring isn't supported; use direct to explain and suggest
- "fix the device" → ask: what specifically? reboot, check connectivity, restart LRR?
- "do something about this device" → ask what action they want

== PRIORITY RULES ==
1. JIRA CREATION: "create ticket/jira/task/issue", "raise/file/log/open a ticket" → ALWAYS
   create_jira regardless of thread context. Strip bot @mentions from title. If no title,
   synthesize from thread: device UDID + host + issue. Ignore "mark as done" alongside create.

2. REBOOT: "reboot", "restart the device", "power cycle" → infra_issue, issue_category=reboot.
   Never classify a reboot request as device_check.

3. MONITORING: "monitor", "let me know when back", "notify when online", "watch and alert" →
   action=direct. Reply: "I don't support continuous monitoring yet — once the device is back,
   mention me with `check now` and I'll verify. :eyes:"

4. DEVICE CHECK: "check", "check now", "is it connected", "is it up" (no reboot context) →
   device_check using host/udid from thread.

5. AMBIGUOUS (multiple actions in one message, unclear intent) → action=direct, ask to clarify.

6. LRR/PLIST RESTART: "restart LRR", "restart the plist", "reload plist", "reload LRR" →
   intent=infra_issue, issue_category=lrr_down.
   CRITICAL: Always set "host" to the IP address (e.g. "10.x.x.x") and "udid" to the device UDID.
   NEVER set host to a UDID string. If thread context has a UDID, always pass it as "udid", not "host".
   If no specific UDID is mentioned but thread has one, use it — do NOT leave udid blank.

7. JENKINS JOB TRIGGER: "run [job] job", "trigger [job]", "execute jenkins", "kick off jenkins",
   "run DSA job", "run sanity", "run device check", "run reboot job", "fire jenkins" →
   intent=infra_issue, issue_category=jenkins_trigger.
   Extract:
   - job_name: the Jenkins job name. Map common aliases:
       "DSA android" / "DSA" / "device sanity android" → ask user for exact job name if not in known list
       "sanity" / "devops sanity" → "realdevice-run-devops-sanity"
       "device check" → "realdevice-device-check"
       "restart container" / "android container" → "realdevice-restart-android-container"
       "reboot job" / "device reboot" → "realdevice-device-reboot"
       "reset proxy" → "realdevice-reset-proxy"
       "gnirehtet" / "apk install" → "realdevice-ubuntu-gnirehtet-apk-install-prod"
       "ucturbo" → "realdevice-ubuntu-install-ucturbo"
       "screenshot android" → "realdevice-takescreen-android-devices"
       "screenshot ios" → "realdevice-takescreen-ios-devices"
       "android uptime" → "realdevice-android-uptime"
     If the job name is ambiguous or NOT in the known list above, use action=direct to ask:
     "Which Jenkins job would you like to run? Known jobs: [list]"
   - host_ips: space-separated host IPs from message/thread (used as job parameter)
   - environment: "stage" | "prod" (default "stage"; infer from context)
   - job_params: JSON object of additional job parameters if mentioned (e.g. {"HOST_IP": "10.x.x.x", "ENV": "PROD"})
     Always include host IPs and environment in job_params.
   IMPORTANT: "run [X] job on [hosts]" with a clear job name → classify directly. Do NOT fall back to adb_issue.

9. DATABASE QUERY: "check in DB", "check database", "check status in DB", "query device",
   "show DB record", "what's in DB for", "DB status", "check in database", "look up in DB",
   "check device in database" → intent=infra_issue, issue_category=db_query.
   Extract:
   - query: a valid SELECT SQL against lambda_lmds.device_host.
     Schema columns: udid, device_id, host_ip, status, dedicated_org, cleanup, manual,
                     automation, remark, meta_data, adb_port
     Default SELECT columns (always use unless user asks for specific fields):
       udid, host_ip, status, remark, dedicated_org, cleanup
     Examples:
       "check UDID123 in DB"   → SELECT udid, host_ip, status, remark, dedicated_org, cleanup FROM lambda_lmds.device_host WHERE udid IN ('UDID123')
       "check 10.x.x.x in DB" → SELECT udid, host_ip, status, remark, dedicated_org, cleanup FROM lambda_lmds.device_host WHERE host_ip = '10.x.x.x'
       "check UDIDs A B C in DB" → SELECT udid, host_ip, status, remark, dedicated_org, cleanup FROM lambda_lmds.device_host WHERE udid IN ('A','B','C')
     ALWAYS build a valid SELECT. NEVER generate INSERT/UPDATE/DELETE/DROP.
   If no UDID or host IP is mentioned → action=direct, ask: "Which device UDID or host IP should I look up?"

8. DEVICE DISPOSE: "dispose device", "mark as disposed", "device is dead", "battery bloated",
   "send to graveyard", "retire device", "decommission" → infra_issue, issue_category=device_dispose.
   Extract:
   - host_udid_pairs: space-separated "host_ip,udid" string (e.g. "10.151.1.1,UDID1 10.151.1.2,UDID2")
     Build from host IPs and UDIDs mentioned in message/thread. Format MUST be "ip,udid" per device.
   - jira: Jira ticket ID — any format accepted: TE-XXXXX, TTN-XXXXX, TPI-XXXXX etc.
     Look in the CURRENT message first, then scan the ENTIRE thread for any "XX-NNNN" pattern.
   - environment: "stage" | "prod" (default "stage"; infer from context or ask).
     "prod env", "production", "live" → "prod". "staging", "stage env" → "stage".
   - status: "disposed" | "inactive" (default "disposed")
   - remark: one of ["Device battery bloated","Device screen is not working",
     "Device needs to be repaired","Device is deprecated","others"] — map user words to these
   - where_status: space-separated status filter (default "active faulty maintenance")
   IMPORTANT: if jira ticket is missing, use action=direct. Write a conversational reply that:
     1. Confirms what you understood — device count, environment, remark/reason.
        Example: "Got it — I'll mark 5 device(s) as *disposed* on *stage* (reason: battery bloated)."
     2. Asks for Jira on the next line.
        Example: "Before I proceed, please provide a *Jira ticket ID* (e.g. `TE-12345` or `TTN-12345`) for the audit trail. :memo:"
     Use actual extracted values. Never use a bare standalone "Please provide a Jira ticket ID" sentence.
   IMPORTANT FOLLOW-UP: if the thread shows bot previously asked for Jira AND the current message
   looks like a Jira ID (e.g. "TE-11204", "TTN-9999"), treat this as a follow-up: extract jira from
   the current message AND re-extract ALL other params (UDIDs, host_ips, environment, dedicated_org,
   etc.) from the thread context. Do NOT ask for Jira again.

8. DEVICE MIGRATION / ORG ASSIGNMENT: "move device to org", "assign to private cloud",
   "migrate device", "move to public cloud", "update dedicated_org", "org assignment",
   "device movement" → infra_issue, issue_category=device_migrate.
   Extract:
   - udids: space-separated UDID list (for WHERE udid IN (...))
   - host_ips: space-separated host IP list (for WHERE host_ip IN (...))
   - jira: Jira ticket ID — any format accepted: TE-XXXXX, TTN-XXXXX, TPI-XXXXX etc.
     Look in the CURRENT message first, then scan the ENTIRE thread for any "XX-NNNN" pattern.
   - environment: "stage" | "prod" (default "stage"). Always lowercase — bot uppercases for workflow.
     "prod env", "production", "live" → "prod". "staging", "stage env" → "stage".
   - dedicated_org: org ID (integer as string) or "NULL" if moving to public cloud
   - status: new status if changing (active|maintenance|faulty|disposed|inactive) or ""
   - cleanup: "full" | "dedicated" | "adaptive" or "" if not specified
   - remark: free text describing the migration reason
   - where_status: space-separated status filter (default "active faulty maintenance")
   - manual / automation / features: leave as "" unless explicitly specified
   IMPORTANT: if jira ticket is missing, use action=direct. Write a conversational reply that:
     1. Confirms what you understood — device count, environment, org change.
        Example: "Got it — I can migrate these 13 devices to public cloud (`dedicated_org = NULL`) on *prod*."
     2. Asks for Jira on the next line.
        Example: "Before I proceed, please provide a *Jira ticket ID* (e.g. `TE-12345` or `TTN-12345`) for the audit trail. :memo:"
     Use actual extracted values. Never use a bare standalone "Please provide a Jira ticket ID" sentence.
   IMPORTANT FOLLOW-UP: if the thread shows bot previously asked for Jira AND the current message
   looks like a Jira ID (e.g. "TE-11204", "TTN-9999"), treat this as a follow-up: extract jira from
   the current message AND re-extract ALL other params (udids, host_ips, environment, dedicated_org,
   etc.) from the thread context. Do NOT ask for Jira again.
   IMPORTANT: "move to public cloud" / "remove from org" → dedicated_org="NULL"

9. NOTE PATTERN / MARK FIXED: "note the pattern", "this is fixed", "fixed nothing to do",
   "note this for future", "remember this fix", "mark as fixed", "note this", "save this fix" →
   intent=note_pattern.
   Extract from the message AND thread context:
   - udid: device UDID (from thread if not in message)
   - host: host IP (from thread if not in message)
   - device_name: device model/name if mentioned (e.g. "iPhone 15 Plus")
   - issue_type: short slug of the issue (e.g. "WDAstatus_failed", "lrr_down", "adb_offline")
   - pattern: one-sentence description of what the issue was and what fixed it
   - steps: list of fix step strings (commands or actions, in order)
   - fixed: true if user says "fixed", "nothing to do", "resolved", "done"; false otherwise
   - region: infer from host IP (10.151→ap, 10.100→dublin, 10.146→us) or null

== DC INFRASTRUCTURE ==
- macOS hosts: iOS devices — services: LRR, Resigner (port 6789), IHM, LRP, Reconciler (launchctl)
- Ubuntu hosts: Android devices in Docker (adbd_<UDID>) — services: RMDM, RDTSA, LRP, Reconciler (systemctl)
- AP=10.151.x.x  Dublin=10.100.x.x  US=10.146.x.x
- UDIDs: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX (8hex-dash-16hex). Android serials: alphanumeric 6-20 chars.
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

== IMPORTANT JENKINS JOBS ==
realdevice-run-devops-sanity | realdevice-device-check | realdevice-restart-android-container
realdevice-device-reboot | realdevice-update-device-status | realdevice-reset-proxy
realdevice-ubuntu-gnirehtet-apk-install-prod | realdevice-ubuntu-install-ucturbo
realdevice-takescreen-android-devices | realdevice-takescreen-ios-devices | realdevice-android-uptime
All at: https://jenkins-stage.lambdatestinternal.com/job/<job-name>/

== DEVICE SETUP CHECKLIST (for direct replies about faulty device) ==
1. Wi-Fi MAC → Phone MAC (not random)  2. USB mode = File Transfer  3. Enable: Stay Awake, USB Debugging, Wireless Debugging
4. Disable: ADB Auth Timeout, Verify Apps over USB  5. Chinese devices: disable Permission Monitoring
6. Xiaomi/Redmi: enable Install via USB + USB Debugging (Security Settings) — needs SIM
7. MDM: confirm 4 profiles (MITM Proxy, LT LittleProxy cert, Android LT Certificate, Android Restrictions)

For ACTION 1 (classify):
{"action":"classify","intent":"<intent>","confidence":0.0-1.0,"params":{"title":"","issue_type":"Task","assignee":"","cc":[],"ticket_key":"","issue_category":"","host":"","udid":"","hosts":[],"udids":[],"devices":[],"region":null,"host_type":null,"log_lines":20,"device_name":"","pattern":"","steps":[],"fixed":false}}

log_lines: number of log lines to tail. Default 20. Extract from message if user says "last 100 lines", "show 200 lines", "tail 30", etc.

IMPORTANT — all list fields must contain plain strings only, never objects/dicts.
For device_check: set "host"="10.x.x.x", "udid"="<serial>", "devices":["10.x.x.x","<serial>"].
For multiple devices: "hosts":["10.x.x.1","10.x.x.2"], "udids":["serial1","serial2"] (parallel arrays).
For note_pattern: set "pattern"="one-sentence description", "steps":["step1","step2"], "fixed":true/false.

Valid intents: create_jira | assign_ticket | send_invite | infra_issue | device_check | note_pattern | unknown
Valid issue_categories: device_down | reboot | adb_issue | network_issue | db_mismatch |
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
- UDIDs: iOS old=40 hex chars, iOS new=XXXXXXXX-XXXXXXXXXXXXXXXX (8hex-dash-16hex). IPs: 10.151→ap, 10.100→dublin, 10.146→us
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
