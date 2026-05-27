from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ENV_FILE = Path(__file__).with_name(".env")


class ConfluenceAPIError(RuntimeError):
    def __init__(self, status_code: int, reason: str, response_body: str = "") -> None:
        self.status_code = status_code
        self.reason = reason
        self.response_body = response_body
        super().__init__(f"Confluence API request failed: HTTP {status_code} {reason}")


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self._parts.append(value)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


@dataclass(frozen=True)
class ConfluenceConfig:
    base_url: str
    email: str
    api_token: str
    timeout_seconds: int = 20


class ConfluenceClient:
    def __init__(self, config: ConfluenceConfig) -> None:
        self.config = config

    @classmethod
    def from_env(
        cls,
        env_file: str | Path = ENV_FILE,
        timeout_seconds: int = 20,
    ) -> "ConfluenceClient":
        load_dotenv(env_file)

        required_values = {
            "CONFLUENCE_URL": os.getenv("CONFLUENCE_URL"),
            "CONFLUENCE_USER": os.getenv("CONFLUENCE_USER"),
            "CONFLUENCE_API_TOKEN": os.getenv("CONFLUENCE_API_TOKEN"),
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

        config = ConfluenceConfig(
            base_url=required_values["CONFLUENCE_URL"].strip(),
            email=required_values["CONFLUENCE_USER"].strip(),
            api_token=required_values["CONFLUENCE_API_TOKEN"].strip(),
            timeout_seconds=timeout_seconds,
        )
        return cls(config)

    def get_current_user(self) -> dict[str, Any]:
        return self._request_confluence_json("GET", "user/current")

    def list_spaces(self, limit: int = 25) -> dict[str, Any]:
        return self._request_confluence_json("GET", "space", params={"limit": limit})

    def get_space_by_key(self, space_key: str) -> dict[str, Any]:
        return self._request_confluence_json("GET", f"space/{space_key}")

    def get_space_id(self, space_key: str) -> str:
        space = self.get_space_by_key(space_key)
        space_id = space.get("id")
        if not space_id:
            raise ValueError(f"Could not resolve Confluence space ID for space key: {space_key}")

        return str(space_id)

    def search_content(self, cql: str, limit: int = 10) -> dict[str, Any]:
        return self._request_confluence_json(
            "GET",
            "content/search",
            params={"cql": cql, "limit": limit},
        )

    def create_page(
        self,
        title: str,
        body: str,
        space_key: str | None = None,
        space_id: str | int | None = None,
        parent_id: str | int | None = None,
        representation: str = "storage",
        status: str = "current",
        subtype: str | None = "live",
        embedded: bool | None = None,
        private: bool | None = None,
        root_level: bool | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("Page title is required.")

        resolved_space_id = self._resolve_space_id(space_key=space_key, space_id=space_id)
        payload: dict[str, Any] = {
            "spaceId": resolved_space_id,
            "status": status,
            "title": title,
            "body": {
                "representation": representation,
                "value": body,
            },
        }
        if parent_id is not None:
            payload["parentId"] = str(parent_id)

        if subtype is not None:
            payload["subtype"] = subtype

        params = self._page_create_query_params(
            embedded=embedded,
            private=private,
            root_level=root_level,
        )
        return self._request_confluence_v2_json(
            "POST",
            "pages",
            params=params,
            payload=payload,
        )

    def create_child_page(
        self,
        parent_page_url: str,
        title: str,
        body: str,
        representation: str = "storage",
        status: str = "current",
        subtype: str | None = "live",
    ) -> dict[str, Any]:
        parent_page = self.get_page_context_by_url(parent_page_url)
        space_id = parent_page.get("space", {}).get("id")
        if not space_id:
            raise ValueError(f"Could not resolve parent page space ID: {parent_page_url}")

        return self.create_page(
            title=title,
            body=body,
            space_id=space_id,
            parent_id=parent_page["id"],
            representation=representation,
            status=status,
            subtype=subtype,
        )

    def get_page_by_id(
        self,
        page_id: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        return self._request_confluence_json(
            "GET",
            f"content/{page_id}",
            params={"expand": f"space,version,body.{body_format}"},
        )

    def get_page_by_title(
        self,
        space_key: str,
        title: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        response = self._request_confluence_json(
            "GET",
            "content",
            params={
                "spaceKey": space_key,
                "title": title,
                "type": "page",
                "expand": f"space,version,body.{body_format}",
            },
        )
        results = response.get("results", [])
        if not results:
            raise ValueError(f"No Confluence page found for {space_key}/{title}.")

        return results[0]

    def get_page_context_by_id(
        self,
        page_id: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        page = self.get_page_by_id(page_id, body_format=body_format)
        return self._page_context(page, page_id, body_format)

    def get_page_context_by_url(
        self,
        page_url: str,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        page_id = self._page_id_from_url(page_url)
        if page_id:
            page = self.get_page_by_id(page_id, body_format=body_format)
        else:
            space_key, title = self._page_space_and_title_from_url(page_url)
            page = self.get_page_by_title(space_key, title, body_format=body_format)

        return self._page_context(page, page_url, body_format)

    def _page_context(
        self,
        page: dict[str, Any],
        fallback_url: str,
        body_format: str,
    ) -> dict[str, Any]:
        body = page.get("body", {}).get(body_format, {})
        body_value = body.get("value", "")
        links = page.get("_links", {})
        space = page.get("space", {})
        version = page.get("version", {})

        return {
            "id": page.get("id"),
            "title": page.get("title"),
            "space": {
                "id": space.get("id"),
                "key": space.get("key"),
                "name": space.get("name"),
            },
            "version": {
                "number": version.get("number"),
                "when": version.get("when"),
                "by": version.get("by", {}).get("displayName"),
            },
            "url": self._absolute_confluence_url(links.get("webui", fallback_url)),
            "body_format": body.get("representation", body_format),
            "text": self._html_to_text(body_value),
            "raw_body": body_value,
        }

    def list_pages(self, limit: int = 25) -> dict[str, Any]:
        return self._request_confluence_json(
            "GET",
            "content/search",
            params={
                "cql": "type=page order by lastmodified desc",
                "expand": "content.space,content.version",
                "limit": limit,
            },
        )

    def _request_confluence_json(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(method, self._confluence_api_url(endpoint, params), payload)

    def _request_confluence_v2_json(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(method, self._confluence_v2_api_url(endpoint, params), payload)

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization_header(),
            "User-Agent": "confluence-webhook-client/1.0",
        }

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            url,
            data=body,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise ConfluenceAPIError(error.code, error.reason, error_body) from error

        return json.loads(response_body) if response_body else {}

    def _confluence_api_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        base = self.config.base_url.rstrip("/")
        if not base.endswith("/wiki"):
            base = f"{base}/wiki"

        url = f"{base}/rest/api/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"

        return url

    def _confluence_v2_api_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        base = self.config.base_url.rstrip("/")
        if not base.endswith("/wiki"):
            base = f"{base}/wiki"

        url = f"{base}/api/v2/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"

        return url

    def _resolve_space_id(
        self,
        space_key: str | None,
        space_id: str | int | None,
    ) -> str:
        if space_id is not None:
            return str(space_id)

        if space_key:
            return self.get_space_id(space_key)

        raise ValueError("Either space_id or space_key is required.")

    def _page_create_query_params(
        self,
        embedded: bool | None,
        private: bool | None,
        root_level: bool | None,
    ) -> dict[str, str] | None:
        params = {}
        if embedded is not None:
            params["embedded"] = str(embedded).lower()

        if private is not None:
            params["private"] = str(private).lower()

        if root_level is not None:
            params["root-level"] = str(root_level).lower()

        return params or None

    def _absolute_confluence_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url

        base = self.config.base_url.rstrip("/")
        if not base.endswith("/wiki"):
            base = f"{base}/wiki"

        return f"{base}/{path_or_url.lstrip('/')}"

    def _page_id_from_url(self, page_url: str) -> str | None:
        parsed_url = urlparse(page_url)
        query = parse_qs(parsed_url.query)
        if query.get("pageId"):
            return query["pageId"][0]

        path_parts = [unquote(part) for part in parsed_url.path.split("/") if part]
        for marker in ("pages", "edit-v2"):
            if marker not in path_parts:
                continue

            marker_index = path_parts.index(marker)
            if len(path_parts) > marker_index + 1 and path_parts[marker_index + 1].isdigit():
                return path_parts[marker_index + 1]

        return None

    def _page_space_and_title_from_url(self, page_url: str) -> tuple[str, str]:
        parsed_url = urlparse(page_url)
        query = parse_qs(parsed_url.query)
        if query.get("spaceKey") and query.get("title"):
            return query["spaceKey"][0], query["title"][0]

        path_parts = [unquote(part).replace("+", " ") for part in parsed_url.path.split("/") if part]
        if "display" in path_parts:
            display_index = path_parts.index("display")
            if len(path_parts) > display_index + 2:
                return path_parts[display_index + 1], path_parts[display_index + 2]

        raise ValueError(
            "Could not find a Confluence page reference in the URL. "
            "Use a URL like /wiki/spaces/SPACE/pages/123456/Page+Title "
            "or /wiki/pages/viewpage.action?pageId=123456."
        )

    def _html_to_text(self, html: str) -> str:
        parser = _HTMLTextExtractor()
        parser.feed(html)
        return parser.text()

    def _authorization_header(self) -> str:
        credentials = f"{self.config.email}:{self.config.api_token}".encode("utf-8")
        encoded_credentials = base64.b64encode(credentials).decode("ascii")
        return f"Basic {encoded_credentials}"
