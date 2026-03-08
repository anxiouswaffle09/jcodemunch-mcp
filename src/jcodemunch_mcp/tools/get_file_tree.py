"""Get file tree for a repository."""

import os
import time
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided
from ._utils import resolve_repo


def get_file_tree(
    repo: str,
    path_prefix: str = "",
    show_empty: bool = False,
    storage_path: Optional[str] = None
) -> dict:
    """Get repository file tree as compact indented text.

    Args:
        repo: Repository identifier (owner/repo or just repo name)
        path_prefix: Optional path prefix to filter
        show_empty: Show files with zero symbols (default False)
        storage_path: Custom storage path

    Returns:
        Dict with tree (str), repo, path_prefix, and _meta envelope.
    """
    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Filter files by prefix
    files = [f for f in index.source_files if f.startswith(path_prefix)]

    if not files:
        return {
            "repo": f"{owner}/{name}",
            "path_prefix": path_prefix,
            "tree": ""
        }

    # Precompute symbol counts and languages per file
    symbol_counts: dict[str, int] = {}
    file_languages: dict[str, str] = {}
    for sym in index.symbols:
        fp = sym.get("file", "")
        symbol_counts[fp] = symbol_counts.get(fp, 0) + 1
        if fp not in file_languages:
            lang = sym.get("language", "")
            if lang:
                file_languages[fp] = lang

    # Build tree text
    tree_text = _render_tree(files, path_prefix, symbol_counts, file_languages, show_empty)

    elapsed = (time.perf_counter() - start) * 1000

    # Token savings: path listing (Glob equivalent) vs compact tree response
    raw_bytes = sum(len(f.encode()) + 1 for f in files)  # +1 for newline per path
    response_bytes = len(tree_text.encode())
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved)

    file_count = len(files)
    if not show_empty:
        file_count = sum(1 for f in files if symbol_counts.get(f, 0) > 0)

    return {
        "repo": f"{owner}/{name}",
        "path_prefix": path_prefix,
        "tree": tree_text,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "file_count": file_count,
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }


def _render_tree(
    files: list[str],
    path_prefix: str,
    symbol_counts: dict[str, int],
    file_languages: dict[str, str],
    show_empty: bool,
) -> str:
    """Render file list as compact indented text tree.

    Files are sorted by symbol count descending within each directory.
    Zero-symbol files are hidden unless show_empty is True.
    """
    root: dict = {}

    for file_path in files:
        count = symbol_counts.get(file_path, 0)
        if not show_empty and count == 0:
            continue

        rel_path = file_path[len(path_prefix):].lstrip("/")
        parts = rel_path.split("/")

        # Get language
        lang = file_languages.get(file_path, "")
        if not lang:
            _, ext = os.path.splitext(file_path)
            from ..parser import LANGUAGE_EXTENSIONS
            lang = LANGUAGE_EXTENSIONS.get(ext, "")

        # Navigate to parent dir
        current = root
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        # Store file entry as tuple: (full_path, count, lang)
        filename = parts[-1]
        current[filename] = (file_path, count, lang)

    lines: list[str] = []
    _render_node(root, lines, indent=0)
    return "\n".join(lines)


def _render_node(node: dict, lines: list[str], indent: int) -> None:
    """Recursively render tree nodes to text lines."""
    prefix = "  " * indent

    # Separate dirs and files
    dirs = []
    file_entries = []
    for key, value in node.items():
        if isinstance(value, tuple):
            file_entries.append((key, value))
        else:
            dirs.append((key, value))

    # Sort dirs alphabetically
    dirs.sort(key=lambda x: x[0])

    # Sort files by symbol count descending, then name ascending
    file_entries.sort(key=lambda x: (-x[1][1], x[0]))

    # Render dirs first, then files
    for dir_name, children in dirs:
        lines.append(f"{prefix}{dir_name}/")
        _render_node(children, lines, indent + 1)

    for filename, (full_path, count, lang) in file_entries:
        symbol_label = "symbol" if count == 1 else "symbols"
        lang_suffix = f"  {lang}" if lang else ""
        lines.append(f"{prefix}{filename:<30} [{count} {symbol_label}]{lang_suffix}")
