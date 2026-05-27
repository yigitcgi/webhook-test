from __future__ import annotations

import base64
import html
import json
import os
import time
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

    def create_page_from_html(
        self,
        title: str,
        html_body: str,
        space_key: str | None = None,
        space_id: str | int | None = None,
        parent_id: str | int | None = None,
        status: str = "current",
        subtype: str | None = "live",
        embedded: bool | None = None,
        private: bool | None = None,
        root_level: bool | None = None,
    ) -> dict[str, Any]:
        return self.create_page(
            title=title,
            body=self._html_to_storage_body(html_body),
            space_key=space_key,
            space_id=space_id,
            parent_id=parent_id,
            representation="storage",
            status=status,
            subtype=subtype,
            embedded=embedded,
            private=private,
            root_level=root_level,
        )

    def create_page_from_template(
        self,
        template_id: str,
        title: str,
        space_key: str | None = None,
        space_id: str | int | None = None,
        parent_id: str | int | None = None,
        replacements: dict[str, Any] | None = None,
        status: str = "current",
        subtype: str | None = "live",
    ) -> dict[str, Any]:
        template = self.get_content_template(
            template_id,
            expand=("body.storage", "body.view"),
        )
        template_body = self._template_body_value(template, "storage")
        if template_body is None:
            template_body = self._template_body_value(template, "view")

        if template_body is None:
            raise ValueError(f"Template {template_id} does not include a body.")

        return self.create_page_from_html(
            title=title,
            html_body=self._apply_template_replacements(template_body, replacements),
            space_key=space_key,
            space_id=space_id,
            parent_id=parent_id,
            status=status,
            subtype=subtype,
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

    def create_child_page_from_html(
        self,
        parent_page_url: str,
        title: str,
        html_body: str,
        status: str = "current",
        subtype: str | None = "live",
    ) -> dict[str, Any]:
        parent_page = self.get_page_context_by_url(parent_page_url)
        space_id = parent_page.get("space", {}).get("id")
        if not space_id:
            raise ValueError(f"Could not resolve parent page space ID: {parent_page_url}")

        return self.create_page_from_html(
            title=title,
            html_body=html_body,
            space_id=space_id,
            parent_id=parent_page["id"],
            status=status,
            subtype=subtype,
        )

    def create_child_page_from_template(
        self,
        parent_page_url: str,
        template_id: str,
        title: str,
        replacements: dict[str, Any] | None = None,
        status: str = "current",
        subtype: str | None = "live",
    ) -> dict[str, Any]:
        parent_page = self.get_page_context_by_url(parent_page_url)
        space_id = parent_page.get("space", {}).get("id")
        if not space_id:
            raise ValueError(f"Could not resolve parent page space ID: {parent_page_url}")

        return self.create_page_from_template(
            template_id=template_id,
            title=title,
            space_id=space_id,
            parent_id=parent_page["id"],
            replacements=replacements,
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

    def list_content_templates(
        self,
        space_key: str | None = None,
        template_kind: str = "page",
        start: int = 0,
        limit: int = 25,
        expand: str | list[str] | tuple[str, ...] | None = ("body.storage", "body.view"),
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "start": start,
            "limit": limit,
        }
        if space_key:
            params["spaceKey"] = space_key

        expand_value = self._expand_value(expand)
        if expand_value:
            params["expand"] = expand_value

        return self._request_confluence_json(
            "GET",
            f"template/{template_kind}",
            params=params,
        )

    def get_content_template(
        self,
        template_id: str,
        expand: str | list[str] | tuple[str, ...] | None = ("body.storage", "body.view"),
    ) -> dict[str, Any]:
        params = {}
        expand_value = self._expand_value(expand)
        if expand_value:
            params["expand"] = expand_value

        return self._request_confluence_json(
            "GET",
            f"template/{template_id}",
            params=params or None,
        )

    def get_content_template_html(
        self,
        template_id: str,
        html_format: str = "view",
        wait_seconds: float = 20,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        template = self.get_content_template(
            template_id,
            expand=(f"body.{html_format}", "body.storage"),
        )
        html = self._template_body_value(template, html_format)
        if html is None:
            storage_body = self._template_body_value(template, "storage")
            if storage_body is None:
                raise ValueError(f"Template {template_id} does not include a storage body.")

            converted_body = self.convert_content_body(
                value=storage_body,
                from_representation="storage",
                to_representation=html_format,
                wait_seconds=wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            html = converted_body.get("value", "")

        return self._template_context(template, html_format=html_format, html=html)

    def list_content_templates_html(
        self,
        space_key: str | None = None,
        template_kind: str = "page",
        start: int = 0,
        limit: int = 25,
        html_format: str = "view",
        wait_seconds: float = 20,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        response = self.list_content_templates(
            space_key=space_key,
            template_kind=template_kind,
            start=start,
            limit=limit,
            expand=(f"body.{html_format}", "body.storage"),
        )
        templates = []
        for template in response.get("results", []):
            template_id = self._template_id(template)
            try:
                templates.append(
                    self.get_content_template_html(
                        template_id=template_id,
                        html_format=html_format,
                        wait_seconds=wait_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                )
            except ValueError:
                html = self._template_body_value(template, html_format) or ""
                templates.append(self._template_context(template, html_format=html_format, html=html))

        return {
            "results": templates,
            "start": response.get("start", start),
            "limit": response.get("limit", limit),
            "size": response.get("size", len(templates)),
            "_links": response.get("_links", {}),
        }

    def convert_content_body(
        self,
        value: str,
        from_representation: str = "storage",
        to_representation: str = "view",
        wait_seconds: float = 20,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        task = self._request_confluence_json(
            "POST",
            f"contentbody/convert/async/{to_representation}",
            payload={
                "value": value,
                "representation": from_representation,
            },
        )
        async_id = task.get("asyncId") or task.get("id")
        if not async_id:
            return task

        return self.get_converted_content_body(
            async_id=async_id,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def get_converted_content_body(
        self,
        async_id: str,
        wait_seconds: float = 20,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        last_response: dict[str, Any] | None = None

        while True:
            last_response = self._request_confluence_json(
                "GET",
                f"contentbody/convert/async/{async_id}",
            )
            status = str(last_response.get("status", "")).upper()
            if last_response.get("value") is not None:
                return last_response

            if status in {"FAILED", "ERROR"}:
                raise RuntimeError(f"Confluence content body conversion failed: {last_response}")

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Confluence content body conversion: {last_response}"
                )

            time.sleep(poll_interval_seconds)

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

    def _expand_value(self, expand: str | list[str] | tuple[str, ...] | None) -> str | None:
        if expand is None:
            return None

        if isinstance(expand, str):
            return expand

        return ",".join(expand)

    def _template_id(self, template: dict[str, Any]) -> str:
        template_id = template.get("templateId") or template.get("id")
        if not template_id:
            raise ValueError(f"Template response does not include a template id: {template}")

        return str(template_id)

    def _template_body_value(self, template: dict[str, Any], representation: str) -> str | None:
        body = template.get("body", {})
        representation_body = body.get(representation) or {}
        value = representation_body.get("value")
        if value is None:
            return None

        return str(value)

    def _template_context(
        self,
        template: dict[str, Any],
        html_format: str,
        html: str,
    ) -> dict[str, Any]:
        return {
            "template_id": self._template_id(template),
            "name": template.get("name"),
            "description": template.get("description"),
            "template_type": template.get("templateType"),
            "editor_version": template.get("editorVersion"),
            "space": template.get("space"),
            "labels": template.get("labels", []),
            "html_format": html_format,
            "html": html,
            "storage": self._template_body_value(template, "storage"),
            "raw_template": template,
        }

    def _html_to_storage_body(self, html_body: str) -> str:
        body = html_body.strip()
        if not body:
            raise ValueError("HTML body is required.")

        if "<" not in body and ">" not in body:
            return f"<p>{html.escape(body)}</p>"

        return body

    def _apply_template_replacements(
        self,
        template_body: str,
        replacements: dict[str, Any] | None,
    ) -> str:
        if not replacements:
            return template_body

        rendered_body = template_body
        for key, value in replacements.items():
            replacement_value = str(value)
            rendered_body = rendered_body.replace(f"{{{{{key}}}}}", replacement_value)
            rendered_body = rendered_body.replace(f"${{{key}}}", replacement_value)

        return rendered_body

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
