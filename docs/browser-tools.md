# NextChatGUI browser tools

NextChatGUI also registers a `next_browser` Hermes toolset when the plugin is
enabled. These tools use the same Chrome DevTools Protocol endpoint as the
Browser drawer:

```bash
BROWSER_CDP_URL=http://chrome-novnc:9222
# or
HERMES_NEXTCHATGUI_BROWSER_CDP_URL=http://chrome-novnc:9222
```

Tools:

- `next_browser_tabs`: list, activate, create, and close Chrome tabs.
- `next_browser_extract_tables`: extract DOM tables and repeated card/list
  results as JSON, with optional CSV output for the first table. It classifies
  tables as `data`, `mixed`, or `layout`, skips layout tables by default, and
  compacts large results with `max_chars`.
- `next_browser_wait_for_text`: wait until visible page text appears.
- `next_browser_wait_for_selector`: wait until a CSS selector exists or is
  visible.
- `next_browser_downloads`: set the download directory, list files, wait for a
  completed download, or click a selector and wait for the download.
- `next_browser_capture_network`: inject a lightweight
  `fetch`/`XMLHttpRequest`/form-submit recorder, then read recent request,
  hidden field, POST body, status, and response previews.
- `next_browser_fill_form`: fill fields by matching semantic names to labels,
  `name`, `id`, placeholder, and aria labels.
- `next_browser_select_autocomplete`: type into an autocomplete field, choose a
  matching suggestion, and report hidden form fields so the model can verify
  values like port/location codes.
- `next_browser_evidence`: collect a compact evidence package containing the
  current target, forms, hidden values, recent network/form logs, table
  summaries, visible text preview, and optionally a screenshot.

The network recorder is page-injected JavaScript, so it captures requests made
after `next_browser_capture_network(action="start")`. It also records
traditional `<form method="POST">` submissions before full-page navigation and
persists the compact log in `sessionStorage` when possible. It is intentionally
lightweight and does not replace full CDP `Network.*` event tracing.

Download handling sets Chrome's download directory through CDP. If Chrome runs
in a separate `chrome-novnc` container, that directory must be backed by a
volume mounted into both `chrome-novnc` and Hermes. Otherwise Chrome may
download the file successfully inside the browser container while Hermes sees an
empty directory. In that case `next_browser_downloads(action="wait")` and
`action="click_and_wait"` return `success: false` with a warning.

By default downloads use `HERMES_NEXTCHATGUI_DOWNLOAD_DIR` when set, then
`/opt/data/nextchatgui-downloads` or `/data/nextchatgui-downloads` when those
data roots exist, and finally `./downloads`. For Docker, mount the same host
directory into both containers at the same path, for example `/opt/data`.

For complex autocomplete widgets, use `next_browser_select_autocomplete` when a
visible label maps to a hidden value. For example, shipping sites may display
`SHANGHAI (CNSHA)` but submit a hidden code such as `CNSHG`. After filling,
inspect `hidden_fields` or call `next_browser_evidence`.

For old JSP/business pages such as schedules:

1. Call `next_browser_tabs(action="list")` and keep the returned `target_id`.
2. Call `next_browser_capture_network(action="start", target_id=...)`.
3. Fill normal fields with `next_browser_fill_form`; use
   `next_browser_select_autocomplete` for port/location widgets.
4. Submit the form.
5. If text waits fail, `next_browser_wait_for_text` returns `body_preview` so
   the model can adjust the expected phrase.
6. Use `next_browser_extract_tables(selector=..., max_chars=...)` for data rows,
   or `next_browser_evidence(output_path=...)` to save a reproducible query
   package.
