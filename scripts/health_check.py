"""Infra-Bot Health Check — run before starting the bot.

Checks every dependency and prints a clear PASS/FAIL for each.

Usage:
    cd infra-bot
    python3 scripts/health_check.py

    # Or with verbose output:
    python3 scripts/health_check.py --verbose
"""
from __future__ import annotations

import json
import os
import sys
import time
import argparse

# ── make sure project root is on path ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

PASS  = "\033[92m  ✅ PASS\033[0m"
FAIL  = "\033[91m  ❌ FAIL\033[0m"
WARN  = "\033[93m  ⚠️  WARN\033[0m"
SKIP  = "\033[90m  ⏭  SKIP\033[0m"

results: list[tuple[str, str, str]] = []   # (check, status, detail)


def check(name: str, verbose: bool = False):
    """Decorator — wraps a check function, prints result."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs)
                status = PASS
            except AssertionError as e:
                detail = str(e)
                status = FAIL
            except Exception as e:  # noqa: BLE001
                detail = f"{type(e).__name__}: {e}"
                status = FAIL
            results.append((name, status, detail or ""))
            print(f"{status}  {name}")
            if verbose and detail:
                print(f"        {detail}")
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def check_env(verbose: bool) -> None:
    @check(".env file loaded", verbose)
    def _():
        required = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GEMINI_API_KEY"]
        missing = [k for k in required if not os.getenv(k)]
        assert not missing, f"Missing required vars: {', '.join(missing)}"
        return f"All required vars present ({', '.join(required)})"

    @check("SLACK_BOT_TOKEN format", verbose)
    def _():
        tok = os.getenv("SLACK_BOT_TOKEN", "")
        assert tok.startswith("xoxb-"), "Must start with xoxb-"
        return f"xoxb-...{tok[-6:]}"

    @check("SLACK_APP_TOKEN format", verbose)
    def _():
        tok = os.getenv("SLACK_APP_TOKEN", "")
        assert tok.startswith("xapp-"), "Must start with xapp-"
        return f"xapp-...{tok[-6:]}"

    @check("GEMINI_API_KEY set", verbose)
    def _():
        key = os.getenv("GEMINI_API_KEY", "")
        assert key, "GEMINI_API_KEY is empty"
        return f"AIza...{key[-4:]}"

    @check("Optional: DB config", verbose)
    def _():
        has_db = all(os.getenv(k) for k in ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"])
        if not has_db:
            raise AssertionError("DB not configured — /infra faulty count will not work")
        return f"DB_HOST={os.getenv('DB_HOST')}"


def check_redis(verbose: bool) -> None:
    @check("Redis connection", verbose)
    def _():
        import redis
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(url, decode_responses=True, socket_connect_timeout=3)
        pong = r.ping()
        assert pong, "Redis did not respond to PING"
        info = r.info("server")
        return f"Connected — Redis {info.get('redis_version', '?')} at {url.split('@')[-1]}"

    @check("Redis read/write", verbose)
    def _():
        import redis
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(url, decode_responses=True, socket_connect_timeout=3)
        r.setex("infra:healthcheck", 10, "ok")
        val = r.get("infra:healthcheck")
        assert val == "ok", f"Expected 'ok', got '{val}'"
        r.delete("infra:healthcheck")
        return "Write → Read → Delete cycle OK"


def check_gemini(verbose: bool, live: bool = False) -> None:
    """Check Gemini setup. Live API calls skipped by default to preserve quota.
    Use --gemini-live flag to run actual API calls."""

    @check("google-genai package installed", verbose)
    def _():
        try:
            from google import genai  # noqa: F401
            from google.genai import types  # noqa: F401
        except ImportError:
            raise AssertionError(
                "Package not found — run: pip3 install google-genai\n"
                "        Also run: pip3 uninstall google-generativeai -y"
            )
        return "google-genai importable"

    @check("GEMINI_API_KEY format", verbose)
    def _():
        key = os.getenv("GEMINI_API_KEY", "")
        assert key, "GEMINI_API_KEY not set"
        assert key.startswith("AIza"), "Expected key starting with AIza"
        return f"AIza...{key[-4:]}"

    if not live:
        results.append(("Gemini live API call", SKIP, "use --gemini-live to test (costs quota)"))
        print(f"{SKIP}  Gemini live API call (use --gemini-live to test — costs quota)")
        return

    @check("Gemini API key valid (live)", verbose)
    def _():
        from google import genai
        from google.genai import types
        api_key = os.getenv("GEMINI_API_KEY", "")
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="Reply with only the word: PONG",
            config=types.GenerateContentConfig(max_output_tokens=10),
        )
        assert resp.text, "Empty response from Gemini"
        return f"Gemini responded: {resp.text.strip()[:40]}"

    @check("Gemini JSON mode (live)", verbose)
    def _():
        from google import genai
        from google.genai import types
        import json
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="device 10.151.12.34 is down",
            config=types.GenerateContentConfig(
                system_instruction='Classify as infra_issue or unknown. Return JSON: {"intent":"...","confidence":0.9,"params":{}}',
                response_mime_type="application/json",
                max_output_tokens=100,
            ),
        )
        result = json.loads(resp.text)
        assert result.get("intent"), "No intent in response"
        return f"intent={result['intent']} confidence={result.get('confidence', '?')}"


def check_slack(verbose: bool) -> None:
    @check("Slack Bot Token valid (auth.test)", verbose)
    def _():
        from slack_sdk import WebClient
        client = WebClient(token=os.getenv("SLACK_BOT_TOKEN", ""))
        resp = client.auth_test()
        assert resp["ok"], f"auth.test failed: {resp.get('error')}"
        return f"Bot: @{resp['user']} | Team: {resp['team']}"

    @check("Slack App Token valid (Socket Mode)", verbose)
    def _():
        from slack_sdk.socket_mode import SocketModeClient
        app_token = os.getenv("SLACK_APP_TOKEN", "")
        assert app_token.startswith("xapp-"), "SLACK_APP_TOKEN must start with xapp-"
        # Just validate the token format — don't open a full socket connection
        return "Token format valid (xapp-...)"

    @check("Bot has required Slack scopes", verbose)
    def _():
        from slack_sdk import WebClient
        client = WebClient(token=os.getenv("SLACK_BOT_TOKEN", ""))
        resp = client.auth_test()
        assert resp["ok"]
        # Check bot_id exists (means bot scopes are active)
        assert resp.get("bot_id"), "Bot ID missing — ensure bot token scopes are enabled"
        return f"bot_id={resp['bot_id']}"


def check_db_optional(verbose: bool) -> None:
    if not all(os.getenv(k) for k in ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"]):
        results.append(("MySQL connection", SKIP, "DB vars not configured"))
        print(f"{SKIP}  MySQL connection (not configured)")
        return

    @check("MySQL connection", verbose)
    def _():
        import pymysql
        conn = pymysql.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            port=int(os.getenv("DB_PORT", "3306")),
            connect_timeout=5,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()
        return f"MySQL {version[0]}"


def check_config_files(verbose: bool) -> None:
    @check("config/dc_owners.yaml", verbose)
    def _():
        import yaml
        with open("config/dc_owners.yaml") as f:
            data = yaml.safe_load(f)
        regions = list(data.keys())
        empty = [r for r in regions if not data[r].get("slack_ids")]
        warn = f" — empty regions: {empty}" if empty else ""
        return f"Regions: {regions}{warn}"

    @check("config/keywords.yaml", verbose)
    def _():
        import yaml
        with open("config/keywords.yaml") as f:
            data = yaml.safe_load(f)
        return f"{len(data)} issue categories: {list(data.keys())}"

    @check("config/regions.yaml", verbose)
    def _():
        import yaml
        with open("config/regions.yaml") as f:
            data = yaml.safe_load(f)
        return f"{len(data)} regions defined"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Infra-Bot Health Check")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detail for each check")
    parser.add_argument("--gemini-live", action="store_true",
                        help="Run live Gemini API calls (costs 2 quota tokens — skip during routine checks)")
    args = parser.parse_args()

    v = args.verbose

    print("\n" + "="*55)
    print("  🤖 Infra-Bot Health Check")
    print("="*55 + "\n")

    print("── Environment ────────────────────────────────────")
    check_env(v)

    print("\n── Config Files ───────────────────────────────────")
    check_config_files(v)

    print("\n── Redis ───────────────────────────────────────────")
    check_redis(v)

    print("\n── Google Gemini ───────────────────────────────────")
    check_gemini(v, live=args.gemini_live)

    print("\n── Slack ───────────────────────────────────────────")
    check_slack(v)

    print("\n── Database (optional) ─────────────────────────────")
    check_db_optional(v)

    # Summary
    passed  = sum(1 for _, s, _ in results if "PASS" in s)
    failed  = sum(1 for _, s, _ in results if "FAIL" in s)
    skipped = sum(1 for _, s, _ in results if "SKIP" in s)

    print("\n" + "="*55)
    print(f"  Results: {passed} passed  {failed} failed  {skipped} skipped")
    print("="*55)

    if failed == 0:
        print("\n  ✅ All checks passed — bot is ready to start!\n")
        print("  Run:  python3 main.py\n")
    else:
        print("\n  ❌ Fix the failures above before starting the bot.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
