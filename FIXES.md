# jcodemunch-mcp — Full Fix Specification

All 29 issues identified across the codebase, grouped by category, with exact file/line
references, root cause, fix specification, and edge cases for each.

---

## Table of Contents

1. [Auto-Refresh & Change Detection](#1-auto-refresh--change-detection) — Issues 1–5
2. [Performance](#2-performance) — Issues 6–13
3. [Correctness](#3-correctness) — Issues 14–20
4. [Security & Privacy](#4-security--privacy) — Issues 21–22
5. [Code Quality & Cleanup](#5-code-quality--cleanup) — Issues 23–29
6. [Implementation Order](#6-implementation-order)
7. [Testing Plan](#7-testing-plan)

---

## 1. Auto-Refresh & Change Detection

### Issue 1 — Path persistence lost on server restart

**Severity:** Critical
**File:** `src/jcodemunch_mcp/server.py:65–68` (`register_path`), `server.py:52–63` (`_load_config`)
**Root cause:** `register_path` only writes to `self._paths` in memory. `autorefresh.json` is
read at startup but never written back. Server restarts wipe all registered paths, so the
auto-refresher has nothing to watch in new sessions. This is why Codex saw stale indexes.

**Fix:**
Persist paths back to `autorefresh.json` whenever `register_path` is called.

```python
def register_path(self, path: str):
    expanded = os.path.expanduser(str(path))
    resolved = os.path.realpath(expanded)   # normalise before storing
    with self._lock:
        if resolved in self._paths:
            return                          # already registered, skip write
        self._paths.add(resolved)

    # Persist to config atomically
    cfg_path = Path(self.CONFIG_PATH)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".json.tmp")
    try:
        existing = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        existing = {}
    paths_set = set(existing.get("paths", [])) | {resolved}
    existing["paths"] = sorted(paths_set)
    tmp.write_text(json.dumps(existing, indent=2))
    tmp.replace(cfg_path)
```

Also update `_load_config` to use `os.path.realpath` (not just `expanduser`) so stored paths
and newly-registered paths always normalise to the same canonical form:

```python
for p in cfg.get("paths", []):
    self._paths.add(os.path.realpath(os.path.expanduser(str(p))))
```

**Edge cases:**
- Concurrent `register_path` calls: the atomic temp-then-replace handles file writes; the
  in-memory set is guarded by `_lock`.
- Path registered via `index_folder` with a relative path (e.g. `../project`): `os.path.realpath`
  resolves it before storage, so the canonical form is always absolute.
- `autorefresh.json` directory not yet created: `mkdir(parents=True, exist_ok=True)` handles it.

---

### Issue 2 — SHA-256 of all file content on every tool call

**Severity:** Critical
**File:** `src/jcodemunch_mcp/storage/index_store.py:333` (`detect_changes`),
`src/jcodemunch_mcp/tools/index_folder.py:278–294` (builds `current_files` by reading all content)
**Root cause:** `detect_changes` receives a `current_files` dict of `{path: content}` and
computes SHA-256 of every file's full content to detect what changed. Building this dict requires
reading every source file from disk on every tool call (cooldown defaults to 0).

**Fix — Two-phase detection:**

**Phase 1 (fast, pre-call, always):** mtime + size check using stored metadata. Only proceed to
Phase 2 for files that mtime/size says changed.

**Phase 2 (targeted, only for changed files):** Read and SHA-256 only those files to confirm.

This requires extending the stored `file_hashes` structure from bare SHA-256 strings to include
mtime and size:

```python
# New format in file_hashes dict:
{
    "src/main.py": {
        "sha256": "abc123...",
        "mtime": 1709123456.789,
        "size": 4096
    }
}
```

**Backwards compatibility:** `load_index` must detect the old format (bare string values) and
treat them as hash-only with no mtime/size. On first refresh after upgrade, fall back to full
content scan for those files, then store in new format going forward.

**New function `detect_changes_fast` in `IndexStore`:**

```python
def detect_changes_fast(
    self,
    owner: str,
    name: str,
    folder_path: Path,
    current_discovered: list[Path],  # paths from discover_local_files
) -> tuple[list[str], list[str], list[str]]:
    """Two-phase change detection: mtime first, SHA-256 only for suspected changes."""
    index = self.load_index(owner, name)
    if not index:
        return [], [str(p.relative_to(folder_path)) for p in current_discovered], []

    old_meta = index.file_hashes          # {rel_path: {sha256, mtime, size} or str}
    current_rel = {
        p.relative_to(folder_path).as_posix(): p
        for p in current_discovered
    }

    old_set = set(old_meta.keys())
    new_set = set(current_rel.keys())

    deleted = list(old_set - new_set)
    added = list(new_set - old_set)
    possibly_changed = []

    for rel_path in old_set & new_set:
        meta = old_meta[rel_path]
        if isinstance(meta, str):
            # Old format — no mtime/size, must verify
            possibly_changed.append(rel_path)
            continue
        abs_path = current_rel[rel_path]
        try:
            stat = abs_path.stat()
            if stat.st_mtime != meta["mtime"] or stat.st_size != meta["size"]:
                possibly_changed.append(rel_path)
        except OSError:
            deleted.append(rel_path)

    # Phase 2: SHA-256 only suspects
    changed = []
    for rel_path in possibly_changed:
        abs_path = current_rel[rel_path]
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            new_hash = _file_hash(content)
            old_meta_entry = old_meta[rel_path]
            old_hash = old_meta_entry if isinstance(old_meta_entry, str) else old_meta_entry["sha256"]
            if new_hash != old_hash:
                changed.append(rel_path)
        except OSError:
            deleted.append(rel_path)

    return changed, added, deleted
```

Also update `save_index` and `incremental_save` to store mtime + size alongside SHA-256:

```python
def _make_file_meta(path: Path, content: str) -> dict:
    stat = path.stat()
    return {
        "sha256": _file_hash(content),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }
```

**Edge cases:**
- File edited and saved within the same second (same mtime, different content): size check
  catches most; SHA-256 in Phase 2 catches the rest (same size different content).
- File on a filesystem with 1-second mtime precision (FAT32, some NFS): more false positives
  trigger Phase 2, but correctness is maintained.
- File shrinks to exact same size as before: mtime differs, triggers Phase 2. Correct.

---

### Issue 3 — `git_head` stored but never used for change detection

**Severity:** High
**File:** `src/jcodemunch_mcp/storage/index_store.py:24–36` (`_get_git_head`),
`index_store.py:419` (stored in index but never used to drive detection)
**Root cause:** `git_head` is captured and stored as metadata, but `detect_changes` ignores it
entirely. The existing git infrastructure is unused.

**Fix — Git-accelerated detection layer (sits above Issue 2's fast detection):**

When the watched folder is a git repo, use git to get an exact file change list before
falling back to mtime. Three scenarios:

1. HEAD matches `last_git_head` and working tree is clean → skip all detection, nothing changed.
2. HEAD differs from `last_git_head` → `git diff --name-only <old> <new>` for committed changes
   + `git status --porcelain` for uncommitted changes.
3. HEAD matches but working tree dirty → `git status --porcelain` only.

```python
def _detect_changes_git(
    source_path: Path,
    stored_head: str,
    stored_file_metas: dict,
) -> tuple[set[str], set[str], str]:
    """
    Returns (modified_set, deleted_set, current_head).
    Falls back to empty sets (triggering mtime fallback) on any git error.
    """
    current_head = _get_git_head(source_path) or ""

    modified, deleted = set(), set()

    # Committed changes since last index
    if stored_head and current_head and current_head != stored_head:
        try:
            result = subprocess.run(
                ["git", "-C", str(source_path), "diff", "--name-only",
                 stored_head, current_head, "--"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    f = line.strip()
                    if f:
                        modified.add(f)
        except Exception:
            pass

    # Uncommitted working tree changes
    try:
        result = subprocess.run(
            ["git", "-C", str(source_path), "status", "--porcelain",
             "--untracked-files=all", "--"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if len(line) < 4:
                    continue
                xy = line[:2]
                path_part = line[3:]
                if "R" in xy and " -> " in path_part:
                    old, new = path_part.split(" -> ", 1)
                    deleted.add(old.strip())
                    modified.add(new.strip())
                elif xy.strip() == "D" or line[0] == "D":
                    deleted.add(path_part.strip())
                else:
                    modified.add(path_part.strip())
    except Exception:
        pass

    return modified, deleted, current_head
```

**Integration with `maybe_refresh`:** Before the mtime scan, attempt git detection. If git
returns results, use those as the authoritative changed-file list and skip scanning files
not in that list. Files git doesn't know about (gitignored) fall through to mtime check.

**Storing `source_path` in index:** The auto-refresher currently stores paths separately in
`autorefresh.json`. For git detection to work without the config, `source_path` should also
be stored in the index itself (add field to `CodeIndex`). This makes each index self-describing
and removes the `autorefresh.json` dependency for git repos.

**Edge cases:**
- Non-git folder: `_get_git_head` returns None → skip git detection → mtime fallback.
- git not installed: `FileNotFoundError` on `subprocess.run` → caught → mtime fallback.
- Detached HEAD: `git rev-parse HEAD` still works. No special handling needed.
- Shallow clone: `git rev-parse HEAD` works. `git diff` between commits may fail if old commit
  is outside the shallow boundary → caught → mtime fallback for that call.
- Gitignored files in watched dir: appear in mtime scan but not in git status → handled by
  mtime fallback path within the same refresh cycle.
- Submodule: `git status` shows `M submodule` not individual files → detect submodule entries
  and run `git status` within the submodule directory separately.

---

### Issue 4 — Concurrent refresh race condition on index

**Severity:** High
**File:** `src/jcodemunch_mcp/server.py:70–96` (`maybe_refresh`),
`src/jcodemunch_mcp/storage/index_store.py:347–450` (`incremental_save`)
**Root cause:** `_lock` in `AutoRefresher` is released before `index_folder` is called.
With cooldown=0, two simultaneous tool calls both pass the cooldown check and both execute
`incremental_save` on the same repo. Both read the old index, compute overlapping change sets,
and the last writer's `incremental_save` silently overwrites the other's work.

**Fix — Per-path non-blocking lock:**

```python
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_mutex = threading.Lock()

def _get_path_lock(path: str) -> threading.Lock:
    with _refresh_locks_mutex:
        if path not in _refresh_locks:
            _refresh_locks[path] = threading.Lock()
        return _refresh_locks[path]

def maybe_refresh(self, storage_path: Optional[str]):
    now = time.monotonic()
    with self._lock:
        paths = list(self._paths)

    for path in paths:
        with self._lock:
            last = self._last_refresh.get(path, 0.0)
            if now - last < self._cooldown:
                continue
            self._last_refresh[path] = now

        path_lock = _get_path_lock(path)
        if not path_lock.acquire(blocking=False):
            _log.debug("autorefresh: %s already refreshing, skipping", path)
            continue                        # another thread is refreshing this path

        try:
            result = index_folder(
                path=path,
                use_ai_summaries=False,
                storage_path=storage_path,
                incremental=True,
            )
            _log.debug("autorefresh: %s done — %s", path, result)
        except Exception as e:
            _log.warning("autorefresh: error on %s: %s", path, e)
        finally:
            path_lock.release()
```

**Edge cases:**
- Lock dict grows unboundedly if many paths are registered over a long session. Add a max
  size check and warn if `len(_refresh_locks) > 50`.
- `blocking=False` means the second thread's cooldown timestamp was already updated to `now`
  (meaning it won't retry for `cooldown` seconds). This is acceptable — the first thread's
  refresh will have completed by then.

---

### Issue 5 — Concurrent refresh race condition on refs

**Severity:** High
**File:** `src/jcodemunch_mcp/storage/index_store.py:542–546` (`merge_refs`)
**Root cause:** `merge_refs` does load→filter→append→write without any locking. Two concurrent
incremental saves both call `merge_refs` and the last write wins, dropping the first thread's
new refs.

**Fix — Atomic merge with file-level locking:**

Use `fcntl.flock` (Linux/macOS) for file-level locking around the read-modify-write cycle:

```python
import fcntl

def merge_refs(self, owner: str, name: str, new_refs: list[dict], removed_files: set[str]) -> None:
    refs_path = self._refs_path(owner, name)
    refs_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a separate lock file to avoid locking the data file during read
    lock_path = refs_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing = self.load_refs(owner, name) or []
            kept = [r for r in existing if r.get("caller_file") not in removed_files]
            self.save_refs(owner, name, kept + new_refs)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
```

For Windows compatibility, fall back to a threading lock (acceptable since multi-process
use is uncommon on Windows):

```python
import sys
if sys.platform == "win32":
    _refs_lock = threading.Lock()
    # use _refs_lock instead of fcntl
```

**Edge cases:**
- Lock file left behind on crash: `flock` is automatically released when the process exits,
  so stale lock files from a previous crash don't block future runs.
- Multiple MCP server instances: `flock` works across processes on the same filesystem.

---

## 2. Performance

### Issue 6 — No in-memory cache for `load_index`

**Severity:** High
**File:** `src/jcodemunch_mcp/storage/index_store.py:257` (`load_index`) — called from every
tool: `get_symbol`, `get_symbols`, `search_symbols`, `get_file_outline`, `get_file_tree`,
`get_repo_outline`, `search_text`, `find_references`, and also from `get_symbol_content`
**Root cause:** Every tool call reads and JSON-parses the full index from disk, even when
nothing has changed. For a 2000-symbol project the index JSON can be 1–3MB, parsed fresh on
every call.

**Fix — Process-level LRU cache with invalidation on write:**

```python
import functools
from threading import Lock as _Lock

_index_cache: dict[str, tuple[float, "CodeIndex"]] = {}  # path -> (mtime, index)
_cache_lock = _Lock()

class IndexStore:
    def load_index(self, owner: str, name: str) -> Optional[CodeIndex]:
        index_path = self._index_path(owner, name)
        if not index_path.exists():
            return None

        try:
            current_mtime = index_path.stat().st_mtime
        except OSError:
            return None

        cache_key = str(index_path)
        with _cache_lock:
            if cache_key in _index_cache:
                cached_mtime, cached_index = _index_cache[cache_key]
                if cached_mtime == current_mtime:
                    return cached_index

        # Cache miss or stale — parse from disk
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None

        index = CodeIndex(...)   # existing construction logic

        with _cache_lock:
            _index_cache[cache_key] = (current_mtime, index)

        return index

    def _invalidate_cache(self, owner: str, name: str):
        """Call after any write to the index."""
        cache_key = str(self._index_path(owner, name))
        with _cache_lock:
            _index_cache.pop(cache_key, None)
```

Call `_invalidate_cache` at the end of `save_index` and `incremental_save`.

**The mtime check is safe because** all writes use atomic `tmp.replace(final)`, which
updates the mtime atomically. A stale cache read can't see a partial write.

**Edge cases:**
- Multiple `IndexStore` instances in the same process (currently `get_file_tree` creates two):
  The module-level `_index_cache` is shared across all instances. ✓
- Multiple MCP server processes: Each process has its own cache. The mtime check ensures
  cross-process writes are picked up within one stat() call. ✓
- Memory growth: cap `_index_cache` at 20 entries with LRU eviction if needed.

---

### Issue 7 — `rglob` cannot prune directories early

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/index_folder.py:114` (`discover_local_files`)
**Root cause:** `folder_path.rglob("*")` visits every file in every subdirectory before
checking skip patterns or `.gitignore`. A project with a gitignored `node_modules/` or
`target/` still gets fully traversed before rejection.

**Fix — Switch to `os.walk` with in-place directory pruning:**

```python
for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
    dir_path = Path(dirpath)
    try:
        dir_rel = dir_path.relative_to(root).as_posix()
    except ValueError:
        dirnames.clear()
        continue

    # Prune directories in-place — os.walk won't descend into them
    dirnames[:] = [
        d for d in dirnames
        if not should_skip_file(f"{dir_rel}/{d}/".lstrip("./"))
        and not (gitignore_spec and gitignore_spec.match_file(f"{dir_rel}/{d}/"))
        and not (extra_spec and extra_spec.match_file(f"{dir_rel}/{d}/"))
    ]

    for filename in filenames:
        file_path = dir_path / filename
        # ... existing per-file checks ...
```

This mirrors the approach already used in `nmockdrunk-mcp`'s `discover_doc_files`.

**Edge cases:**
- `followlinks=True` with circular symlinks: `os.walk` with `followlinks=True` can loop.
  Add `topdown=True` (default) and detect visited inodes if `follow_symlinks=True` is set.
- `.gitignore` patterns that use `**` (double-star): `pathspec` handles this correctly.
- Directory named the same as a skip pattern substring (e.g. a dir called `no_vendor`
  doesn't match `vendor/` pattern): the trailing slash in the pattern prevents false matches.

---

### Issue 8 — `search_text` reads all files from disk on every call

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/search_text.py:68–87`
**Root cause:** For every `search_text` call, every source file in the index is read from the
content cache directory in full. No streaming, no early termination. For 200 files at 20KB
average = 4MB of disk reads per call.

**Fix — Two options depending on acceptable complexity:**

**Option A (simpler):** Use the in-memory index cache (Issue 6). The content cache files are
already on disk; the bottleneck is the disk reads. Add an optional content cache in the index
JSON: store file contents inline for small repos (< 500KB total). For larger repos, keep
reading from disk but add a file-level read cache using mtime.

**Option B (recommended):** Build a search index at index time using an inverted word index
stored alongside the main index. On `search_text`, look up the word index rather than scanning
files. This is a larger change but reduces search from O(files × content) to O(query_words).

For now, minimum viable fix: add a size guard and an early-termination option.

```python
MAX_SEARCH_BYTES = 50 * 1024 * 1024  # 50MB limit

bytes_read = 0
for file_path in files:
    if bytes_read > MAX_SEARCH_BYTES:
        response["warning"] = "Search stopped at 50MB — use file_pattern to narrow scope"
        break
    full_path = content_dir / file_path
    # ... existing read logic ...
    bytes_read += len(content)
```

Also add the `_safe_content_path` check (see Issue 22 fix below).

**Edge cases:**
- File deleted from content cache but still in index: `full_path.exists()` check already
  handles this (skips silently).
- Binary file somehow in content cache: `read_text(errors="replace")` handles it.

---

### Issue 9 — No watchlist size cap

**Severity:** Medium
**File:** `src/jcodemunch_mcp/server.py:65` (`register_path`)
**Root cause:** Every successful `index_folder` registers the path permanently for the session.
After indexing 50 projects, every tool call triggers 50 refresh cycles.

**Fix:**

```python
MAX_WATCHED_PATHS = 20

def register_path(self, path: str):
    ...
    with self._lock:
        if len(self._paths) >= MAX_WATCHED_PATHS:
            _log.warning(
                "autorefresh: watchlist full (%d paths). "
                "Add path to autorefresh.json manually to persist it.",
                MAX_WATCHED_PATHS,
            )
            return
        self._paths.add(resolved)
```

For the persisted `autorefresh.json` paths (Issue 1 fix), apply the same cap at load time
and warn when the config exceeds it.

---

### Issue 10 — `get_symbol` loads the full index twice

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/get_symbol.py:47` and
`src/jcodemunch_mcp/storage/index_store.py:219` (`get_symbol_content` calls `load_index` again)
**Root cause:** `get_symbol` calls `store.load_index()` to find the symbol dict, then calls
`store.get_symbol_content()` which calls `self.load_index()` again internally to get byte
offsets.

**Fix — Pass the already-loaded index to `get_symbol_content`:**

Add an overload to `get_symbol_content` that accepts a pre-loaded index:

```python
def get_symbol_content(
    self,
    owner: str,
    name: str,
    symbol_id: str,
    index: Optional[CodeIndex] = None,   # NEW — avoid double load
) -> Optional[str]:
    if index is None:
        index = self.load_index(owner, name)
    if not index:
        return None
    # ... rest of existing implementation ...
```

In `get_symbol.py`, pass the already-loaded `index` object:

```python
index = store.load_index(owner, name)
# ...
source = store.get_symbol_content(owner, name, symbol_id, index=index)
```

With the cache from Issue 6 this is already much cheaper (just a dict lookup), but
eliminating the second call entirely is still better.

---

### Issue 11 — `list_repos` triggers unnecessary auto-refresh

**Severity:** Medium
**File:** `src/jcodemunch_mcp/server.py:35,458` (`_INDEX_TOOLS` set, `call_tool`)
**Root cause:** `_INDEX_TOOLS = {"index_folder", "index_repo", "invalidate_cache"}`. Any tool
not in this set triggers `maybe_refresh`. `list_repos` just scans index JSON files — it never
needs a fresh index.

**Fix:** Add `list_repos` to the exclusion set, or rename the set to be a read-tool allowlist:

```python
_REFRESH_TOOLS = {
    "get_file_tree", "get_file_outline", "get_symbol", "get_symbols",
    "search_symbols", "search_text", "get_repo_outline",
    "find_references", "find_callers", "find_constructors",
    "find_field_reads", "find_field_writes",
}

# In call_tool:
if name in _REFRESH_TOOLS:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, auto_refresher.maybe_refresh, storage_path)
```

This is safer than the current exclusion approach — new tools added in future don't
accidentally trigger refresh unless explicitly listed.

---

### Issue 12 — `resolve_repo` calls `list_repos` (scans all JSON files) on every call

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/_utils.py:15–20`
**Root cause:** Bare repo names (without `/`) trigger `store.list_repos()` which globs
`*.json` in the base path and parses each one. Called on every tool invocation.

**Fix — Cache the name→(owner, name) mapping:**

```python
_repo_name_cache: dict[str, tuple[str, str]] = {}   # bare_name -> (owner, name)
_repo_cache_lock = threading.Lock()

def resolve_repo(repo: str, storage_path: Optional[str] = None) -> tuple[str, str]:
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

    result = tuple(matching[0]["repo"].split("/", 1))

    with _repo_cache_lock:
        _repo_name_cache[repo] = result

    return result
```

Invalidate cache entries in `invalidate_cache` tool and after `index_folder`/`index_repo`.

**Edge cases:**
- Two repos with the same bare name (see Issue 24): the cache would lock in the first match.
  This is the same behaviour as before, just faster.
- New repo indexed mid-session: cache is invalidated when `index_folder` succeeds, so the
  new name becomes resolvable.

---

### Issue 13 — `get_file_tree` creates a redundant second `IndexStore`

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/get_file_tree.py:57`
**Root cause:** `store2 = IndexStore(base_path=storage_path)` is created on line 57 purely
to call `_content_dir`. `store` from line 35 has the identical `base_path`.

**Fix:** Remove `store2`, use `store._content_dir(owner, name)` directly. One line change.

---

## 3. Correctness

### Issue 14 — Telemetry enabled by default without prominent opt-in notice

**Severity:** Medium
**File:** `src/jcodemunch_mcp/storage/token_tracker.py:74`
**Root cause:** `JCODEMUNCH_SHARE_SAVINGS` defaults to `"1"` (on). Every tool call that records
savings fires a background HTTP POST to `https://j.gravelle.us`. No notification in the
server startup, README summary, or tool responses.

**Fix:**
1. Add a notice to `README.md` at the top of the Usage section explaining telemetry and how
   to disable it.
2. Log a one-time startup notice:

```python
# In AutoRefresher.__init__ or server main():
if os.environ.get("JCODEMUNCH_SHARE_SAVINGS", "1") != "0":
    _log.info(
        "jcodemunch: anonymous token-savings telemetry is ON. "
        "Set JCODEMUNCH_SHARE_SAVINGS=0 to disable. "
        "See README for details."
    )
```

3. Consider flipping default to opt-in (`"0"`) to follow principle of least surprise.

---

### Issue 15 — `file_summaries` defined but never persisted

**Severity:** Medium
**File:** `src/jcodemunch_mcp/storage/index_store.py:52` (field), `index_store.py:570`
(`_index_to_dict` — field missing), `index_store.py:272` (`load_index` — field not loaded)
**Root cause:** `file_summaries: dict[str, str]` is a `CodeIndex` dataclass field but
`_index_to_dict` omits it and `load_index` never restores it. Anything written to it is
silently dropped on save.

**Fix — Add to serialization:**

```python
# _index_to_dict:
return {
    ...
    "file_hashes": index.file_hashes,
    "git_head": index.git_head,
    "file_summaries": index.file_summaries,   # ADD
}

# load_index:
return CodeIndex(
    ...
    git_head=data.get("git_head", ""),
    file_summaries=data.get("file_summaries", {}),   # ADD
)
```

Also populate `file_summaries` during indexing — derive one-line summaries per file from the
file's top-level symbols' summaries or docstrings.

---

### Issue 16 — `include_summaries=True` silently returns empty strings

**Severity:** Low (depends on Issue 15)
**File:** `src/jcodemunch_mcp/tools/get_file_tree.py:123`
**Root cause:** `index.file_summaries.get(file_path, "")` — since `file_summaries` is never
populated (Issue 15), this always returns `""` with no error or warning.

**Fix:** After Issue 15 is resolved, this works correctly. Additionally add a warning in the
response if `include_summaries=True` but all summaries are empty:

```python
if include_summaries and all(
    not n.get("summary") for n in _flatten_tree(tree)
):
    response["warning"] = (
        "include_summaries=True but no file summaries are available. "
        "Re-index with a recent version to generate them."
    )
```

---

### Issue 17 — `index_repo` incremental downloads all files anyway

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/index_repo.py:274`
**Root cause:** The incremental path fetches all files concurrently (line 274), then compares
hashes to find what changed. For a 200-file remote repo, "incremental" still downloads all
200 files — identical bandwidth to a full re-index, with extra hash-comparison overhead.

**Fix — Use the GitHub commit SHA to avoid re-downloading:**

GitHub's git trees API includes the blob SHA for each file. Store these SHAs at index time
alongside file hashes. On incremental re-index, fetch the tree again (one API call), compare
blob SHAs, and only download files whose SHA changed:

```python
# At index time, store blob SHAs:
file_hashes[path] = {
    "sha256": hashlib.sha256(content.encode()).hexdigest(),
    "github_blob_sha": entry["sha"],  # from tree entry
}

# On incremental:
old_blob_shas = {p: meta.get("github_blob_sha") for p, meta in old_hashes.items()}
changed_paths = [
    entry["path"] for entry in new_tree
    if entry["path"] in old_blob_shas
    and entry["sha"] != old_blob_shas[entry["path"]]
]
# Only download changed_paths + added_paths
```

This reduces incremental re-index of a 200-file repo with 3 changed files to 3 API calls
instead of 200.

---

### Issue 18 — `index_repo` no retry on file fetch failure

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/index_repo.py:266–272` (`fetch_with_limit`)
**Root cause:** Any exception returns `path, ""` silently. Rate limits, transient network
errors, and timeouts all result in silently missing files with no warning.

**Fix — Exponential backoff with limited retries:**

```python
async def fetch_with_limit(path: str) -> tuple[str, Optional[str]]:
    async with semaphore:
        for attempt in range(3):
            try:
                content = await fetch_file_content(owner, repo, path, github_token)
                return path, content
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    # Rate limited — wait longer
                    await asyncio.sleep(2 ** attempt * 2)
                elif e.response.status_code == 404:
                    return path, None   # Genuinely missing
                else:
                    await asyncio.sleep(2 ** attempt)
            except Exception:
                await asyncio.sleep(2 ** attempt)
        warnings.append(f"Failed to fetch {path} after 3 attempts")
        return path, None
```

Also distinguish `None` (failed) from `""` (empty file) in the content processing:

```python
for path, content in file_contents:
    if content is not None:
        current_files[path] = content
```

---

### Issue 19 — `index_repo` uses Contents API (60 req/hr unauthenticated)

**Severity:** Medium
**File:** `src/jcodemunch_mcp/tools/index_repo.py:184` (`fetch_file_content`)
**Root cause:** `/repos/{owner}/{repo}/contents/{path}` is rate-limited to 60 requests/hr
without a token. A 200-file repo will hit this limit unauthenticated.

**Fix — Use raw.githubusercontent.com or Git Blobs API:**

```python
async def fetch_file_content(owner, repo, path, ref="HEAD", token=None):
    # Use raw download — no rate limit beyond general GitHub limits
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()
        return response.text
```

`raw.githubusercontent.com` has a much higher rate limit and doesn't count against the API
quota. For private repos, the token still provides access.

**Edge cases:**
- Private repos without token: 404. Existing error handling catches this.
- Files with spaces or special chars in paths: URL-encode the path component.

---

### Issue 20 — Ambiguous bare repo name silently picks first match

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/_utils.py:18`
**Root cause:** `matching[0]["repo"]` — if `local/utils` and `external/utils` are both indexed,
the first match wins silently.

**Fix:**

```python
if len(matching) > 1:
    candidates = [r["repo"] for r in matching]
    raise ValueError(
        f"Ambiguous repo name '{repo}'. Multiple matches: {candidates}. "
        f"Use the full 'owner/repo' form."
    )
return matching[0]["repo"].split("/", 1)
```

---

## 4. Security & Privacy

### Issue 21 — `search_text` missing `_safe_content_path` traversal check

**Severity:** Low-Medium
**File:** `src/jcodemunch_mcp/tools/search_text.py:69`
**Root cause:** `full_path = content_dir / file_path` with no validation. `file_path` comes
from `index.source_files` which should be safe, but there's no defence-in-depth check.
Every other raw file read (`get_symbol_content`, `incremental_save`) uses `_safe_content_path`.

**Fix — Apply the same guard:**

```python
for file_path in files:
    full_path = store._safe_content_path(content_dir, file_path)
    if not full_path or not full_path.exists():
        continue
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
```

---

### Issue 22 — `record_savings` non-atomic write and thread race

**Severity:** Low
**File:** `src/jcodemunch_mcp/storage/token_tracker.py:62–82`
**Root cause:** `path.write_text(json.dumps(data))` is a direct write (no temp file). Two
concurrent tool calls read the same total, add their delta, and one overwrites the other.
Also, a crash mid-write corrupts `_savings.json`.

**Fix — Atomic write + per-file lock:**

```python
_savings_lock = threading.Lock()

def record_savings(tokens_saved: int, base_path: Optional[str] = None) -> int:
    path = _savings_path(base_path)
    with _savings_lock:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            data = {}

        delta = max(0, tokens_saved)
        total = data.get("total_tokens_saved", 0) + delta
        data["total_tokens_saved"] = total

        # Atomic write
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)

    if delta > 0 and os.environ.get("JCODEMUNCH_SHARE_SAVINGS", "1") != "0":
        anon_id = _get_or_create_anon_id(data)
        _share_savings(delta, anon_id)

    return total
```

---

## 5. Code Quality & Cleanup

### Issue 23 — `search_symbols` scores every result twice

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/search_symbols.py:55,77`
**Root cause:** `index.search()` already scores and sorts all symbols via `_score_symbol`.
Then `search_symbols` calls `_calculate_score()` again on the paginated subset. Both functions
are identical implementations.

**Fix:** Remove `_calculate_score` from `search_symbols.py` entirely. Use the score already
embedded during `index.search()` — modify `CodeIndex.search` to include the score in the
returned dicts:

```python
# In CodeIndex.search:
scored.sort(key=lambda x: x[0], reverse=True)
return [{"score": score, **sym} for score, sym in scored[:max_results]]
```

Then in `search_symbols.py`, use the returned `score` field directly.

---

### Issue 24 — Language filter runs after scoring all symbols

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/search_symbols.py:55–59`,
`src/jcodemunch_mcp/storage/index_store.py:61` (`CodeIndex.search`)
**Root cause:** `index.search()` scores all symbols, returns sorted results, then language
filter is applied in `search_symbols`. All non-matching languages are scored unnecessarily.

**Fix:** Pass language as a filter into `CodeIndex.search`:

```python
def search(self, query, kind=None, file_pattern=None, language=None) -> list[dict]:
    for sym in self.symbols:
        if kind and sym.get("kind") != kind:
            continue
        if file_pattern and not self._match_pattern(sym.get("file", ""), file_pattern):
            continue
        if language and sym.get("language") != language:   # ADD
            continue
        score = self._score_symbol(sym, query_lower, query_words)
        ...
```

Remove the post-search language filter from `search_symbols.py`.

---

### Issue 25 — `get_symbols` O(n²) in token savings calculation

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/get_symbol.py:181–192`
**Root cause:** After building the `symbols` list (line 154–175), the token savings loop on
line 181 calls `index.get_symbol(symbol_id)` again for each ID — another O(n) scan. The
symbol objects were already retrieved in the first loop.

**Fix:** Collect symbol objects during the first loop and reuse:

```python
retrieved: dict[str, dict] = {}   # symbol_id -> symbol dict

for symbol_id in symbol_ids:
    symbol = index.get_symbol(symbol_id)
    if not symbol:
        errors.append(...)
        continue
    retrieved[symbol_id] = symbol
    source = store.get_symbol_content(owner, name, symbol_id, index=index)
    symbols.append({...})

# Token savings — use retrieved dict, no second scan
for symbol_id, symbol in retrieved.items():
    ...
```

---

### Issue 26 — `_dict_to_symbol` unnecessary round-trip in `get_file_outline`

**Severity:** Low
**File:** `src/jcodemunch_mcp/tools/get_file_outline.py:54–55`
**Root cause:** `_dict_to_symbol` converts stored dicts back to `Symbol` dataclass objects
just to pass to `build_symbol_tree`, which then converts them back to dicts for the response.

**Fix:** Modify `build_symbol_tree` (or create a dict-native variant) to operate directly on
symbol dicts, removing the roundtrip:

```python
def build_symbol_tree_from_dicts(symbol_dicts: list[dict]) -> list:
    """Build hierarchy from raw symbol dicts without Symbol conversion."""
    # Adapt existing hierarchy logic to use dict keys
    ...
```

If `build_symbol_tree` is also used elsewhere with `Symbol` objects, add an overload or a
small adapter that checks the input type.

---

### Issue 27 — `should_exclude_file` composite function defined but unused

**Severity:** Low
**File:** `src/jcodemunch_mcp/security.py:243`
**Root cause:** `should_exclude_file` runs all security checks and returns a reason string.
`discover_local_files` does its own manual checks in sequence, duplicating the logic.

**Fix:** Either:
a) Replace the manual checks in `discover_local_files` with `should_exclude_file` calls
   (simpler, single source of truth):

```python
reason = should_exclude_file(
    file_path, root,
    max_file_size=max_size,
    check_binary=True,
    check_secrets=True,
    check_symlinks=not follow_symlinks,
)
if reason:
    skip_counts[reason] = skip_counts.get(reason, 0) + 1
    continue
```

b) Delete `should_exclude_file` if the manual approach is preferred. Don't leave dead code.

---

### Issue 28 — Path normalisation inconsistency in `AutoRefresher`

**Severity:** Low
**File:** `src/jcodemunch_mcp/server.py:57–63` (`_load_config`), `server.py:65–68`
(`register_path`)
**Root cause:** `_load_config` applies `os.path.expanduser` but not `os.path.realpath`.
The same physical path registered as `~/project` (from config) and `/home/user/project`
(from `register_path` after `index_folder` resolves it) ends up twice in `_paths`,
causing double refresh.

**Fix:** Apply `os.path.realpath(os.path.expanduser(...))` consistently in both places.
This is already part of the Issue 1 fix above — include it there.

---

### Issue 29 — `autorefresh.json` not hot-reloaded at runtime

**Severity:** Low
**File:** `src/jcodemunch_mcp/server.py:52` (`_load_config`)
**Root cause:** Config read once at `AutoRefresher.__init__`. Manual edits to
`autorefresh.json` require a server restart to take effect.

**Fix:** Re-read config on each `maybe_refresh` call if the file's mtime changed:

```python
def _maybe_reload_config(self):
    try:
        cfg_mtime = Path(self.CONFIG_PATH).stat().st_mtime
        if cfg_mtime != getattr(self, "_cfg_mtime", None):
            self._cfg_mtime = cfg_mtime
            self._load_config()
    except OSError:
        pass

def maybe_refresh(self, storage_path):
    self._maybe_reload_config()
    # ... rest of existing logic ...
```

---

## 6. Implementation Order

Dependencies flow top-down. Do not skip ahead.

```
Phase 1 — Foundation (no external dependencies)
  1.  Issue 1  — Path persistence (register_path writes autorefresh.json)
  2.  Issue 28 — Path normalisation (fix at same time as Issue 1)
  3.  Issue 11 — list_repos excluded from refresh trigger
  4.  Issue 13 — Remove duplicate IndexStore in get_file_tree

Phase 2 — Performance core (each is independent)
  5.  Issue 6  — In-memory index cache (load_index mtime-gated)
  6.  Issue 10 — get_symbol double index load (depends on Issue 6 API change)
  7.  Issue 7  — Switch rglob to os.walk with directory pruning
  8.  Issue 9  — Watchlist size cap

Phase 3 — Change detection overhaul (sequential dependencies)
  9.  Issue 2  — mtime+size metadata in file_hashes (schema change)
  10. Issue 3  — Git-accelerated detection (depends on Issue 2 schema)
  11. Issue 4  — Per-path refresh lock (concurrent safety)
  12. Issue 5  — merge_refs locking (concurrent safety)
  13. Issue 29 — Config hot-reload (small addition to maybe_refresh)

Phase 4 — Correctness fixes (independent)
  14. Issue 15 — file_summaries serialization
  15. Issue 16 — include_summaries warning (depends on Issue 15)
  16. Issue 17 — index_repo incremental with blob SHAs
  17. Issue 18 — index_repo retry with backoff
  18. Issue 19 — Use raw.githubusercontent.com instead of Contents API
  19. Issue 20 — Ambiguous bare repo name error

Phase 5 — Search quality (independent)
  20. Issue 23 — Remove double scoring in search_symbols
  21. Issue 24 — Move language filter into CodeIndex.search
  22. Issue 25 — get_symbols O(n²) fix
  23. Issue 26 — Remove _dict_to_symbol roundtrip

Phase 6 — Security & privacy
  24. Issue 21 — _safe_content_path in search_text
  25. Issue 22 — record_savings atomic write + lock
  26. Issue 14 — Telemetry opt-in notice

Phase 7 — Cleanup
  27. Issue 8  — search_text size guard (quick guard; full inverted index is future work)
  28. Issue 12 — resolve_repo cache
  29. Issue 27 — Unify should_exclude_file or delete it
```

---

## 7. Testing Plan

### Unit tests (new or modified)

**Phase 1:**
- `test_register_path_persists`: call `register_path`, restart `AutoRefresher`, verify path
  present in `_paths`.
- `test_register_path_normalises`: register `~/project` and `/home/user/project` — verify
  only one entry in `_paths`.
- `test_list_repos_no_refresh`: verify `maybe_refresh` not called when `list_repos` is invoked.

**Phase 2:**
- `test_load_index_cached`: call `load_index` twice on same file, verify JSON parsed only once.
- `test_cache_invalidated_on_write`: save_index then load_index, verify fresh parse.
- `test_os_walk_prunes_node_modules`: create fixture with `node_modules/` subtree, verify none
  of its contents appear in discovered files.
- `test_watchlist_cap`: register MAX+1 paths, verify warning logged and cap enforced.

**Phase 3:**
- `test_mtime_no_change`: unchanged file → not in changed set.
- `test_mtime_changed`: bump mtime → file in changed set → SHA-256 Phase 2 confirms change.
- `test_mtime_same_size_different_content`: same mtime and size, different content → SHA-256
  still catches it.
- `test_git_committed_change`: mock `git diff --name-only` output → correct files detected.
- `test_git_uncommitted_change`: mock `git status --porcelain` → modified/added/deleted correct.
- `test_git_rename`: R status → old in deleted, new in modified.
- `test_git_fallback_non_repo`: non-git directory → mtime detection used.
- `test_git_fallback_timeout`: `git status` times out → mtime fallback.
- `test_concurrent_refresh_no_race`: two threads hit refresh simultaneously → second skips,
  index not corrupted.
- `test_merge_refs_concurrent`: two threads call `merge_refs` simultaneously → all refs
  present in final result.

**Phase 4:**
- `test_file_summaries_persisted`: set `file_summaries`, save, load, verify present.
- `test_index_repo_incremental_blob_sha`: mock tree API with matching SHAs → no files fetched.
- `test_index_repo_retry`: first fetch fails with 429, second succeeds → file indexed.
- `test_ambiguous_repo_name_error`: two repos share a bare name → ValueError with candidates.

**Phase 5:**
- `test_search_symbols_no_double_score`: verify `_calculate_score` is called exactly once
  per result.
- `test_language_filter_pre_search`: mock `index.search` to assert language arg passed.
- `test_get_symbols_no_redundant_scan`: mock `index.get_symbol` and verify call count equals
  `len(symbol_ids)`, not `2 * len(symbol_ids)`.

**Phase 6:**
- `test_search_text_safe_path`: crafted index with path traversal entry → `_safe_content_path`
  rejects it.
- `test_record_savings_concurrent`: 50 threads all call `record_savings(1)` → final total
  equals 50.
- `test_record_savings_atomic`: mock crash mid-write → old file intact.

### Integration tests

- `test_full_workflow_new_file`: index folder, create new file, run `search_symbols` →
  new file's symbols appear (auto-refresh picked up untracked file).
- `test_full_workflow_modified_file`: index, modify a function signature, run `get_symbol` →
  new signature returned.
- `test_full_workflow_deleted_file`: index, delete file, run `get_file_tree` → deleted file
  absent.
- `test_full_workflow_git_branch_switch`: index on branch A, switch to branch B (mocked HEAD
  change), run tool → branch B's files indexed.
- `test_full_workflow_server_restart`: index folder, restart `AutoRefresher`, make change,
  run tool → change detected (path was persisted).
- `test_full_workflow_github_incremental`: mock GitHub API, index repo, change one file's
  blob SHA, re-index → only that file re-fetched.
