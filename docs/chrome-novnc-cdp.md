# chrome-novnc-cdp trial notes

Use `miminashi/chrome-novnc-cdp` as a small visible browser sidecar:

- noVNC web client: `9220`
- browser control API: `9221`
- Chrome DevTools Protocol: `9222`

Recommended 1Panel shape:

1. Run `chrome-novnc-cdp` as a separate container.
2. Put the browser container and Hermes container on the same Docker network.
3. Reverse proxy only the noVNC web client to the public Hermes domain.
4. Keep the CDP port private to Docker networking.

Example Hermes environment:

```bash
HERMES_NEXTCHATGUI_BROWSER_NOVNC_URL=/browser/
HERMES_NEXTCHATGUI_BROWSER_CDP_URL=http://chrome-novnc:9222
BROWSER_CDP_URL=http://chrome-novnc:9222
```

If noVNC is exposed on another internal host or subdomain, set
`HERMES_NEXTCHATGUI_BROWSER_NOVNC_URL` to that URL instead.

The Browser drawer uses the private CDP URL to list, create, activate, and
close Chrome tabs while the iframe shows the noVNC view.

Each Hermes session is bound to a primary browser tab. When the Browser drawer
is open and the active conversation changes, the plugin activates that session's
bound tab or creates one if it no longer exists.

The same CDP endpoint also powers the `next_browser` Hermes tools. See
`docs/browser-tools.md` for the model-facing table extraction, wait, download,
network capture, form fill, and tab tools.

Do not expose `9222` publicly. CDP gives full browser inspection and control.
