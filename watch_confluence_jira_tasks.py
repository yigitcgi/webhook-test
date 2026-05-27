from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any
from urllib.error import URLError

from confluence_client import ConfluenceAPIError, ConfluenceClient
from jira_client import JiraAPIError, JiraClient


DEFAULT_INTERVAL_SECONDS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Watch a Confluence page for Jira issue macros and print the current "
            "status of matching Jira tasks."
        )
    )
    parser.add_argument(
        "--page-url",
        default=os.getenv("CONFLUENCE_PAGE_URL"),
        help="Confluence page URL to watch. Defaults to CONFLUENCE_PAGE_URL.",
    )
    parser.add_argument(
        "--project-key",
        "--project-keys",
        action="append",
        default=[],
        help=(
            "Jira project key(s) to follow, for example KAN. Can be repeated or "
            "comma-separated. If omitted, all Jira issue macros on the page are followed."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("CONFLUENCE_WATCH_INTERVAL", DEFAULT_INTERVAL_SECONDS)),
        help=f"Polling interval in seconds. Defaults to {DEFAULT_INTERVAL_SECONDS}.",
    )
    parser.add_argument(
        "--done-status",
        action="append",
        default=["Done"],
        help=(
            "Jira status name considered complete. Can be repeated or comma-separated. "
            "Jira status category 'Done' is also considered complete."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit.",
    )
    parser.add_argument(
        "--stop-when-done",
        action="store_true",
        help="Exit after all matching Jira tasks are done.",
    )
    args = parser.parse_args()

    if not args.page_url:
        parser.error("--page-url is required unless CONFLUENCE_PAGE_URL is set.")

    if args.interval <= 0:
        parser.error("--interval must be greater than 0.")

    args.project_key = split_csv_args(args.project_key)
    args.done_status = split_csv_args(args.done_status)
    return args


def split_csv_args(values: list[str]) -> list[str]:
    items = []
    for value in values:
        items.extend(part.strip() for part in value.split(","))

    return [item for item in items if item]


def check_page(
    confluence_client: ConfluenceClient,
    jira_client: JiraClient,
    page_url: str,
    project_keys: list[str],
    done_statuses: list[str],
) -> bool:
    page_context = confluence_client.get_page_context_by_url(page_url)
    result = jira_client.get_jira_links_from_page_context(
        page_context,
        project_keys=project_keys,
    )
    jira_links = result["jira_links"]
    ignored_count = len(result.get("ignored_jira_macros", []))
    page = result["page"]

    print_header(page, project_keys, ignored_count)

    if not jira_links:
        print("No matching Jira tasks found on this Confluence page.")
        return False

    completed_count = 0
    for jira_link in jira_links:
        is_done = is_completed(jira_link, done_statuses)
        if is_done:
            completed_count += 1

        print_task(jira_link, is_done)

    if completed_count == len(jira_links):
        print(f"All tasks are done. ({completed_count}/{len(jira_links)})")
        return True

    print(f"Waiting: {completed_count}/{len(jira_links)} tasks are done.")
    return False


def print_header(page: dict[str, Any], project_keys: list[str], ignored_count: int) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("")
    print(f"[{timestamp}] Page: {page.get('title')} ({page.get('url')})")

    if project_keys:
        print(f"Following Jira project keys: {', '.join(project_keys)}")
    else:
        print("Following Jira project keys: all")

    if ignored_count:
        print(f"Ignored Jira macros outside the project filter: {ignored_count}")


def print_task(jira_link: dict[str, Any], is_done: bool) -> None:
    key = jira_link.get("key") or "unknown"
    status = jira_link.get("status") or "unknown"
    summary = jira_link.get("summary") or ""
    url = jira_link.get("url") or ""
    marker = "DONE" if is_done else "OPEN"

    if jira_link.get("error"):
        print(f"- {key} [{marker}] status={status} error={jira_link['error']}")
        return

    print(f"- {key} [{marker}] status={status} summary={summary} url={url}")


def is_completed(jira_link: dict[str, Any], done_statuses: list[str]) -> bool:
    if jira_link.get("error"):
        return False

    normalized_done_statuses = {status.casefold() for status in done_statuses}
    status = str(jira_link.get("status") or "").casefold()
    status_category = str(jira_link.get("status_category") or "").casefold()

    return status in normalized_done_statuses or status_category == "done"


def main() -> int:
    args = parse_args()
    confluence_client = ConfluenceClient.from_env()
    jira_client = JiraClient.from_env()

    try:
        while True:
            try:
                all_done = check_page(
                    confluence_client=confluence_client,
                    jira_client=jira_client,
                    page_url=args.page_url,
                    project_keys=args.project_key,
                    done_statuses=args.done_status,
                )
            except ConfluenceAPIError as error:
                print(f"Confluence API error: {error}", file=sys.stderr)
                if error.response_body:
                    print(error.response_body[:500], file=sys.stderr)
                all_done = False
            except JiraAPIError as error:
                print(f"Jira API error: {error}", file=sys.stderr)
                if error.response_body:
                    print(error.response_body[:500], file=sys.stderr)
                all_done = False
            except URLError as error:
                print(f"Network error: {error.reason}", file=sys.stderr)
                all_done = False
            except TimeoutError:
                print("Atlassian API request timed out.", file=sys.stderr)
                all_done = False

            if args.once or (all_done and args.stop_when_done):
                return 0 if all_done else 1

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped watcher.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
