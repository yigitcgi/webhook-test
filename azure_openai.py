from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

if TYPE_CHECKING:
    from openai import AzureOpenAI


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_API_VERSION = "2024-02-15-preview"
TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


@dataclass(frozen=True)
class AzureOpenAIConfig:
    endpoint: str
    api_key: str = field(repr=False)
    model: str
    api_version: str = DEFAULT_API_VERSION
    ssl_cert_check: bool = True
    timeout_seconds: float | None = None
    max_retries: int | None = None


def load_config(env_file: str | Path = ENV_FILE) -> AzureOpenAIConfig:
    load_dotenv(env_file)

    required_values = {
        "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
        "AZURE_OPENAI_API_KEY": os.getenv("AZURE_OPENAI_API_KEY"),
        "AZURE_OPENAI_MODEL": os.getenv("AZURE_OPENAI_MODEL"),
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    return AzureOpenAIConfig(
        endpoint=required_values["AZURE_OPENAI_ENDPOINT"].strip().rstrip("/"),
        api_key=required_values["AZURE_OPENAI_API_KEY"].strip(),
        model=required_values["AZURE_OPENAI_MODEL"].strip(),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION).strip(),
        ssl_cert_check=env_bool(
            "AZURE_OPENAI_SSL_CERT_CHECK",
            default=env_bool("SSL_CERT_CHECK", default=True),
        ),
        timeout_seconds=env_float("AZURE_OPENAI_TIMEOUT"),
        max_retries=env_int("AZURE_OPENAI_MAX_RETRIES"),
    )


def get_client(config: AzureOpenAIConfig | None = None) -> "AzureOpenAI":
    try:
        from openai import AzureOpenAI
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "The openai package is not installed. Run `pip install -r requirements.txt` first."
        ) from error

    config = config or load_config()
    client_kwargs: dict[str, Any] = {
        "api_key": config.api_key,
        "api_version": config.api_version,
        "azure_endpoint": config.endpoint,
    }

    if config.timeout_seconds is not None:
        client_kwargs["timeout"] = config.timeout_seconds

    if config.max_retries is not None:
        client_kwargs["max_retries"] = config.max_retries

    http_client = build_http_client(config.ssl_cert_check)
    if http_client is not None:
        client_kwargs["http_client"] = http_client

    return AzureOpenAI(**client_kwargs)


def get_model(config: AzureOpenAIConfig | None = None) -> str:
    return (config or load_config()).model


def create_chat_completion(
    messages: list[dict[str, str]],
    config: AzureOpenAIConfig | None = None,
    **kwargs: Any,
) -> Any:
    config = config or load_config()
    client = get_client(config)
    return client.chat.completions.create(
        model=config.model,
        messages=messages,
        **kwargs,
    )


def create_response(
    input: str | list[Any],
    config: AzureOpenAIConfig | None = None,
    **kwargs: Any,
) -> Any:
    config = config or load_config()
    client = get_client(config)
    return client.responses.create(
        model=config.model,
        input=input,
        **kwargs,
    )


def build_http_client(ssl_cert_check: bool) -> Any | None:
    if ssl_cert_check:
        return None

    warnings.warn(
        "Azure OpenAI SSL certificate verification is disabled. "
        "Use this only for local development or controlled network environments.",
        RuntimeWarning,
        stacklevel=2,
    )

    import httpx

    return httpx.Client(verify=False)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized_value = value.strip().lower()
    if normalized_value in TRUE_VALUES:
        return True

    if normalized_value in FALSE_VALUES:
        return False

    raise ValueError(f"{name} must be one of: true/false, yes/no, 1/0, on/off.")


def env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None

    return float(value.strip())


def env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None

    return int(value.strip())
