from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError

from confluence_client import ConfluenceAPIError, ConfluenceClient
from jira_client import JiraAPIError, JiraClient


DEFAULT_SOURCE_PAGE_URL = (
    "https://cgi-team-agentic.atlassian.net/wiki/spaces/"
    "~71202088ffd733b2124e7bb41b871c943c3687/pages/9338881/PI+Feature+Development"
)
DEFAULT_INTERVAL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TEMPLATE_NAME = "Weekly Executive Report"
DEFAULT_REPORT_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "project",
    "assignee",
    "priority",
    "created",
    "updated",
    "duedate",
    "labels",
    "description",
    "comment",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a weekly Confluence executive report from Jira links on a source page."
    )
    parser.add_argument(
        "--source-page-url",
        default=os.getenv("WEEKLY_REPORT_SOURCE_PAGE_URL", DEFAULT_SOURCE_PAGE_URL),
        help="Confluence page URL to inspect for Jira links.",
    )
    parser.add_argument(
        "--project-key",
        "--project-keys",
        action="append",
        default=[],
        help="Jira project key(s) to include. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--template-id",
        default=os.getenv("WEEKLY_REPORT_TEMPLATE_ID"),
        help="Confluence template ID to use for the report page.",
    )
    parser.add_argument(
        "--template-name",
        default=os.getenv("WEEKLY_REPORT_TEMPLATE_NAME", DEFAULT_TEMPLATE_NAME),
        help="Template name fragment used when --template-id is not provided.",
    )
    parser.add_argument(
        "--template-space-key",
        default=os.getenv("WEEKLY_REPORT_TEMPLATE_SPACE_KEY"),
        help="Space key to search for templates. Defaults to source page space, then all templates.",
    )
    parser.add_argument(
        "--output-parent-page-url",
        default=os.getenv("WEEKLY_REPORT_OUTPUT_PARENT_PAGE_URL"),
        help="Parent page for the created report. Defaults to --source-page-url.",
    )
    parser.add_argument(
        "--output-space-key",
        default=os.getenv("WEEKLY_REPORT_OUTPUT_SPACE_KEY"),
        help="Create as a root-level page in this space when no parent page URL is used.",
    )
    parser.add_argument(
        "--title-prefix",
        default=os.getenv("WEEKLY_REPORT_TITLE_PREFIX", "Weekly Executive Report"),
        help="Created report title prefix.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("WEEKLY_REPORT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
        help="Interval between runs in seconds. Defaults to one week.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report and skip Confluence page creation.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use deterministic summary generation instead of Azure OpenAI.",
    )
    parser.add_argument(
        "--max-comments-per-issue",
        type=int,
        default=5,
        help="Maximum comments to include per Jira issue in the generated report context.",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be greater than 0.")

    args.project_key = split_csv_args(args.project_key)
    if not args.output_parent_page_url and not args.output_space_key:
        args.output_parent_page_url = args.source_page_url

    return args


def split_csv_args(values: list[str]) -> list[str]:
    items = []
    for value in values:
        items.extend(part.strip() for part in value.split(","))

    return [item for item in items if item]


def run_cycle(
    confluence_client: ConfluenceClient,
    jira_client: JiraClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    source_page = confluence_client.get_page_context_by_url(args.source_page_url)
    linked_issues = jira_client.get_jira_links_from_page_context(
        source_page,
        project_keys=args.project_key,
        issue_fields=DEFAULT_REPORT_FIELDS,
    )
    jira_links = linked_issues["jira_links"]

    template = resolve_template(
        confluence_client=confluence_client,
        source_page=source_page,
        template_id=args.template_id,
        template_name=args.template_name,
        template_space_key=args.template_space_key,
    )
    template_body = template.get("storage") or template.get("html") or ""
    report_html = build_filled_report_html(
        template_body=template_body,
        jira_links=jira_links,
        source_page=source_page,
        generated_at=generated_at,
        project_keys=args.project_key,
        max_comments_per_issue=args.max_comments_per_issue,
        use_llm=not args.no_llm,
    )
    title = f"{args.title_prefix} - {generated_at.strftime('%Y-%m-%d')}"

    result = {
        "dry_run": args.dry_run,
        "title": title,
        "source_page": source_page,
        "template": {
            "template_id": template.get("template_id"),
            "name": template.get("name"),
        },
        "jira_links": jira_links,
        "ignored_jira_macros": linked_issues.get("ignored_jira_macros", []),
        "report_html": report_html,
        "created_page": None,
    }

    if args.dry_run:
        print_dry_run(result)
        return result

    result["created_page"] = create_report_page(
        confluence_client=confluence_client,
        title=title,
        report_html=report_html,
        output_parent_page_url=args.output_parent_page_url,
        output_space_key=args.output_space_key,
    )
    print_created_page(result["created_page"])
    return result


def resolve_template(
    confluence_client: ConfluenceClient,
    source_page: dict[str, Any],
    template_id: str | None,
    template_name: str,
    template_space_key: str | None,
) -> dict[str, Any]:
    if template_id:
        return confluence_client.get_content_template_html(template_id)

    source_space_key = (source_page.get("space") or {}).get("key")
    search_space_keys = []
    for key in (template_space_key, source_space_key, None):
        if key not in search_space_keys:
            search_space_keys.append(key)

    if template_space_key is None:
        for space in confluence_client.list_spaces(limit=100).get("results", []):
            space_key = space.get("key")
            if space_key not in search_space_keys:
                search_space_keys.append(space_key)

    for space_key in search_space_keys:
        response = confluence_client.list_content_templates_html(
            space_key=space_key,
            limit=50,
        )
        for template in response.get("results", []):
            name = template.get("name") or ""
            if template_name.casefold() in name.casefold():
                return template

    raise ValueError(f"Could not find Confluence template matching: {template_name}")


def build_filled_report_html(
    template_body: str,
    jira_links: list[dict[str, Any]],
    source_page: dict[str, Any],
    generated_at: datetime,
    project_keys: list[str],
    max_comments_per_issue: int,
    use_llm: bool,
) -> str:
    if use_llm:
        llm_report_html = build_llm_report_html(
            template_body=template_body,
            jira_links=jira_links,
            source_page=source_page,
            generated_at=generated_at,
            project_keys=project_keys,
            max_comments_per_issue=max_comments_per_issue,
        )
        if llm_report_html:
            return llm_report_html

    return build_deterministic_report_html(
        template_body=template_body,
        source_page=source_page,
        jira_links=jira_links,
        generated_at=generated_at,
        project_keys=project_keys,
    )


def build_llm_report_html(
    template_body: str,
    jira_links: list[dict[str, Any]],
    source_page: dict[str, Any],
    generated_at: datetime,
    project_keys: list[str],
    max_comments_per_issue: int,
) -> str | None:
    try:
        from azure_openai import create_chat_completion
    except Exception as error:
        print(f"Azure OpenAI report generation skipped: {error}", file=sys.stderr)
        return None

    context = {
        "source_page": {
            "title": source_page.get("title"),
            "url": source_page.get("url"),
        },
        "generated_at": generated_at.isoformat(),
        "project_filter": project_keys or "all",
        "issues": [
            issue_for_prompt(jira_link, max_comments_per_issue=max_comments_per_issue)
            for jira_link in jira_links
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You fill an existing Confluence report template with Jira facts. "
                "Return only complete Confluence storage-compatible HTML. Do not return Markdown, "
                "code fences, commentary, or JSON. Use only the provided Jira facts. "
                "Replace placeholder/example template content with real issue content. "
                "Preserve the template's main headings, tables, and structure where practical. "
                "Do not append a second report after the template."
            ),
        },
        {
            "role": "user",
            "content": (
                "Fill this Confluence template for the weekly executive report.\n\n"
                "Template HTML:\n"
                f"{template_body or '<p></p>'}\n\n"
                "Jira context JSON:\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
                "Use the template sections as follows when present:\n"
                "- Open Items: Jira issues that are not in a Done status category.\n"
                "- Open Item Assignees: a table mapping each open issue to assignee, status, and next step.\n"
                "- Closed Items: Jira issues in a Done status category.\n"
                "- Summaries of Open Items: concise executive one-line summaries using status, description, and comments.\n"
                "If a section has no matching issues, fill it with a short empty-state sentence."
            ),
        },
    ]

    try:
        response = create_chat_completion(messages)
    except Exception as error:
        print(f"Azure OpenAI report generation skipped: {error}", file=sys.stderr)
        return None

    return normalize_llm_html(response.choices[0].message.content)


def build_deterministic_report_html(
    template_body: str,
    source_page: dict[str, Any],
    jira_links: list[dict[str, Any]],
    generated_at: datetime,
    project_keys: list[str],
) -> str:
    total = len(jira_links)
    done = [issue for issue in jira_links if is_done(issue)]
    open_items = [issue for issue in jira_links if not is_done(issue)]
    unassigned = [issue for issue in jira_links if not issue.get("assignee")]

    section_replacements = {
        "Open Items": issue_list(
            open_items,
            empty_text="No open tracked Jira issues found.",
            include_status=True,
        ),
        "Open Item Assignees": issue_assignee_table(open_items),
        "Closed Items": issue_list(
            done,
            empty_text="No closed tracked Jira issues found.",
            include_status=False,
        ),
        "Summaries of Open Items": issue_summary_list(open_items),
    }

    if template_body:
        filled_template, replacements = fill_template_sections(template_body, section_replacements)
        if replacements:
            return filled_template

    return render_default_report_html(
        source_page=source_page,
        jira_links=jira_links,
        generated_at=generated_at,
        project_keys=project_keys,
        open_items=open_items,
        done=done,
        unassigned=unassigned,
        total=total,
    )


def render_default_report_html(
    source_page: dict[str, Any],
    jira_links: list[dict[str, Any]],
    generated_at: datetime,
    project_keys: list[str],
    open_items: list[dict[str, Any]],
    done: list[dict[str, Any]],
    unassigned: list[dict[str, Any]],
    total: int,
) -> str:
    status_table = issue_table(jira_links)
    metadata = "\n".join(
        [
            "<h2>Report Metadata</h2>",
            "<ul>",
            f"<li>Source page: <a href=\"{html.escape(source_page.get('url') or '')}\">"
            f"{html.escape(source_page.get('title') or 'source page')}</a></li>",
            f"<li>Generated at: {html.escape(generated_at.isoformat())}</li>",
            f"<li>Project filter: {html.escape(', '.join(project_keys) if project_keys else 'all')}</li>",
            "</ul>",
        ]
    )
    issue_details = render_issue_details(jira_links)
    sections = [
        "<h1>Weekly Summary Report</h1>",
        metadata,
        "<h2>Executive Summary</h2>",
        f"<p>{len(done)} of {total} tracked Jira issues are done.</p>",
        "<h2>Completed Work</h2>",
        issue_table(done) if done else "<p>No completed issues found.</p>",
        "<h2>In Progress / Open Work</h2>",
        issue_table(open_items) if open_items else "<p>No open issues found.</p>",
        "<h2>Risks and Ownership Notes</h2>",
    ]
    if unassigned:
        sections.append(f"<p>{len(unassigned)} tracked issue(s) are unassigned.</p>")
    else:
        sections.append("<p>No unassigned tracked issues found.</p>")

    sections.extend(
        [
            "<h2>Next Steps</h2>",
            "<ul>",
            "<li>Review open issues and confirm owner/date expectations.</li>",
            "<li>Update Jira comments before the next weekly report run.</li>",
            "</ul>",
            "<h2>Tracked Jira Status</h2>",
            status_table,
            "<h2>Jira Details</h2>",
            issue_details,
        ]
    )
    return "\n".join(sections)


def fill_template_sections(
    template_body: str,
    section_replacements: dict[str, str],
) -> tuple[str, int]:
    filled = template_body
    replacement_count = 0
    for heading_text, replacement_html in section_replacements.items():
        filled, replaced = replace_section_after_heading(filled, heading_text, replacement_html)
        if replaced:
            replacement_count += 1

    return filled, replacement_count


def replace_section_after_heading(
    template_body: str,
    heading_text: str,
    replacement_html: str,
) -> tuple[str, bool]:
    headings = list(re.finditer(r"<h[1-6]\b[^>]*>.*?</h[1-6]>", template_body, re.IGNORECASE | re.DOTALL))
    normalized_heading_text = normalize_html_text(heading_text).casefold()

    for index, heading in enumerate(headings):
        current_heading_text = normalize_html_text(heading.group(0)).casefold()
        if current_heading_text != normalized_heading_text:
            continue

        section_start = heading.end()
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(template_body)
        return (
            f"{template_body[:section_start]}\n{replacement_html}\n{template_body[section_end:]}",
            True,
        )

    return template_body, False


def normalize_html_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def normalize_llm_html(value: str | None) -> str | None:
    if value is None:
        return None

    content = value.strip()
    fenced_match = re.fullmatch(r"```(?:html)?\s*(.*?)\s*```", content, re.IGNORECASE | re.DOTALL)
    if fenced_match:
        content = fenced_match.group(1).strip()

    if not content:
        return None

    return content


def issue_list(
    jira_links: list[dict[str, Any]],
    empty_text: str,
    include_status: bool,
) -> str:
    if not jira_links:
        return f"<p>{html.escape(empty_text)}</p>"

    rows = ["<ul>"]
    for issue in jira_links:
        details = [
            f"<a href=\"{html.escape(issue.get('url') or '')}\">{html.escape(issue.get('key') or '')}</a>",
            html.escape(issue.get("summary") or ""),
        ]
        if include_status:
            details.append(f"Status: {html.escape(issue.get('status') or '')}")
        assignee = issue.get("assignee") or "Unassigned"
        details.append(f"Assignee: {html.escape(assignee)}")
        rows.append(f"<li><p>{' - '.join(part for part in details if part)}</p></li>")

    rows.append("</ul>")
    return "\n".join(rows)


def issue_assignee_table(jira_links: list[dict[str, Any]]) -> str:
    if not jira_links:
        return "<p>No open tracked Jira issues found.</p>"

    rows = [
        '<table data-layout="default">',
        "<tbody>",
        "<tr><th><p>Open Item</p></th><th><p>Assignee</p></th><th><p>Status</p></th></tr>",
    ]
    for issue in jira_links:
        rows.append(
            "<tr>"
            f"<td><p><a href=\"{html.escape(issue.get('url') or '')}\">{html.escape(issue.get('key') or '')}</a> "
            f"{html.escape(issue.get('summary') or '')}</p></td>"
            f"<td><p>{html.escape(issue.get('assignee') or 'Unassigned')}</p></td>"
            f"<td><p>{html.escape(issue.get('status') or '')}</p></td>"
            "</tr>"
        )

    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def issue_summary_list(jira_links: list[dict[str, Any]]) -> str:
    if not jira_links:
        return "<p>No open tracked Jira issues found.</p>"

    rows = ["<ul>"]
    for issue in jira_links:
        rows.append(
            "<li><p>"
            f"{html.escape(issue.get('key') or '')}: "
            f"{html.escape(issue_summary_sentence(issue))}"
            "</p></li>"
        )

    rows.append("</ul>")
    return "\n".join(rows)


def issue_summary_sentence(issue: dict[str, Any]) -> str:
    comments = (issue.get("comments") or {}).get("comments", [])
    latest_comment = comments[0].get("body") if comments else None
    if latest_comment:
        return (
            f"{issue.get('summary') or 'Issue'} is {issue.get('status') or 'unknown'}; "
            f"latest comment: {latest_comment}"
        )

    description = issue.get("description")
    if description:
        first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
        if first_line:
            return f"{issue.get('summary') or 'Issue'} is {issue.get('status') or 'unknown'}; {first_line}"

    return f"{issue.get('summary') or 'Issue'} is {issue.get('status') or 'unknown'}."


def issue_table(jira_links: list[dict[str, Any]]) -> str:
    rows = [
        "<table>",
        "<tbody>",
        "<tr><th>Key</th><th>Status</th><th>Summary</th><th>Assignee</th><th>Priority</th></tr>",
    ]
    for issue in jira_links:
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(issue.get('url') or '')}\">{html.escape(issue.get('key') or '')}</a></td>"
            f"<td>{html.escape(issue.get('status') or '')}</td>"
            f"<td>{html.escape(issue.get('summary') or '')}</td>"
            f"<td>{html.escape(issue.get('assignee') or 'Unassigned')}</td>"
            f"<td>{html.escape(issue.get('priority') or '')}</td>"
            "</tr>"
        )

    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def render_issue_details(jira_links: list[dict[str, Any]]) -> str:
    sections = []
    for issue in jira_links:
        sections.append(f"<h3>{html.escape(issue.get('key') or '')}: {html.escape(issue.get('summary') or '')}</h3>")
        sections.append("<ul>")
        sections.append(f"<li>Status: {html.escape(issue.get('status') or '')}</li>")
        sections.append(f"<li>Assignee: {html.escape(issue.get('assignee') or 'Unassigned')}</li>")
        sections.append(f"<li>Updated: {html.escape(issue.get('updated') or '')}</li>")
        sections.append(f"<li>Due date: {html.escape(issue.get('due_date') or '')}</li>")
        sections.append("</ul>")

        description = issue.get("description")
        if description:
            sections.append("<p><strong>Description</strong></p>")
            sections.append(f"<p>{html.escape(description)}</p>")

        comments = (issue.get("comments") or {}).get("comments", [])
        if comments:
            sections.append("<p><strong>Recent Comments</strong></p>")
            sections.append("<ul>")
            for comment in comments:
                body = comment.get("body") or ""
                sections.append(
                    "<li>"
                    f"{html.escape(comment.get('author') or 'Unknown')}: "
                    f"{html.escape(body)}"
                    "</li>"
                )
            sections.append("</ul>")

    return "\n".join(sections) if sections else "<p>No Jira details found.</p>"


def create_report_page(
    confluence_client: ConfluenceClient,
    title: str,
    report_html: str,
    output_parent_page_url: str | None,
    output_space_key: str | None,
) -> dict[str, Any]:
    if output_parent_page_url:
        return confluence_client.create_child_page_from_html(
            parent_page_url=output_parent_page_url,
            title=title,
            html_body=report_html,
        )

    if output_space_key:
        return confluence_client.create_page_from_html(
            space_key=output_space_key,
            title=title,
            html_body=report_html,
        )

    raise ValueError("Either output_parent_page_url or output_space_key is required.")


def issue_for_prompt(jira_link: dict[str, Any], max_comments_per_issue: int) -> dict[str, Any]:
    comments = (jira_link.get("comments") or {}).get("comments", [])
    return {
        "key": jira_link.get("key"),
        "url": jira_link.get("url"),
        "summary": jira_link.get("summary"),
        "description": jira_link.get("description"),
        "status": jira_link.get("status"),
        "status_category": jira_link.get("status_category"),
        "issue_type": jira_link.get("issue_type"),
        "project": jira_link.get("project"),
        "assignee": jira_link.get("assignee"),
        "priority": jira_link.get("priority"),
        "created": jira_link.get("created"),
        "updated": jira_link.get("updated"),
        "due_date": jira_link.get("due_date"),
        "labels": jira_link.get("labels"),
        "comments": comments[:max_comments_per_issue],
    }


def is_done(jira_link: dict[str, Any]) -> bool:
    return str(jira_link.get("status_category") or "").casefold() == "done"


def print_dry_run(result: dict[str, Any]) -> None:
    print(json.dumps(
        {
            "dry_run": True,
            "title": result["title"],
            "source_page": {
                "title": result["source_page"].get("title"),
                "url": result["source_page"].get("url"),
            },
            "template": result["template"],
            "jira_issue_count": len(result["jira_links"]),
            "ignored_jira_macro_count": len(result["ignored_jira_macros"]),
        },
        indent=2,
    ))
    print("\n--- REPORT HTML ---")
    print(result["report_html"])


def print_created_page(created_page: dict[str, Any]) -> None:
    print("Created Weekly Executive Report page.")
    print(json.dumps(created_page, indent=2))


def main() -> int:
    args = parse_args()
    confluence_client = ConfluenceClient.from_env()
    jira_client = JiraClient.from_env()

    try:
        while True:
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{started_at}] Running weekly executive report cycle.")
            run_cycle(confluence_client, jira_client, args)

            if args.once:
                return 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped weekly executive report runner.")
        return 130
    except (ConfluenceAPIError, JiraAPIError, URLError, TimeoutError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
