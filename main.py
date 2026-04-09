"""Infra-Bot: Autonomous DC Infrastructure Assistant

Entry point — starts the Slack bot in Socket Mode.
"""
import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import settings
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


PID_FILE = "/Users/ltadmin/infra-bot/bot.pid"


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


if __name__ == "__main__":
    import atexit, os  # noqa: E401

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Kill any previously running instance before starting
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
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

    logger.info("Infra-Bot is running in Socket Mode")
    handler.start()
