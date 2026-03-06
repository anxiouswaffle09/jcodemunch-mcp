"""Index repository tool - fetch, parse, summarize, save."""

import asyncio
import hashlib
import os
from typing import Optional
from types import SimpleNamespace
from urllib.parse import quote, urlparse

import httpx

from ..parser import LANGUAGE_EXTENSIONS, extract_refs, parse_file
from ..security import is_secret_file, is_binary_extension, get_max_index_files
from ..storage import IndexStore
from ..summarizer import summarize_symbols


# File patterns to skip
SKIP_PATTERNS = [
    "node_modules/", "vendor/", "venv/", ".venv/", "__pycache__/",
    "dist/", "build/", ".git/", ".tox/", ".mypy_cache/",
    "target/",
    ".gradle/",
    "test_data/", "testdata/", "fixtures/", "snapshots/",
    "migrations/",
    ".min.js", ".min.ts", ".bundle.js",
    "package-lock.json", "yarn.lock", "go.sum",
    "generated/", "proto/",
]


def parse_github_url(url: str) -> tuple[str, str]:
    """Extract owner/repo from GitHub URL or owner/repo string.
    
    Supports:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - owner/repo
    """
    # Remove .git suffix
    url = url.removesuffix(".git")
    
    # If it contains a / but not ://, treat as owner/repo
    if "/" in url and "://" not in url:
        parts = url.split("/")
        return parts[0], parts[1]
    
    # Parse URL
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    
    # Extract owner/repo from path
    parts = path.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    
    raise ValueError(f"Could not parse GitHub URL: {url}")


async def fetch_repo_tree(owner: str, repo: str, token: Optional[str] = None) -> list[dict]:
    """Fetch full repository tree via git/trees API.
    
    Uses recursive=1 to get all paths in a single API call.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD"
    params = {"recursive": "1"}
    headers = {"Accept": "application/vnd.github.v3+json"}
    
    if token:
        headers["Authorization"] = f"token {token}"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
    
    return data.get("tree", [])


def should_skip_file(path: str) -> bool:
    """Check if file should be skipped based on path patterns."""
    for pattern in SKIP_PATTERNS:
        if pattern in path:
            return True
    return False


def discover_source_files(
    tree_entries: list[dict],
    gitignore_content: Optional[str] = None,
    max_files: Optional[int] = None,
    max_size: int = 500 * 1024  # 500KB
) -> tuple[list[str], bool]:
    """Discover source files from tree entries.
    
    Applies filtering pipeline:
    1. Type filter (blobs only)
    2. Extension filter (supported languages)
    3. Skip list patterns
    4. Size limit
    5. .gitignore matching
    6. File count limit
    """
    import pathspec

    max_files = get_max_index_files(max_files)
    
    # Parse gitignore if provided
    gitignore_spec = None
    if gitignore_content:
        try:
            gitignore_spec = pathspec.PathSpec.from_lines(
                "gitignore",
                gitignore_content.split("\n")
            )
        except Exception:
            pass
    
    files = []
    
    for entry in tree_entries:
        # Type filter - only blobs (files)
        if entry.get("type") != "blob":
            continue
        
        path = entry.get("path", "")
        size = entry.get("size", 0)
        
        # Extension filter
        _, ext = os.path.splitext(path)
        if ext not in LANGUAGE_EXTENSIONS:
            continue

        # Skip list
        if should_skip_file(path):
            continue

        # Secret detection
        if is_secret_file(path):
            continue

        # Binary extension check
        if is_binary_extension(path):
            continue
        
        # Size limit
        if size > max_size:
            continue
        
        # Gitignore matching
        if gitignore_spec and gitignore_spec.match_file(path):
            continue
        
        files.append(path)
    
    truncated = len(files) > max_files

    # File count limit with prioritization
    if truncated:
        # Prioritize: src/, lib/, pkg/, cmd/, internal/ first
        priority_dirs = ["src/", "lib/", "pkg/", "cmd/", "internal/"]
        
        def priority_key(path):
            # Check if in priority dir
            for i, prefix in enumerate(priority_dirs):
                if path.startswith(prefix):
                    return (i, path.count("/"), path)
            # Not in priority dir - sort after
            return (len(priority_dirs), path.count("/"), path)
        
        files.sort(key=priority_key)
        files = files[:max_files]
    
    return files, truncated


async def fetch_file_content(
    owner: str,
    repo: str,
    path: str,
    token: Optional[str] = None,
    ref: str = "HEAD",
) -> str:
    """Fetch raw file content from raw.githubusercontent.com.

    Uses raw download endpoint which has higher rate limits than the Contents API
    and doesn't count against the 60 req/hr unauthenticated API quota.
    """
    encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{encoded_path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"token {token}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return response.text


async def fetch_gitignore(
    owner: str,
    repo: str,
    token: Optional[str] = None
) -> Optional[str]:
    """Fetch .gitignore file if it exists."""
    try:
        return await fetch_file_content(owner, repo, ".gitignore", token)
    except Exception:
        return None


async def index_repo(
    url: str,
    use_ai_summaries: bool = True,
    github_token: Optional[str] = None,
    storage_path: Optional[str] = None,
    incremental: bool = False,
) -> dict:
    """Index a GitHub repository.
    
    Args:
        url: GitHub repository URL or owner/repo string
        use_ai_summaries: Whether to use AI for symbol summaries
        github_token: GitHub API token (optional, for private repos/higher rate limits)
        storage_path: Custom storage path (default: ~/.code-index/)
    
    Returns:
        Dict with indexing results
    """
    # Parse URL
    try:
        owner, repo = parse_github_url(url)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    
    # Get GitHub token from env if not provided
    if not github_token:
        github_token = os.environ.get("GITHUB_TOKEN")
    
    warnings = []
    max_files = get_max_index_files()
    
    try:
        # Fetch tree
        try:
            tree_entries = await fetch_repo_tree(owner, repo, github_token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"success": False, "error": f"Repository not found: {owner}/{repo}"}
            elif e.response.status_code == 403:
                return {"success": False, "error": "GitHub API rate limit exceeded. Set GITHUB_TOKEN."}
            raise
        
        # Fetch .gitignore
        gitignore_content = await fetch_gitignore(owner, repo, github_token)
        
        # Discover source files
        source_files, truncated = discover_source_files(
            tree_entries,
            gitignore_content,
            max_files=max_files,
        )
        
        if not source_files:
            return {"success": False, "error": "No source files found"}
        
        # Fetch all file contents concurrently
        semaphore = asyncio.Semaphore(10)  # Limit concurrent requests

        fetch_warnings: list[str] = []

        async def fetch_with_limit(path: str) -> tuple[str, Optional[str]]:
            async with semaphore:
                for attempt in range(3):
                    try:
                        content = await fetch_file_content(owner, repo, path, github_token)
                        return path, content
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            return path, None  # Genuinely missing
                        if e.response.status_code in (403, 429):
                            await asyncio.sleep(2 ** attempt * 2)
                        else:
                            await asyncio.sleep(2 ** attempt)
                    except Exception:
                        await asyncio.sleep(2 ** attempt)
                fetch_warnings.append(f"Failed to fetch {path} after 3 attempts")
                return path, None

        tasks = [fetch_with_limit(path) for path in source_files]
        file_contents = await asyncio.gather(*tasks)

        # Build current_files map from fetched content (None = failed, "" = empty file)
        current_files: dict[str, str] = {}
        for path, content in file_contents:
            if content is not None:
                current_files[path] = content
        warnings.extend(fetch_warnings)

        store = IndexStore(base_path=storage_path)

        # Incremental path
        if incremental and store.load_index(owner, repo) is not None:
            changed, new, deleted = store.detect_changes(owner, repo, current_files)

            if not changed and not new and not deleted:
                return {
                    "success": True,
                    "message": "No changes detected",
                    "repo": f"{owner}/{repo}",
                    "changed": 0, "new": 0, "deleted": 0,
                }

            files_to_parse = set(changed) | set(new)
            new_symbols = []
            raw_files_subset: dict[str, str] = {}

            for path in files_to_parse:
                content = current_files[path]
                # Track file hashes for changed/new files even when symbol extraction yields none.
                raw_files_subset[path] = content
                _, ext = os.path.splitext(path)
                language = LANGUAGE_EXTENSIONS.get(ext)
                if not language:
                    continue
                try:
                    symbols = parse_file(content, path, language)
                    if symbols:
                        new_symbols.extend(symbols)
                except Exception:
                    warnings.append(f"Failed to parse {path}")

            new_symbols = summarize_symbols(new_symbols, use_ai=use_ai_summaries)

            needs_full_backfill = store.load_refs(owner, repo) is None
            updated = store.incremental_save(
                owner=owner, name=repo,
                changed_files=changed, new_files=new, deleted_files=deleted,
                new_symbols=new_symbols, raw_files=raw_files_subset,
                languages={},
            )

            if needs_full_backfill:
                # No refs.json existed — backfill refs for all current files
                all_sym_dicts = updated.symbols if updated else []
                all_refs = []
                for path, content in current_files.items():
                    _, ext = os.path.splitext(path)
                    language = LANGUAGE_EXTENSIONS.get(ext)
                    if not language:
                        continue
                    try:
                        proxies = [
                            SimpleNamespace(line=s["line"], end_line=s["end_line"],
                                            id=s["id"], file=s["file"])
                            for s in all_sym_dicts if s.get("file") == path
                        ]
                        all_refs.extend(extract_refs(content, path, language, proxies))
                    except Exception:
                        pass
                store.save_refs(owner, repo, all_refs)
            else:
                incremental_refs = []
                for path in files_to_parse:
                    content = current_files[path]
                    _, ext = os.path.splitext(path)
                    language = LANGUAGE_EXTENSIONS.get(ext)
                    if not language:
                        continue
                    try:
                        file_symbols = [s for s in new_symbols if s.file == path]
                        incremental_refs.extend(extract_refs(content, path, language, file_symbols))
                    except Exception:
                        warnings.append(f"Failed to extract refs for {path}")
                removed = set(changed) | set(deleted)
                store.merge_refs(owner, repo, incremental_refs, removed)

            result = {
                "success": True,
                "repo": f"{owner}/{repo}",
                "incremental": True,
                "changed": len(changed), "new": len(new), "deleted": len(deleted),
                "symbol_count": len(updated.symbols) if updated else 0,
                "indexed_at": updated.indexed_at if updated else "",
                "ref_count": len(store.load_refs(owner, repo) or []),
            }
            if warnings:
                result["warnings"] = warnings
            return result

        # Full index path
        all_symbols = []
        languages = {}
        raw_files = {}
        parsed_files = []

        for path, content in current_files.items():
            _, ext = os.path.splitext(path)
            language = LANGUAGE_EXTENSIONS.get(ext)
            if not language:
                continue
            try:
                symbols = parse_file(content, path, language)
                raw_files[path] = content
                if symbols:
                    all_symbols.extend(symbols)
                    file_language = symbols[0].language or language
                    languages[file_language] = languages.get(file_language, 0) + 1
                    parsed_files.append(path)
            except Exception:
                warnings.append(f"Failed to parse {path}")
                continue

        if not all_symbols:
            return {"success": False, "error": "No symbols extracted"}

        # Generate summaries
        all_symbols = summarize_symbols(all_symbols, use_ai=use_ai_summaries)

        # Save index
        # Track hashes for all discovered source files so incremental change detection
        # does not repeatedly report no-symbol files as "new".
        file_hashes = {
            fp: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for fp, content in current_files.items()
        }
        store.save_index(
            owner=owner,
            name=repo,
            source_files=sorted(current_files),
            symbols=all_symbols,
            raw_files=raw_files,
            languages=languages,
            file_hashes=file_hashes,
        )

        all_refs = []
        for path, content in current_files.items():
            _, ext = os.path.splitext(path)
            language = LANGUAGE_EXTENSIONS.get(ext)
            if not language:
                continue
            try:
                file_symbols = [s for s in all_symbols if s.file == path]
                all_refs.extend(extract_refs(content, path, language, file_symbols))
            except Exception:
                warnings.append(f"Failed to extract refs for {path}")
        store.save_refs(owner, repo, all_refs)

        result = {
            "success": True,
            "repo": f"{owner}/{repo}",
            "indexed_at": store.load_index(owner, repo).indexed_at,
            "file_count": len(parsed_files),
            "symbol_count": len(all_symbols),
            "ref_count": len(all_refs),
            "languages": languages,
            "files": parsed_files[:20],  # Limit files in response
        }

        if warnings:
            result["warnings"] = warnings

        if truncated:
            result["warnings"] = warnings + [f"Repository has many files; indexed first {max_files}"]

        return result
    
    except Exception as e:
        return {"success": False, "error": f"Indexing failed: {str(e)}"}
