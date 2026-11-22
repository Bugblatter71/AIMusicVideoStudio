"""Path-traversal containment and JSON request validation helpers for Studio UI."""

from __future__ import annotations

import json
from pathlib import Path


def resolve_project_dir_safe(projects_root: Path, slug: str | None) -> Path | None:
    """Validates a project slug and returns its contained Path inside projects_root, or None."""
    if not isinstance(slug, str):
        return None
    s = slug.strip()
    if not s or s in (".", "..") or "/" in s or "\\" in s or "\x00" in s:
        return None
    try:
        root = Path(projects_root).resolve()
        cand = (root / s).resolve()
        return cand if cand.parent == root else None
    except Exception:
        return None


def resolve_config_in_project_safe(proj_dir: Path, config_name: str | None = None) -> Path | None:
    """Resolves the target project JSON file inside proj_dir without allowing traversal."""
    if not isinstance(proj_dir, Path) or not proj_dir.is_dir():
        return None
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    if not jsons:
        return None
    if config_name is None or not str(config_name).strip():
        return jsons[-1]
    cn = str(config_name).strip()
    if cn in (".", "..") or "/" in cn or "\\" in cn or "\x00" in cn or cn.startswith("state_hashes"):
        return None
    try:
        base = proj_dir.resolve()
        cand = (base / cn).resolve()
        if cand.parent != base or not cand.is_file():
            return None
        return cand
    except Exception:
        return None


def resolve_path_under_dir(base_dir: Path, rel_path: str | None) -> Path | None:
    """Resolves a relative path safely inside base_dir, rejecting absolute paths or '..' escapes."""
    if not isinstance(base_dir, Path) or rel_path is None:
        return None
    raw = str(rel_path).strip()
    if not raw or "\x00" in raw:
        return None
    try:
        base = base_dir.resolve()
        cand = (base / raw.lstrip("/\\")).resolve()
        if cand == base:
            return None
        cand.relative_to(base)
        return cand
    except Exception:
        return None


def read_json_dict(handler, max_bytes: int = 16 * 1024 * 1024) -> tuple[dict | None, str | None]:
    """Parses a bounded JSON object body from an HTTP request handler.

    Returns `(dict_data, None)` on success (or empty dict `{}` when Content-Length is 0/absent),
    or `(None, error_message)` if headers, UTF-8, size limit, or JSON object validation fails.
    """
    raw_len = handler.headers.get("Content-Length")
    if raw_len is None or str(raw_len).strip() == "":
        return {}, None
    try:
        length = int(str(raw_len).strip())
    except ValueError:
        return None, "Invalid Content-Length header"
    if length < 0 or length > max_bytes:
        return None, f"Request body size {length} exceeds allowed limit ({max_bytes} bytes)"
    if length == 0:
        return {}, None
    try:
        raw_bytes = handler.rfile.read(length)
        if len(raw_bytes) != length:
            return None, "Truncated request body"
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Invalid JSON request body: {exc}"
    if not isinstance(data, dict):
        return None, "JSON request body must be an object"
    return data, None
