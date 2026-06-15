"""Hermes model tools for the NextChatGUI visible browser.

These tools intentionally wrap Chrome DevTools Protocol (CDP) into higher-level
operations so the model does not have to hand-write Runtime.evaluate snippets
for common business-browser tasks.
"""

from __future__ import annotations

import csv
import base64
import copy
import io
import json
import os
import time
from pathlib import Path
from typing import Any

from tools.registry import tool_error, tool_result


ENV_BROWSER_CDP_KEYS = (
    "BROWSER_CDP_URL",
    "HERMES_NEXTCHATGUI_BROWSER_CDP_URL",
    "NEXTCHATGUI_BROWSER_CDP_URL",
)

ENV_WORKSPACE_ROOT_KEYS = (
    "HERMES_NEXTCHATGUI_WORKSPACE_ROOT",
    "HERMES_NEXTCHAT_WORKSPACE_ROOT",
    "HERMES_WORKSPACES_ROOT",
)

DOWNLOAD_NOT_VISIBLE_WARNING = (
    "No completed download file became visible from the Hermes process. "
    "When Chrome runs in a separate chrome-novnc container, the download "
    "directory must be a volume shared by both the browser container and the "
    "Hermes container. Some sites may also ignore synthetic element.click() "
    "for downloads that require a real user gesture."
)

DEFAULT_WORKSPACE_ROOTS = (
    Path("/opt/data/nextchatgui-workspaces"),
    Path("/data/nextchatgui-workspaces"),
)

_LAST_TARGET_ID: str | None = None


def _first_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _ensure_cdp_env() -> str:
    cdp_url = _first_env(ENV_BROWSER_CDP_KEYS)
    if cdp_url and not os.environ.get("BROWSER_CDP_URL"):
        os.environ["BROWSER_CDP_URL"] = cdp_url
    return cdp_url


def _check_browser_tools_available() -> bool:
    return bool(_first_env(ENV_BROWSER_CDP_KEYS))


def _json_loads_tool(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON CDP result: {raw[:300]}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected CDP result shape")
    return payload


def _cdp(
    method: str,
    params: dict[str, Any] | None = None,
    target_id: str | None = None,
    timeout: float = 30.0,
    task_id: str | None = None,
) -> dict[str, Any]:
    _ensure_cdp_env()
    from tools.browser_cdp_tool import browser_cdp

    raw = browser_cdp(
        method=method,
        params=params or {},
        target_id=target_id or None,
        timeout=timeout,
        task_id=task_id,
    )
    payload = _json_loads_tool(raw)
    return payload.get("result", {}) if isinstance(payload.get("result"), dict) else payload


def _page_targets() -> list[dict[str, Any]]:
    result = _cdp("Target.getTargets", {})
    rows = result.get("targetInfos") or []
    pages: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "page":
            continue
        target_id = str(row.get("targetId") or "").strip()
        if not target_id:
            continue
        pages.append(
            {
                "target_id": target_id,
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "attached": bool(row.get("attached")),
            }
        )
    return pages


def _target_exists(target_id: str | None, pages: list[dict[str, Any]] | None = None) -> bool:
    if not target_id:
        return False
    rows = pages if pages is not None else _page_targets()
    return any(page.get("target_id") == target_id for page in rows)


def _remember_target(target_id: str | None) -> None:
    global _LAST_TARGET_ID
    value = str(target_id or "").strip()
    if value:
        _LAST_TARGET_ID = value


def _focused_target_id(pages: list[dict[str, Any]]) -> str:
    for page in pages:
        target_id = page.get("target_id")
        url = page.get("url", "")
        if not target_id or url.startswith(("chrome://", "devtools://", "blob:chrome://")):
            continue
        try:
            result = _cdp(
                "Runtime.evaluate",
                {"expression": "document.hasFocus()", "returnByValue": True},
                target_id=target_id,
                timeout=1.5,
            )
            if result.get("result", {}).get("value") is True:
                return str(target_id)
        except Exception:
            continue
    return ""


def _pick_target(target_id: str | None = None) -> str:
    if target_id:
        target = str(target_id).strip()
        _remember_target(target)
        return target
    pages = _page_targets()
    focused = _focused_target_id(pages)
    if focused:
        _remember_target(focused)
        return focused
    if _target_exists(_LAST_TARGET_ID, pages):
        return str(_LAST_TARGET_ID)
    for page in pages:
        url = page.get("url", "")
        if url and not url.startswith(("chrome://", "devtools://", "blob:chrome://")):
            _remember_target(page["target_id"])
            return page["target_id"]
    if pages:
        _remember_target(pages[0]["target_id"])
        return pages[0]["target_id"]
    raise RuntimeError("No browser page target found")


def _target_status(target_id: str | None = None) -> dict[str, Any]:
    target = _pick_target(target_id)
    pages = _page_targets()
    info = next((page for page in pages if page.get("target_id") == target), {})
    status: dict[str, Any] = {
        "target_id": target,
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "attached": bool(info.get("attached")),
        "active": False,
    }
    try:
        result = _cdp(
            "Runtime.evaluate",
            {
                "expression": "({url: location.href, title: document.title, active: document.hasFocus(), forms: document.forms.length})",
                "returnByValue": True,
            },
            target_id=target,
            timeout=3,
        )
        value = result.get("result", {}).get("value") or {}
        if isinstance(value, dict):
            status.update(
                {
                    "url": str(value.get("url") or status["url"]),
                    "title": str(value.get("title") or status["title"]),
                    "active": bool(value.get("active")),
                    "form_count": int(value.get("forms") or 0),
                }
            )
    except Exception:
        pass
    return status


def _runtime_eval(
    expression: str,
    target_id: str | None = None,
    timeout: float = 30.0,
    await_promise: bool = True,
    task_id: str | None = None,
) -> Any:
    target = _pick_target(target_id)
    _remember_target(target)
    result = _cdp(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": bool(await_promise),
        },
        target_id=target,
        timeout=timeout,
        task_id=task_id,
    )
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        text = details.get("text") or details.get("exception", {}).get("description")
        raise RuntimeError(text or "Runtime.evaluate failed")
    value = result.get("result", {})
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    if isinstance(value, dict):
        return value.get("description") or value
    return value


def _workspace_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ENV_WORKSPACE_ROOT_KEYS:
        raw = os.environ.get(key, "").strip()
        if raw:
            roots.append(Path(raw).expanduser().resolve(strict=False))
    roots.extend(root.resolve(strict=False) for root in DEFAULT_WORKSPACE_ROOTS if root.parent.exists())
    return roots


def _allowed_output_roots() -> list[Path]:
    roots = _workspace_roots()
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        roots.append(Path(hermes_home).expanduser().resolve(strict=False))
    for root in (Path("/opt/data"), Path("/data")):
        if root.exists():
            roots.append(root.resolve(strict=False))

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _safe_output_path(value: str | None) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    was_absolute = candidate.is_absolute()
    if not was_absolute:
        candidate = (Path.cwd() / candidate).resolve(strict=False)
    else:
        candidate = candidate.resolve(strict=False)

    roots = _allowed_output_roots()
    if roots and was_absolute:
        allowed = False
        for root in roots:
            try:
                candidate.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise RuntimeError(
                "output_path must be inside the NextChatGUI workspace root "
                "or be a relative path"
            )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _rows_to_csv(rows: list[list[Any]]) -> str:
    stream = io.StringIO()
    writer = csv.writer(stream)
    for row in rows:
        writer.writerow(["" if value is None else str(value) for value in row])
    return stream.getvalue()


def _first_table_csv(payload: dict[str, Any]) -> str:
    tables = payload.get("tables") or []
    if not tables:
        return ""
    rows = tables[0].get("rows") or []
    return _rows_to_csv(rows)


EXTRACT_TABLES_SCHEMA: dict[str, Any] = {
    "name": "next_browser_extract_tables",
    "description": (
        "Extract structured tables and repeated card/list rows from the current "
        "visible CDP browser tab. Use this for shipping schedules, prices, "
        "flight lists, search results, recruiting pages, and other business "
        "web pages where the useful data is in tables or repeated cards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {"type": "string", "description": "Optional Chrome target/tab id."},
            "selector": {"type": "string", "description": "Optional CSS selector that scopes table/card extraction."},
            "table_index": {"type": "integer", "description": "Optional table index to return after filtering/classification.", "minimum": 0},
            "include_cards": {"type": "boolean", "description": "Also extract repeated card/list groups.", "default": True},
            "include_layout_tables": {"type": "boolean", "description": "Include low-score layout/navigation tables.", "default": False},
            "max_rows": {"type": "integer", "description": "Maximum rows per table/list.", "default": 200, "minimum": 1},
            "max_tables": {"type": "integer", "description": "Maximum tables to return.", "default": 20, "minimum": 1},
            "max_chars": {"type": "integer", "description": "Maximum JSON-sized response budget before summaries are compacted.", "default": 60000, "minimum": 1000},
            "output_format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
            "output_path": {"type": "string", "description": "Optional path to save JSON/CSV output."},
        },
        "additionalProperties": False,
    },
}


WAIT_TEXT_SCHEMA: dict[str, Any] = {
    "name": "next_browser_wait_for_text",
    "description": "Wait until the current browser tab contains visible text.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to wait for."},
            "target_id": {"type": "string"},
            "timeout": {"type": "number", "default": 30},
            "case_sensitive": {"type": "boolean", "default": False},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


WAIT_SELECTOR_SCHEMA: dict[str, Any] = {
    "name": "next_browser_wait_for_selector",
    "description": "Wait until a CSS selector exists, optionally requiring it to be visible.",
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector to wait for."},
            "target_id": {"type": "string"},
            "timeout": {"type": "number", "default": 30},
            "visible": {"type": "boolean", "default": True},
        },
        "required": ["selector"],
        "additionalProperties": False,
    },
}


DOWNLOADS_SCHEMA: dict[str, Any] = {
    "name": "next_browser_downloads",
    "description": (
        "Manage browser downloads for the visible Chrome session. Can set the "
        "download directory, list downloaded files, wait for a download to "
        "finish, or click a selector and wait for the resulting file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set_dir", "list", "wait", "click_and_wait"], "default": "list"},
            "download_dir": {"type": "string", "description": "Directory for downloads. Relative paths use the process cwd."},
            "selector": {"type": "string", "description": "CSS selector to click for click_and_wait."},
            "target_id": {"type": "string"},
            "timeout": {"type": "number", "default": 60},
        },
        "additionalProperties": False,
    },
}


NETWORK_SCHEMA: dict[str, Any] = {
    "name": "next_browser_capture_network",
    "description": (
        "Start/read/clear a lightweight in-page fetch/XMLHttpRequest/form-submit "
        "recorder. Use it to inspect recent XHR/fetch URLs, traditional form "
        "POST parameters, status codes, and response previews after interacting "
        "with a page."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "read", "clear"], "default": "read"},
            "target_id": {"type": "string"},
            "max_entries": {"type": "integer", "default": 30, "minimum": 1},
            "max_body_chars": {"type": "integer", "default": 4000, "minimum": 100},
        },
        "additionalProperties": False,
    },
}


AUTOCOMPLETE_SCHEMA: dict[str, Any] = {
    "name": "next_browser_select_autocomplete",
    "description": (
        "Fill an autocomplete field, wait for suggestions, click the matching "
        "suggestion, and report hidden form values. Use this for business sites "
        "where visible text such as CNSHA maps to hidden codes such as CNSHG."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "Semantic field label/name to match when selector is not supplied."},
            "field_selector": {"type": "string", "description": "CSS selector for the input/control."},
            "query": {"type": "string", "description": "Text to type into the autocomplete field."},
            "match_text": {"type": "string", "description": "Suggestion text to click. Defaults to query."},
            "target_id": {"type": "string"},
            "wait_ms": {"type": "integer", "default": 1500, "minimum": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


EVIDENCE_SCHEMA: dict[str, Any] = {
    "name": "next_browser_evidence",
    "description": (
        "Collect a compact evidence package for the current browser tab: target "
        "status, URL/title, forms including hidden values, recent network/form "
        "logs, table summaries, visible text preview, and optional screenshot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {"type": "string"},
            "output_path": {"type": "string", "description": "Optional JSON file path for the evidence package."},
            "screenshot_path": {"type": "string", "description": "Optional PNG path for a screenshot."},
            "max_tables": {"type": "integer", "default": 12, "minimum": 1},
            "max_network_entries": {"type": "integer", "default": 30, "minimum": 1},
            "text_chars": {"type": "integer", "default": 1200, "minimum": 0},
        },
        "additionalProperties": False,
    },
}


HUMAN_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "name": "next_browser_human_checkpoint",
    "description": (
        "Pause browser automation so the user can manually solve a CAPTCHA, "
        "slider verification, login, or MFA challenge in the visible browser. "
        "This tool does not bypass verification; it waits until the challenge "
        "appears cleared, the page changes, or an explicit success condition is met."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {"type": "string"},
            "reason": {"type": "string", "description": "Short user-facing reason, e.g. Solve OOCL slider verification."},
            "mode": {"type": "string", "enum": ["captcha", "login", "mfa", "manual"], "default": "captcha"},
            "wait_for_text": {"type": "string", "description": "Optional text that means the user finished verification."},
            "wait_for_selector": {"type": "string", "description": "Optional selector that should appear after verification."},
            "wait_until_text_gone": {"type": "string", "description": "Optional text that should disappear after verification."},
            "wait_until_selector_gone": {"type": "string", "description": "Optional selector that should disappear after verification."},
            "require_page_change": {"type": "boolean", "default": False},
            "timeout": {"type": "number", "default": 300},
            "poll_interval": {"type": "number", "default": 1},
        },
        "additionalProperties": False,
    },
}


SHIPPING_DETECT_SCHEMA: dict[str, Any] = {
    "name": "next_browser_shipping_detect",
    "description": (
        "Detect whether the current page is an Evergreen/ShipmentLink or OOCL "
        "sailing schedule page, then summarize forms, hidden fields, detail "
        "modal triggers, and recommended next browser tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {"type": "string"},
            "text_chars": {"type": "integer", "default": 1000, "minimum": 0},
        },
        "additionalProperties": False,
    },
}


SHIPPING_EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "next_browser_shipping_extract_schedules",
    "description": (
        "Carrier-aware extraction for Evergreen/ShipmentLink and OOCL schedule "
        "result pages. It converts schedule tables/JSP row groups into normalized "
        "records with ETD/ETA/vessel/voyage/service/cutoff/POL/POD/delivery "
        "where possible, while preserving raw cells and detail triggers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "carrier": {"type": "string", "enum": ["auto", "evergreen", "oocl"], "default": "auto"},
            "target_id": {"type": "string"},
            "selector": {"type": "string", "description": "Optional CSS selector scoping result extraction."},
            "max_records": {"type": "integer", "default": 50, "minimum": 1},
            "include_raw": {"type": "boolean", "default": True},
            "output_path": {"type": "string", "description": "Optional JSON file path for full extraction."},
        },
        "additionalProperties": False,
    },
}


SHIPPING_MODAL_SCHEMA: dict[str, Any] = {
    "name": "next_browser_shipping_modal",
    "description": (
        "Open and extract a carrier schedule detail modal, especially "
        "ShipmentLink/Evergreen Details links with ectype='modalbox' and params "
        "such as seq=21. Returns modal text, tables, and then optionally closes it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {"type": "string"},
            "selector": {"type": "string", "description": "CSS selector for the detail trigger."},
            "text": {"type": "string", "description": "Trigger text to match, e.g. Details."},
            "params": {"type": "string", "description": "Trigger params attribute to match, e.g. seq=21."},
            "row_index": {"type": "integer", "description": "Nth matching detail trigger, 0-based.", "minimum": 0},
            "timeout": {"type": "number", "default": 8},
            "close": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
}


FILL_FORM_SCHEMA: dict[str, Any] = {
    "name": "next_browser_fill_form",
    "description": (
        "Fill a web form semantically by matching field names to labels, name/id, "
        "placeholder, aria-label, or select option text. Useful for ordinary "
        "business forms; for complex autocomplete widgets, fill then inspect and "
        "confirm with wait/extract tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "Map of semantic field label to value, e.g. origin -> Shanghai.",
                "additionalProperties": True,
            },
            "target_id": {"type": "string"},
            "submit_selector": {"type": "string", "description": "Optional CSS selector to click after filling."},
            "submit_text": {"type": "string", "description": "Optional button/link text to click after filling."},
        },
        "required": ["fields"],
        "additionalProperties": False,
    },
}


TABS_SCHEMA: dict[str, Any] = {
    "name": "next_browser_tabs",
    "description": "List, activate, create, or close Chrome CDP page tabs for the visible browser.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "activate", "new", "close"], "default": "list"},
            "target_id": {"type": "string", "description": "Target id for activate/close."},
            "url": {"type": "string", "description": "URL for action=new. Defaults to about:blank."},
        },
        "additionalProperties": False,
    },
}


def handle_tabs(args: dict, **_kw) -> str:
    try:
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            target = _pick_target(args.get("target_id"))
            return tool_result({"success": True, "current": _target_status(target), "tabs": _page_targets()})
        if action == "activate":
            target_id = str(args.get("target_id") or "").strip()
            if not target_id:
                return tool_error("target_id is required for action=activate")
            _cdp("Target.activateTarget", {"targetId": target_id})
            _remember_target(target_id)
            return tool_result({"success": True, "action": action, "target": _target_status(target_id), "tabs": _page_targets()})
        if action == "new":
            url = str(args.get("url") or "about:blank").strip() or "about:blank"
            result = _cdp("Target.createTarget", {"url": url})
            target_id = str(result.get("targetId") or "")
            _remember_target(target_id)
            return tool_result({"success": True, "action": action, "target": _target_status(target_id), "tabs": _page_targets()})
        if action == "close":
            target_id = str(args.get("target_id") or "").strip()
            if not target_id:
                return tool_error("target_id is required for action=close")
            result = _cdp("Target.closeTarget", {"targetId": target_id})
            return tool_result({"success": True, "action": action, "target_id": target_id, "result": result, "tabs": _page_targets()})
        return tool_error(f"Unknown action: {action}")
    except Exception as exc:
        return tool_error(f"next_browser_tabs failed: {exc}")


def handle_extract_tables(args: dict, **kw) -> str:
    try:
        include_cards = bool(args.get("include_cards", True))
        max_rows = max(1, min(int(args.get("max_rows") or 200), 2000))
        max_tables = max(1, min(int(args.get("max_tables") or 20), 100))
        max_chars = max(1000, min(int(args.get("max_chars") or 60000), 250000))
        output_format = str(args.get("output_format") or "json").lower()
        target = _pick_target(args.get("target_id"))
        script = _extract_tables_script(
            include_cards=include_cards,
            max_rows=max_rows,
            max_tables=max_tables,
            selector=args.get("selector"),
            include_layout_tables=bool(args.get("include_layout_tables", False)),
        )
        payload = _runtime_eval(
            script,
            target_id=target,
            timeout=45,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            return tool_error("Table extraction returned an unexpected result")
        if args.get("table_index") is not None:
            wanted = int(args.get("table_index"))
            tables = payload.get("tables") or []
            payload["tables"] = [table for table in tables if int(table.get("index", -1)) == wanted]
            payload["table_index_filter"] = wanted
        payload["target"] = _target_status(target)
        payload = _compact_extraction_payload(payload, max_chars=max_chars)

        output_path = _safe_output_path(args.get("output_path"))
        if output_format == "csv":
            content = _first_table_csv(payload)
            if output_path:
                output_path.write_text(content, encoding="utf-8", newline="")
                payload["saved_path"] = str(output_path)
            payload["csv_preview"] = content[:12000]
        else:
            if output_path:
                output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                payload["saved_path"] = str(output_path)
        payload["success"] = True
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_extract_tables failed: {exc}")


def handle_wait_for_text(args: dict, **kw) -> str:
    text = str(args.get("text") or "")
    if not text:
        return tool_error("text is required")
    try:
        timeout = max(1.0, min(float(args.get("timeout") or 30), 300.0))
        target = _pick_target(args.get("target_id"))
        payload = _runtime_eval(
            _wait_text_script(text, timeout, bool(args.get("case_sensitive", False))),
            target_id=target,
            timeout=timeout + 5,
            task_id=kw.get("task_id"),
        )
        if isinstance(payload, dict):
            payload["target"] = _target_status(target)
        return tool_result(payload if isinstance(payload, dict) else {"success": False, "result": payload})
    except Exception as exc:
        return tool_error(f"next_browser_wait_for_text failed: {exc}")


def handle_wait_for_selector(args: dict, **kw) -> str:
    selector = str(args.get("selector") or "")
    if not selector:
        return tool_error("selector is required")
    try:
        timeout = max(1.0, min(float(args.get("timeout") or 30), 300.0))
        target = _pick_target(args.get("target_id"))
        payload = _runtime_eval(
            _wait_selector_script(selector, timeout, bool(args.get("visible", True))),
            target_id=target,
            timeout=timeout + 5,
            task_id=kw.get("task_id"),
        )
        if isinstance(payload, dict):
            payload["target"] = _target_status(target)
        return tool_result(payload if isinstance(payload, dict) else {"success": False, "result": payload})
    except Exception as exc:
        return tool_error(f"next_browser_wait_for_selector failed: {exc}")


def handle_downloads(args: dict, **kw) -> str:
    try:
        action = str(args.get("action") or "list").strip().lower()
        download_dir = _download_dir(args.get("download_dir"))
        if action == "set_dir":
            result = _set_download_dir(download_dir, args.get("target_id"))
            return tool_result({"success": True, "download_dir": str(download_dir), "result": result})
        if action == "list":
            return tool_result({"success": True, "download_dir": str(download_dir), "files": _download_files(download_dir)})
        if action == "wait":
            timeout = max(1.0, min(float(args.get("timeout") or 60), 600.0))
            files = _wait_for_download(download_dir, timeout)
            completed = [item for item in files if item.get("complete")]
            payload: dict[str, Any] = {
                "success": bool(completed),
                "download_dir": str(download_dir),
                "files": completed or files,
                "all_files": files,
                "timed_out": not bool(completed),
            }
            if not completed:
                payload["warning"] = DOWNLOAD_NOT_VISIBLE_WARNING
            return tool_result(payload)
        if action == "click_and_wait":
            selector = str(args.get("selector") or "")
            if not selector:
                return tool_error("selector is required for action=click_and_wait")
            _set_download_dir(download_dir, args.get("target_id"))
            before = {item["name"]: item["modified_at"] for item in _download_files(download_dir)}
            click_result = _runtime_eval(
                _click_selector_script(selector),
                target_id=args.get("target_id"),
                timeout=10,
                task_id=kw.get("task_id"),
            )
            timeout = max(1.0, min(float(args.get("timeout") or 60), 600.0))
            files = _wait_for_download(download_dir, timeout, before=before)
            changed_files = _changed_downloads(files, before)
            completed = [item for item in changed_files if item.get("complete")]
            payload = {
                "success": bool(completed),
                "download_dir": str(download_dir),
                "files": completed or changed_files,
                "all_files": files,
                "click_result": click_result,
                "timed_out": not bool(completed),
            }
            if not completed:
                payload["warning"] = DOWNLOAD_NOT_VISIBLE_WARNING
            return tool_result(payload)
        return tool_error(f"Unknown action: {action}")
    except Exception as exc:
        return tool_error(f"next_browser_downloads failed: {exc}")


def handle_capture_network(args: dict, **kw) -> str:
    try:
        action = str(args.get("action") or "read").strip().lower()
        max_entries = max(1, min(int(args.get("max_entries") or 30), 500))
        max_body_chars = max(100, min(int(args.get("max_body_chars") or 4000), 50000))
        target = _pick_target(args.get("target_id"))
        if action == "start":
            payload = _runtime_eval(
                _network_hook_script(max_body_chars=max_body_chars),
                target_id=target,
                timeout=10,
                task_id=kw.get("task_id"),
            )
            if isinstance(payload, dict):
                payload["target"] = _target_status(target)
            return tool_result(payload if isinstance(payload, dict) else {"success": True, "result": payload})
        if action == "clear":
            payload = _runtime_eval(
                "(() => { window.__nextchatguiNetworkLog = []; try { sessionStorage.removeItem('__nextchatguiNetworkLog'); } catch (_e) {} return {success: true, count: 0}; })()",
                target_id=target,
                timeout=10,
                task_id=kw.get("task_id"),
            )
            if isinstance(payload, dict):
                payload["target"] = _target_status(target)
            return tool_result(payload if isinstance(payload, dict) else {"success": True})
        if action == "read":
            payload = _runtime_eval(
                _network_read_script(max_entries=max_entries),
                target_id=target,
                timeout=10,
                task_id=kw.get("task_id"),
            )
            if isinstance(payload, dict):
                payload["target"] = _target_status(target)
            return tool_result(payload if isinstance(payload, dict) else {"success": True, "entries": payload})
        return tool_error(f"Unknown action: {action}")
    except Exception as exc:
        return tool_error(f"next_browser_capture_network failed: {exc}")


def handle_fill_form(args: dict, **kw) -> str:
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        return tool_error("fields must be a non-empty object")
    try:
        target = _pick_target(args.get("target_id"))
        payload = _runtime_eval(
            _fill_form_script(
                fields=fields,
                submit_selector=args.get("submit_selector"),
                submit_text=args.get("submit_text"),
            ),
            target_id=target,
            timeout=20,
            task_id=kw.get("task_id"),
        )
        if isinstance(payload, dict):
            payload["target"] = _target_status(target)
        return tool_result(payload if isinstance(payload, dict) else {"success": True, "result": payload})
    except Exception as exc:
        return tool_error(f"next_browser_fill_form failed: {exc}")


def handle_select_autocomplete(args: dict, **kw) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")
    try:
        target = _pick_target(args.get("target_id"))
        wait_ms = max(100, min(int(args.get("wait_ms") or 1500), 10000))
        payload = _runtime_eval(
            _select_autocomplete_script(
                field=str(args.get("field") or ""),
                field_selector=str(args.get("field_selector") or ""),
                query=query,
                match_text=str(args.get("match_text") or query),
                wait_ms=wait_ms,
            ),
            target_id=target,
            timeout=max(10, wait_ms / 1000 + 5),
            task_id=kw.get("task_id"),
        )
        if isinstance(payload, dict):
            payload["target"] = _target_status(target)
        return tool_result(payload if isinstance(payload, dict) else {"success": True, "result": payload})
    except Exception as exc:
        return tool_error(f"next_browser_select_autocomplete failed: {exc}")


def handle_evidence(args: dict, **kw) -> str:
    try:
        target = _pick_target(args.get("target_id"))
        max_tables = max(1, min(int(args.get("max_tables") or 12), 50))
        max_network_entries = max(1, min(int(args.get("max_network_entries") or 30), 200))
        text_chars = max(0, min(int(args.get("text_chars") or 1200), 10000))
        payload = _runtime_eval(
            _evidence_script(
                max_tables=max_tables,
                max_network_entries=max_network_entries,
                text_chars=text_chars,
            ),
            target_id=target,
            timeout=20,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            payload = {"result": payload}
        payload["success"] = True
        payload["target"] = _target_status(target)
        screenshot_path = _safe_output_path(args.get("screenshot_path"))
        if screenshot_path:
            shot = _cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, target_id=target, timeout=30)
            data = str(shot.get("data") or "")
            if data:
                screenshot_path.write_bytes(base64.b64decode(data))
                payload["screenshot_path"] = str(screenshot_path)
        output_path = _safe_output_path(args.get("output_path"))
        if output_path:
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["saved_path"] = str(output_path)
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_evidence failed: {exc}")


def handle_human_checkpoint(args: dict, **kw) -> str:
    try:
        target = _pick_target(args.get("target_id"))
        timeout = max(5.0, min(float(args.get("timeout") or 300), 1800.0))
        poll_interval = max(0.25, min(float(args.get("poll_interval") or 1), 10.0))
        payload = _runtime_eval(
            _human_checkpoint_script(
                reason=str(args.get("reason") or ""),
                mode=str(args.get("mode") or "captcha"),
                wait_for_text=str(args.get("wait_for_text") or ""),
                wait_for_selector=str(args.get("wait_for_selector") or ""),
                wait_until_text_gone=str(args.get("wait_until_text_gone") or ""),
                wait_until_selector_gone=str(args.get("wait_until_selector_gone") or ""),
                require_page_change=bool(args.get("require_page_change", False)),
                timeout=timeout,
                poll_interval=poll_interval,
            ),
            target_id=target,
            timeout=timeout + 10,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            payload = {"result": payload}
        payload["target"] = _target_status(target)
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_human_checkpoint failed: {exc}")


def handle_shipping_detect(args: dict, **kw) -> str:
    try:
        target = _pick_target(args.get("target_id"))
        text_chars = max(0, min(int(args.get("text_chars") or 1000), 8000))
        payload = _runtime_eval(
            _shipping_detect_script(text_chars=text_chars),
            target_id=target,
            timeout=15,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            payload = {"result": payload}
        payload["success"] = True
        payload["target"] = _target_status(target)
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_shipping_detect failed: {exc}")


def handle_shipping_extract_schedules(args: dict, **kw) -> str:
    try:
        target = _pick_target(args.get("target_id"))
        max_records = max(1, min(int(args.get("max_records") or 50), 500))
        carrier = str(args.get("carrier") or "auto").strip().lower()
        include_raw = bool(args.get("include_raw", True))
        payload = _runtime_eval(
            _shipping_extract_script(
                carrier=carrier,
                selector=str(args.get("selector") or ""),
                max_records=max_records,
                include_raw=include_raw,
            ),
            target_id=target,
            timeout=35,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            return tool_error("Shipping schedule extraction returned an unexpected result")
        payload["success"] = True
        payload["target"] = _target_status(target)
        output_path = _safe_output_path(args.get("output_path"))
        if output_path:
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["saved_path"] = str(output_path)
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_shipping_extract_schedules failed: {exc}")


def handle_shipping_modal(args: dict, **kw) -> str:
    try:
        target = _pick_target(args.get("target_id"))
        timeout = max(1.0, min(float(args.get("timeout") or 8), 60.0))
        payload = _runtime_eval(
            _shipping_modal_script(
                selector=str(args.get("selector") or ""),
                text=str(args.get("text") or ""),
                params=str(args.get("params") or ""),
                row_index=args.get("row_index"),
                timeout=timeout,
                close=bool(args.get("close", True)),
            ),
            target_id=target,
            timeout=timeout + 5,
            task_id=kw.get("task_id"),
        )
        if not isinstance(payload, dict):
            payload = {"result": payload}
        payload["target"] = _target_status(target)
        return tool_result(payload)
    except Exception as exc:
        return tool_error(f"next_browser_shipping_modal failed: {exc}")


def _payload_size(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return 0


def _compact_extraction_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    if _payload_size(payload) <= max_chars:
        payload["truncated"] = False
        return payload

    compact = copy.deepcopy(payload)
    compact["truncated"] = True
    compact["warning"] = (
        "Extraction exceeded max_chars; rows/records were compacted. "
        "Use output_path, selector, table_index, or a larger max_chars for full data."
    )

    def reduce_tables(row_limit: int, record_limit: int) -> None:
        for table in compact.get("tables") or []:
            rows = table.get("rows") or []
            records = table.get("records") or []
            row_groups = table.get("row_groups") or []
            table["rows"] = rows[:row_limit]
            table["records"] = records[:record_limit]
            table["row_groups"] = row_groups[:record_limit]
            table["truncated_rows"] = max(0, int(table.get("row_count") or len(rows)) - len(table["rows"]))
            table["truncated_records"] = max(0, len(records) - len(table["records"]))

    def reduce_cards(record_limit: int) -> None:
        for group in compact.get("card_lists") or []:
            records = group.get("records") or []
            group["records"] = records[:record_limit]
            group["truncated_records"] = max(0, len(records) - len(group["records"]))

    for row_limit, record_limit, card_limit in ((40, 30, 12), (15, 10, 5), (5, 5, 2)):
        reduce_tables(row_limit, record_limit)
        reduce_cards(card_limit)
        if _payload_size(compact) <= max_chars:
            return compact

    summaries = []
    for table in compact.get("tables") or []:
        summaries.append(
            {
                "index": table.get("index"),
                "caption": table.get("caption"),
                "classification": table.get("classification"),
                "score": table.get("score"),
                "row_count": table.get("row_count"),
                "column_count": table.get("column_count"),
                "headers": table.get("headers", [])[:20],
                "sample_rows": (table.get("rows") or [])[:3],
            }
        )
    compact["tables"] = summaries
    compact["card_lists"] = [
        {
            "key": group.get("key"),
            "count": group.get("count"),
            "sample": (group.get("records") or [])[:1],
        }
        for group in (compact.get("card_lists") or [])[:5]
    ]
    return compact


def _download_dir(value: str | None) -> Path:
    if value:
        path = _safe_output_path(str(value).rstrip("/\\") + "/placeholder")
        assert path is not None
        directory = path.parent
    else:
        configured = os.environ.get("HERMES_NEXTCHATGUI_DOWNLOAD_DIR", "").strip()
        if configured:
            base = Path(configured)
        elif Path("/opt/data").exists():
            base = Path("/opt/data/nextchatgui-downloads")
        elif Path("/data").exists():
            base = Path("/data/nextchatgui-downloads")
        else:
            base = Path.cwd() / "downloads"
        directory = base.expanduser().resolve(strict=False)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _set_download_dir(download_dir: Path, target_id: str | None = None) -> dict[str, Any]:
    try:
        return _cdp(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir), "eventsEnabled": True},
        )
    except Exception:
        target = _pick_target(target_id)
        return _cdp(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir)},
            target_id=target,
        )


def _download_files(download_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(download_dir.glob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
                "complete": not path.name.endswith((".crdownload", ".tmp", ".part")),
            }
        )
    return rows


def _changed_downloads(files: list[dict[str, Any]], before: dict[str, float]) -> list[dict[str, Any]]:
    return [
        item
        for item in files
        if item["name"] not in before or item["modified_at"] != before[item["name"]]
    ]


def _wait_for_download(
    download_dir: Path,
    timeout: float,
    before: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    before = before or {}
    deadline = time.time() + timeout
    last_files: list[dict[str, Any]] = []
    while time.time() < deadline:
        files = _download_files(download_dir)
        last_files = files
        changed = _changed_downloads(files, before)
        if changed and all(item["complete"] for item in changed):
            return changed
        time.sleep(0.5)
    return last_files


def _extract_tables_script(
    include_cards: bool,
    max_rows: int,
    max_tables: int,
    selector: str | None,
    include_layout_tables: bool,
) -> str:
    return f"""
(() => {{
  const maxRows = {int(max_rows)};
  const maxTables = {int(max_tables)};
  const includeCards = {json.dumps(bool(include_cards))};
  const includeLayoutTables = {json.dumps(bool(include_layout_tables))};
  const scopeSelector = {json.dumps(selector or "")};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const trimCell = (value) => clean(value).slice(0, 1000);
  const scopes = scopeSelector ? Array.from(document.querySelectorAll(scopeSelector)) : [document];
  const unique = (nodes) => Array.from(new Set(nodes.filter(Boolean)));
  const tableNodes = unique(scopes.flatMap((scope) => {{
    if (scope.tagName && scope.tagName.toLowerCase() === 'table') return [scope];
    return Array.from(scope.querySelectorAll ? scope.querySelectorAll('table') : []);
  }}));
  const roleTableNodes = unique(scopes.flatMap((scope) =>
    Array.from(scope.querySelectorAll ? scope.querySelectorAll('[role="table"],[role="grid"]') : [])
      .filter((node) => node.tagName.toLowerCase() !== 'table')
  ));
  const directRows = (table) => Array.from(table.rows || table.querySelectorAll('tr'))
    .filter((row) => row.closest('table') === table)
    .slice(0, maxRows);
  const rowsFromTable = (table) => {{
    const grid = [];
    const spans = [];
    directRows(table).forEach((rowNode, rowIndex) => {{
      const row = [];
      let col = 0;
      while (spans[col] && spans[col].rowsLeft > 0) {{
        row[col] = spans[col].text;
        spans[col].rowsLeft -= 1;
        col += 1;
      }}
      const cells = Array.from(rowNode.children || [])
        .filter((cell) => ['td', 'th'].includes(cell.tagName.toLowerCase()));
      cells.forEach((cell) => {{
        while (row[col] !== undefined) col += 1;
        const text = trimCell(cell.innerText || cell.textContent);
        const colspan = Math.max(1, Number(cell.colSpan || 1));
        const rowspan = Math.max(1, Number(cell.rowSpan || 1));
        for (let i = 0; i < colspan; i += 1) {{
          row[col + i] = text;
          if (rowspan > 1) spans[col + i] = {{ text, rowsLeft: rowspan - 1 }};
        }}
        col += colspan;
      }});
      if (row.some(Boolean)) grid.push(row);
    }});
    return grid;
  }};
  const rowsFromRole = (node) => Array.from(node.querySelectorAll('[role="row"]')).slice(0, maxRows).map((row) =>
    Array.from(row.querySelectorAll('[role="cell"],[role="columnheader"],[role="rowheader"]')).map((cell) => trimCell(cell.innerText || cell.textContent))
  ).filter((row) => row.some(Boolean));
  const recordsFrom = (rows) => {{
    if (!rows.length) return [];
    const headers = rows[0].map((h, i) => h || `column_${{i + 1}}`);
    return rows.slice(1).map((row) => {{
      const record = {{}};
      headers.forEach((header, i) => {{ record[header] = row[i] || ''; }});
      return record;
    }});
  }};
  const classify = (node, rows, caption) => {{
    const rowCount = rows.length;
    const columnCount = Math.max(0, ...rows.map((row) => row.length));
    const textLength = rows.flat().join(' ').length;
    const headerCount = node.querySelectorAll ? node.querySelectorAll('th,[role="columnheader"]').length : 0;
    const inputCount = node.querySelectorAll ? node.querySelectorAll('input,select,textarea,button').length : 0;
    const linkCount = node.querySelectorAll ? node.querySelectorAll('a[href]').length : 0;
    const nestedTableCount = node.querySelectorAll ? Array.from(node.querySelectorAll('table')).filter((t) => t !== node).length : 0;
    const nonEmptyCells = rows.flat().filter(Boolean).length;
    const shapeCounts = new Map();
    rows.forEach((row) => shapeCounts.set(row.length, (shapeCounts.get(row.length) || 0) + 1));
    const repeatedShape = Math.max(0, ...Array.from(shapeCounts.values()));
    let score = 0;
    if (rowCount >= 2 && columnCount >= 2) score += 45;
    if (rowCount >= 4) score += 15;
    if (columnCount >= 4) score += 10;
    if (headerCount > 0) score += 18;
    if (caption) score += 8;
    if (repeatedShape >= Math.min(4, rowCount)) score += 12;
    if (inputCount > 0 && rowCount >= 2) score += 6;
    if (rowCount <= 1 || columnCount <= 1) score -= 45;
    if (nestedTableCount > 0 && nonEmptyCells < 8) score -= 25;
    if (textLength > 20000 && rowCount < 8) score -= 25;
    if (linkCount > nonEmptyCells * 0.8 && rowCount < 4) score -= 20;
    const classification = score >= 60 ? 'data' : (score >= 35 ? 'mixed' : 'layout');
    return {{ rowCount, columnCount, textLength, headerCount, inputCount, linkCount, nestedTableCount, score, classification }};
  }};
  const tableFrom = (table, index, kind) => {{
    const rows = kind === 'role' ? rowsFromRole(table) : rowsFromTable(table);
    const caption = clean((table.querySelector('caption') || {{}}).innerText || '');
    const label = caption || clean(table.getAttribute('aria-label') || table.getAttribute('summary') || '');
    const metrics = classify(table, rows, label);
    const headers = rows[0] || [];
    const records = recordsFrom(rows);
    const rowGroups = [];
    if (rows.length >= 6 && headers.length <= 2) {{
      for (let i = 0; i < rows.length; i += 3) {{
        const group = rows.slice(i, i + 3).flat().filter(Boolean);
        if (group.length) rowGroups.push({{ index: i / 3, cells: group }});
      }}
    }}
    return {{
      index,
      kind,
      caption: label,
      classification: metrics.classification,
      score: metrics.score,
      row_count: metrics.rowCount,
      column_count: metrics.columnCount,
      metrics,
      headers,
      rows,
      records,
      row_groups: rowGroups
    }};
  }};
  const allTables = tableNodes.map((table, index) => tableFrom(table, index, 'table'))
    .concat(roleTableNodes.map((node, index) => tableFrom(node, tableNodes.length + index, 'role')))
    .filter((table) => table.rows.length);
  const tables = allTables
    .filter((table) => includeLayoutTables || table.classification !== 'layout')
    .sort((a, b) => (b.score - a.score) || (b.row_count - a.row_count))
    .slice(0, maxTables);
  const cardLists = [];
  if (includeCards) {{
    const containers = unique(scopes.flatMap((scope) =>
      Array.from(scope.querySelectorAll ? scope.querySelectorAll('main, section, article, ul, ol, div') : [])
    ));
    for (const container of containers.slice(0, 300)) {{
      const children = Array.from(container.children || []).filter((child) => clean(child.innerText).length > 15);
      if (children.length < 3 || children.length > 200) continue;
      const groups = new Map();
      for (const child of children) {{
        const key = child.tagName.toLowerCase() + '|' + Array.from(child.classList || []).slice(0, 3).join('.');
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(child);
      }}
      for (const [key, group] of groups.entries()) {{
        if (group.length < 3) continue;
        const records = group.slice(0, maxRows).map((node, index) => {{
          const lines = clean(node.innerText).split(/(?<=\\S)\\s{{2,}}|\\n/).map(clean).filter(Boolean);
          const links = Array.from(node.querySelectorAll('a[href]')).slice(0, 5).map((a) => ({{ text: clean(a.innerText), href: a.href }}));
          return {{ index, text: clean(node.innerText), lines, links }};
        }});
        cardLists.push({{ key, count: group.length, records }});
        if (cardLists.length >= 8) break;
      }}
      if (cardLists.length >= 8) break;
    }}
  }}
  return {{
    url: location.href,
    title: document.title,
    extracted_at: new Date().toISOString(),
    table_count: allTables.length,
    omitted_tables: Math.max(0, allTables.length - tables.length),
    selector: scopeSelector || '',
    tables,
    card_lists: cardLists
  }};
}})()
"""


def _wait_text_script(text: str, timeout: float, case_sensitive: bool) -> str:
    return f"""
new Promise((resolve) => {{
  const needleRaw = {json.dumps(text)};
  const caseSensitive = {json.dumps(bool(case_sensitive))};
  const needle = caseSensitive ? needleRaw : needleRaw.toLowerCase();
  const started = Date.now();
  const deadline = started + {int(timeout * 1000)};
  const tick = () => {{
    const hayRaw = document.body ? document.body.innerText : '';
    const hay = caseSensitive ? hayRaw : hayRaw.toLowerCase();
    const found = hay.includes(needle);
    if (found || Date.now() >= deadline) {{
      const preview = cleanPreview(hayRaw);
      resolve({{
        success: found,
        found,
        elapsed_ms: Date.now() - started,
        url: location.href,
        title: document.title,
        body_chars: hayRaw.length,
        body_preview: preview.slice(0, 1200)
      }});
      return;
    }}
    setTimeout(tick, 250);
  }};
  tick();
  function cleanPreview(value) {{
    return String(value || '').replace(/\\s+/g, ' ').trim();
  }}
}})
"""


def _wait_selector_script(selector: str, timeout: float, visible: bool) -> str:
    return f"""
new Promise((resolve) => {{
  const selector = {json.dumps(selector)};
  const requireVisible = {json.dumps(bool(visible))};
  const started = Date.now();
  const deadline = started + {int(timeout * 1000)};
  const isVisible = (el) => {{
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  }};
  const tick = () => {{
    const el = document.querySelector(selector);
    const found = !!el && (!requireVisible || isVisible(el));
    if (found || Date.now() >= deadline) {{
      const bodyText = document.body ? String(document.body.innerText || '') : '';
      resolve({{
        success: found,
        found,
        selector,
        elapsed_ms: Date.now() - started,
        text: el ? String(el.innerText || el.textContent || '').slice(0, 500) : '',
        url: location.href,
        title: document.title,
        body_preview: bodyText.replace(/\\s+/g, ' ').trim().slice(0, 1200)
      }});
      return;
    }}
    setTimeout(tick, 250);
  }};
  tick();
}})
"""


def _click_selector_script(selector: str) -> str:
    return f"""
(() => {{
  const selector = {json.dumps(selector)};
  const el = document.querySelector(selector);
  if (!el) return {{ success: false, error: 'selector not found', selector }};
  el.scrollIntoView({{ block: 'center', inline: 'center' }});
  el.click();
  return {{ success: true, selector, text: String(el.innerText || el.textContent || '').slice(0, 300) }};
}})()
"""


def _network_hook_script(max_body_chars: int) -> str:
    return f"""
(() => {{
  const maxBody = {int(max_body_chars)};
  const storageKey = '__nextchatguiNetworkLog';
  const restore = () => {{
    try {{
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
      return Array.isArray(saved) ? saved : [];
    }} catch (_e) {{
      return [];
    }}
  }};
  const persist = () => {{
    try {{ sessionStorage.setItem(storageKey, JSON.stringify(window.__nextchatguiNetworkLog.slice(-200))); }} catch (_e) {{}}
  }};
  window.__nextchatguiNetworkLog = window.__nextchatguiNetworkLog || restore();
  const push = (entry) => {{
    window.__nextchatguiNetworkLog.push(entry);
    if (window.__nextchatguiNetworkLog.length > 500) window.__nextchatguiNetworkLog.shift();
    persist();
  }};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const fieldRecord = (field) => {{
    const tag = field.tagName.toLowerCase();
    const type = (field.getAttribute('type') || '').toLowerCase();
    return {{
      name: field.name || field.id || '',
      id: field.id || '',
      tag,
      type,
      hidden: type === 'hidden' || field.hidden || getComputedStyle(field).display === 'none',
      value: tag === 'select'
        ? Array.from(field.selectedOptions || []).map((opt) => opt.value || opt.text).join(',')
        : (type === 'checkbox' || type === 'radio' ? (field.checked ? field.value || 'on' : '') : String(field.value || '')),
      label: clean(field.getAttribute('aria-label') || field.getAttribute('title') || field.getAttribute('placeholder') || '')
    }};
  }};
  if (!window.__nextchatguiNetworkHooked) {{
    window.__nextchatguiNetworkHooked = true;
    document.addEventListener('submit', function(event) {{
      const form = event.target;
      if (!form || !form.elements) return;
      const fields = Array.from(form.elements).map(fieldRecord).filter((row) => row.name || row.value);
      const method = String(form.method || 'GET').toUpperCase();
      const url = form.action || location.href;
      const body = fields.map((row) => encodeURIComponent(row.name) + '=' + encodeURIComponent(row.value)).join('&');
      push({{
        type: 'form_submit',
        url,
        method,
        started_at: new Date().toISOString(),
        request_preview: body.slice(0, maxBody),
        fields,
        hidden_fields: fields.filter((row) => row.hidden),
        submitter: event.submitter ? clean(event.submitter.innerText || event.submitter.value || event.submitter.name || '') : ''
      }});
    }}, true);
    const originalFetch = window.fetch;
    window.fetch = async function(input, init) {{
      const started = new Date().toISOString();
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = (init && init.method) || (input && input.method) || 'GET';
      const entry = {{ type: 'fetch', url, method, started_at: started, request_preview: String((init && init.body) || '').slice(0, maxBody) }};
      push(entry);
      try {{
        const response = await originalFetch.apply(this, arguments);
        entry.status = response.status;
        entry.response_url = response.url;
        entry.ok = response.ok;
        entry.content_type = response.headers.get('content-type') || '';
        response.clone().text().then((text) => {{ entry.response_preview = text.slice(0, maxBody); persist(); }}).catch((err) => {{ entry.response_error = String(err); persist(); }});
        return response;
      }} catch (err) {{
        entry.error = String(err);
        persist();
        throw err;
      }}
    }};
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {{
      this.__nextchatguiRecord = {{ type: 'xhr', method: method || 'GET', url: String(url || ''), started_at: new Date().toISOString() }};
      return originalOpen.apply(this, arguments);
    }};
    XMLHttpRequest.prototype.send = function(body) {{
      const record = this.__nextchatguiRecord || {{ type: 'xhr', method: 'GET', url: '', started_at: new Date().toISOString() }};
      record.request_preview = String(body || '').slice(0, maxBody);
      push(record);
      this.addEventListener('loadend', function() {{
        record.status = this.status;
        record.response_url = this.responseURL;
        record.content_type = this.getResponseHeader('content-type') || '';
        try {{ record.response_preview = String(this.responseText || '').slice(0, maxBody); }} catch (err) {{ record.response_error = String(err); }}
        persist();
      }});
      return originalSend.apply(this, arguments);
    }};
  }}
  return {{ success: true, hooked: true, count: window.__nextchatguiNetworkLog.length, url: location.href }};
}})()
"""


def _network_read_script(max_entries: int) -> str:
    return f"""
(() => {{
  const storageKey = '__nextchatguiNetworkLog';
  let saved = [];
  try {{
    const parsed = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    if (Array.isArray(parsed)) saved = parsed;
  }} catch (_e) {{}}
  const live = window.__nextchatguiNetworkLog || [];
  const entries = (live.length ? live : saved).slice(-{int(max_entries)});
  return {{ success: true, hooked: !!window.__nextchatguiNetworkHooked, count: entries.length, entries, url: location.href }};
}})()
"""


def _fill_form_script(fields: dict[str, Any], submit_selector: str | None, submit_text: str | None) -> str:
    return f"""
(() => {{
  const fields = {json.dumps(fields, ensure_ascii=False)};
  const submitSelector = {json.dumps(submit_selector or "")};
  const submitText = {json.dumps(submit_text or "")};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase().replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
  const labelFor = (el) => {{
    const parts = [];
    for (const attr of ['name', 'id', 'placeholder', 'aria-label', 'title']) parts.push(el.getAttribute(attr) || '');
    if (el.id) {{
      const label = document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`);
      if (label) parts.push(label.innerText || label.textContent || '');
    }}
    const wrappingLabel = el.closest('label');
    if (wrappingLabel) parts.push(wrappingLabel.innerText || wrappingLabel.textContent || '');
    const parent = el.closest('div,li,tr,section,fieldset');
    if (parent) parts.push(Array.from(parent.querySelectorAll('label,legend')).map((n) => n.innerText || n.textContent || '').join(' '));
    return clean(parts.filter(Boolean).join(' '));
  }};
  const controls = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, select, [contenteditable="true"]'));
  const setValue = (el, value) => {{
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'select') {{
      const wanted = norm(value);
      let match = Array.from(el.options).find((opt) => norm(opt.text) === wanted || norm(opt.value) === wanted)
        || Array.from(el.options).find((opt) => norm(opt.text).includes(wanted) || norm(opt.value).includes(wanted));
      if (match) el.value = match.value;
    }} else if (type === 'checkbox' || type === 'radio') {{
      el.checked = ['1', 'true', 'yes', 'on', 'checked'].includes(String(value).toLowerCase());
    }} else if (el.isContentEditable) {{
      el.textContent = String(value);
    }} else {{
      el.focus();
      el.value = String(value);
    }}
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  }};
  const filled = [];
  const missing = [];
  const warnings = [];
  const hiddenRows = (root) => Array.from((root || document).querySelectorAll('input[type="hidden"]')).map((el) => ({{
    name: el.name || el.id || '',
    id: el.id || '',
    value: String(el.value || '')
  }})).filter((row) => row.name || row.value);
  for (const [key, value] of Object.entries(fields)) {{
    const wanted = norm(key);
    let best = null;
    let bestScore = 0;
    for (const el of controls) {{
      const label = labelFor(el);
      const n = norm(label);
      let score = 0;
      if (n === wanted) score = 100;
      else if (n.includes(wanted)) score = 70;
      else if (wanted.includes(n) && n.length > 2) score = 45;
      if (score > bestScore) {{ best = el; bestScore = score; }}
    }}
    if (best && bestScore > 0) {{
      setValue(best, value);
      const form = best.closest('form');
      const hidden_fields = hiddenRows(form || best.closest('tr,li,div,section') || document);
      const emptyLikelyHidden = hidden_fields.filter((row) => !row.value && norm(row.name + ' ' + row.id).includes(norm(best.name || best.id || key).slice(0, 4)));
      if (emptyLikelyHidden.length) {{
        warnings.push(`Field "${{key}}" may require autocomplete selection; nearby hidden value is still empty.`);
      }}
      filled.push({{
        key,
        value: String(value),
        matched_label: labelFor(best),
        tag: best.tagName.toLowerCase(),
        name: best.name || '',
        id: best.id || '',
        score: bestScore,
        hidden_fields
      }});
    }} else {{
      missing.push(key);
    }}
  }}
  let submitted = false;
  if (submitSelector) {{
    const button = document.querySelector(submitSelector);
    if (button) {{ button.click(); submitted = true; }}
  }} else if (submitText) {{
    const wanted = norm(submitText);
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a'));
    const button = buttons.find((el) => norm(el.innerText || el.value || el.textContent).includes(wanted));
    if (button) {{ button.click(); submitted = true; }}
  }}
  return {{
    success: true,
    filled,
    missing,
    warnings,
    all_hidden_fields: hiddenRows(document),
    submitted,
    url: location.href,
    title: document.title
  }};
}})()
"""


def _select_autocomplete_script(
    field: str,
    field_selector: str,
    query: str,
    match_text: str,
    wait_ms: int,
) -> str:
    return f"""
new Promise((resolve) => {{
  const fieldName = {json.dumps(field, ensure_ascii=False)};
  const fieldSelector = {json.dumps(field_selector)};
  const query = {json.dumps(query, ensure_ascii=False)};
  const matchText = {json.dumps(match_text, ensure_ascii=False)};
  const waitMs = {int(wait_ms)};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase().replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, '');
  const visible = (el) => {{
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const labelFor = (el) => {{
    const parts = [];
    for (const attr of ['name', 'id', 'placeholder', 'aria-label', 'title']) parts.push(el.getAttribute(attr) || '');
    if (el.id) {{
      const label = document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`);
      if (label) parts.push(label.innerText || label.textContent || '');
    }}
    const parent = el.closest('label,td,th,div,li,section,fieldset');
    if (parent) parts.push(parent.innerText || parent.textContent || '');
    return clean(parts.filter(Boolean).join(' '));
  }};
  const controls = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]'));
  let input = fieldSelector ? document.querySelector(fieldSelector) : null;
  if (!input && fieldName) {{
    const wanted = norm(fieldName);
    input = controls.map((el) => {{
      const label = norm(labelFor(el));
      let score = 0;
      if (label === wanted) score = 100;
      else if (label.includes(wanted)) score = 70;
      else if (wanted.includes(label) && label.length > 2) score = 45;
      return {{ el, score }};
    }}).sort((a, b) => b.score - a.score)[0]?.el || null;
  }}
  if (!input) {{
    resolve({{ success: false, error: 'autocomplete field not found', field: fieldName, field_selector: fieldSelector }});
    return;
  }}
  input.scrollIntoView({{ block: 'center', inline: 'center' }});
  input.focus();
  if (input.isContentEditable) input.textContent = query;
  else input.value = query;
  input.dispatchEvent(new Event('input', {{ bubbles: true }}));
  input.dispatchEvent(new Event('change', {{ bubbles: true }}));
  input.dispatchEvent(new KeyboardEvent('keyup', {{ key: query.slice(-1) || 'a', bubbles: true }}));

  const hiddenRows = (root) => Array.from((root || document).querySelectorAll('input[type="hidden"]')).map((el) => ({{
    name: el.name || el.id || '',
    id: el.id || '',
    value: String(el.value || '')
  }})).filter((row) => row.name || row.value);
  const candidates = () => {{
    const wanted = norm(matchText || query);
    const nodes = Array.from(document.querySelectorAll(
      '[role="option"], .ui-menu-item, .autocomplete-suggestion, .autocomplete-item, .tt-suggestion, li, a, div, span, td'
    ));
    return nodes
      .filter((node) => visible(node))
      .map((node) => ({{ node, text: clean(node.innerText || node.textContent || '') }}))
      .filter((row) => row.text && row.text.length <= 240 && norm(row.text).includes(wanted))
      .slice(0, 30);
  }};
  setTimeout(() => {{
    const rows = candidates();
    const chosen = rows[0] || null;
    if (chosen) {{
      chosen.node.scrollIntoView({{ block: 'center', inline: 'center' }});
      chosen.node.click();
    }} else {{
      input.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }}));
      input.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Enter', bubbles: true }}));
    }}
    setTimeout(() => {{
      const form = input.closest('form') || document;
      resolve({{
        success: !!chosen,
        query,
        match_text: matchText,
        selected_text: chosen ? chosen.text : '',
        candidates: rows.map((row) => row.text),
        input: {{
          name: input.name || '',
          id: input.id || '',
          value: input.isContentEditable ? clean(input.textContent) : String(input.value || ''),
          label: labelFor(input)
        }},
        hidden_fields: hiddenRows(form),
        url: location.href,
        title: document.title
      }});
    }}, 250);
  }}, waitMs);
}})
"""


def _evidence_script(max_tables: int, max_network_entries: int, text_chars: int) -> str:
    return f"""
(() => {{
  const maxTables = {int(max_tables)};
  const maxNetwork = {int(max_network_entries)};
  const textChars = {int(text_chars)};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const fieldRecord = (field) => {{
    const tag = field.tagName.toLowerCase();
    const type = (field.getAttribute('type') || '').toLowerCase();
    const label = (() => {{
      const parts = [field.getAttribute('aria-label') || '', field.getAttribute('title') || '', field.getAttribute('placeholder') || ''];
      if (field.id) {{
        const node = document.querySelector(`label[for="${{CSS.escape(field.id)}}"]`);
        if (node) parts.push(node.innerText || node.textContent || '');
      }}
      return clean(parts.filter(Boolean).join(' '));
    }})();
    return {{
      name: field.name || field.id || '',
      id: field.id || '',
      tag,
      type,
      label,
      hidden: type === 'hidden' || field.hidden || getComputedStyle(field).display === 'none',
      value: tag === 'select'
        ? Array.from(field.selectedOptions || []).map((opt) => opt.value || opt.text).join(',')
        : (type === 'checkbox' || type === 'radio' ? (field.checked ? field.value || 'on' : '') : String(field.value || ''))
    }};
  }};
  const forms = Array.from(document.forms || []).map((form, index) => {{
    const fields = Array.from(form.elements || []).map(fieldRecord).filter((row) => row.name || row.value || row.label);
    return {{
      index,
      action: form.action || location.href,
      method: String(form.method || 'GET').toUpperCase(),
      id: form.id || '',
      name: form.name || '',
      fields,
      hidden_fields: fields.filter((row) => row.hidden)
    }};
  }});
  const tableSummaries = Array.from(document.querySelectorAll('table')).slice(0, maxTables).map((table, index) => {{
    const rows = Array.from(table.rows || []).filter((row) => row.closest('table') === table);
    const sampleRows = rows.slice(0, 3).map((row) => Array.from(row.children || []).filter((cell) => ['td', 'th'].includes(cell.tagName.toLowerCase())).map((cell) => clean(cell.innerText || cell.textContent).slice(0, 200)));
    const headers = sampleRows[0] || [];
    return {{
      index,
      caption: clean((table.querySelector('caption') || {{}}).innerText || table.getAttribute('summary') || ''),
      row_count: rows.length,
      column_count: Math.max(0, ...sampleRows.map((row) => row.length)),
      headers,
      sample_rows: sampleRows
    }};
  }});
  let networkLog = window.__nextchatguiNetworkLog || [];
  if (!networkLog.length) {{
    try {{
      const parsed = JSON.parse(sessionStorage.getItem('__nextchatguiNetworkLog') || '[]');
      if (Array.isArray(parsed)) networkLog = parsed;
    }} catch (_e) {{}}
  }}
  const performance_nav = performance.getEntriesByType ? performance.getEntriesByType('navigation').map((entry) => ({{
    name: entry.name,
    type: entry.type,
    startTime: entry.startTime,
    duration: entry.duration,
    transferSize: entry.transferSize,
    encodedBodySize: entry.encodedBodySize
  }})) : [];
  const bodyText = document.body ? clean(document.body.innerText || '') : '';
  return {{
    url: location.href,
    title: document.title,
    extracted_at: new Date().toISOString(),
    forms,
    tables: tableSummaries,
    network_entries: networkLog.slice(-maxNetwork),
    performance_nav,
    visible_text_preview: bodyText.slice(0, textChars),
    body_chars: bodyText.length
  }};
}})()
"""


def _human_checkpoint_script(
    reason: str,
    mode: str,
    wait_for_text: str,
    wait_for_selector: str,
    wait_until_text_gone: str,
    wait_until_selector_gone: str,
    require_page_change: bool,
    timeout: float,
    poll_interval: float,
) -> str:
    return f"""
new Promise((resolve) => {{
  const reason = {json.dumps(reason, ensure_ascii=False)};
  const mode = {json.dumps(mode)};
  const waitForText = {json.dumps(wait_for_text, ensure_ascii=False)};
  const waitForSelector = {json.dumps(wait_for_selector)};
  const waitUntilTextGone = {json.dumps(wait_until_text_gone, ensure_ascii=False)};
  const waitUntilSelectorGone = {json.dumps(wait_until_selector_gone)};
  const requirePageChange = {json.dumps(bool(require_page_change))};
  const timeoutMs = {int(timeout * 1000)};
  const pollMs = {int(poll_interval * 1000)};
  const started = Date.now();
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase();
  const visible = (el) => {{
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const textOfPage = () => document.body ? clean(document.body.innerText || document.body.textContent || '') : '';
  const signature = () => {{
    const text = textOfPage();
    return {{
      url: location.href,
      title: document.title,
      text_head: text.slice(0, 600),
      body_chars: text.length
    }};
  }};
  const initial = signature();
  const captchaPatterns = [
    /captcha/i,
    /re\\s*captcha/i,
    /hcaptcha/i,
    /turnstile/i,
    /cloudflare/i,
    /verify\\s+(you|yourself|human|identity)/i,
    /human\\s+verification/i,
    /security\\s+check/i,
    /slide\\s+to\\s+fit\\s+the\\s+piece/i,
    /drag\\s+.*(slider|piece|puzzle)/i,
    /robot/i,
    /验证码|人机验证|安全验证|滑块|拖动|拼图|验证/i
  ];
  const captchaSelectors = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]',
    '.g-recaptcha',
    '.h-captcha',
    '[class*="captcha" i]',
    '[id*="captcha" i]',
    '[class*="turnstile" i]',
    '[id*="turnstile" i]',
    '[class*="verify" i]',
    '[id*="verify" i]',
    '#cf-challenge-running',
    '.cf-challenge',
    '.geetest_panel',
    '[class*="geetest" i]',
    '[id*="geetest" i]',
    '[class*="slider" i]',
    '[id*="slider" i]'
  ];
  const detectChallenge = () => {{
    const pageText = textOfPage();
    const textMatches = captchaPatterns
      .filter((pattern) => pattern.test(pageText))
      .map((pattern) => String(pattern));
    const selectorMatches = [];
    for (const selector of captchaSelectors) {{
      try {{
        const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
        if (nodes.length) selectorMatches.push({{ selector, count: nodes.length }});
      }} catch (_e) {{}}
    }}
    return {{
      detected: textMatches.length > 0 || selectorMatches.length > 0,
      text_matches: textMatches.slice(0, 10),
      selector_matches: selectorMatches.slice(0, 20)
    }};
  }};
  const conditionState = () => {{
    const text = textOfPage();
    const current = signature();
    const challenge = detectChallenge();
    const checks = [];
    if (waitForText) checks.push({{ name: 'wait_for_text', ok: norm(text).includes(norm(waitForText)), value: waitForText }});
    if (waitForSelector) {{
      let ok = false;
      try {{ ok = Array.from(document.querySelectorAll(waitForSelector)).some(visible); }} catch (_e) {{}}
      checks.push({{ name: 'wait_for_selector', ok, value: waitForSelector }});
    }}
    if (waitUntilTextGone) checks.push({{ name: 'wait_until_text_gone', ok: !norm(text).includes(norm(waitUntilTextGone)), value: waitUntilTextGone }});
    if (waitUntilSelectorGone) {{
      let ok = true;
      try {{ ok = !Array.from(document.querySelectorAll(waitUntilSelectorGone)).some(visible); }} catch (_e) {{}}
      checks.push({{ name: 'wait_until_selector_gone', ok, value: waitUntilSelectorGone }});
    }}
    if (requirePageChange) {{
      const changed = current.url !== initial.url || current.title !== initial.title || current.text_head !== initial.text_head || Math.abs(current.body_chars - initial.body_chars) > 80;
      checks.push({{ name: 'page_changed', ok: changed, value: 'url/title/body changed' }});
    }}
    if (!checks.length) {{
      checks.push({{ name: 'challenge_cleared_or_page_changed', ok: !challenge.detected || current.url !== initial.url || current.title !== initial.title, value: 'auto-detected challenge cleared' }});
    }}
    return {{ current, challenge, checks, ok: checks.every((check) => check.ok) }};
  }};
  const initialChallenge = detectChallenge();
  const tick = () => {{
    const state = conditionState();
    const elapsed = Date.now() - started;
    if (state.ok || elapsed >= timeoutMs) {{
      resolve({{
        success: state.ok,
        needs_human: !state.ok,
        timed_out: !state.ok && elapsed >= timeoutMs,
        reason,
        mode,
        elapsed_ms: elapsed,
        waiting_for: state.checks,
        captcha_detected_initial: initialChallenge,
        captcha_detected_final: state.challenge,
        url: location.href,
        title: document.title,
        body_preview: textOfPage().slice(0, 1200),
        message: state.ok
          ? 'Human checkpoint completed; browser automation can continue.'
          : 'Timed out while waiting for manual verification. Ask the user to finish verification in the visible browser and retry.'
      }});
      return;
    }}
    setTimeout(tick, pollMs);
  }};
  tick();
}})
"""


def _shipping_detect_script(text_chars: int) -> str:
    return f"""
(() => {{
  const textChars = {int(text_chars)};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const lower = (value) => clean(value).toLowerCase();
  const url = location.href;
  const host = location.hostname.toLowerCase();
  const bodyText = document.body ? clean(document.body.innerText || '') : '';
  const carrier = host.includes('shipmentlink')
    ? 'evergreen'
    : (host.includes('oocl') || host.includes('moc.oocl') ? 'oocl' : 'unknown');
  const pageHints = [];
  if (/TVS2_InteractiveScheduleRouting/i.test(url)) pageHints.push('shipmentlink_routing_result_or_form');
  if (/TVS2_InteractiveScheduleRoutingDetail/i.test(url)) pageHints.push('shipmentlink_detail');
  if (/mobile_ss_search|sailingschedule/i.test(url)) pageHints.push('oocl_sailing_schedule');
  if (/The schedules from/i.test(bodyText)) pageHints.push('schedule_result_text');
  if (/Origin City|Destination City|Cargo Nature/i.test(bodyText)) pageHints.push('schedule_search_form');
  const fieldsOf = (form) => Array.from(form.elements || []).map((field) => {{
    const tag = field.tagName.toLowerCase();
    const type = (field.getAttribute('type') || '').toLowerCase();
    return {{
      name: field.name || '',
      id: field.id || '',
      tag,
      type,
      value: tag === 'select'
        ? Array.from(field.selectedOptions || []).map((opt) => opt.text || opt.value).join(',')
        : (type === 'checkbox' || type === 'radio' ? (field.checked ? field.value || 'on' : '') : String(field.value || '')),
      hidden: type === 'hidden' || field.hidden || getComputedStyle(field).display === 'none',
      placeholder: field.getAttribute('placeholder') || '',
      title: field.getAttribute('title') || ''
    }};
  }}).filter((field) => field.name || field.id || field.value || field.placeholder || field.title);
  const forms = Array.from(document.forms || []).map((form, index) => {{
    const fields = fieldsOf(form);
    return {{
      index,
      action: form.action || url,
      method: String(form.method || 'GET').toUpperCase(),
      id: form.id || '',
      name: form.name || '',
      fields,
      hidden_fields: fields.filter((field) => field.hidden)
    }};
  }});
  const detailTriggers = Array.from(document.querySelectorAll('[ectype="modalbox"], a[href*="Detail"], span[href*="Detail"], button, a'))
    .map((node, index) => ({{
      index,
      text: clean(node.innerText || node.textContent || node.value || ''),
      href: node.getAttribute('href') || node.href || '',
      params: node.getAttribute('params') || '',
      ectype: node.getAttribute('ectype') || '',
      selector_hint: node.id ? `#${{CSS.escape(node.id)}}` : ''
    }}))
    .filter((row) => row.ectype === 'modalbox' || /detail/i.test(row.text + ' ' + row.href));
  const recommended_tools = [];
  recommended_tools.push('next_browser_tabs(action="list")');
  recommended_tools.push('next_browser_capture_network(action="start") before submitting forms');
  if (carrier === 'evergreen') {{
    recommended_tools.push('next_browser_select_autocomplete for ShipmentLink port fields');
    recommended_tools.push('next_browser_shipping_extract_schedules(carrier="evergreen")');
    if (detailTriggers.length) recommended_tools.push('next_browser_shipping_modal(params="seq=...") for Details');
  }} else if (carrier === 'oocl') {{
    recommended_tools.push('next_browser_select_autocomplete for OOCL Origin/Destination City fields');
    recommended_tools.push('next_browser_shipping_extract_schedules(carrier="oocl")');
  }} else {{
    recommended_tools.push('next_browser_shipping_extract_schedules(carrier="auto")');
  }}
  return {{
    carrier,
    page_hints: pageHints,
    url,
    title: document.title,
    form_count: forms.length,
    forms,
    detail_triggers: detailTriggers.slice(0, 50),
    recommended_tools,
    visible_text_preview: bodyText.slice(0, textChars),
    extracted_at: new Date().toISOString()
  }};
}})()
"""


def _shipping_extract_script(carrier: str, selector: str, max_records: int, include_raw: bool) -> str:
    return f"""
(() => {{
  const requestedCarrier = {json.dumps(carrier)};
  const scopeSelector = {json.dumps(selector)};
  const maxRecords = {int(max_records)};
  const includeRaw = {json.dumps(bool(include_raw))};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, '');
  const host = location.hostname.toLowerCase();
  const autoCarrier = host.includes('shipmentlink')
    ? 'evergreen'
    : (host.includes('oocl') || host.includes('moc.oocl') ? 'oocl' : 'unknown');
  const carrier = requestedCarrier === 'auto' ? autoCarrier : requestedCarrier;
  const scopes = scopeSelector ? Array.from(document.querySelectorAll(scopeSelector)) : [document];
  const unique = (nodes) => Array.from(new Set(nodes.filter(Boolean)));
  const ownRows = (table) => Array.from(table.rows || table.querySelectorAll('tr')).filter((row) => row.closest('table') === table);
  const cellText = (cell) => clean(cell.innerText || cell.textContent || '');
  const detailTrigger = (root) => {{
    const node = root.querySelector ? root.querySelector('[ectype="modalbox"], a[href*="Detail"], span[href*="Detail"], a, button') : null;
    if (!node) return null;
    const text = clean(node.innerText || node.textContent || node.value || '');
    const href = node.getAttribute('href') || node.href || '';
    const params = node.getAttribute('params') || '';
    const ectype = node.getAttribute('ectype') || '';
    if (ectype !== 'modalbox' && !/detail/i.test(text + ' ' + href)) return null;
    return {{ text, href, params, ectype }};
  }};
  const parseDateLike = (text) => {{
    const value = clean(text);
    const m = value.match(/\\b(?:\\d{{1,2}}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/ ]?\\d{{0,4}}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/ ]\\d{{1,2}}[-/ ,]*\\d{{0,4}}|\\d{{4}}[-/]\\d{{1,2}}[-/]\\d{{1,2}}|\\d{{1,2}}[-/]\\d{{1,2}}[-/]\\d{{2,4}})\\b/i);
    return m ? clean(m[0]) : '';
  }};
  const firstMatch = (text, patterns) => {{
    for (const pattern of patterns) {{
      const m = text.match(pattern);
      if (m) return clean(m[1] || m[0]);
    }}
    return '';
  }};
  const headerKey = (header) => {{
    const h = norm(header);
    if (/placeofreceipt|receipt|origin|from/.test(h)) return 'origin';
    if (/pol|portofloading|loadingport|departureport/.test(h)) return 'pol';
    if (/pod|portofdischarge|dischargeport/.test(h)) return 'pod';
    if (/placeofdelivery|delivery|destination|to/.test(h)) return 'destination';
    if (/etd|departure|depart|sailing|saildate/.test(h)) return 'etd';
    if (/eta|arrival|arrive/.test(h)) return 'eta';
    if (/cutoff|closing|cycut|sicut|doccut|vgm/.test(h)) return 'cutoff';
    if (/vessel|ship/.test(h)) return 'vessel';
    if (/voyage|voy|vyg/.test(h)) return 'voyage';
    if (/service|loop|string/.test(h)) return 'service';
    if (/transit|duration|days/.test(h)) return 'transit_days';
    if (/cargo|nature|reefer/.test(h)) return 'cargo_nature';
    return '';
  }};
  const normalizeRecord = (raw, source) => {{
    const cells = raw.cells || [];
    const rawText = clean(raw.raw_text || cells.join(' | '));
    const record = {{
      carrier,
      source,
      origin: raw.origin || '',
      pol: raw.pol || '',
      pod: raw.pod || '',
      destination: raw.destination || raw.delivery || '',
      delivery: raw.delivery || '',
      etd: raw.etd || '',
      eta: raw.eta || '',
      cutoff: raw.cutoff || '',
      service: raw.service || '',
      vessel: raw.vessel || '',
      voyage: raw.voyage || '',
      transit_days: raw.transit_days || '',
      cargo_nature: raw.cargo_nature || '',
      detail_trigger: raw.detail_trigger || null,
      confidence: 0,
      notes: []
    }};
    if (!record.etd) record.etd = firstMatch(rawText, [/\\bETD\\s*[:：]?\\s*([^|,;]+?)(?=\\s+(ETA|Vessel|Voyage|Service|Cut|$))/i]) || parseDateLike(rawText);
    if (!record.eta) record.eta = firstMatch(rawText, [/\\bETA\\s*[:：]?\\s*([^|,;]+?)(?=\\s+(ETD|Vessel|Voyage|Service|Cut|$))/i]);
    if (!record.voyage) record.voyage = firstMatch(rawText, [/\\bVoy(?:age|\\.)?\\s*[:：#]?\\s*([A-Z0-9-]{{2,12}})\\b/i]);
    if (!record.service) record.service = firstMatch(rawText, [/\\bService\\s*[:：]?\\s*([A-Z0-9 -]{{2,20}})\\b/i]);
    if (!record.transit_days) record.transit_days = firstMatch(rawText, [/\\b(?:Transit|Duration)\\D{{0,8}}(\\d{{1,3}})\\s*(?:days?|D)?\\b/i]);
    if (!record.cutoff) record.cutoff = firstMatch(rawText, [/\\b(?:Cut\\s*off|Cutoff|Closing|CY Cut|SI Cut)\\s*[:：]?\\s*([^|;]+?)(?=\\s+(ETD|ETA|Vessel|Voyage|Service|$))/i]);
    if (!record.vessel) {{
      const vesselByLabel = firstMatch(rawText, [/\\bVessel\\s*[:：]?\\s*([^|;]+?)(?=\\s+(Voyage|Voy\\.?|ETD|ETA|Service|$))/i]);
      if (vesselByLabel) record.vessel = vesselByLabel;
    }}
    const filled = ['origin','pol','pod','destination','delivery','etd','eta','cutoff','service','vessel','voyage','transit_days']
      .filter((key) => record[key]);
    record.confidence = Math.min(1, filled.length / 8);
    if (!record.etd && !record.eta) record.notes.push('No ETD/ETA parsed; inspect raw cells.');
    if (!record.vessel && !record.voyage) record.notes.push('No vessel/voyage parsed; inspect raw cells or Details modal.');
    if (includeRaw) {{
      record.raw_cells = cells;
      record.raw_text = rawText;
      record.raw_headers = raw.headers || [];
    }}
    return record;
  }};
  const tables = unique(scopes.flatMap((scope) => {{
    if (scope.tagName && scope.tagName.toLowerCase() === 'table') return [scope];
    return Array.from(scope.querySelectorAll ? scope.querySelectorAll('table') : []);
  }}));
  const records = [];
  const table_summaries = [];
  tables.forEach((table, tableIndex) => {{
    const rows = ownRows(table).map((row, rowIndex) => {{
      const cells = Array.from(row.children || []).filter((cell) => ['td', 'th'].includes(cell.tagName.toLowerCase())).map(cellText);
      return {{ node: row, rowIndex, cells }};
    }}).filter((row) => row.cells.some(Boolean));
    if (!rows.length) return;
    const headerIndex = rows.findIndex((row) => row.cells.map(headerKey).filter(Boolean).length >= 2);
    table_summaries.push({{ table_index: tableIndex, row_count: rows.length, header_index: headerIndex, sample: rows.slice(0, 2).map((row) => row.cells) }});
    if (headerIndex >= 0) {{
      const headers = rows[headerIndex].cells;
      const keys = headers.map(headerKey);
      rows.slice(headerIndex + 1).forEach((row) => {{
        const raw = {{ cells: row.cells, headers, detail_trigger: detailTrigger(row.node), raw_text: row.cells.join(' | ') }};
        keys.forEach((key, index) => {{
          if (key) raw[key] = row.cells[index] || '';
        }});
        const hasScheduleSignal = raw.etd || raw.eta || raw.vessel || raw.voyage || raw.service || raw.detail_trigger || row.cells.join(' ').match(/\\b(ETD|ETA|Vessel|Voy|Service|Cut|\\d{{1,2}}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))/i);
        if (hasScheduleSignal) records.push(normalizeRecord(raw, `table:${{tableIndex}}`));
      }});
    }} else {{
      rows.forEach((row) => {{
        const rawText = row.cells.join(' | ');
        const trigger = detailTrigger(row.node);
        const signal = trigger || rawText.match(/\\b(ETD|ETA|Vessel|Voy|Service|Cut|\\d{{1,2}}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)|\\d{{4}}[-/]\\d{{1,2}}[-/]\\d{{1,2}})/i);
        if (!signal) return;
        records.push(normalizeRecord({{ cells: row.cells, raw_text: rawText, detail_trigger: trigger }}, `table:${{tableIndex}}:row:${{row.rowIndex}}`));
      }});
      for (let i = 0; i < rows.length - 1; i += 3) {{
        const groupRows = rows.slice(i, i + 3);
        const rawText = groupRows.map((row) => row.cells.join(' | ')).join(' || ');
        if (!rawText.match(/\\b(ETD|ETA|Vessel|Voy|Service|Cut|\\d{{1,2}}[-/ ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)|\\d{{4}}[-/]\\d{{1,2}}[-/]\\d{{1,2}})/i)) continue;
        records.push(normalizeRecord({{ cells: groupRows.flatMap((row) => row.cells), raw_text: rawText, detail_trigger: detailTrigger(groupRows[0].node) }}, `table:${{tableIndex}}:row_group:${{i}}`));
      }}
    }}
  }});
  const seen = new Set();
  const deduped = [];
  for (const record of records) {{
    const key = [record.etd, record.eta, record.vessel, record.voyage, record.service, record.raw_text || ''].join('|').slice(0, 400);
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(record);
    if (deduped.length >= maxRecords) break;
  }}
  const detail_triggers = Array.from(document.querySelectorAll('[ectype="modalbox"], a[href*="Detail"], span[href*="Detail"]'))
    .map((node, index) => ({{
      index,
      text: clean(node.innerText || node.textContent || ''),
      href: node.getAttribute('href') || node.href || '',
      params: node.getAttribute('params') || '',
      ectype: node.getAttribute('ectype') || ''
    }}))
    .filter((row) => row.ectype === 'modalbox' || /detail/i.test(row.text + ' ' + row.href))
    .slice(0, 100);
  return {{
    carrier,
    url: location.href,
    title: document.title,
    extracted_at: new Date().toISOString(),
    record_count: deduped.length,
    records: deduped,
    table_summaries,
    detail_triggers,
    notes: deduped.length ? [] : ['No schedule records parsed. Try selector, next_browser_extract_tables(include_layout_tables=true), or next_browser_evidence.']
  }};
}})()
"""


def _shipping_modal_script(
    selector: str,
    text: str,
    params: str,
    row_index: Any,
    timeout: float,
    close: bool,
) -> str:
    return f"""
new Promise((resolve) => {{
  const selector = {json.dumps(selector)};
  const wantedText = {json.dumps(text, ensure_ascii=False)};
  const wantedParams = {json.dumps(params)};
  const rowIndex = {json.dumps(row_index)};
  const timeoutMs = {int(timeout * 1000)};
  const shouldClose = {json.dumps(bool(close))};
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase();
  const visible = (el) => {{
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const rowsFromTable = (table) => Array.from(table.rows || []).map((row) =>
    Array.from(row.children || []).filter((cell) => ['td', 'th'].includes(cell.tagName.toLowerCase())).map((cell) => clean(cell.innerText || cell.textContent))
  ).filter((row) => row.some(Boolean));
  const candidates = selector
    ? Array.from(document.querySelectorAll(selector))
    : Array.from(document.querySelectorAll('[ectype="modalbox"], a[href*="Detail"], span[href*="Detail"], button, a'));
  const matches = candidates.filter((node) => {{
    const nodeText = clean(node.innerText || node.textContent || node.value || '');
    const nodeParams = node.getAttribute('params') || '';
    const href = node.getAttribute('href') || node.href || '';
    const isDetail = node.getAttribute('ectype') === 'modalbox' || /detail/i.test(nodeText + ' ' + href);
    if (!selector && !isDetail) return false;
    if (wantedParams && nodeParams !== wantedParams && !nodeParams.includes(wantedParams)) return false;
    if (wantedText && !norm(nodeText).includes(norm(wantedText))) return false;
    return true;
  }});
  const chosenIndex = rowIndex == null || rowIndex === '' ? 0 : Number(rowIndex);
  const trigger = matches[chosenIndex];
  if (!trigger) {{
    resolve({{
      success: false,
      error: 'detail trigger not found',
      selector,
      text: wantedText,
      params: wantedParams,
      available: matches.slice(0, 30).map((node, index) => ({{
        index,
        text: clean(node.innerText || node.textContent || node.value || ''),
        href: node.getAttribute('href') || node.href || '',
        params: node.getAttribute('params') || ''
      }}))
    }});
    return;
  }}
  const beforeText = document.body ? clean(document.body.innerText || '') : '';
  trigger.scrollIntoView({{ block: 'center', inline: 'center' }});
  trigger.click();
  const started = Date.now();
  const findModal = () => {{
    const nodes = Array.from(document.querySelectorAll(
      '[role="dialog"], .modal, .modalbox, .ui-dialog, .fancybox-wrap, .fancybox-container, .popup, .pop, div'
    )).filter(visible);
    const scored = nodes.map((node) => {{
      const text = clean(node.innerText || node.textContent || '');
      let score = 0;
      if (node.getAttribute('role') === 'dialog') score += 40;
      if (/modal|dialog|fancybox|popup/i.test(node.className || '')) score += 30;
      if (text.length > 20 && beforeText.indexOf(text.slice(0, 80)) === -1) score += 25;
      if (node.querySelector('table')) score += 20;
      return {{ node, text, score }};
    }}).filter((row) => row.score > 20 && row.text);
    scored.sort((a, b) => b.score - a.score);
    return scored[0] || null;
  }};
  const finish = () => {{
    const modal = findModal();
    if (!modal) {{
      if (Date.now() - started >= timeoutMs) {{
        resolve({{
          success: false,
          error: 'modal did not appear before timeout',
          trigger: {{
            text: clean(trigger.innerText || trigger.textContent || trigger.value || ''),
            href: trigger.getAttribute('href') || trigger.href || '',
            params: trigger.getAttribute('params') || ''
          }}
        }});
      }} else {{
        setTimeout(finish, 250);
      }}
      return;
    }}
    const tables = Array.from(modal.node.querySelectorAll('table')).map((table, index) => ({{
      index,
      rows: rowsFromTable(table)
    }})).filter((table) => table.rows.length);
    const payload = {{
      success: true,
      trigger: {{
        text: clean(trigger.innerText || trigger.textContent || trigger.value || ''),
        href: trigger.getAttribute('href') || trigger.href || '',
        params: trigger.getAttribute('params') || '',
        ectype: trigger.getAttribute('ectype') || ''
      }},
      modal_text: modal.text.slice(0, 12000),
      tables,
      url: location.href,
      title: document.title
    }};
    if (shouldClose) {{
      const closeButton = modal.node.querySelector('[aria-label="Close"], .close, .ui-dialog-titlebar-close, .fancybox-close-small, button');
      if (closeButton) closeButton.click();
      else document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
      payload.closed = true;
    }}
    resolve(payload);
  }};
  setTimeout(finish, 250);
}})
"""
