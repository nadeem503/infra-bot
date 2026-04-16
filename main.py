"""Infra-Bot: Autonomous DC Infrastructure Assistant

Entry point — starts the Slack bot in Socket Mode.
"""
import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import settings
from bot.claude_config.installer import install_claude_config
from bot.listeners.message_listener import register_message_listeners
from bot.listeners.action_listener import register_action_listeners
from bot.listeners.slash_listener import register_slash_listeners
from bot.listeners.reaction_listener import register_reaction_listeners
from bot.listeners.home_tab_listener import register_home_tab_listener
from utils.activity_log import log_bot_session
from utils.logger import get_logger

logger = get_logger(__name__)


def create_app() -> App:
    app = App(token=settings.SLACK_BOT_TOKEN)
    register_message_listeners(app)
    register_action_listeners(app)
    register_slash_listeners(app)
    register_reaction_listeners(app)
    register_home_tab_listener(app)
    return app


PID_FILE = str(__import__("pathlib").Path(__file__).parent / "bot.pid")


def _write_pid() -> None:
    import os
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid() -> None:
    import os
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _validate_config() -> None:
    """Fail fast if required env vars are missing."""
    required = {
        "SLACK_BOT_TOKEN": settings.SLACK_BOT_TOKEN,
        "SLACK_APP_TOKEN": settings.SLACK_APP_TOKEN,
        "GEMINI_API_KEY": settings.GEMINI_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)} — check your .env")


if __name__ == "__main__":
    import atexit, os  # noqa: E401

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _validate_config()
    install_claude_config()

    # Kill any previously running instance before starting
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as _f:
                old_pid = int(_f.read().strip())
            os.kill(old_pid, 0)          # check if process exists
            os.kill(old_pid, 15)         # SIGTERM
            import time; time.sleep(1)   # give it a moment to exit
            logger.info("Stopped previous instance (PID %d)", old_pid)
        except (ProcessLookupError, ValueError):
            pass                         # process already gone
        except PermissionError:
            logger.warning("Could not stop PID %d — permission denied", old_pid)

    _write_pid()
    atexit.register(_remove_pid)

    logger.info("Starting Infra-Bot (PID %d)...", os.getpid())
    log_bot_session("start")

    app = create_app()
    handler = SocketModeHandler(app, settings.SLACK_APP_TOKEN)

    # Start Jenkins build status poller (checks every 5 min, posts to thread when done)
    from bot.workers.jenkins_poller import start_poller  # noqa: PLC0415
    from slack_sdk import WebClient  # noqa: PLC0415
    _slack_client = WebClient(token=settings.SLACK_BOT_TOKEN)
    start_poller(_slack_client)

    logger.info("Infra-Bot is running in Socket Mode")

    # BrokenPipeError loop detector — if >5 pipe errors in 60s, exit so watchdog restarts
    import threading, time as _time  # noqa: E401
    _pipe_errors: list[float] = []
    _original_error_handler = app.error

    @app.error
    def _pipe_guard(error):
        if isinstance(error, BrokenPipeError):
            now = _time.monotonic()
            _pipe_errors.append(now)
            # Keep only errors in the last 60s
            _pipe_errors[:] = [t for t in _pipe_errors if now - t < 60]
            if len(_pipe_errors) > 5:
                logger.error("BrokenPipeError loop detected (%d errors in 60s) — exiting for watchdog restart", len(_pipe_errors))
                threading.Thread(target=lambda: (_time.sleep(1), os._exit(1)), daemon=True).start()
        if _original_error_handler:
            return _original_error_handler(error)

    try:
        handler.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("SocketModeHandler crashed: %s — exiting for watchdog restart", exc)
        raise SystemExit(1) from exc
