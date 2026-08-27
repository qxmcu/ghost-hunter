"""
config.py
Manages the application configuration using pydantic-settings.
Extracts environment variables, providing validation and defaults.
"""

import logging
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The App ID obtained when registering your GitHub App.
    GITHUB_APP_ID: str = ""

    # The RSA private key generated for your GitHub App. Must contain literal '\n' or actual newlines.
    GITHUB_PRIVATE_KEY: str = ""

    # The secret configured in the GitHub App webhook settings, used to cryptographically verify payloads.
    WEBHOOK_SECRET: str = ""

    # API key for the LLM provider (e.g., OpenAI API Key)
    LLM_API_KEY: str = ""
    
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "gpt-4o"

    # Standard Python logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    LOG_LEVEL: str = "INFO"

    # SettingsConfigDict defines that we pull variables from the .env file
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    def get_clean_private_key(self) -> str:
        """
        Helper to ensure the private key has actual newlines,
        as environment variables might escape them as literal '\n'.
        """
        return self.GITHUB_PRIVATE_KEY.replace("\\n", "\n")


# Instantiate settings globally
settings = Settings()

# Configure logging application-wide based on the setting
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
