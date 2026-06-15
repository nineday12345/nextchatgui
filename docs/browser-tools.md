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
  results as JSON, with optional CSV output for the first table.
- `next_browser_wait_for_text`: wait until visible page text appears.
- `next_browser_wait_for_selector`: wait until a CSS selector exists or is
  visible.
- `next_browser_downloads`: set the download directory, list files, wait for a
  completed download, or click a selector and wait for the download.
- `next_browser_capture_network`: inject a lightweight `fetch`/`XMLHttpRequest`
  recorder, then read recent request and response previews.
- `next_browser_fill_form`: fill fields by matching semantic names to labels,
  `name`, `id`, placeholder, and aria labels.

The network recorder is page-injected JavaScript, so it captures requests made
after `next_browser_capture_network(action="start")`. It is intentionally
lightweight and does not replace full CDP `Network.*` event tracing.

For complex autocomplete widgets, use `next_browser_fill_form` as a first pass,
then verify with `next_browser_wait_for_text`, `next_browser_wait_for_selector`,
or `next_browser_extract_tables`.
