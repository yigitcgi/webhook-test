from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class MSGraphAPIError(RuntimeError):
    def __init__(self, status_code: int, reason: str, response_body: str = "") -> None:
        self.status_code = status_code
        self.reason = reason
        self.response_body = response_body
        super().__init__(f"Microsoft Graph API request failed: HTTP {status_code} {reason}")


@dataclass(frozen=True)
class MSGraphConfig:
    access_token: str = field(repr=False)
    base_url: str = DEFAULT_GRAPH_BASE_URL
    timeout_seconds: int = 20


class MSGraphClient:
    def __init__(self, config: MSGraphConfig) -> None:
        self.config = config

    @classmethod
    def from_env(
        cls,
        env_file: str | Path = ENV_FILE,
        timeout_seconds: int = 20,
    ) -> "MSGraphClient":
        load_dotenv(env_file)

        access_token = os.getenv("MS_GRAPH_ACCESS_TOKEN")
        if not access_token:
            raise RuntimeError("Missing required environment variable: MS_GRAPH_ACCESS_TOKEN")

        base_url = os.getenv("MS_GRAPH_BASE_URL") or DEFAULT_GRAPH_BASE_URL
        config = MSGraphConfig(
            access_token=access_token.strip(),
            base_url=base_url.strip().rstrip("/"),
            timeout_seconds=timeout_seconds,
        )
        return cls(config)

    def health_check(self) -> dict[str, Any]:
        user = self.get_current_user(
            select=[
                "id",
                "displayName",
                "userPrincipalName",
                "mail",
            ]
        )
        return {
            "ok": True,
            "service": "ms_graph",
            "base_url": self.config.base_url,
            "user": {
                "id": user.get("id"),
                "display_name": user.get("displayName"),
                "user_principal_name": user.get("userPrincipalName"),
                "mail": user.get("mail"),
            },
        }

    def get_current_user(self, select: list[str] | str | None = None) -> dict[str, Any]:
        params = self._select_params(select)
        return self.request_json("GET", "me", params=params)

    def get_me(self, select: list[str] | str | None = None) -> dict[str, Any]:
        return self.get_current_user(select=select)

    def get_user(
        self,
        user_id_or_principal_name: str,
        select: list[str] | str | None = None,
    ) -> dict[str, Any]:
        endpoint = f"users/{quote(user_id_or_principal_name, safe='')}"
        return self.request_json("GET", endpoint, params=self._select_params(select))

    def list_users(
        self,
        select: list[str] | str | None = None,
        top: int = 25,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"$top": top}
        params.update(self._select_params(select))
        return self.request_json("GET", "users", params=params)

    def request_json(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return self._request_json(method=method, endpoint=endpoint, params=params, payload=payload)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.access_token}",
            "User-Agent": "ms-graph-client/1.0",
        }

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            self._api_url(endpoint, params=params),
            data=body,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise MSGraphAPIError(error.code, error.reason, error_body) from error

        return json.loads(response_body) if response_body else {}

    def _api_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        if endpoint.startswith(("http://", "https://")):
            url = endpoint
        else:
            url = f"{self.config.base_url}/{endpoint.lstrip('/')}"

        if params:
            url = f"{url}?{urlencode(params)}"

        return url

    def _select_params(self, select: list[str] | str | None) -> dict[str, str]:
        if not select:
            return {}

        if isinstance(select, str):
            select_value = select
        else:
            select_value = ",".join(select)

        return {"$select": select_value}


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query Microsoft Graph with an access token from .env.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["health", "me", "users", "request"],
        default="health",
        help="Graph command to run. Defaults to `health`.",
    )
    parser.add_argument("--select", help="Comma-separated Graph fields for $select.")
    parser.add_argument("--top", type=int, default=25, help="Maximum users to return for `users`.")
    parser.add_argument("--endpoint", help="Graph endpoint for `request`, for example: me/memberOf.")
    parser.add_argument("--method", default="GET", help="HTTP method for `request`.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = MSGraphClient.from_env()

    try:
        if args.command == "health":
            output = client.health_check()
        elif args.command == "users":
            output = client.list_users(select=split_csv(args.select), top=args.top)
        elif args.command == "request":
            if not args.endpoint:
                raise ValueError("--endpoint is required when command is `request`.")
            output = client.request_json(method=args.method, endpoint=args.endpoint)
        else:
            output = client.get_current_user(select=split_csv(args.select))
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    except MSGraphAPIError as error:
        print(error, file=sys.stderr)
        if error.status_code in (401, 403):
            print(
                "Check MS_GRAPH_ACCESS_TOKEN in .env and confirm the token has the required scopes.",
                file=sys.stderr,
            )
        if error.response_body:
            print(error.response_body[:1000], file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not reach Microsoft Graph API: {error.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Microsoft Graph API request timed out.", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("Microsoft Graph API returned a non-JSON response.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
