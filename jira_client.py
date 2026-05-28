from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "project",
    "assignee",
    "priority",
    "updated",
]


class JiraAPIError(RuntimeError):
    def __init__(self, status_code: int, reason: str, response_body: str = "") -> None:
        self.status_code = status_code
        self.reason = reason
        self.response_body = response_body
        super().__init__(f"Jira API request failed: HTTP {status_code} {reason}")


class _JiraMacroExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.macros: list[dict[str, Any]] = []
        self._current_macro: dict[str, Any] | None = None
        self._current_param: str | None = None
        self._current_param_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "ac:structured-macro" and attributes.get("ac:name") == "jira":
            self._current_macro = {
                "key": None,
                "server": None,
                "server_id": None,
                "macro_id": attributes.get("ac:macro-id"),
                "local_id": attributes.get("ac:local-id"),
                "parameters": {},
            }
            return

        if self._current_macro and tag == "ac:parameter":
            self._current_param = attributes.get("ac:name")
            self._current_param_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_macro and self._current_param:
            self._current_param_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current_macro and self._current_param and tag == "ac:parameter":
            value = "".join(self._current_param_parts).strip()
            self._current_macro["parameters"][self._current_param] = value
            self._current_param = None
            self._current_param_parts = []
            return

        if self._current_macro and tag == "ac:structured-macro":
            parameters = self._current_macro["parameters"]
            self._current_macro["key"] = parameters.get("key")
            self._current_macro["server"] = parameters.get("server")
            self._current_macro["server_id"] = parameters.get("serverId")
            self.macros.append(self._current_macro)
            self._current_macro = None


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    timeout_seconds: int = 20


class JiraClient:
    def __init__(self, config: JiraConfig) -> None:
        self.config = config

    @classmethod
    def from_env(
        cls,
        env_file: str | Path = ENV_FILE,
        timeout_seconds: int = 20,
    ) -> "JiraClient":
        load_dotenv(env_file)

        required_values = {
            "JIRA_URL or CONFLUENCE_URL": os.getenv("JIRA_URL") or os.getenv("CONFLUENCE_URL"),
            "JIRA_USER or CONFLUENCE_USER": os.getenv("JIRA_USER") or os.getenv("CONFLUENCE_USER"),
            "JIRA_API_TOKEN or CONFLUENCE_API_TOKEN": (
                os.getenv("JIRA_API_TOKEN") or os.getenv("CONFLUENCE_API_TOKEN")
            ),
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

        config = JiraConfig(
            base_url=required_values["JIRA_URL or CONFLUENCE_URL"].strip(),
            email=required_values["JIRA_USER or CONFLUENCE_USER"].strip(),
            api_token=required_values["JIRA_API_TOKEN or CONFLUENCE_API_TOKEN"].strip(),
            timeout_seconds=timeout_seconds,
        )
        return cls(config)

    def get_current_user(self, expand: str | None = None) -> dict[str, Any]:
        params = {"expand": expand} if expand else None
        return self._request_json("GET", "myself", params=params)

    def get_jira_current_user(self, expand: str | None = None) -> dict[str, Any]:
        return self.get_current_user(expand=expand)

    def list_application_roles(self) -> dict[str, Any]:
        user = self.get_current_user(expand="applicationRoles")
        return user.get("applicationRoles", {})

    def list_jira_application_roles(self) -> dict[str, Any]:
        return self.list_application_roles()

    def list_projects(self, max_results: int = 50) -> dict[str, Any]:
        return self._request_json(
            "GET",
            "project/search",
            params={"maxResults": max_results},
        )

    def list_jira_projects(self, max_results: int = 50) -> dict[str, Any]:
        return self.list_projects(max_results=max_results)

    def search_issues(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int = 50,
        next_page_token: str | None = None,
        expand: list[str] | str | None = None,
        fields_by_keys: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or DEFAULT_FIELDS,
            "fieldsByKeys": fields_by_keys,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        if expand:
            body["expand"] = ",".join(expand) if isinstance(expand, list) else expand

        return self._request_json("POST", "search/jql", payload=body)

    def search_all_issues(
        self,
        jql: str,
        fields: list[str] | None = None,
        page_size: int = 50,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        issues = []
        next_page_token = None
        page_count = 0

        while True:
            page_count += 1
            response = self.search_issues(
                jql=jql,
                fields=fields,
                max_results=page_size,
                next_page_token=next_page_token,
            )
            issues.extend(response.get("issues", []))

            next_page_token = response.get("nextPageToken")
            if not next_page_token or response.get("isLast") is True:
                return issues

            if max_pages is not None and page_count >= max_pages:
                return issues

    def get_jql_schema(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        response = self.search_issues(
            jql=jql,
            fields=fields or ["*all"],
            max_results=max_results,
            expand=["names", "schema"],
        )
        issues = response.get("issues", [])

        return {
            "jql": jql,
            "names": response.get("names", {}),
            "schema": response.get("schema", {}),
            "issues": self.summarize_issues(issues),
            "raw_issues": issues,
            "count": len(issues),
            "next_page_token": response.get("nextPageToken"),
            "is_last": response.get("isLast"),
        }

    def get_issue_keys_schema(
        self,
        issue_keys: Iterable[str],
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_issue_keys = self._unique_values(issue_keys)
        if not normalized_issue_keys:
            return {
                "jql": "",
                "issue_keys": [],
                "names": {},
                "schema": {},
                "issues": [],
                "raw_issues": [],
                "count": 0,
                "next_page_token": None,
                "is_last": True,
            }

        jql = f"key in ({', '.join(self._quote_jql_value(key) for key in normalized_issue_keys)})"
        schema = self.get_jql_schema(
            jql=jql,
            fields=fields,
            max_results=len(normalized_issue_keys),
        )
        schema["issue_keys"] = normalized_issue_keys
        return schema

    def get_jira_schema_from_page_url(
        self,
        page_url: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        issue_key = self.issue_key_from_url(page_url)
        schema = self.get_issue_keys_schema([issue_key], fields=fields)
        issue = schema["issues"][0] if schema["issues"] else None

        return {
            "page_url": page_url,
            "issue_key": issue_key,
            "issue_url": self.issue_url(issue_key),
            "jql": schema["jql"],
            "names": schema["names"],
            "schema": schema["schema"],
            "issue": issue,
            "raw_issue": schema["raw_issues"][0] if schema["raw_issues"] else None,
            "count": schema["count"],
        }

    def get_issue(
        self,
        issue_key: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"issue/{quote(issue_key, safe='')}",
            params={"fields": ",".join(fields or DEFAULT_FIELDS)},
        )

    def get_jira_issue(
        self,
        issue_key: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.get_issue(issue_key, fields=fields)

    def get_jira_links_from_page_context(
        self,
        page_context: dict[str, Any],
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        issue_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        jira_macros = self.extract_jira_macros_from_page_context(page_context)
        jira_links = self.get_jira_links_from_macros(
            jira_macros,
            project_keys=project_keys,
            issue_fields=issue_fields,
        )
        normalized_project_keys = self.normalize_project_keys(project_keys)

        return {
            "page": {
                "id": page_context.get("id"),
                "title": page_context.get("title"),
                "url": page_context.get("url"),
                "space": page_context.get("space"),
            },
            "jira_links": jira_links["jira_links"],
            "ignored_jira_macros": jira_links["ignored_jira_macros"],
            "project_keys": sorted(normalized_project_keys),
        }

    def get_jira_schema_from_page_context(
        self,
        page_context: dict[str, Any],
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        jira_macros = self.extract_jira_macros_from_page_context(page_context)
        filtered_macros = []
        ignored_jira_macros = []
        normalized_project_keys = self.normalize_project_keys(project_keys)

        for macro in jira_macros:
            issue_key = macro.get("key")
            if self.issue_matches_project_keys(issue_key, normalized_project_keys):
                filtered_macros.append(macro)
            else:
                ignored_jira_macros.append(macro)

        issue_keys = self._unique_values(
            macro.get("key")
            for macro in filtered_macros
            if macro.get("key")
        )
        schema = self.get_issue_keys_schema(issue_keys, fields=fields)

        return {
            "page": {
                "id": page_context.get("id"),
                "title": page_context.get("title"),
                "url": page_context.get("url"),
                "space": page_context.get("space"),
            },
            "project_keys": sorted(normalized_project_keys),
            "issue_keys": issue_keys,
            "jira_macros": filtered_macros,
            "ignored_jira_macros": ignored_jira_macros,
            "jql": schema["jql"],
            "names": schema["names"],
            "schema": schema["schema"],
            "issues": schema["issues"],
            "raw_issues": schema["raw_issues"],
            "count": schema["count"],
        }

    def get_jira_schema_from_confluence_page_url(
        self,
        page_url: str,
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        fields: list[str] | None = None,
        body_format: str = "storage",
        confluence_client: Any | None = None,
    ) -> dict[str, Any]:
        if confluence_client is None:
            from confluence_client import ConfluenceClient

            confluence_client = ConfluenceClient.from_env(
                timeout_seconds=self.config.timeout_seconds,
            )

        page_context = confluence_client.get_page_context_by_url(
            page_url,
            body_format=body_format,
        )
        return self.get_jira_schema_from_page_context(
            page_context,
            project_keys=project_keys,
            fields=fields,
        )

    def get_jira_schema_from_page_id(
        self,
        confluence_client: Any,
        page_id: str,
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        fields: list[str] | None = None,
        body_format: str = "storage",
    ) -> dict[str, Any]:
        page_context = confluence_client.get_page_context_by_id(
            page_id,
            body_format=body_format,
        )
        return self.get_jira_schema_from_page_context(
            page_context,
            project_keys=project_keys,
            fields=fields,
        )

    def get_jira_schema_from_macros(
        self,
        jira_macros: list[dict[str, Any]],
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_project_keys = self.normalize_project_keys(project_keys)
        filtered_macros = []
        ignored_jira_macros = []

        for macro in jira_macros:
            issue_key = macro.get("key")
            if self.issue_matches_project_keys(issue_key, normalized_project_keys):
                filtered_macros.append(macro)
            else:
                ignored_jira_macros.append(macro)

        issue_keys = self._unique_values(
            macro.get("key")
            for macro in filtered_macros
            if macro.get("key")
        )
        schema = self.get_issue_keys_schema(issue_keys, fields=fields)

        return {
            "project_keys": sorted(normalized_project_keys),
            "issue_keys": issue_keys,
            "jira_macros": filtered_macros,
            "ignored_jira_macros": ignored_jira_macros,
            "jql": schema["jql"],
            "names": schema["names"],
            "schema": schema["schema"],
            "issues": schema["issues"],
            "raw_issues": schema["raw_issues"],
            "count": schema["count"],
        }

    def get_jira_links_from_macros(
        self,
        jira_macros: list[dict[str, Any]],
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
        issue_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_project_keys = self.normalize_project_keys(project_keys)
        issue_cache: dict[str, dict[str, Any] | JiraAPIError] = {}
        jira_links = []
        ignored_jira_macros = []

        for macro in jira_macros:
            issue_key = macro.get("key")
            if not self.issue_matches_project_keys(issue_key, normalized_project_keys):
                ignored_jira_macros.append(macro)
                continue

            if not issue_key:
                jira_links.append(
                    {
                        "key": None,
                        "url": None,
                        "status": None,
                        "summary": None,
                        "macro": macro,
                        "error": "Jira macro does not contain a single issue key.",
                    }
                )
                continue

            if issue_key not in issue_cache:
                try:
                    issue_cache[issue_key] = self.get_issue(issue_key, fields=issue_fields)
                except JiraAPIError as error:
                    issue_cache[issue_key] = error

            issue_or_error = issue_cache[issue_key]
            if isinstance(issue_or_error, JiraAPIError):
                jira_links.append(
                    {
                        "key": issue_key,
                        "url": self.issue_url(issue_key),
                        "status": None,
                        "summary": None,
                        "macro": macro,
                        "error": str(issue_or_error),
                        "error_detail": issue_or_error.response_body[:500],
                    }
                )
                continue

            jira_links.append(self.jira_issue_context(issue_or_error, macro))

        return {
            "jira_links": jira_links,
            "ignored_jira_macros": ignored_jira_macros,
            "project_keys": sorted(normalized_project_keys),
        }

    def extract_jira_macros_from_page_context(self, page_context: dict[str, Any]) -> list[dict[str, Any]]:
        return self.extract_jira_macros(page_context.get("raw_body", ""))

    def extract_jira_macros(self, storage_body: str) -> list[dict[str, Any]]:
        if not storage_body:
            return []

        wrapped_body = (
            '<root xmlns:ac="http://atlassian.com/content" '
            'xmlns:ri="http://atlassian.com/resource/identifier">'
            f"{storage_body}</root>"
        )
        try:
            root = ET.fromstring(wrapped_body)
        except ET.ParseError:
            parser = _JiraMacroExtractor()
            parser.feed(storage_body)
            return parser.macros

        macros = []
        for element in root.iter():
            if self._local_name(element.tag) != "structured-macro":
                continue

            if self._attribute_value(element.attrib, "name") != "jira":
                continue

            parameters = {}
            for child in element:
                if self._local_name(child.tag) != "parameter":
                    continue

                parameter_name = self._attribute_value(child.attrib, "name")
                if parameter_name:
                    parameters[parameter_name] = "".join(child.itertext()).strip()

            macros.append(
                {
                    "key": parameters.get("key"),
                    "server": parameters.get("server"),
                    "server_id": parameters.get("serverId"),
                    "macro_id": self._attribute_value(element.attrib, "macro-id"),
                    "local_id": self._attribute_value(element.attrib, "local-id"),
                    "parameters": parameters,
                }
            )

        return macros

    def summarize_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        fields = issue.get("fields", {})
        status = fields.get("status") or {}
        status_category = status.get("statusCategory") or {}
        issue_type = fields.get("issuetype") or {}
        project = fields.get("project") or {}
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        comment_page = fields.get("comment")
        description = fields.get("description")
        issue_key = issue.get("key")
        issue_summary = {
            "id": issue.get("id"),
            "key": issue_key,
            "url": self.issue_url(issue_key),
            "summary": fields.get("summary"),
            "description": self._adf_to_text(description) if description is not None else None,
            "status": status.get("name"),
            "status_category": status_category.get("name"),
            "issue_type": issue_type.get("name"),
            "project": {
                "key": project.get("key"),
                "name": project.get("name"),
            },
            "assignee": assignee.get("displayName") if assignee else None,
            "priority": priority.get("name") if priority else None,
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "due_date": fields.get("duedate"),
            "labels": fields.get("labels", []),
        }
        if comment_page is not None:
            issue_summary["comments"] = self.summarize_comments(comment_page)

        return issue_summary

    def summarize_issues(self, issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.summarize_issue(issue) for issue in issues]

    def summarize_comments(self, comment_page: dict[str, Any]) -> dict[str, Any]:
        return {
            "total": comment_page.get("total", 0),
            "max_results": comment_page.get("maxResults"),
            "start_at": comment_page.get("startAt"),
            "comments": [
                {
                    "id": comment.get("id"),
                    "author": (comment.get("author") or {}).get("displayName"),
                    "created": comment.get("created"),
                    "updated": comment.get("updated"),
                    "body": self._adf_to_text(comment.get("body")),
                }
                for comment in comment_page.get("comments", [])
            ],
        }

    def jira_issue_context(
        self,
        issue: dict[str, Any],
        macro: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issue_summary = self.summarize_issue(issue)
        issue_summary["macro"] = macro
        issue_summary["error"] = None
        return issue_summary

    def issue_url(self, issue_key: str | None) -> str | None:
        if not issue_key:
            return None

        return f"{self._site_url()}/browse/{issue_key}"

    def issue_key_from_url(self, page_url: str) -> str:
        parsed_url = urlparse(page_url)
        query = parse_qs(parsed_url.query)

        for query_key in ("selectedIssue", "issueKey", "issue"):
            if query.get(query_key):
                return self._normalize_issue_key(query[query_key][0])

        path_parts = [unquote(part) for part in parsed_url.path.split("/") if part]
        if "browse" in path_parts:
            browse_index = path_parts.index("browse")
            if len(path_parts) > browse_index + 1:
                return self._normalize_issue_key(path_parts[browse_index + 1])

        for part in reversed(path_parts):
            try:
                return self._normalize_issue_key(part)
            except ValueError:
                continue

        raise ValueError(
            "Could not find a Jira issue key in the URL. "
            "Use a URL like https://your-site.atlassian.net/browse/ABC-123 "
            "or a Jira URL with selectedIssue=ABC-123."
        )

    def normalize_project_keys(
        self,
        project_keys: list[str] | tuple[str, ...] | set[str] | None,
    ) -> set[str]:
        if not project_keys:
            return set()

        return {project_key.strip().upper() for project_key in project_keys if project_key.strip()}

    def issue_matches_project_keys(self, issue_key: str | None, project_keys: set[str]) -> bool:
        if not project_keys:
            return True

        if not issue_key or "-" not in issue_key:
            return False

        issue_project_key = issue_key.split("-", 1)[0].upper()
        return issue_project_key in project_keys

    def _normalize_issue_key(self, issue_key: str) -> str:
        normalized_issue_key = unquote(issue_key).strip().upper()
        if "/" in normalized_issue_key:
            normalized_issue_key = normalized_issue_key.split("/", 1)[0]

        if "-" not in normalized_issue_key:
            raise ValueError(f"Invalid Jira issue key: {issue_key}")

        project_key, issue_number = normalized_issue_key.split("-", 1)
        if not project_key or not issue_number.isdigit():
            raise ValueError(f"Invalid Jira issue key: {issue_key}")

        return f"{project_key}-{issue_number}"

    def _unique_values(self, values: Iterable[str | None]) -> list[str]:
        unique_values = []
        seen_values = set()
        for value in values:
            if not value:
                continue

            normalized_value = value.strip()
            if not normalized_value or normalized_value in seen_values:
                continue

            unique_values.append(normalized_value)
            seen_values.add(normalized_value)

        return unique_values

    def _quote_jql_value(self, value: str) -> str:
        escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped_value}"'

    def _adf_to_text(self, value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, str):
            return value

        if isinstance(value, list):
            return self._join_text_parts(self._adf_to_text(item) for item in value)

        if not isinstance(value, dict):
            return str(value)

        node_type = value.get("type")
        if node_type == "text":
            return value.get("text", "")

        if node_type == "hardBreak":
            return "\n"

        content = value.get("content", [])
        text = self._join_text_parts(self._adf_to_text(item) for item in content)
        if node_type in {"paragraph", "heading", "blockquote", "bulletList", "orderedList", "listItem"}:
            return f"{text}\n" if text else ""

        return text

    def _join_text_parts(self, parts: Iterable[str]) -> str:
        text = "".join(parts)
        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization_header(),
            "User-Agent": "jira-jql-client/1.0",
        }

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            self._api_url(endpoint, params),
            data=body,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise JiraAPIError(error.code, error.reason, error_body) from error

        return json.loads(response_body) if response_body else {}

    def _api_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self._site_url()}/rest/api/3/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"

        return url

    def _site_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/wiki"):
            return base[: -len("/wiki")]

        return base

    def _local_name(self, name: str) -> str:
        return name.rsplit("}", 1)[-1].split(":", 1)[-1]

    def _attribute_value(self, attributes: dict[str, str], attribute_name: str) -> str | None:
        for key, value in attributes.items():
            if self._local_name(key) == attribute_name:
                return value

        return None

    def _authorization_header(self) -> str:
        credentials = f"{self.config.email}:{self.config.api_token}".encode("utf-8")
        encoded_credentials = base64.b64encode(credentials).decode("ascii")
        return f"Basic {encoded_credentials}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query Jira issues with JQL.")
    parser.add_argument(
        "jql",
        nargs="?",
        help='JQL query, for example: "project = KAN ORDER BY updated DESC"',
    )
    parser.add_argument(
        "--page-url",
        default=None,
        help="Jira issue page URL. When set with --schema, returns the Jira schema for that issue.",
    )
    parser.add_argument(
        "--confluence-page-url",
        default=None,
        help=(
            "Confluence page URL. When set with --schema, returns the Jira schema "
            "for Jira macros on that Confluence page."
        ),
    )
    parser.add_argument(
        "--project-key",
        "--project-keys",
        action="append",
        default=[],
        help="Jira project key(s) to include from a Confluence page. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Field to return. Can be repeated or comma-separated.",
    )
    parser.add_argument("--max-results", type=int, default=50, help="Maximum results for one page.")
    parser.add_argument("--all", action="store_true", help="Fetch every page of results.")
    parser.add_argument("--page-size", type=int, default=50, help="Page size when using --all.")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum pages when using --all.")
    parser.add_argument("--schema", action="store_true", help="Print Jira field names and schema for the JQL.")
    parser.add_argument("--raw", action="store_true", help="Print the raw Jira API response as JSON.")
    args = parser.parse_args(argv)

    if not args.jql and not args.page_url and not args.confluence_page_url:
        parser.error("Provide a JQL query, --page-url, or --confluence-page-url.")

    if (args.page_url or args.confluence_page_url) and not args.schema:
        parser.error("--page-url and --confluence-page-url are currently supported with --schema.")

    args.project_key = split_csv_args(args.project_key)
    return args


def split_csv_args(values: list[str]) -> list[str]:
    items = []
    for value in values:
        items.extend(part.strip() for part in value.split(","))

    return [item for item in items if item]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested_fields = split_csv_args(args.field)
    fields = requested_fields or DEFAULT_FIELDS
    client = JiraClient.from_env()

    try:
        if args.page_url:
            output = client.get_jira_schema_from_page_url(
                page_url=args.page_url,
                fields=requested_fields or None,
            )
            if not args.raw:
                output.pop("raw_issue", None)
        elif args.confluence_page_url:
            output = client.get_jira_schema_from_confluence_page_url(
                page_url=args.confluence_page_url,
                project_keys=args.project_key,
                fields=requested_fields or None,
            )
            if not args.raw:
                output.pop("raw_issues", None)
        elif args.schema:
            output = client.get_jql_schema(
                jql=args.jql,
                fields=requested_fields or None,
                max_results=args.max_results,
            )
            if not args.raw:
                output.pop("raw_issues", None)
        elif args.all:
            issues = client.search_all_issues(
                jql=args.jql,
                fields=fields,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            output = {"issues": client.summarize_issues(issues), "count": len(issues)}
        else:
            response = client.search_issues(
                jql=args.jql,
                fields=fields,
                max_results=args.max_results,
            )
            if args.raw:
                output = response
            else:
                issues = response.get("issues", [])
                output = {
                    "issues": client.summarize_issues(issues),
                    "count": len(issues),
                    "next_page_token": response.get("nextPageToken"),
                    "is_last": response.get("isLast"),
                }
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    except JiraAPIError as error:
        print(error, file=sys.stderr)
        if error.response_body:
            print(error.response_body[:1000], file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not reach Jira API: {error.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Jira API request timed out.", file=sys.stderr)
        return 1

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
