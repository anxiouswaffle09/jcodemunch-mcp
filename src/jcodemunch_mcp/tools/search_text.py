"""Full-text search across indexed file contents."""

import os
import time
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided
from ._utils import resolve_repo


def search_text(
    repo: str,
    query: str,
    file_pattern: Optional[str] = None,
    max_results: int = 20,
    offset: int = 0,
    exhaustive: bool = False,
    exact: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Search for text across all indexed files in a repository.

    Useful when symbol search misses — e.g., searching for string literals,
    comments, configuration values, or patterns not captured as symbols.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        query: Text to search for.
        file_pattern: Optional glob pattern to filter files.
        max_results: Maximum number of matching lines to return (ignored when exhaustive=True).
        offset: Skip this many results before returning (for pagination).
        exhaustive: Return all results regardless of max_results cap.
        exact: Case-sensitive exact substring match. Default is case-insensitive.
               Use exact=True for punctuation-heavy queries like `Foo::new(`, enum variants,
               macro invocations, and log strings where case matters.
        storage_path: Custom storage path.

    Returns:
        Dict with matching lines grouped by file, plus _meta envelope.
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 500))
    offset = max(0, offset)

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Filter files
    import fnmatch
    files = index.source_files
    if file_pattern:
        files = [f for f in files if fnmatch.fnmatch(f, file_pattern) or fnmatch.fnmatch(f, f"*/{file_pattern}")]

    content_dir = store._content_dir(owner, name)
    # exact=True: raw case-sensitive match; default: case-insensitive
    query_match = query if exact else query.lower()
    all_matches = []
    files_searched = 0

    for file_path in files:
        full_path = content_dir / file_path
        if not full_path.exists():
            continue

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        files_searched += 1
        lines = content.split("\n")
        for line_num, line in enumerate(lines, 1):
            haystack = line if exact else line.lower()
            if query_match in haystack:
                all_matches.append({
                    "file": file_path,
                    "line": line_num,
                    "text": line.rstrip()[:200],
                })

    total_hits = len(all_matches)

    # Paginate / cap
    if exhaustive:
        page = all_matches[offset:]
    else:
        page = all_matches[offset:offset + max_results]

    truncated = (not exhaustive) and (offset + len(page)) < total_hits

    elapsed = (time.perf_counter() - start) * 1000

    # Token savings: raw bytes of searched files vs matched lines returned
    raw_bytes = 0
    for file_path in files[:files_searched]:
        try:
            raw_bytes += os.path.getsize(content_dir / file_path)
        except OSError:
            pass
    response_bytes = sum(len(m["text"].encode()) for m in page)
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved)

    response = {
        "repo": f"{owner}/{name}",
        "query": query,
        "result_count": len(page),
        "total_hits": total_hits,
        "offset": offset,
        "results": page,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "files_searched": files_searched,
            "truncated": truncated,
            "exhaustive": exhaustive,
            "exact": exact,
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }

    if truncated:
        response["warning"] = (
            f"WARNING: results truncated — showing {offset}–{offset + len(page)} "
            f"of {total_hits} total matches. "
            f"Rerun with offset={offset + max_results} to page, "
            f"or exhaustive=true to get all."
        )

    return response
