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
from urllib.parse import quote, urlencode
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
    ) -> dict[str, Any]:
        jira_macros = self.extract_jira_macros_from_page_context(page_context)
        jira_links = self.get_jira_links_from_macros(jira_macros, project_keys=project_keys)
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

    def get_jira_links_from_macros(
        self,
        jira_macros: list[dict[str, Any]],
        project_keys: list[str] | tuple[str, ...] | set[str] | None = None,
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
                    issue_cache[issue_key] = self.get_issue(issue_key)
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
        issue_key = issue.get("key")

        return {
            "id": issue.get("id"),
            "key": issue_key,
            "url": self.issue_url(issue_key),
            "summary": fields.get("summary"),
            "status": status.get("name"),
            "status_category": status_category.get("name"),
            "issue_type": issue_type.get("name"),
            "project": {
                "key": project.get("key"),
                "name": project.get("name"),
            },
            "assignee": assignee.get("displayName") if assignee else None,
            "priority": priority.get("name") if priority else None,
            "updated": fields.get("updated"),
        }

    def summarize_issues(self, issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.summarize_issue(issue) for issue in issues]

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
    parser.add_argument("jql", help='JQL query, for example: "project = KAN ORDER BY updated DESC"')
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
    parser.add_argument("--raw", action="store_true", help="Print the raw Jira API response as JSON.")
    return parser.parse_args(argv)


def split_csv_args(values: list[str]) -> list[str]:
    items = []
    for value in values:
        items.extend(part.strip() for part in value.split(","))

    return [item for item in items if item]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fields = split_csv_args(args.field) or DEFAULT_FIELDS
    client = JiraClient.from_env()

    try:
        if args.all:
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
