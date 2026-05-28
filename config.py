from functools import cached_property

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfluenceConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONFLUENCE_",
        env_file=".env",
        extra="ignore",
    )

    base_url: str = Field(default="https://your-domain.atlassian.net/wiki")
    username: str = Field(default="")
    api_token: str = Field(default="")
    space_key: str = Field(default="DEFAULT")
    timeout_seconds: int = Field(default=30)


class JiraConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JIRA_",
        env_file=".env",
        extra="ignore",
    )

    base_url: str = Field(default="https://your-domain.atlassian.net")
    username: str = Field(default="")
    api_token: str = Field(default="")
    project_key: str = Field(default="DEFAULT")
    timeout_seconds: int = Field(default=30)


class AzureOpenAIConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AZURE_OPENAI_",
        env_file=".env",
        extra="ignore",
    )

    endpoint: str = Field(default="https://your-resource.openai.azure.com")
    api_key: str = Field(default="")
    api_version: str = Field(default="2024-02-15-preview")
    deployment_name: str = Field(default="gpt-4o")
    timeout_seconds: int = Field(default=60)

    @field_validator("api_key")
    @classmethod
    def api_key_required(cls, value: str) -> str:
        if not value:
            raise ValueError("AZURE_OPENAI_API_KEY is required")
        return value


class ConfigManager:
    @cached_property
    def confluence(self) -> ConfluenceConfig:
        return ConfluenceConfig()

    @cached_property
    def jira(self) -> JiraConfig:
        return JiraConfig()

    @cached_property
    def azure_openai(self) -> AzureOpenAIConfig:
        return AzureOpenAIConfig()


config = ConfigManager()