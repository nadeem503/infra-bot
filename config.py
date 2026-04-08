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
    BASTION_HOST: str = os.getenv("BASTION_HOST", "")
    BASTION_USER: str = os.getenv("BASTION_USER", "")
    BASTION_KEY_PATH: str = os.getenv("BASTION_KEY_PATH", "")

    # Jenkins
    JENKINS_URL: str = os.getenv("JENKINS_URL", "")
    JENKINS_USER: str = os.getenv("JENKINS_USER", "")
    JENKINS_API_TOKEN: str = os.getenv("JENKINS_API_TOKEN", "")

    # GitHub
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # Database
    DB_HOST: str = os.getenv("DB_HOST", "")
    DB_USER: str = os.getenv("DB_USER", "")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))

    # Jira
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_CLOUD_ID: str = os.getenv("JIRA_CLOUD_ID", "3def4f78-101d-4614-9b65-735c17a98a93")
    JIRA_ASSIGNEE_ID: str = os.getenv("JIRA_ASSIGNEE_ID", "642196d2b05b4e3e7dab5355")

    # Token expiry dates (YYYY-MM-DD) — checked in Home Tab dashboard
    JIRA_TOKEN_EXPIRES: str = os.getenv("JIRA_TOKEN_EXPIRES", "")
    JENKINS_TOKEN_EXPIRES: str = os.getenv("JENKINS_TOKEN_EXPIRES", "")


settings = Settings()
