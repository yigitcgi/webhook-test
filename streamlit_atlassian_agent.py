from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

from langgraph_atlassian_agent import (
    DEFAULT_MCP_SERVER,
    DEFAULT_RECURSION_LIMIT,
    agent_structured_output,
    build_agent,
    extract_tool_io,
    invoke_agent,
    message_content,
)


ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_HISTORY_LIMIT = 12


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []


def run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def run_agent_turn(
    prompt: str,
    mcp_server: str,
    recursion_limit: int,
    history_limit: int,
) -> dict[str, Any]:
    agent = await build_agent(mcp_server)
    history = [
        {"role": message["role"], "content": message["content"]}
        for message in st.session_state.messages[-history_limit:]
        if message["role"] in {"user", "assistant"}
    ]
    messages = [*history, {"role": "user", "content": prompt}]
    return await invoke_agent(
        agent=agent,
        messages=messages,
        recursion_limit=recursion_limit,
    )


def render_trace(result: dict[str, Any]) -> None:
    messages = result.get("messages", [])
    if not messages:
        return

    with st.expander("Trace", expanded=False):
        for index, message in enumerate(messages, start=1):
            role = getattr(message, "type", message.__class__.__name__)
            st.markdown(f"**{index}. {role}**")
            st.code(message_content(message))


def render_citations(citations: list[dict[str, Any]]) -> None:
    if not citations:
        return

    with st.expander("Supporting Citations", expanded=False):
        for citation in citations:
            source = citation.get("source") or "Source"
            reference = citation.get("reference")
            quote = citation.get("quote")
            st.markdown(f"**{source}**")
            if reference:
                st.caption(str(reference))
            if quote:
                st.write(str(quote))


def render_tool_io(tool_io: list[dict[str, Any]]) -> None:
    if not tool_io:
        return

    with st.expander("Tool Inputs and Outputs", expanded=False):
        for tool_call in tool_io:
            st.markdown(f"**{tool_call.get('tool') or 'tool'}**")
            st.json(tool_call)


def render_chat_message(message: dict[str, Any], show_trace: bool, show_tool_io: bool) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("plan"):
            with st.expander("Agent Plan", expanded=False):
                st.code(message["plan"])
        render_citations(message.get("citations") or [])
        if show_tool_io:
            render_tool_io(message.get("tool_io") or [])
        if show_trace and message.get("trace"):
            render_trace(message["trace"])


def clear_chat() -> None:
    st.session_state.messages = []


def sidebar_settings() -> tuple[str, int, int, bool, bool]:
    default_mcp_server = os.getenv("ATLASSIAN_MCP_SERVER", str(DEFAULT_MCP_SERVER))
    default_recursion_limit = int(
        os.getenv("ATLASSIAN_AGENT_RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT)
    )
    default_history_limit = int(os.getenv("ATLASSIAN_AGENT_HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT))

    with st.sidebar:
        mcp_server = st.text_input("MCP server", value=default_mcp_server)
        recursion_limit = st.number_input(
            "Recursion limit",
            min_value=5,
            max_value=100,
            value=default_recursion_limit,
            step=1,
        )
        history_limit = st.number_input(
            "History messages",
            min_value=2,
            max_value=50,
            value=default_history_limit,
            step=2,
        )
        show_tool_io = st.checkbox(
            "Show tool inputs and outputs",
            value=env_bool("SHOW_TOOL_IO", default=False),
        )
        show_trace = st.checkbox("Show raw trace", value=False)
        st.button("Clear chat", on_click=clear_chat, use_container_width=True)

    return mcp_server, int(recursion_limit), int(history_limit), show_tool_io, show_trace


def main() -> None:
    load_dotenv(ENV_FILE)
    st.set_page_config(page_title="Atlassian Agent", page_icon="A", layout="wide")
    init_state()

    mcp_server, recursion_limit, history_limit, show_tool_io, show_trace = sidebar_settings()

    st.title("Atlassian Agent")

    for message in st.session_state.messages:
        render_chat_message(message, show_trace=show_trace, show_tool_io=show_tool_io)

    prompt = st.chat_input("Ask about Confluence or Jira")
    if not prompt:
        return

    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    render_chat_message(user_message, show_trace=False, show_tool_io=False)

    with st.chat_message("assistant"):
        with st.spinner("Working"):
            try:
                result = run_async(
                    run_agent_turn(
                        prompt=prompt,
                        mcp_server=mcp_server,
                        recursion_limit=recursion_limit,
                        history_limit=history_limit,
                    )
                )
                answer_data = agent_structured_output(result)
                answer = answer_data["answer"]
                plan = answer_data.get("plan") or ""
                citations = answer_data.get("citations") or []
                tool_io = extract_tool_io(result)
            except Exception as error:
                result = None
                answer = f"Agent error: {error}"
                plan = ""
                citations = []
                tool_io = []

        st.markdown(answer)
        if plan:
            with st.expander("Agent Plan", expanded=False):
                st.code(plan)
        render_citations(citations)
        if show_tool_io:
            render_tool_io(tool_io)
        if show_trace and result:
            render_trace(result)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "plan": plan,
            "citations": citations,
            "tool_io": tool_io,
            "trace": result,
        }
    )


if __name__ == "__main__":
    main()
