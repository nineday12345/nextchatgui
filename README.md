# NextChatGUI for Hermes Dashboard

一个 Hermes dashboard 对话页插件。

## 功能

- 覆盖 Hermes dashboard 内置 `/chat` 页，不再显示 xterm/TUI。
- 通过 Hermes 自带 `/api/ws` JSON-RPC 与 `tui_gateway` 通信。
- 支持新建会话、搜索/恢复历史会话、流式输出、停止当前回合。
- 输入栏内支持切换当前模型和推理强度。
- 新会话会自动创建独立工作目录，并把该目录作为 Hermes session 的 `cwd`。
- Conversation 右上角提供文件抽屉，可查看当前会话工作目录、复制路径、下载文件、删除文件。
- 支持常见阻塞提示：clarify、approval、sudo、secret。
- 使用 dashboard SDK 的鉴权 helper，适配公网域名/OAuth gate/1Panel 反代场景。

## 安装

### GitHub 安装

上传到 GitHub 后，可以在 Hermes 服务器/容器里安装：

```bash
hermes plugins install owner/nextchatgui
```

或者使用完整仓库地址：

```bash
hermes plugins install https://github.com/owner/nextchatgui.git
```

安装后重启 `hermes dashboard` 或整个 Hermes 容器。这个仓库根目录包含 `plugin.yaml`，dashboard 插件入口在 `dashboard/manifest.json`。

### 手动复制

把整个目录复制到 Hermes home 的插件目录，例如：

```powershell
Copy-Item -Recurse E:\WORK\nextchatgui $env:USERPROFILE\.hermes\plugins\nextchatgui
```

1Panel Docker 常见路径类似：

```bash
/opt/1panel/apps/hermes/.hermes/plugins/nextchatgui
```

容器内对应：

```bash
~/.hermes/plugins/nextchatgui
```

然后重启 `hermes dashboard` 或整个 Hermes 容器。纯前端插件通常也可以打开 dashboard 后调用：

```text
/api/dashboard/plugins/rescan
```

但覆盖 `/chat` 这类路由时，重启和刷新浏览器最稳。

## 会话工作目录

默认每个新对话会创建一个独立目录。Linux/1Panel/Docker 环境会优先使用已挂载的 `/opt/data`：

```text
/opt/data/nextchatgui-workspaces/<timestamp-title-random>/
```

如果容器内没有 `/opt/data`，才会回退到：

```text
/data/nextchatgui-workspaces/<timestamp-title-random>/
```

Windows 本地预览默认：

```text
<HERMES_HOME>/workspaces/nextchatgui/<timestamp-title-random>/
```

可以通过环境变量改根目录：

```bash
HERMES_NEXTCHATGUI_WORKSPACE_ROOT=/opt/data/nextchatgui-workspaces
```

在 1Panel Docker 里建议把这个目录放到已挂载的数据卷内。注意这里必须是容器内路径，不是 Windows 的 `E:\...`。

## 会话文件

聊天页右上角的文件按钮会打开当前会话 `cwd` 的文件树。下载和删除都走 dashboard 插件 API，并限制在 NextChatGUI 工作区根目录内。

默认只允许删除文件、符号链接和空目录。如果确实需要允许删除非空目录，可以在部署环境里设置：

```bash
HERMES_NEXTCHATGUI_RECURSIVE_DELETE=1
```

Windows 本地预览时，新会话目录会自动写入隐藏的 `.hermes.md`，提醒模型优先使用相对路径，并在必须使用绝对路径时用 `C:/Users/...` 这种正斜杠格式，避免 Git Bash 把 `C:\...` 的反斜杠当成转义字符。

## 兼容性

需要 Hermes dashboard 提供：

- dashboard plugin SDK
- `/api/ws` JSON-RPC WebSocket
- `session.create`、`session.resume`、`prompt.submit` 等 `tui_gateway` 方法
- dashboard plugin backend API，用于创建会话工作目录

这些都是近期 Hermes dashboard/TUI 共用的能力。公网部署下不要手写 token；本插件只使用 `window.__HERMES_PLUGIN_SDK__.buildWsUrl()`。
