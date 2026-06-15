from __future__ import annotations

import mimetypes
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter()

ENV_ROOT_KEYS = (
    "HERMES_NEXTCHATGUI_WORKSPACE_ROOT",
    "HERMES_NEXTCHAT_WORKSPACE_ROOT",
    "HERMES_WORKSPACES_ROOT",
)
ENV_BROWSER_NOVNC_KEYS = (
    "HERMES_NEXTCHATGUI_BROWSER_NOVNC_URL",
    "NEXTCHATGUI_BROWSER_NOVNC_URL",
)
ENV_BROWSER_CDP_KEYS = (
    "HERMES_NEXTCHATGUI_BROWSER_CDP_URL",
    "NEXTCHATGUI_BROWSER_CDP_URL",
    "BROWSER_CDP_URL",
)
CONTAINER_DEFAULT_WORKSPACE_ROOTS = (
    Path("/opt/data/nextchatgui-workspaces"),
    Path("/data/nextchatgui-workspaces"),
)

WORKSPACE_CONTEXT_FILE = ".hermes.md"
WORKSPACE_CONTEXT = """# NextChatGUI Workspace Notes

- Treat this folder as the current working directory and prefer relative paths when creating or editing files.
- This Hermes session may execute terminal commands through Git Bash on Windows.
- If an absolute Windows path is unavoidable, quote it and use forward slashes, for example `C:/Users/name/file.md`.
- Avoid backslash Windows paths like `C:\\Users\\name\\file.md` inside shell commands because bash treats backslashes as escapes.
"""


class WorkspaceCreateRequest(BaseModel):
    title: str | None = None


class FileDeleteRequest(BaseModel):
    cwd: str
    path: str
    confirm: bool = False


def _hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hermes"


def _workspace_root() -> Path:
    for key in ENV_ROOT_KEYS:
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser().resolve(strict=False)
    if os.name != "nt":
        for root in CONTAINER_DEFAULT_WORKSPACE_ROOTS:
            if root.parent.exists():
                return root.resolve(strict=False)
        return CONTAINER_DEFAULT_WORKSPACE_ROOTS[0].resolve(strict=False)
    return (_hermes_home() / "workspaces" / "nextchatgui").resolve(strict=False)


def _portable_path(path: Path) -> str:
    text = str(path)
    if os.name == "nt":
        return text.replace("\\", "/")
    return text


def _slug(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or "conversation")[:48].strip("-") or "conversation"


def _unique_workspace_path(root: Path, title: str | None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{_slug(title)}"
    for index in range(20):
        suffix = secrets.token_hex(3) if index == 0 else f"{index}-{secrets.token_hex(2)}"
        candidate = root / f"{base}-{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate a unique workspace folder")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _first_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _normalize_approval_mode(value: Any, default: str = "manual") -> str:
    if isinstance(value, bool):
        return "off" if value is False else default
    mode = str(value or "").strip().lower()
    return mode or default


def _context_file_enabled() -> bool:
    raw = os.environ.get("HERMES_NEXTCHATGUI_CONTEXT_FILE", "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _write_workspace_context(path: Path) -> None:
    if not _context_file_enabled():
        return
    target = path / WORKSPACE_CONTEXT_FILE
    if target.exists():
        return
    target.write_text(WORKSPACE_CONTEXT, encoding="utf-8")


def _ensure_inside(path: Path, root: Path, detail: str = "Path is outside the workspace") -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=detail) from exc


def _clean_relative_path(value: str | None) -> Path:
    raw = str(value or "").replace("\\", "/").strip()
    if raw in {"", "."}:
        return Path()
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        raise HTTPException(status_code=400, detail="Path must be relative")
    parts = [part for part in raw.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="Parent traversal is not allowed")
    return Path(*parts)


def _resolve_workspace(cwd: str) -> Path:
    if not str(cwd or "").strip():
        raise HTTPException(status_code=400, detail="Workspace cwd is required")

    root = _workspace_root().resolve(strict=False)
    workspace = Path(cwd).expanduser().resolve(strict=False)
    _ensure_inside(workspace, root, "Workspace is outside the NextChatGUI workspace root")

    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not workspace.is_dir():
        raise HTTPException(status_code=400, detail="Workspace cwd is not a directory")
    return workspace


def _resolve_inside(workspace: Path, value: str | None, follow_final_symlink: bool = True) -> Path:
    rel = _clean_relative_path(value)
    if not rel.parts:
        return workspace

    raw_target = workspace / rel
    if follow_final_symlink:
        target = raw_target.resolve(strict=False)
        _ensure_inside(target, workspace)
        return target

    parent = raw_target.parent.resolve(strict=False)
    _ensure_inside(parent, workspace)
    return parent / raw_target.name


def _relative_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(workspace).as_posix()
    except ValueError:
        return path.relative_to(workspace).as_posix()


_REF_NEEDS_QUOTING_RE = re.compile(r"""[\s()\[\]{}<>"'`]""")


def _format_ref_value(value: str) -> str:
    if not value or not _REF_NEEDS_QUOTING_RE.search(value):
        return value
    if "`" not in value:
        return f"`{value}`"
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return value


def _modified_at(stat_result: os.stat_result | None) -> str | None:
    if stat_result is None:
        return None
    return datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat()


def _entry_type(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "directory"
        if path.is_file():
            return "file"
    except OSError:
        return "unknown"
    return "unknown"


def _file_item(path: Path, workspace: Path) -> dict[str, Any]:
    stat_result: os.stat_result | None = None
    try:
        stat_result = path.lstat() if path.is_symlink() else path.stat()
    except OSError:
        stat_result = None

    kind = _entry_type(path)
    return {
        "name": path.name,
        "path": _relative_path(path, workspace),
        "full_path": _portable_path(path),
        "type": kind,
        "size": stat_result.st_size if stat_result is not None and kind == "file" else None,
        "modified_at": _modified_at(stat_result),
        "downloadable": kind == "file",
        "deletable": path != workspace,
    }


def _safe_upload_name(value: str | None) -> str:
    name = Path(str(value or "").replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    if not name:
        return f"upload-{secrets.token_hex(3)}"

    if len(name) <= 180:
        return name

    parsed = Path(name)
    suffix = parsed.suffix[:32]
    stem = (parsed.stem or "upload")[: 180 - len(suffix) - 1]
    return f"{stem}{suffix}" if suffix else stem[:180]


def _unique_upload_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists() and not candidate.is_symlink():
        return candidate

    parsed = Path(filename)
    suffix = parsed.suffix
    stem = parsed.stem or "upload"
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate a unique upload filename")


def _upload_item(path: Path, workspace: Path, content_type: str | None) -> dict[str, Any]:
    mime = content_type or mimetypes.guess_type(path.name)[0] or ""
    rel_path = _relative_path(path, workspace)
    item = _file_item(path, workspace)
    item["mime"] = mime
    item["kind"] = "image" if mime.startswith("image/") else "pdf" if mime == "application/pdf" or path.suffix.lower() == ".pdf" else "file"
    item["ref_text"] = f"@file:{_format_ref_value(rel_path)}"
    return item


def _path_sort_key(path: Path) -> tuple[int, str]:
    try:
        is_dir = path.is_dir() and not path.is_symlink()
    except OSError:
        is_dir = False
    return (0 if is_dir else 1, path.name.lower())


def _iter_children(path: Path, include_hidden: bool) -> list[Path]:
    children = []
    for child in path.iterdir():
        if not include_hidden and child.name.startswith("."):
            continue
        children.append(child)
    return sorted(children, key=_path_sort_key)


def _tree_item(path: Path, workspace: Path, include_hidden: bool, counter: dict[str, int | bool], max_entries: int) -> dict[str, Any] | None:
    if int(counter["count"]) >= max_entries:
        counter["truncated"] = True
        return None

    counter["count"] = int(counter["count"]) + 1
    item = _file_item(path, workspace)
    if item["type"] != "directory":
        return item

    children: list[dict[str, Any]] = []
    try:
        for child in _iter_children(path, include_hidden):
            child_item = _tree_item(child, workspace, include_hidden, counter, max_entries)
            if child_item is None:
                break
            children.append(child_item)
    except PermissionError:
        item["error"] = "Permission denied"
    except OSError as exc:
        item["error"] = str(exc)

    item["children"] = children
    return item


@router.get("/workspaces/config")
async def workspace_config() -> dict:
    root = _workspace_root()
    return {
        "root": _portable_path(root),
        "exists": root.exists(),
        "env_keys": ENV_ROOT_KEYS,
    }


@router.get("/permissions")
async def permissions() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    approvals = cfg.get("approvals") if isinstance(cfg, dict) else {}
    if not isinstance(approvals, dict):
        approvals = {}
    return {
        "mode": _normalize_approval_mode(approvals.get("mode", "manual")),
        "cron_mode": _normalize_approval_mode(approvals.get("cron_mode", "deny"), "deny"),
    }


@router.get("/browser/config")
async def browser_config() -> dict[str, Any]:
    novnc_url = _first_env(ENV_BROWSER_NOVNC_KEYS)
    cdp_url = _first_env(ENV_BROWSER_CDP_KEYS)
    return {
        "novnc_url": novnc_url,
        "cdp_url": cdp_url,
        "configured": bool(novnc_url or cdp_url),
        "novnc_env_keys": ENV_BROWSER_NOVNC_KEYS,
        "cdp_env_keys": ENV_BROWSER_CDP_KEYS,
    }


@router.post("/workspaces")
async def create_workspace(body: WorkspaceCreateRequest) -> dict:
    root = _workspace_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = _unique_workspace_path(root, body.title)
        path.mkdir(parents=False, exist_ok=False)
        _write_workspace_context(path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied creating workspace under {root}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "root": _portable_path(root),
        "name": path.name,
        "cwd": _portable_path(path),
    }


@router.get("/files/tree")
async def file_tree(
    cwd: str = Query(..., min_length=1),
    include_hidden: bool = Query(default=False),
) -> dict[str, Any]:
    workspace = _resolve_workspace(cwd)
    max_entries = max(1, int(os.environ.get("HERMES_NEXTCHATGUI_MAX_FILE_ENTRIES", "500")))
    counter: dict[str, int | bool] = {"count": 0, "truncated": False}
    items: list[dict[str, Any]] = []

    try:
        for child in _iter_children(workspace, include_hidden):
            child_item = _tree_item(child, workspace, include_hidden, counter, max_entries)
            if child_item is None:
                break
            items.append(child_item)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "cwd": _portable_path(workspace),
        "items": items,
        "count": counter["count"],
        "truncated": counter["truncated"],
        "max_entries": max_entries,
    }


@router.post("/files/upload")
async def upload_files(
    cwd: str = Form(...),
    path: str = Form(default=""),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    workspace = _resolve_workspace(cwd)
    target_dir = _resolve_inside(workspace, path, follow_final_symlink=False)

    if target_dir.exists() or target_dir.is_symlink():
        resolved_dir = target_dir.resolve(strict=False)
        _ensure_inside(resolved_dir, workspace)
        if not resolved_dir.is_dir():
            raise HTTPException(status_code=400, detail="Upload target is not a directory")
        target_dir = resolved_dir
    else:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Permission denied creating upload folder") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    max_files = max(1, int(os.environ.get("HERMES_NEXTCHATGUI_MAX_UPLOAD_FILES", "20")))
    max_bytes = max(1, int(os.environ.get("HERMES_NEXTCHATGUI_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))))
    incoming = [file for file in files if file is not None]
    if not incoming:
        raise HTTPException(status_code=400, detail="No files were uploaded")
    if len(incoming) > max_files:
        raise HTTPException(status_code=413, detail=f"Upload limit is {max_files} files")

    items: list[dict[str, Any]] = []
    for upload in incoming:
        filename = _safe_upload_name(upload.filename)
        target = _unique_upload_path(target_dir, filename)
        written = 0
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(status_code=413, detail=f"{filename} exceeds the {max_bytes} byte upload limit")
                    handle.write(chunk)
        except HTTPException:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except PermissionError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(status_code=403, detail=f"Permission denied writing {filename}") from exc
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            await upload.close()

        items.append(_upload_item(target, workspace, upload.content_type))

    return {
        "ok": True,
        "cwd": _portable_path(workspace),
        "target": _relative_path(target_dir, workspace) if target_dir != workspace else "",
        "items": items,
    }


@router.get("/files/download")
async def download_file(
    cwd: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
) -> FileResponse:
    workspace = _resolve_workspace(cwd)
    target = _resolve_inside(workspace, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    requested_name = _clean_relative_path(path).name or target.name
    return FileResponse(
        target,
        filename=requested_name,
        media_type="application/octet-stream",
    )


@router.delete("/files")
async def delete_file(body: FileDeleteRequest) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Delete confirmation is required")

    workspace = _resolve_workspace(body.cwd)
    target = _resolve_inside(workspace, body.path, follow_final_symlink=False)
    if target == workspace:
        raise HTTPException(status_code=400, detail="Cannot delete workspace root")
    if not target.exists() and not target.is_symlink():
        raise HTTPException(status_code=404, detail="Path not found")

    recursive_delete = _truthy(os.environ.get("HERMES_NEXTCHATGUI_RECURSIVE_DELETE"))
    try:
        if target.is_dir() and not target.is_symlink():
            if recursive_delete:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        detail = "Directory is not empty" if target.is_dir() else str(exc)
        raise HTTPException(status_code=409, detail=detail) from exc

    return {
        "ok": True,
        "path": body.path,
    }
