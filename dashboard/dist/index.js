(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REGISTRY = window.__HERMES_PLUGINS__;
  if (!SDK || !REGISTRY || typeof REGISTRY.register !== "function") return;

  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks;
  const useState = hooks.useState;
  const useEffect = hooks.useEffect;
  const useMemo = hooks.useMemo;
  const useRef = hooks.useRef;
  const useCallback = hooks.useCallback;

  const ANY = "*";
  const REQUEST_TIMEOUT_MS = 120000;
  const PLUGIN_API = "/api/plugins/nextchatgui";

  function cx() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
  }

  function nowId(prefix) {
    return prefix + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function textOf(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.map(textOf).filter(Boolean).join("\n");
    if (typeof value === "object") {
      if (typeof value.text === "string") return value.text;
      if (typeof value.content === "string") return value.content;
      return "[structured content]";
    }
    return String(value);
  }

  function displayText(value, fallback) {
    const seen = new WeakSet();
    function render(v) {
      if (v == null) return "";
      if (typeof v === "string") return v;
      if (typeof v === "number" || typeof v === "boolean") return String(v);
      if (Array.isArray(v)) return v.map(render).filter(Boolean).join("\n");
      if (typeof v === "object") {
        if (seen.has(v)) return "[circular]";
        seen.add(v);
        const preferred = ["text", "content", "message", "summary", "output", "stdout", "stderr", "result", "error", "command", "description"];
        for (let i = 0; i < preferred.length; i += 1) {
          if (v[preferred[i]] != null) {
            const rendered = render(v[preferred[i]]);
            if (rendered) return rendered;
          }
        }
        try {
          return JSON.stringify(v, null, 2);
        } catch (_e) {
          return String(v);
        }
      }
      return String(v);
    }
    const text = render(value).trim();
    return text || fallback || "";
  }

  function limitText(value, max) {
    const text = displayText(value);
    const limit = max || 4000;
    return text.length > limit ? text.slice(0, limit) + "\n..." : text;
  }

  function titleFromPrompt(text) {
    const firstLine = String(text || "").split(/\r?\n/).map(function (line) {
      return line.trim();
    }).find(Boolean);
    return (firstLine || "New conversation").slice(0, 96);
  }

  function shortModelName(model) {
    const value = String(model || "").trim();
    if (!value) return "Model";
    const parts = value.split("/");
    return parts[parts.length - 1] || value;
  }

  function normalizeApprovalMode(value) {
    if (typeof value === "boolean") return value ? "manual" : "off";
    const mode = String(value || "").trim().toLowerCase();
    if (!mode) return "manual";
    if (mode === "off" || mode === "yolo" || mode === "allow" || mode === "approve") return "off";
    if (mode === "ask" || mode === "manual") return "manual";
    if (mode === "smart") return "smart";
    if (mode === "deny" || mode === "block") return "deny";
    return mode;
  }

  function approvalDisplay(info, configMode) {
    const mode = normalizeApprovalMode(configMode);
    if (info && info.yolo) {
      return {
        label: "Yolo",
        tone: "danger",
        title: "Hermes approval bypass is active for this session or globally."
      };
    }
    if (mode === "off") {
      return {
        label: "Yolo",
        tone: "danger",
        title: "Hermes approvals.mode is off."
      };
    }
    if (mode === "smart") {
      return {
        label: "Smart",
        tone: "smart",
        title: "Hermes smart approval is enabled."
      };
    }
    if (mode === "deny") {
      return {
        label: "Deny",
        tone: "deny",
        title: "Hermes approval mode is set to deny/block."
      };
    }
    return {
      label: "Ask",
      tone: "ask",
      title: "Hermes will ask before guarded actions."
    };
  }

  function flattenModelOptions(payload) {
    const rows = [];
    (payload.providers || []).forEach(function (provider) {
      (provider.models || []).forEach(function (model) {
        rows.push({
          provider: provider.slug || provider.name || "",
          providerName: provider.name || provider.slug || "",
          authenticated: provider.authenticated !== false,
          warning: provider.warning || "",
          model: model,
          value: (provider.slug || provider.name || "") + "\n" + model
        });
      });
    });
    return rows;
  }

  function formatTime(ts) {
    if (!ts) return "";
    try {
      const date = new Date(ts > 1000000000000 ? ts : ts * 1000);
      return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch (_e) {
      return "";
    }
  }

  function formatBytes(value) {
    if (value == null || Number.isNaN(Number(value))) return "";
    const bytes = Number(value);
    if (bytes < 1024) return bytes + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let size = bytes / 1024;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size = size / 1024;
      index += 1;
    }
    return size.toFixed(size >= 10 ? 0 : 1) + " " + units[index];
  }

  function pluginPath(path, params) {
    const search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value == null || value === "") return;
      search.set(key, String(value));
    });
    const query = search.toString();
    return PLUGIN_API + path + (query ? "?" + query : "");
  }

  function authedFetch(url, init) {
    if (SDK.authedFetch) return SDK.authedFetch(url, init);
    return fetch(url, init);
  }

  async function responseMessage(response) {
    let text = "";
    try {
      text = await response.text();
    } catch (_e) {
      return response.statusText || "Request failed";
    }
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed.detail === "string") return parsed.detail;
      if (typeof parsed.message === "string") return parsed.message;
    } catch (_e) {
      /* keep raw text */
    }
    return text || response.statusText || "Request failed";
  }

  function parseApiError(err) {
    const raw = err && err.message ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === "string") return parsed.detail;
      if (parsed.detail && typeof parsed.detail.message === "string") return parsed.detail.message;
      if (typeof parsed.error === "string") return parsed.error;
    } catch (_e) {
      /* fall through */
    }
    return body || raw || "Unknown error";
  }

  function normalizeGatewayMessages(messages) {
    return (messages || []).map(function (msg) {
      const role = msg.role || "assistant";
      return {
        id: nowId("msg"),
        role: role,
        text: textOf(msg.text != null ? msg.text : msg.content),
        name: displayText(msg.name),
        streaming: false,
        reasoning: textOf(msg.reasoning || msg.reasoning_content || "")
      };
    }).filter(function (msg) {
      return msg.role === "tool" || msg.text.trim();
    });
  }

  function normalizeSession(row) {
    return {
      id: row.id || row.session_id || "",
      title: displayText(row.title, "Untitled"),
      preview: displayText(row.preview),
      source: displayText(row.source),
      model: displayText(row.model),
      messageCount: row.message_count || 0,
      lastActive: row.last_active || row.started_at || 0,
      active: !!row.is_active
    };
  }

  function GatewayClient() {
    this.ws = null;
    this.reqId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    this.stateListeners = new Set();
    this.state = "idle";
  }

  GatewayClient.prototype.setState = function (state) {
    if (this.state === state) return;
    this.state = state;
    this.stateListeners.forEach(function (cb) { cb(state); });
  };

  GatewayClient.prototype.onState = function (cb) {
    this.stateListeners.add(cb);
    cb(this.state);
    return () => this.stateListeners.delete(cb);
  };

  GatewayClient.prototype.on = function (type, cb) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(cb);
    return () => this.listeners.get(type).delete(cb);
  };

  GatewayClient.prototype.onAny = function (cb) {
    return this.on(ANY, cb);
  };

  GatewayClient.prototype.dispatch = function (msg) {
    const id = msg && msg.id;
    if (id != null && this.pending.has(id)) {
      const pending = this.pending.get(id);
      this.pending.delete(id);
      clearTimeout(pending.timer);
      if (msg.error) {
        pending.reject(new Error(msg.error.message || "request failed"));
      } else {
        pending.resolve(msg.result);
      }
      return;
    }
    if (!msg || msg.method !== "event") return;
    const ev = msg.params || {};
    if (typeof ev.type !== "string") return;
    (this.listeners.get(ev.type) || []).forEach(function (cb) { cb(ev); });
    (this.listeners.get(ANY) || []).forEach(function (cb) { cb(ev); });
  };

  GatewayClient.prototype.connect = async function () {
    if (this.state === "open" || this.state === "connecting") return;
    if (typeof SDK.buildWsUrl !== "function") {
      throw new Error("Hermes plugin SDK is too old: buildWsUrl is missing");
    }
    this.setState("connecting");
    const url = await SDK.buildWsUrl("/api/ws");
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.addEventListener("message", (ev) => {
      try {
        this.dispatch(JSON.parse(ev.data));
      } catch (_e) {
        /* ignore malformed frames */
      }
    });

    ws.addEventListener("close", () => {
      this.setState("closed");
      this.rejectAll(new Error("WebSocket closed"));
    });

    await new Promise((resolve, reject) => {
      const open = () => {
        ws.removeEventListener("error", error);
        this.setState("open");
        resolve();
      };
      const error = () => {
        ws.removeEventListener("open", open);
        this.setState("error");
        reject(new Error("WebSocket connection failed"));
      };
      ws.addEventListener("open", open, { once: true });
      ws.addEventListener("error", error, { once: true });
    });
  };

  GatewayClient.prototype.rejectAll = function (err) {
    this.pending.forEach(function (pending) {
      clearTimeout(pending.timer);
      pending.reject(err);
    });
    this.pending.clear();
  };

  GatewayClient.prototype.close = function () {
    if (this.ws) this.ws.close();
    this.ws = null;
  };

  GatewayClient.prototype.request = function (method, params, timeoutMs) {
    params = params || {};
    timeoutMs = timeoutMs || REQUEST_TIMEOUT_MS;
    if (!this.ws || this.state !== "open") {
      return Promise.reject(new Error("gateway not connected (state=" + this.state + ")"));
    }
    const id = "ncg-" + (++this.reqId);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error("request timed out: " + method));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      }
    });
  };

  function MarkdownLite(props) {
    const text = props.text || "";
    const blocks = [];
    const parts = text.split(/```/g);
    for (let i = 0; i < parts.length; i++) {
      const value = parts[i];
      if (!value) continue;
      if (i % 2 === 1) {
        const lines = value.replace(/^\w+\n/, "");
        blocks.push(h("pre", { key: "c" + i, className: "ncg-code" }, h("code", null, lines)));
      } else {
        value.split(/\n{2,}/).forEach(function (paragraph, idx) {
          const trimmed = paragraph.trim();
          if (!trimmed) return;
          if (/^\s*[-*]\s+/m.test(trimmed)) {
            blocks.push(h("ul", { key: "u" + i + "-" + idx, className: "ncg-list" },
              trimmed.split(/\n/).filter(Boolean).map(function (line, li) {
                return h("li", { key: li }, line.replace(/^\s*[-*]\s+/, ""));
              })
            ));
          } else if (/^#{1,3}\s/.test(trimmed)) {
            blocks.push(h("strong", { key: "h" + i + "-" + idx, className: "ncg-heading" }, trimmed.replace(/^#{1,3}\s+/, "")));
          } else {
            blocks.push(h("p", { key: "p" + i + "-" + idx }, trimmed));
          }
        });
      }
    }
    return h("div", { className: "ncg-md" }, blocks.length ? blocks : h("p", null, ""));
  }

  function StatusDot(props) {
    return h("span", { className: cx("ncg-dot", "ncg-dot-" + props.tone), title: props.label });
  }

  function IconButton(props) {
    return h("button", {
      type: "button",
      className: cx("ncg-icon-btn", props.className),
      title: props.title,
      "aria-label": props.title,
      disabled: props.disabled,
      onClick: props.onClick
    }, props.children);
  }

  function SessionList(props) {
    const sessions = props.sessions || [];
    const [query, setQuery] = useState("");
    const q = query.trim().toLowerCase();
    const filtered = q
      ? sessions.filter(function (s) {
        return [
          s.title,
          s.preview,
          s.model,
          s.source,
          formatTime(s.lastActive)
        ].join(" ").toLowerCase().indexOf(q) !== -1;
      })
      : sessions;
    return h("aside", { className: cx("ncg-sidebar", props.open && "ncg-sidebar-open") },
      h("div", { className: "ncg-sidebar-head" },
        h("div", null,
          h("div", { className: "ncg-eyebrow" }, "Hermes"),
          h("h2", null, "NextChat")
        ),
        h("div", { className: "ncg-side-actions" },
          h(IconButton, { title: "Refresh sessions", onClick: props.onRefresh }, "↻"),
          h(IconButton, { title: "Close sidebar", className: "ncg-mobile-only", onClick: props.onClose }, "×")
        )
      ),
      h("button", { type: "button", className: "ncg-new-chat", onClick: props.onNew },
        h("span", null, "+"),
        h("span", null, "New conversation")
      ),
      h("label", { className: "ncg-session-search" },
        h("span", null, "Search"),
        h("input", {
          type: "search",
          value: query,
          placeholder: "搜索会话、模型或预览",
          onChange: function (e) { setQuery(e.target.value); }
        })
      ),
      h("div", { className: "ncg-session-list" },
        props.loading
          ? h("div", { className: "ncg-muted-block" }, "Loading sessions...")
          : sessions.length === 0
            ? h("div", { className: "ncg-muted-block" }, "No saved sessions yet.")
            : filtered.length === 0
              ? h("div", { className: "ncg-muted-block" }, "No matching sessions.")
              : filtered.map(function (s) {
              const active = props.activeKey === s.id || props.activeKey === s.sessionKey;
              return h("button", {
                type: "button",
                key: s.id,
                className: cx("ncg-session-row", active && "active"),
                onClick: function () { props.onResume(s.id); }
              },
                h("div", { className: "ncg-session-title" },
                  h("span", null, s.title || "Untitled"),
                  s.active ? h(StatusDot, { tone: "green", label: "active" }) : null
                ),
                h("div", { className: "ncg-session-preview" }, s.preview || "No preview"),
                h("div", { className: "ncg-session-meta" },
                  h("span", null, (s.messageCount || 0) + " messages"),
                  h("span", null, formatTime(s.lastActive))
                )
              );
            })
      )
    );
  }

  function Launcher(props) {
    const starters = [
      "帮我梳理这个项目接下来怎么推进",
      "一起排查一个代码问题",
      "把想法拆成可执行任务",
      "总结当前会话并给出下一步"
    ];
    return h("div", { className: "ncg-launcher" },
      h("div", { className: "ncg-launcher-copy" },
        h("h1", null, "我们从哪里开始？"),
        h("p", null, "用更安静的工作台界面和 Hermes 对话。")
      ),
      h("div", { className: "ncg-starter-grid" },
        starters.map(function (text) {
          return h("button", {
            type: "button",
            key: text,
            className: "ncg-starter-card",
            onClick: function () { props.onStarter(text); }
          },
            h("span", { className: "ncg-starter-mark" }, "↗"),
            h("strong", null, text),
            h("span", null, "点击填入并发送")
          );
        })
      )
    );
  }

  function MessageBubble(props) {
    const msg = props.message;
    const role = msg.role || "assistant";
    if (role === "tool") {
      return h("div", { className: "ncg-tool-inline" },
        h("span", null, msg.name || "tool"),
        h("code", null, msg.text || msg.context || "")
      );
    }
    return h("article", { className: cx("ncg-message", "ncg-message-" + role, msg.streaming && "streaming") },
      h("div", { className: "ncg-message-avatar" }, role === "user" ? "U" : role === "system" ? "S" : "H"),
      h("div", { className: "ncg-message-body" },
        h("div", { className: "ncg-message-label" }, role === "user" ? "You" : role === "system" ? "System" : "Hermes"),
        msg.reasoning ? h("details", { className: "ncg-reasoning" },
          h("summary", null, "Reasoning"),
          h("pre", null, msg.reasoning)
        ) : null,
        h(MarkdownLite, { text: msg.text }),
        msg.streaming ? h("div", { className: "ncg-stream-mark" }, h("span", null), h("span", null), h("span", null)) : null
      )
    );
  }

  function MessagesPane(props) {
    const ref = useRef(null);
    useEffect(function () {
      const node = ref.current;
      if (!node) return;
      node.scrollTop = node.scrollHeight;
    }, [props.messages, props.running]);

    return h("section", { ref: ref, className: "ncg-messages" },
      props.messages.length === 0
        ? h(Launcher, { onStarter: props.onStarter })
        : props.messages.map(function (msg) {
          return h(MessageBubble, { key: msg.id, message: msg });
        })
    );
  }

  function PromptPanel(props) {
    const prompt = props.prompt;
    const [value, setValue] = useState("");
    useEffect(function () { setValue(""); }, [prompt && prompt.requestId]);
    if (!prompt) return null;

    if (prompt.type === "approval") {
      return h("div", { className: "ncg-prompt ncg-prompt-warn" },
        h("div", null,
          h("strong", null, "需要执行确认"),
          h("p", null, prompt.description || "Hermes wants to run a guarded action."),
          prompt.command ? h("code", null, prompt.command) : null
        ),
        h("div", { className: "ncg-prompt-actions" },
          h("button", { type: "button", onClick: function () { props.onApproval("deny"); } }, "Reject"),
          h("button", { type: "button", onClick: function () { props.onApproval("session"); } }, "Allow session"),
          h("button", { type: "button", className: "primary", onClick: function () { props.onApproval("once"); } }, "Run once")
        )
      );
    }

    if (prompt.type === "clarify") {
      const choices = prompt.choices || [];
      return h("div", { className: "ncg-prompt" },
        h("strong", null, "Hermes 需要你补充信息"),
        h("p", null, prompt.question || "Please clarify."),
        choices.length ? h("div", { className: "ncg-choice-list" },
          choices.map(function (choice) {
            return h("button", {
              type: "button",
              key: choice,
              onClick: function () { props.onClarify(choice); }
            }, choice);
          })
        ) : null,
        h("div", { className: "ncg-inline-form" },
          h("input", {
            value: value,
            placeholder: choices.length ? "Or type another answer" : "Type your answer",
            onChange: function (e) { setValue(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === "Enter" && value.trim()) props.onClarify(value.trim());
            }
          }),
          h("button", { type: "button", disabled: !value.trim(), onClick: function () { props.onClarify(value.trim()); } }, "Send"),
          h("button", { type: "button", onClick: function () { props.onClarify(""); } }, "Skip")
        )
      );
    }

    const secretLike = prompt.type === "sudo" || prompt.type === "secret";
    if (secretLike) {
      return h("div", { className: "ncg-prompt ncg-prompt-secret" },
        h("strong", null, prompt.type === "sudo" ? "需要 sudo 密码" : (prompt.envVar || "需要密钥")),
        h("p", null, prompt.prompt || "This value is sent to Hermes only for this pending request."),
        h("div", { className: "ncg-inline-form" },
          h("input", {
            type: "password",
            value: value,
            placeholder: prompt.type === "sudo" ? "Password" : "Secret value",
            onChange: function (e) { setValue(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === "Enter") props.onSecret(value);
            }
          }),
          h("button", { type: "button", className: "primary", onClick: function () { props.onSecret(value); } }, "Send"),
          h("button", { type: "button", onClick: function () { props.onSecret(""); } }, "Cancel")
        )
      );
    }
    return null;
  }

  function ModelControls(props) {
    const [models, setModels] = useState([]);
    const [optionsLoading, setOptionsLoading] = useState(false);
    const [reasoning, setReasoning] = useState("medium");
    const [applying, setApplying] = useState("");

    const currentModel = props.info.model || props.currentModel || "";
    const selectedModelValue = useMemo(function () {
      const match = models.find(function (row) { return row.model === currentModel; });
      return match ? match.value : "";
    }, [models, currentModel]);

    useEffect(function () {
      if (!props.connected) return;
      let cancelled = false;
      setOptionsLoading(true);
      const optionsPromise = props.gw
        ? props.gw.request("model.options", props.sessionId ? { session_id: props.sessionId } : {}, 30000)
        : SDK.fetchJSON("/api/model/options");
      const reasoningPromise = props.gw
        ? props.gw.request("config.get", { key: "reasoning" }, 30000)
        : Promise.resolve({ value: "medium" });
      Promise.all([optionsPromise, reasoningPromise])
        .then(function (results) {
          if (cancelled) return;
          const modelPayload = results[0] || {};
          const reasoningPayload = results[1] || {};
          setModels(flattenModelOptions(modelPayload));
          props.onInfo(Object.assign({}, {
            model: props.info.model || modelPayload.model || "",
            provider: props.info.provider || modelPayload.provider || ""
          }));
          setReasoning(props.info.reasoning_effort || reasoningPayload.value || "medium");
        })
        .catch(function (err) {
          if (!cancelled) props.onError(parseApiError(err));
        })
        .finally(function () {
          if (!cancelled) setOptionsLoading(false);
        });
      return function () { cancelled = true; };
    }, [props.gw, props.connected, props.sessionId]);

    useEffect(function () {
      if (props.info.reasoning_effort) setReasoning(props.info.reasoning_effort);
    }, [props.info.reasoning_effort]);

    async function applyModel(value, confirmExpensive) {
      if (!value || !props.gw || props.disabled) return;
      const parts = value.split("\n");
      const provider = parts[0] || "";
      const model = parts.slice(1).join("\n") || "";
      if (!provider || !model) return;
      setApplying("model");
      try {
        const command = model + " --provider " + provider + (props.sessionId ? "" : " --global");
        const params = {
          key: "model",
          value: command,
          confirm_expensive_model: !!confirmExpensive
        };
        if (props.sessionId) params.session_id = props.sessionId;
        const result = await props.gw.request("config.set", params, 120000);
        if (result && result.confirm_required) {
          const ok = window.confirm(result.confirm_message || result.warning || "This model may be expensive. Continue?");
          if (ok) await applyModel(value, true);
          return;
        }
        props.onInfo({ model: (result && result.value) || model, provider: provider });
      } catch (err) {
        props.onError(parseApiError(err));
      } finally {
        setApplying("");
      }
    }

    async function applyReasoning(value) {
      if (!value || !props.gw || props.disabled) return;
      setApplying("reasoning");
      try {
        const params = { key: "reasoning", value: value };
        if (props.sessionId) params.session_id = props.sessionId;
        const result = await props.gw.request("config.set", params, 30000);
        const next = (result && result.value) || value;
        setReasoning(next);
        props.onInfo({ reasoning_effort: next });
      } catch (err) {
        props.onError(parseApiError(err));
      } finally {
        setApplying("");
      }
    }

    return h("div", { className: "ncg-model-controls" },
      h("label", { className: "ncg-model-select-wrap" },
        h("span", null, "Model"),
        h("select", {
          value: selectedModelValue,
          disabled: props.disabled || optionsLoading || applying === "model" || models.length === 0,
          onChange: function (e) { applyModel(e.target.value, false); },
          title: currentModel || "Select model"
        },
          selectedModelValue ? null : h("option", { value: "" }, optionsLoading ? "Loading models" : shortModelName(currentModel)),
          models.map(function (row) {
            return h("option", {
              key: row.value,
              value: row.value,
              disabled: !row.authenticated
            }, shortModelName(row.model) + " · " + row.providerName);
          })
        )
      ),
      h("label", { className: "ncg-reasoning-select-wrap" },
        h("span", null, "Reasoning"),
        h("select", {
          value: reasoning || "medium",
          disabled: props.disabled || applying === "reasoning",
          onChange: function (e) { applyReasoning(e.target.value); },
          title: "Reasoning effort"
        },
          ["none", "minimal", "low", "medium", "high", "xhigh"].map(function (value) {
            return h("option", { key: value, value: value }, value);
          })
        )
      )
    );
  }

  function PermissionBadge(props) {
    const [mode, setMode] = useState("manual");

    useEffect(function () {
      if (!props.connected) return;
      let cancelled = false;
      SDK.fetchJSON(PLUGIN_API + "/permissions")
        .then(function (result) {
          if (cancelled) return;
          setMode(normalizeApprovalMode((result && result.mode) || "manual"));
        })
        .catch(function () {
          if (!cancelled) setMode("manual");
        });
      return function () { cancelled = true; };
    }, [props.connected, props.sessionId]);

    const display = approvalDisplay(props.info || {}, mode);
    return h("span", {
      className: cx("ncg-permission-badge", "ncg-permission-" + display.tone),
      title: display.title
    },
      h("span", { className: "ncg-permission-dot", "aria-hidden": true }),
      display.label
    );
  }

  function isImageFile(file) {
    return !!(file && String(file.type || "").toLowerCase().startsWith("image/"));
  }

  function isPdfFile(file) {
    const type = String((file && file.type) || "").toLowerCase();
    const name = String((file && file.name) || "").toLowerCase();
    return type === "application/pdf" || name.endsWith(".pdf");
  }

  function readFileAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result || "")); };
      reader.onerror = function () { reject(reader.error || new Error("Could not read file")); };
      reader.readAsDataURL(file);
    });
  }

  function uploadKindLabel(item) {
    if (item && item.kind === "image") return "IMG";
    if (item && item.kind === "pdf") return "PDF";
    return "FILE";
  }

  function uploadReferenceText(item) {
    if (item && item.native_text) return item.native_text;
    if (item && item.ref_text) return item.ref_text;
    if (item && item.text) return item.text;
    const kind = item && item.kind === "image" ? "Image" : item && item.kind === "pdf" ? "PDF" : "File";
    return kind + ": " + ((item && item.path) || (item && item.name) || "uploaded file");
  }

  function uploadPromptReference(item) {
    if (item && item.native_attached) return "";
    return uploadReferenceText(item);
  }

  function createPreviewUrl(file) {
    try {
      return URL.createObjectURL(file);
    } catch (_e) {
      return "";
    }
  }

  function revokePreviewUrl(value) {
    try {
      if (value && String(value).indexOf("blob:") === 0) URL.revokeObjectURL(value);
    } catch (_e) {
      /* ignore */
    }
  }

  function isUnknownGatewayMethod(err) {
    const message = parseApiError(err).toLowerCase();
    return message.indexOf("unknown method") >= 0 || message.indexOf("-32601") >= 0;
  }

  function Composer(props) {
    const [draft, setDraft] = useState("");
    const [uploads, setUploads] = useState([]);
    const [uploading, setUploading] = useState(false);
    const imageInputRef = useRef(null);
    const fileInputRef = useRef(null);
    const textareaRef = useRef(null);

    useEffect(function () {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.style.height = "0px";
      ta.style.height = Math.min(180, ta.scrollHeight) + "px";
    }, [draft]);

    const send = function () {
      const text = draft.trim();
      const refs = uploads.map(uploadPromptReference).filter(Boolean);
      const outgoingText = refs.length ? (text ? text + "\n\n" + refs.join("\n") : refs.join("\n")) : text;
      if ((!outgoingText && !uploads.some(function (item) { return item && item.native_attached; })) || props.disabled || props.running) return;
      setDraft("");
      const sentUploads = uploads.slice();
      setUploads([]);
      sentUploads.forEach(function (item) { revokePreviewUrl(item && item.preview_url); });
      props.onSend(outgoingText);
    };

    function removeUpload(item) {
      setUploads(function (prev) {
        return prev.filter(function (candidate) {
          const remove = candidate === item || (item && candidate && candidate.upload_id === item.upload_id);
          if (remove) revokePreviewUrl(candidate && candidate.preview_url);
          return !remove;
        });
      });
      if (item && item.native_path && props.gw && props.gw.request) {
        props.gw.request("image.detach", {
          session_id: item.session_id || props.sessionId,
          path: item.native_path
        }, 30000).catch(function () {});
      }
    }

    async function uploadWorkspaceFiles(cwd, files) {
      if (!files.length) return [];
      const form = new FormData();
      form.append("cwd", cwd);
      files.forEach(function (file) {
        form.append("files", file, file.name);
      });

      const response = await authedFetch(PLUGIN_API + "/files/upload", {
        method: "POST",
        body: form
      });
      if (!response.ok) {
        throw new Error(response.status + ": " + await responseMessage(response));
      }
      const payload = await response.json();
      return payload && Array.isArray(payload.items) ? payload.items : [];
    }

    async function attachImagePath(sessionId, item, file) {
      const result = await props.gw.request("image.attach", {
        session_id: sessionId,
        path: (item && (item.full_path || item.path)) || file.name
      }, 120000);
      return {
        name: file.name,
        path: (result && result.path) || (item && item.path) || file.name,
        full_path: result && result.path,
        size: file.size,
        kind: "image",
        text: (result && result.text) || ("[User attached image: " + file.name + "]")
      };
    }

    async function attachImageFile(sessionId, file, item) {
      const dataUrl = await readFileAsDataUrl(file);
      try {
        const result = await props.gw.request("image.attach_bytes", {
          session_id: sessionId,
          content_base64: dataUrl,
          filename: file.name
        }, 120000);
        return {
          name: file.name,
          path: (result && result.path) || file.name,
          full_path: result && result.path,
          size: file.size,
          kind: "image",
          text: (result && result.text) || ("[User attached image: " + file.name + "]")
        };
      } catch (err) {
        if (!isUnknownGatewayMethod(err)) throw err;
        return attachImagePath(sessionId, item, file);
      }
    }

    async function attachPdfFile(sessionId, file) {
      const dataUrl = await readFileAsDataUrl(file);
      const result = await props.gw.request("pdf.attach", {
        session_id: sessionId,
        content_base64: dataUrl,
        filename: file.name
      }, 180000);
      return {
        name: file.name,
        path: (result && result.path) || file.name,
        full_path: result && result.path,
        size: file.size,
        kind: "pdf",
        text: (result && result.text) || ("[User attached PDF: " + file.name + "]")
      };
    }

    async function uploadSelectedFiles(fileList) {
      const selected = Array.prototype.slice.call(fileList || []);
      if (!selected.length || uploading || props.disabled || props.running) return;
      setUploading(true);
      try {
        let cwd = props.info && props.info.cwd;
        let activeSessionId = props.sessionId;
        if ((!cwd || !activeSessionId) && props.onEnsureWorkspace) {
          const ensured = await props.onEnsureWorkspace();
          cwd = cwd || (ensured && ensured.cwd);
          activeSessionId = activeSessionId || (ensured && ensured.sessionId);
        }
        if (!cwd || !activeSessionId) {
          throw new Error("Create or resume a conversation before uploading files.");
        }

        const items = await uploadWorkspaceFiles(cwd, selected);
        for (let i = 0; i < selected.length && i < items.length; i += 1) {
          const file = selected[i];
          items[i] = Object.assign({}, items[i], {
            upload_id: nowId("upload"),
            session_id: activeSessionId,
            preview_url: isImageFile(file) ? createPreviewUrl(file) : ""
          });
          try {
            const nativeItem = isImageFile(file)
              ? await attachImageFile(activeSessionId, file, items[i])
              : isPdfFile(file)
                ? await attachPdfFile(activeSessionId, file)
                : null;
            if (nativeItem && nativeItem.text) {
              items[i] = Object.assign({}, items[i], {
                native_attached: true,
                native_text: nativeItem.text,
                native_path: nativeItem.path || nativeItem.full_path || ""
              });
            }
          } catch (err) {
            if (!isUnknownGatewayMethod(err)) {
              items[i] = Object.assign({}, items[i], {
                native_error: parseApiError(err)
              });
            }
          }
        }

        if (items.length) {
          setUploads(function (prev) {
            const next = prev.concat(items);
            const keep = next.slice(-8);
            next.slice(0, Math.max(0, next.length - keep.length)).forEach(function (item) {
              revokePreviewUrl(item && item.preview_url);
            });
            return keep;
          });
          if (props.onFilesChanged) props.onFilesChanged();
          setTimeout(function () { textareaRef.current && textareaRef.current.focus(); }, 0);
        }
      } catch (err) {
        if (props.onError) props.onError(parseApiError(err));
      } finally {
        setUploading(false);
      }
    }

    useEffect(function () {
      if (props.prefill) {
        setDraft(props.prefill);
        setTimeout(function () { textareaRef.current && textareaRef.current.focus(); }, 0);
      }
    }, [props.prefill]);

    return h("form", {
      className: "ncg-composer",
      onSubmit: function (e) { e.preventDefault(); send(); }
    },
      h("div", { className: "ncg-composer-box" },
        h("textarea", {
          ref: textareaRef,
          rows: 1,
          value: draft,
          disabled: props.disabled,
          placeholder: props.running ? "Hermes is working..." : "描述你要做的事，Shift+Enter 换行",
          onChange: function (e) { setDraft(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }
        }),
        uploads.length || uploading ? h("div", { className: "ncg-upload-strip" },
          uploads.map(function (item) {
            if (item.kind === "image" && item.preview_url) {
              return h("div", {
                key: item.upload_id || item.path || item.name,
                className: "ncg-upload-preview",
                title: (item.full_path || item.path || item.name) + (item.size ? " - " + formatBytes(item.size) : "")
              },
                h("img", { src: item.preview_url, alt: item.name || "Uploaded image" }),
                h("span", { className: "ncg-upload-preview-name" }, item.name || item.path),
                h("button", {
                  type: "button",
                  className: "ncg-upload-remove",
                  title: "Remove image",
                  "aria-label": "Remove image",
                  onClick: function () { removeUpload(item); }
                }, "x")
              );
            }
            return h("span", {
              key: item.upload_id || item.path || item.name,
              className: cx("ncg-upload-chip", item.kind === "image" && "ncg-upload-chip-image", item.kind === "pdf" && "ncg-upload-chip-pdf"),
              title: (item.full_path || item.path || item.name) + (item.size ? " - " + formatBytes(item.size) : "")
            },
              h("span", { className: "ncg-upload-chip-icon", "aria-hidden": true }, uploadKindLabel(item)),
              h("span", { className: "ncg-upload-chip-name" }, item.name || item.path),
              h("button", {
                type: "button",
                className: "ncg-upload-chip-remove",
                title: "Remove file",
                "aria-label": "Remove file",
                onClick: function () { removeUpload(item); }
              }, "x")
            );
          }),
          uploading ? h("span", { className: "ncg-upload-chip ncg-upload-chip-loading" }, "Uploading...") : null
        ) : null,
        h("div", { className: "ncg-composer-footer" },
          h(ModelControls, {
            gw: props.gw,
            connected: props.connected,
            disabled: props.disabled || props.running,
            sessionId: props.sessionId,
            info: props.info || {},
            currentModel: props.currentModel,
            onInfo: props.onInfo,
            onError: props.onError
          }),
          h("div", { className: "ncg-composer-actions" },
            h("input", {
              ref: imageInputRef,
              className: "ncg-upload-input",
              type: "file",
              accept: "image/*",
              multiple: true,
              onChange: function (e) {
                uploadSelectedFiles(e.target.files);
                e.target.value = "";
              }
            }),
            h("input", {
              ref: fileInputRef,
              className: "ncg-upload-input",
              type: "file",
              multiple: true,
              onChange: function (e) {
                uploadSelectedFiles(e.target.files);
                e.target.value = "";
              }
            }),
            h("button", {
              type: "button",
              className: "ncg-upload-btn",
              title: "Upload image",
              "aria-label": "Upload image",
              disabled: props.disabled || props.running || uploading,
              onClick: function () { imageInputRef.current && imageInputRef.current.click(); }
            }, "Img"),
            h("button", {
              type: "button",
              className: "ncg-upload-btn",
              title: "Upload file",
              "aria-label": "Upload file",
              disabled: props.disabled || props.running || uploading,
              onClick: function () { fileInputRef.current && fileInputRef.current.click(); }
            }, "File"),
            h(PermissionBadge, {
              gw: props.gw,
              connected: props.connected,
              sessionId: props.sessionId,
              info: props.info || {}
            }),
            props.running ? h("button", {
              type: "button",
              className: "ncg-stop-btn",
              onClick: props.onInterrupt
            }, "Stop") : null,
            h("button", {
              type: "submit",
              className: "ncg-send-btn",
              disabled: props.disabled || props.running || uploading || (!draft.trim() && !uploads.length)
            }, "↑")
          )
        )
      )
    );
  }

  function FileActionButton(props) {
    return h("button", {
      type: "button",
      className: cx("ncg-file-action", props.danger && "ncg-file-danger"),
      title: props.title,
      "aria-label": props.title,
      disabled: props.disabled,
      onClick: function (event) {
        event.stopPropagation();
        if (props.onClick) props.onClick();
      }
    }, props.children);
  }

  function FileTreeNode(props) {
    const item = props.item;
    const depth = props.depth || 0;
    const isDir = item.type === "directory";
    const children = Array.isArray(item.children) ? item.children : [];

    return h("div", { className: "ncg-file-node" },
      h("div", {
        className: cx("ncg-file-row", isDir && "ncg-file-row-dir"),
        style: { paddingLeft: 8 + depth * 14 + "px" },
        title: item.full_path || item.path || item.name
      },
        h("span", { className: "ncg-file-caret", "aria-hidden": true }, isDir ? "▾" : ""),
        h("span", { className: "ncg-file-icon", "aria-hidden": true }, isDir ? "▣" : "□"),
        h("span", { className: "ncg-file-name" }, item.name),
        h("span", { className: "ncg-file-size" }, isDir ? "" : formatBytes(item.size)),
        h("div", { className: "ncg-file-actions" },
          h(FileActionButton, {
            title: "Delete",
            danger: true,
            disabled: !item.deletable,
            onClick: function () { props.onDelete(item); }
          }, "×"),
          h(FileActionButton, {
            title: "Copy path",
            onClick: function () { props.onCopy(item); }
          }, "⧉"),
          !isDir ? h(FileActionButton, {
            title: "Download",
            disabled: !item.downloadable,
            onClick: function () { props.onDownload(item); }
          }, "↓") : null
        )
      ),
      item.error ? h("div", {
        className: "ncg-file-node-error",
        style: { marginLeft: 40 + depth * 14 + "px" }
      }, item.error) : null,
      isDir && children.length ? h("div", { className: "ncg-file-children" },
        children.map(function (child) {
          return h(FileTreeNode, {
            key: child.path,
            item: child,
            depth: depth + 1,
            onCopy: props.onCopy,
            onDelete: props.onDelete,
            onDownload: props.onDownload
          });
        })
      ) : null
    );
  }

  function FileDrawer(props) {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(false);
    const [notice, setNotice] = useState("");
    const [localError, setLocalError] = useState("");
    const cwd = props.cwd || "";

    const loadFiles = useCallback(function () {
      setNotice("");
      setLocalError("");
      if (!cwd) {
        setItems([]);
        return Promise.resolve();
      }
      setLoading(true);
      return SDK.fetchJSON(pluginPath("/files/tree", { cwd: cwd }))
        .then(function (data) {
          setItems(data.items || []);
          if (data.truncated) {
            setNotice("Showing first " + data.max_entries + " entries.");
          }
        })
        .catch(function (err) {
          setItems([]);
          setLocalError(parseApiError(err));
        })
        .finally(function () {
          setLoading(false);
        });
    }, [cwd]);

    useEffect(function () {
      if (props.open) loadFiles();
    }, [props.open, props.version, loadFiles]);

    async function downloadItem(item) {
      setNotice("");
      setLocalError("");
      try {
        const response = await authedFetch(pluginPath("/files/download", { cwd: cwd, path: item.path }));
        if (!response.ok) throw new Error(await responseMessage(response));
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = item.name;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        setNotice("Download started.");
      } catch (err) {
        setLocalError(err.message || String(err));
      }
    }

    async function copyItem(item) {
      const value = item.full_path || item.path || item.name;
      setNotice("");
      setLocalError("");
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error(value);
        await navigator.clipboard.writeText(value);
        setNotice("Path copied.");
      } catch (_err) {
        setLocalError(value);
      }
    }

    async function deleteItem(item) {
      const ok = window.confirm("Delete " + item.name + "?");
      if (!ok) return;
      setNotice("");
      setLocalError("");
      try {
        await SDK.fetchJSON(PLUGIN_API + "/files", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cwd: cwd, path: item.path, confirm: true })
        });
        setNotice("Deleted " + item.name + ".");
        loadFiles();
      } catch (err) {
        setLocalError(parseApiError(err));
      }
    }

    const body = !cwd
      ? h("div", { className: "ncg-file-empty" }, "No workspace yet.")
      : loading
        ? h("div", { className: "ncg-file-empty" }, "Loading files...")
        : items.length
          ? h("div", { className: "ncg-file-tree" },
            items.map(function (item) {
              return h(FileTreeNode, {
                key: item.path,
                item: item,
                depth: 0,
                onCopy: copyItem,
                onDelete: deleteItem,
                onDownload: downloadItem
              });
            })
          )
          : h("div", { className: "ncg-file-empty" }, "No files yet.");

    return h("aside", { className: "ncg-file-drawer", "aria-label": "Conversation files" },
      h("div", { className: "ncg-file-head" },
        h("div", null,
          h("div", { className: "ncg-eyebrow" }, "Files"),
          h("h2", null, "Workspace files")
        ),
        h("div", { className: "ncg-file-head-actions" },
          h(IconButton, { title: "Refresh files", onClick: loadFiles, disabled: loading || !cwd }, "↻"),
          h(IconButton, { title: "Close files", onClick: props.onClose }, "×")
        )
      ),
      cwd ? h("div", { className: "ncg-file-cwd", title: cwd }, cwd) : null,
      notice ? h("div", { className: "ncg-file-notice" }, notice) : null,
      localError ? h("div", { className: "ncg-file-error" }, localError) : null,
      h("div", { className: "ncg-file-body" }, body)
    );
  }

  function ToolPanel(props) {
    const tools = props.tools || [];
    const [expandedTools, setExpandedTools] = useState({});
    function toggleTool(toolId) {
      setExpandedTools(function (prev) {
        const next = Object.assign({}, prev);
        next[toolId] = !next[toolId];
        return next;
      });
    }
    return h("aside", { className: "ncg-inspector" },
      h("div", { className: "ncg-inspector-section" },
        h("div", { className: "ncg-inspector-title" }, "Runtime"),
        h("div", { className: "ncg-runtime-row" },
          h(StatusDot, { tone: props.connected ? "green" : props.connecting ? "amber" : "red", label: props.connectionState }),
          h("span", null, props.connectionState)
        ),
        h("div", { className: "ncg-runtime-meta" },
          h("span", null, "Session"),
          h("code", null, props.sessionKey || props.sessionId || "new")
        ),
        h("div", { className: "ncg-runtime-meta" },
          h("span", null, "Model"),
          h("code", null, props.info.model || "default")
        ),
        h("div", { className: "ncg-runtime-meta" },
          h("span", null, "Reason"),
          h("code", null, props.info.reasoning_effort || "medium")
        ),
        props.info.cwd ? h("div", { className: "ncg-runtime-meta" },
          h("span", null, "Workspace"),
          h("code", null, props.info.cwd)
        ) : null,
        props.info.workspaceName ? h("div", { className: "ncg-runtime-meta" },
          h("span", null, "Folder"),
          h("code", null, props.info.workspaceName)
        ) : null
      ),
      h("div", { className: "ncg-inspector-section ncg-tools-section" },
        h("div", { className: "ncg-inspector-title" }, "Tool activity"),
        tools.length === 0
          ? h("div", { className: "ncg-empty-tools" }, "No tool calls yet.")
          : tools.map(function (tool) {
            const expanded = !!expandedTools[tool.id];
            const hasDetails = !!(tool.context || tool.preview || tool.error || tool.summary);
            return h("div", { key: tool.id, className: cx("ncg-tool-row", "ncg-tool-" + tool.status) },
              h("button", {
                type: "button",
                className: "ncg-tool-top",
                disabled: !hasDetails,
                "aria-expanded": expanded,
                onClick: function () { if (hasDetails) toggleTool(tool.id); }
              },
                h("span", { className: cx("ncg-tool-caret", expanded && "ncg-tool-caret-open"), "aria-hidden": true }, hasDetails ? ">" : ""),
                h("strong", null, tool.name || "tool"),
                h("span", null, tool.status)
              ),
              expanded ? h("div", { className: "ncg-tool-details" },
                tool.context ? h("p", null, tool.context) : null,
                tool.preview ? h("code", null, tool.preview) : null,
                tool.error ? h("p", { className: "ncg-error-text" }, tool.error) : null,
                tool.summary ? h("p", null, tool.summary) : null
              ) : null
            );
          })
      )
    );
  }

  function NextChatPage() {
    const [clientVersion, setClientVersion] = useState(0);
    const gw = useMemo(function () { return new GatewayClient(); }, [clientVersion]);
    const [connectionState, setConnectionState] = useState("idle");
    const [sessions, setSessions] = useState([]);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [sessionId, setSessionId] = useState(null);
    const [sessionKey, setSessionKey] = useState(null);
    const [messages, setMessages] = useState([]);
    const [tools, setTools] = useState([]);
    const [info, setInfo] = useState({});
    const [running, setRunning] = useState(false);
    const [status, setStatus] = useState("ready");
    const [error, setError] = useState("");
    const [mobileSidebar, setMobileSidebar] = useState(false);
    const [prefill, setPrefill] = useState("");
    const [prompt, setPrompt] = useState(null);
    const [filesOpen, setFilesOpen] = useState(false);
    const [filesVersion, setFilesVersion] = useState(0);
    const sessionIdRef = useRef(null);
    const infoRef = useRef({});

    const connected = connectionState === "open";
    const connecting = connectionState === "connecting";

    const refreshSessions = useCallback(function () {
      setSessionsLoading(true);
      SDK.fetchJSON("/api/sessions?limit=40&order=recent&exclude_sources=cron")
        .then(function (data) {
          setSessions((data.sessions || []).map(normalizeSession));
        })
        .catch(function (err) {
          setError(parseApiError(err));
        })
        .finally(function () { setSessionsLoading(false); });
    }, []);

    async function createWorkspaceForPrompt(text) {
      setStatus("creating workspace");
      const workspace = await SDK.fetchJSON(PLUGIN_API + "/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: titleFromPrompt(text) })
      });
      if (!workspace || !workspace.cwd) {
        throw new Error("Workspace folder was not created");
      }
      return workspace;
    }

    useEffect(function () {
      sessionIdRef.current = sessionId;
    }, [sessionId]);

    useEffect(function () {
      infoRef.current = info || {};
    }, [info]);

    useEffect(function () {
      let cancelled = false;
      const offState = gw.onState(setConnectionState);
      const offAny = gw.onAny(function (ev) {
        const activeSessionId = sessionIdRef.current;
        if (ev.session_id && activeSessionId && ev.session_id !== activeSessionId) return;
        handleGatewayEvent(ev);
      });
      gw.connect()
        .then(function () {
          if (!cancelled) {
            setError("");
            setStatus("connected");
          }
        })
        .catch(function (err) {
          if (!cancelled) setError(err.message || String(err));
        });
      refreshSessions();
      return function () {
        cancelled = true;
        offState();
        offAny();
        gw.close();
      };
    }, [gw, refreshSessions]);

    function patchLastAssistant(fn) {
      setMessages(function (prev) {
        const copy = prev.slice();
        let idx = copy.length - 1;
        while (idx >= 0 && copy[idx].role !== "assistant") idx--;
        if (idx < 0 || !copy[idx].streaming) {
          copy.push(fn({ id: nowId("assistant"), role: "assistant", text: "", streaming: true }));
        } else {
          copy[idx] = fn(copy[idx]);
        }
        return copy;
      });
    }

    function handleGatewayEvent(ev) {
      const payload = ev.payload || {};
      if (ev.type === "session.info") {
        setInfo(function (prev) { return Object.assign({}, prev, payload); });
        if (ev.session_id) {
          sessionIdRef.current = ev.session_id;
          setSessionId(ev.session_id);
        }
        if (payload.running === false) setRunning(false);
        return;
      }
      if (ev.type === "message.start") {
        setRunning(true);
        setStatus("streaming");
        setPrompt(null);
        patchLastAssistant(function (msg) {
          return Object.assign({}, msg, { text: "", streaming: true });
        });
        return;
      }
      if (ev.type === "message.delta") {
        const delta = textOf(payload.text);
        patchLastAssistant(function (msg) {
          return Object.assign({}, msg, { text: (msg.text || "") + delta, streaming: true });
        });
        return;
      }
      if (ev.type === "reasoning.delta" || ev.type === "reasoning.available") {
        const delta = textOf(payload.text);
        patchLastAssistant(function (msg) {
          return Object.assign({}, msg, { reasoning: (msg.reasoning || "") + delta });
        });
        return;
      }
      if (ev.type === "message.complete") {
        const finalText = textOf(payload.text || payload.rendered);
        patchLastAssistant(function (msg) {
          return Object.assign({}, msg, {
            text: finalText || msg.text || "",
            streaming: false,
            reasoning: textOf(payload.reasoning || msg.reasoning || "")
          });
        });
        setRunning(false);
        setStatus(payload.status || "ready");
        setTimeout(refreshSessions, 700);
        return;
      }
      if (ev.type === "status.update") {
        setStatus(textOf(payload.text || payload.kind || "working"));
        return;
      }
      if (ev.type === "tool.start") {
        const id = payload.tool_id || nowId("tool");
        setTools(function (prev) {
          return prev.concat([{
            id: id,
            name: displayText(payload.name, "tool"),
            context: limitText(payload.context, 1000),
            status: "running",
            preview: ""
          }]).slice(-30);
        });
        return;
      }
      if (ev.type === "tool.progress" || ev.type === "tool.generating") {
        setTools(function (prev) {
          return prev.map(function (tool) {
            const name = displayText(payload.name);
            if ((tool.id === payload.tool_id || tool.name === name) && tool.status === "running") {
              const preview = limitText(payload.preview || payload.text, 2000);
              return Object.assign({}, tool, { preview: preview || tool.preview });
            }
            return tool;
          });
        });
        return;
      }
      if (ev.type === "tool.complete") {
        setTools(function (prev) {
          return prev.map(function (tool) {
            const name = displayText(payload.name);
            if (tool.id === payload.tool_id || tool.name === name) {
              const errorText = limitText(payload.error, 2000);
              const summaryText = limitText(payload.summary || payload.result, 3000);
              return Object.assign({}, tool, {
                status: errorText ? "error" : "done",
                error: errorText,
                summary: summaryText
              });
            }
            return tool;
          });
        });
        return;
      }
      if (ev.type === "clarify.request") {
        setRunning(false);
        setPrompt({
          type: "clarify",
          requestId: payload.request_id,
          question: displayText(payload.question),
          choices: (payload.choices || []).map(function (choice) { return displayText(choice); }).filter(Boolean)
        });
        return;
      }
      if (ev.type === "approval.request") {
        setRunning(false);
        setPrompt({
          type: "approval",
          command: limitText(payload.command, 2000),
          description: displayText(payload.description, "dangerous command")
        });
        return;
      }
      if (ev.type === "sudo.request") {
        setRunning(false);
        setPrompt({ type: "sudo", requestId: payload.request_id });
        return;
      }
      if (ev.type === "secret.request") {
        setRunning(false);
        setPrompt({
          type: "secret",
          requestId: payload.request_id,
          envVar: displayText(payload.env_var),
          prompt: displayText(payload.prompt)
        });
        return;
      }
      if (ev.type === "error") {
        const message = displayText(payload.message, "unknown error");
        setError(message);
        setRunning(false);
        setStatus("error");
        setMessages(function (prev) {
          return prev.concat([{ id: nowId("err"), role: "system", text: "error: " + message }]);
        });
      }
    }

    async function ensureSession(initialText) {
      if (sessionId) return sessionId;
      const workspace = await createWorkspaceForPrompt(initialText);
      const created = await gw.request("session.create", {
        cwd: workspace.cwd,
        title: titleFromPrompt(initialText)
      });
      sessionIdRef.current = created.session_id;
      setSessionId(created.session_id);
      setSessionKey(created.session_key || created.stored_session_id || created.session_id);
      const nextInfo = Object.assign({}, created.info || {}, {
        workspaceRoot: workspace.root || "",
        workspaceName: workspace.name || ""
      });
      infoRef.current = nextInfo;
      setInfo(nextInfo);
      setMessages(normalizeGatewayMessages(created.messages || []));
      return created.session_id;
    }

    async function ensureWorkspaceForUpload() {
      const sid = await ensureSession("Uploaded files");
      const currentInfo = infoRef.current || {};
      setStatus("ready");
      return {
        sessionId: sid,
        cwd: currentInfo.cwd || "",
        info: currentInfo
      };
    }

    async function sendSlash(text, sid) {
      const command = text.replace(/^\/+/, "");
      if (command === "new" || command === "clear") {
        startNew();
        return;
      }
      if (command === "interrupt" || command === "stop") {
        await interrupt();
        return;
      }
      try {
        const result = await gw.request("slash.exec", { command: command, session_id: sid }, 30000);
        const output = textOf((result && (result.output || result.warning)) || "");
        setMessages(function (prev) {
          return prev.concat([{ id: nowId("sys"), role: "system", text: output || "/" + command + ": done" }]);
        });
      } catch (_e) {
        setMessages(function (prev) {
          return prev.concat([{ id: nowId("sys"), role: "system", text: "Slash command was not handled by this GUI." }]);
        });
      }
    }

    async function sendMessage(text) {
      if (!connected || running) return;
      setError("");
      const trimmed = text.trim();
      if (trimmed === "/new" || trimmed === "/clear") {
        startNew();
        return;
      }
      if (trimmed === "/interrupt" || trimmed === "/stop") {
        await interrupt();
        return;
      }
      setRunning(true);
      try {
        const sid = await ensureSession(text);
        setMessages(function (prev) {
          return prev.concat([{ id: nowId("user"), role: "user", text: text }]);
        });
        if (trimmed.startsWith("/")) {
          await sendSlash(trimmed, sid);
          setRunning(false);
          setStatus("ready");
          return;
        }
        setStatus("queued");
        await gw.request("prompt.submit", { session_id: sid, text: text }, 30000);
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        setError(message);
        setRunning(false);
      }
    }

    function startNew() {
      sessionIdRef.current = null;
      setSessionId(null);
      setSessionKey(null);
      setMessages([]);
      setTools([]);
      infoRef.current = {};
      setInfo({});
      setPrompt(null);
      setStatus("ready");
      setError("");
      setMobileSidebar(false);
    }

    async function resumeSession(storedId) {
      if (!connected || !storedId) return;
      setError("");
      setStatus("resuming");
      setMobileSidebar(false);
      try {
        const resumed = await gw.request("session.resume", { session_id: storedId, cols: 100 }, 120000);
        sessionIdRef.current = resumed.session_id;
        setSessionId(resumed.session_id);
        setSessionKey(resumed.session_key || resumed.resumed || storedId);
        infoRef.current = resumed.info || {};
        setInfo(infoRef.current);
        setRunning(!!resumed.running);
        setMessages(normalizeGatewayMessages(resumed.messages || []));
        if (resumed.inflight) {
          setMessages(function (prev) {
            const next = prev.slice();
            if (resumed.inflight.user) {
              next.push({ id: nowId("user"), role: "user", text: resumed.inflight.user });
            }
            next.push({
              id: nowId("assistant"),
              role: "assistant",
              text: resumed.inflight.assistant || "",
              streaming: !!resumed.inflight.streaming
            });
            return next;
          });
        }
        setStatus(resumed.status || "ready");
      } catch (err) {
        setError(parseApiError(err));
        setStatus("error");
      }
    }

    async function interrupt() {
      if (!sessionId) return;
      try {
        await gw.request("session.interrupt", { session_id: sessionId }, 30000);
        setRunning(false);
        setStatus("interrupted");
      } catch (err) {
        setError(parseApiError(err));
      }
    }

    async function answerClarify(answer) {
      if (!prompt || !prompt.requestId) return;
      try {
        await gw.request("clarify.respond", { request_id: prompt.requestId, answer: answer || "" }, 30000);
        if (answer) {
          setMessages(function (prev) {
            return prev.concat([{ id: nowId("user"), role: "user", text: answer }]);
          });
        }
        setPrompt(null);
        setRunning(true);
        setStatus("running");
      } catch (err) {
        setError(parseApiError(err));
      }
    }

    async function answerApproval(choice) {
      if (!sessionId) return;
      try {
        await gw.request("approval.respond", { session_id: sessionId, choice: choice }, 30000);
        setPrompt(null);
        setRunning(choice !== "deny");
        setStatus(choice === "deny" ? "denied" : "running");
      } catch (err) {
        setError(parseApiError(err));
      }
    }

    async function answerSecret(value) {
      if (!prompt || !prompt.requestId) return;
      const method = prompt.type === "sudo" ? "sudo.respond" : "secret.respond";
      const key = prompt.type === "sudo" ? "password" : "value";
      const params = { request_id: prompt.requestId };
      params[key] = value || "";
      try {
        await gw.request(method, params, 30000);
        setPrompt(null);
        setRunning(true);
        setStatus("running");
      } catch (err) {
        setError(parseApiError(err));
      }
    }

    const stateLabel = error
      ? "Error"
      : connecting
        ? "Connecting"
        : connected
          ? (running ? "Hermes is working" : status || "Ready")
          : "Disconnected";

    return h("div", { className: "ncg-root" },
      mobileSidebar ? h("button", { className: "ncg-scrim", type: "button", onClick: function () { setMobileSidebar(false); } }) : null,
      h(SessionList, {
        sessions: sessions,
        loading: sessionsLoading,
        activeKey: sessionKey || sessionId,
        open: mobileSidebar,
        onClose: function () { setMobileSidebar(false); },
        onRefresh: refreshSessions,
        onNew: startNew,
        onResume: resumeSession
      }),
      h("main", { className: "ncg-main" },
        h("header", { className: "ncg-topbar" },
          h("div", { className: "ncg-top-left" },
            h(IconButton, { className: "ncg-mobile-only", title: "Open sessions", onClick: function () { setMobileSidebar(true); } }, "☰"),
            h("div", null,
              h("div", { className: "ncg-eyebrow" }, "Conversation"),
              h("h1", null, sessionKey ? "Hermes session" : "New conversation")
            )
          ),
          h("div", { className: "ncg-top-actions" },
            h(IconButton, {
              className: cx("ncg-file-toggle", filesOpen && "ncg-file-toggle-active"),
              title: info.cwd ? (filesOpen ? "Close files" : "Open files") : "No workspace folder yet",
              disabled: !info.cwd,
              onClick: function () {
                setFilesOpen(function (open) { return !open; });
              }
            }, "▣"),
            h("span", { className: "ncg-status-pill" },
              h(StatusDot, { tone: connected ? "green" : connecting ? "amber" : "red", label: connectionState }),
              stateLabel
            ),
            h("button", { type: "button", className: "ncg-plain-btn", onClick: function () { setClientVersion(function (v) { return v + 1; }); } }, "Reconnect")
          )
        ),
        error ? h("div", { className: "ncg-error-banner" }, error) : null,
        h(MessagesPane, {
          messages: messages,
          running: running,
          onStarter: function (text) {
            setPrefill("");
            sendMessage(text);
          }
        }),
        h(PromptPanel, {
          prompt: prompt,
          onClarify: answerClarify,
          onApproval: answerApproval,
          onSecret: answerSecret
        }),
        h(Composer, {
          disabled: !connected || !!prompt,
          running: running,
          stateLabel: stateLabel,
          gw: gw,
          connected: connected,
          sessionId: sessionId,
          info: info,
          currentModel: info.model,
          prefill: prefill,
          onSend: sendMessage,
          onInterrupt: interrupt,
          onEnsureWorkspace: ensureWorkspaceForUpload,
          onFilesChanged: function () {
            setFilesVersion(function (version) { return version + 1; });
          },
          onInfo: function (patch) {
            setInfo(function (prev) {
              const next = Object.assign({}, prev, patch || {});
              infoRef.current = next;
              return next;
            });
          },
          onError: function (message) {
            setError(message || "");
          }
        })
      ),
      filesOpen ? h(FileDrawer, {
        open: filesOpen,
        cwd: info.cwd,
        version: filesVersion,
        onClose: function () { setFilesOpen(false); }
      }) : null,
      h(ToolPanel, {
        tools: tools,
        info: info,
        connected: connected,
        connecting: connecting,
        connectionState: connectionState,
        sessionId: sessionId,
        sessionKey: sessionKey
      })
    );
  }

  REGISTRY.register("nextchatgui", NextChatPage);
})();
