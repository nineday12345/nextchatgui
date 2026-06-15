"""NextChatGUI dashboard and browser-enhancement plugin."""

from __future__ import annotations

from .browser_tools import (
    AUTOCOMPLETE_SCHEMA,
    DOWNLOADS_SCHEMA,
    EVIDENCE_SCHEMA,
    EXTRACT_TABLES_SCHEMA,
    FILL_FORM_SCHEMA,
    HUMAN_CHECKPOINT_SCHEMA,
    NETWORK_SCHEMA,
    SHIPPING_DETECT_SCHEMA,
    SHIPPING_EXTRACT_SCHEMA,
    SHIPPING_MODAL_SCHEMA,
    TABS_SCHEMA,
    WAIT_SELECTOR_SCHEMA,
    WAIT_TEXT_SCHEMA,
    _check_browser_tools_available,
    handle_capture_network,
    handle_downloads,
    handle_evidence,
    handle_extract_tables,
    handle_fill_form,
    handle_human_checkpoint,
    handle_select_autocomplete,
    handle_shipping_detect,
    handle_shipping_extract_schedules,
    handle_shipping_modal,
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
    ("next_browser_select_autocomplete", AUTOCOMPLETE_SCHEMA, handle_select_autocomplete),
    ("next_browser_evidence", EVIDENCE_SCHEMA, handle_evidence),
    ("next_browser_human_checkpoint", HUMAN_CHECKPOINT_SCHEMA, handle_human_checkpoint),
    ("next_browser_shipping_detect", SHIPPING_DETECT_SCHEMA, handle_shipping_detect),
    ("next_browser_shipping_extract_schedules", SHIPPING_EXTRACT_SCHEMA, handle_shipping_extract_schedules),
    ("next_browser_shipping_modal", SHIPPING_MODAL_SCHEMA, handle_shipping_modal),
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
