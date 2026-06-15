"""NextChatGUI dashboard and browser-enhancement plugin."""

from __future__ import annotations

from .browser_tools import (
    DOWNLOADS_SCHEMA,
    EXTRACT_TABLES_SCHEMA,
    FILL_FORM_SCHEMA,
    NETWORK_SCHEMA,
    TABS_SCHEMA,
    WAIT_SELECTOR_SCHEMA,
    WAIT_TEXT_SCHEMA,
    _check_browser_tools_available,
    handle_capture_network,
    handle_downloads,
    handle_extract_tables,
    handle_fill_form,
    handle_tabs,
    handle_wait_for_selector,
    handle_wait_for_text,
)


_TOOLS = (
    ("next_browser_tabs", TABS_SCHEMA, handle_tabs),
    ("next_browser_extract_tables", EXTRACT_TABLES_SCHEMA, handle_extract_tables),
    ("next_browser_wait_for_text", WAIT_TEXT_SCHEMA, handle_wait_for_text),
    ("next_browser_wait_for_selector", WAIT_SELECTOR_SCHEMA, handle_wait_for_selector),
    ("next_browser_downloads", DOWNLOADS_SCHEMA, handle_downloads),
    ("next_browser_capture_network", NETWORK_SCHEMA, handle_capture_network),
    ("next_browser_fill_form", FILL_FORM_SCHEMA, handle_fill_form),
)


def register(ctx) -> None:
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="next_browser",
            schema=schema,
            handler=handler,
            check_fn=_check_browser_tools_available,
        )
