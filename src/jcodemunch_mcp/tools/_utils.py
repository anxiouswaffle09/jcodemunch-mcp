"""Shared helpers for tool modules."""

import threading
from pathlib import Path
from typing import Optional

from ..storage import IndexStore

RepoLookup = tuple[str, str]
RepoCacheKey = tuple[str, str]

# Cache: (storage_scope, bare_name) -> (owner, name)
_repo_name_cache: dict[RepoCacheKey, RepoLookup] = {}
_repo_cache_lock = threading.Lock()


def _storage_scope(store: IndexStore) -> str:
    """Return a stable cache scope for one storage root."""
    try:
        return str(Path(store.base_path).resolve())
    except OSError:
        return str(store.base_path)


def _resolve_bare_repo(repo: str, store: IndexStore) -> RepoLookup:
    """Resolve a bare repo name within one storage root."""
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
    return tuple(matching[0]["repo"].split("/", 1))


def invalidate_repo_name_cache() -> None:
    """Clear the bare-name -> (owner, name) cache."""
    with _repo_cache_lock:
        _repo_name_cache.clear()


def resolve_repo(repo: str, storage_path: Optional[str] = None) -> RepoLookup:
    """Parse 'owner/repo' or look up single name. Returns (owner, name).

    Raises ValueError if repo not found or name is ambiguous.
    """
    if "/" in repo:
        parts = repo.split("/", 1)
        return parts[0], parts[1]

    store = IndexStore(base_path=storage_path)
    cache_key = (_storage_scope(store), repo)

    with _repo_cache_lock:
        cached = _repo_name_cache.get(cache_key)
        if cached is not None:
            return cached

    result = _resolve_bare_repo(repo, store)

    with _repo_cache_lock:
        _repo_name_cache[cache_key] = result

    return result
