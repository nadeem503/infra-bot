"""Configuration loader for environment variables."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Slack
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")
    APPROVER_SLACK_ID: str = os.getenv("APPROVER_SLACK_ID", "U04UTG30V9A")

    # Google Gemini AI (free tier — get key at https://aistudio.google.com/apikey)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Escalation chain
    ESCALATION_APPROVER_ID: str = os.getenv("ESCALATION_APPROVER_ID", "")   # backup approver
    ESCALATION_WAIT_MINUTES: int = int(os.getenv("ESCALATION_WAIT_MINUTES", "15"))

    # SSH / Bastion
    BASTION_HOST: str = os.getenv("BASTION_HOST", "bastion-stage.lambdatest.com")
    BASTION_USER: str = os.getenv("BASTION_USER", "nadeemk")
    BASTION_KEY_PATH: str = os.getenv("BASTION_KEY_PATH", "")
    BASTION_PASS: str = os.getenv("BASTION_PASS", "")
    HOST_USER: str = os.getenv("HOST_USER", "ltadmin")
    HOST_PASS: str = os.getenv("HOST_PASS", "")

    # Jenkins
    JENKINS_URL: str = os.getenv("JENKINS_URL", "")
    JENKINS_USER: str = os.getenv("JENKINS_USER", "")
    JENKINS_API_TOKEN: str = os.getenv("JENKINS_API_TOKEN", "")

    # GitHub
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # Slack group/user to notify after auto-executing lifecycle workflows (device_dispose, device_migrate).
    # Set to a user group handle like "!subteam^S..." or a channel ID like "C..." or user ID "U..."
    MOBILE_INFRA_SLACK_ID: str = os.getenv("MOBILE_INFRA_SLACK_ID", "")

    # Database
    DB_HOST: str = os.getenv("DB_HOST", "")
    DB_USER: str = os.getenv("DB_USER", "")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    # SSH tunnel to reach DB (set when DB is not directly reachable from the bot host)
    DB_TUNNEL_HOST: str = os.getenv("DB_TUNNEL_HOST", "")
    DB_TUNNEL_PORT: int = int(os.getenv("DB_TUNNEL_PORT", "22"))
    DB_TUNNEL_USER: str = os.getenv("DB_TUNNEL_USER", "")
    DB_TUNNEL_PASS: str = os.getenv("DB_TUNNEL_PASS", "")

    # Jira
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_CLOUD_ID: str = os.getenv("JIRA_CLOUD_ID", "")  # must be set in .env
    JIRA_ASSIGNEE_ID: str = os.getenv("JIRA_ASSIGNEE_ID", "642196d2b05b4e3e7dab5355")

    # Token expiry dates (YYYY-MM-DD) — checked in Home Tab dashboard
    JIRA_TOKEN_EXPIRES: str = os.getenv("JIRA_TOKEN_EXPIRES", "")
    JENKINS_TOKEN_EXPIRES: str = os.getenv("JENKINS_TOKEN_EXPIRES", "")


settings = Settings()
