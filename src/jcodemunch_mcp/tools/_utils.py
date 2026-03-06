"""Shared helpers for tool modules."""

import threading
from typing import Optional

from ..storage import IndexStore

# Cache: bare_name -> (owner, name)
_repo_name_cache: dict[str, tuple[str, str]] = {}
_repo_cache_lock = threading.Lock()


def invalidate_repo_name_cache() -> None:
    """Clear the bare-name -> (owner, name) cache."""
    with _repo_cache_lock:
        _repo_name_cache.clear()


def resolve_repo(repo: str, storage_path: Optional[str] = None) -> tuple[str, str]:
    """Parse 'owner/repo' or look up single name. Returns (owner, name).

    Raises ValueError if repo not found or name is ambiguous.
    """
    if "/" in repo:
        parts = repo.split("/", 1)
        return parts[0], parts[1]

    with _repo_cache_lock:
        if repo in _repo_name_cache:
            return _repo_name_cache[repo]

    store = IndexStore(base_path=storage_path)
    repos = store.list_repos()
    matching = [r for r in repos if r["repo"].endswith(f"/{repo}")]
    if not matching:
        raise ValueError(f"Repository not found: {repo}")
    if len(matching) > 1:
        candidates = [r["repo"] for r in matching]
        raise ValueError(
            f"Ambiguous repo name '{repo}'. Multiple matches: {candidates}. "
            f"Use the full 'owner/repo' form."
        )
    result = tuple(matching[0]["repo"].split("/", 1))

    with _repo_cache_lock:
        _repo_name_cache[repo] = result

    return result
