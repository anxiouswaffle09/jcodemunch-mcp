"""Regression tests for cross-reference indexing and query tools."""

from pathlib import Path

import pytest

from jcodemunch_mcp.server import list_tools
from jcodemunch_mcp.storage import IndexStore
from jcodemunch_mcp.tools.find_references import find_callers, find_references, find_field_reads, find_field_writes
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.index_repo import index_repo


def _write_file(root: Path, relative_path: str, content: str) -> Path:
    """Write a source file under the test repo root."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_full_index_folder_extracts_refs_from_no_symbol_files(tmp_path):
    """Full indexing should preserve refs from files that declare no symbols."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"

    _write_file(src, "helper.py", "def helper():\n    return 1\n")
    _write_file(src, "runner.py", "from helper import helper\n\nhelper()\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))

    assert result["success"] is True
    assert result["ref_count"] >= 1

    refs = IndexStore(base_path=str(store)).load_refs("local", "src") or []
    assert any(r["callee"] == "helper" and r["caller_file"] == "runner.py" for r in refs)

    callers = find_callers("local/src", "helper", storage_path=str(store))
    assert callers["total_refs"] == 1
    assert callers["refs"][0]["caller_file"] == "runner.py"
    assert callers["refs"][0]["caller_symbol_id"] is None


def test_find_callers_warns_for_unsupported_languages_without_false_unwired_claim(tmp_path):
    """Unsupported-language repos should report coverage limits without fake wiring conclusions."""
    src = tmp_path / "cpp_repo"
    src.mkdir()
    store = tmp_path / "store"

    _write_file(
        src,
        "main.cpp",
        "int add(int a, int b) { return a + b; }\nint main() { return add(1, 2); }\n",
    )

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True

    callers = find_callers("local/cpp_repo", "add", storage_path=str(store))
    warning_blob = "\n".join(callers.get("warnings", [callers.get("warning", "")]))

    assert callers["total_refs"] == 0
    assert "supports only python, rust" in warning_blob
    assert "no recorded references" not in warning_blob
    assert "dynamic dispatch" not in warning_blob


def test_find_callers_withholds_ambiguous_short_names(tmp_path):
    """Short-name collisions should return candidates instead of conflated counts."""
    src = tmp_path / "ambiguous_repo"
    src.mkdir()
    store = tmp_path / "store"

    _write_file(
        src,
        "shapes.py",
        "class Header:\n"
        "    def render(self):\n"
        "        return 'header'\n\n"
        "class Footer:\n"
        "    def render(self):\n"
        "        return 'footer'\n\n"
        "def use(header, footer):\n"
        "    header.render()\n"
        "    footer.render()\n",
    )

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True

    callers = find_callers("local/ambiguous_repo", "render", storage_path=str(store))

    assert callers["total_refs"] == 0
    assert callers["refs"] == []
    assert len(callers["candidates"]) == 2
    assert "ambiguous" in callers["warning"]


@pytest.mark.asyncio
async def test_index_repo_builds_refs_for_github_indexes(tmp_path, monkeypatch):
    """GitHub indexing should persist refs so the query tools work immediately."""
    store = tmp_path / "store"
    contents = {
        "src/helper.py": "def helper():\n    return 1\n",
        "src/runner.py": "from helper import helper\n\nhelper()\n",
    }

    async def fake_fetch_repo_tree(owner: str, repo: str, token=None):
        return [
            {"path": "src/helper.py", "type": "blob", "size": len(contents["src/helper.py"])},
            {"path": "src/runner.py", "type": "blob", "size": len(contents["src/runner.py"])},
        ]

    async def fake_fetch_gitignore(owner: str, repo: str, token=None):
        return None

    async def fake_fetch_file_content(owner: str, repo: str, path: str, token=None):
        return contents[path]

    monkeypatch.setattr(
        "jcodemunch_mcp.tools.index_repo.fetch_repo_tree",
        fake_fetch_repo_tree,
    )
    monkeypatch.setattr(
        "jcodemunch_mcp.tools.index_repo.fetch_gitignore",
        fake_fetch_gitignore,
    )
    monkeypatch.setattr(
        "jcodemunch_mcp.tools.index_repo.fetch_file_content",
        fake_fetch_file_content,
    )

    result = await index_repo(
        "octocat/demo",
        use_ai_summaries=False,
        storage_path=str(store),
    )

    assert result["success"] is True
    assert result["ref_count"] >= 1

    callers = find_callers("octocat/demo", "helper", storage_path=str(store))
    assert callers["total_refs"] == 1
    assert callers["refs"][0]["caller_file"] == "src/runner.py"


@pytest.mark.asyncio
async def test_find_tool_descriptions_note_xref_scope():
    """Tool metadata should advertise current xref coverage limits."""
    tools = await list_tools()
    descriptions = {
        tool.name: tool.description
        for tool in tools
        if tool.name.startswith("find_")
    }

    assert "Rust and Python" in descriptions["find_references"]
    assert "Rust and Python" in descriptions["find_callers"]
    assert "Rust and Python" in descriptions["find_constructors"]
    assert "Rust and Python" in descriptions["find_field_reads"]
    assert "Rust and Python" in descriptions["find_field_writes"]


# ── Fix 1 regressions: incremental backfill when refs.json absent ──────────


def test_incremental_backfill_folder_rebuilds_all_refs(tmp_path):
    """After deleting refs.json, the next incremental index must backfill all files."""
    src = tmp_path / "src"
    src.mkdir()
    store_path = str(tmp_path / "store")

    _write_file(src, "helper.py", "def helper():\n    return 1\n")
    _write_file(src, "caller.py", "import helper\n\nhelper()\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    # Simulate a pre-refs index by deleting the refs file
    store = IndexStore(base_path=store_path)
    store._refs_path("local", "src").unlink()
    assert store.load_refs("local", "src") is None

    # Incremental re-index — only helper.py changes
    _write_file(src, "helper.py", "def helper():\n    return 42\n")
    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path, incremental=True)
    assert result["success"] is True
    assert result.get("incremental") is True

    # caller.py was unchanged but its refs must be present after backfill
    callers = find_callers("local/src", "helper", storage_path=store_path)
    assert callers["total_refs"] >= 1


@pytest.mark.asyncio
async def test_incremental_backfill_repo_rebuilds_all_refs(tmp_path, monkeypatch):
    """index_repo incremental backfill: refs for unchanged files are preserved."""
    store_path = str(tmp_path / "store")
    contents = {
        "helper.py": "def helper():\n    return 1\n",
        "caller.py": "import helper\n\nhelper()\n",
    }
    version = {"v": 1}
    updated_contents = {**contents, "helper.py": "def helper():\n    return 42\n"}

    async def fake_fetch_repo_tree(owner, repo, token=None):
        import hashlib
        active = contents if version["v"] == 1 else updated_contents
        return [
            {"path": p, "type": "blob", "size": len(v),
             "sha": hashlib.sha1(v.encode()).hexdigest()}
            for p, v in active.items()
        ]

    async def fake_fetch_gitignore(owner, repo, token=None):
        return None

    async def fake_fetch_file_content(owner, repo, path, token=None):
        active = contents if version["v"] == 1 else updated_contents
        return active[path]

    monkeypatch.setattr("jcodemunch_mcp.tools.index_repo.fetch_repo_tree", fake_fetch_repo_tree)
    monkeypatch.setattr("jcodemunch_mcp.tools.index_repo.fetch_gitignore", fake_fetch_gitignore)
    monkeypatch.setattr("jcodemunch_mcp.tools.index_repo.fetch_file_content", fake_fetch_file_content)

    result = await index_repo("octocat/backfill", use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    store = IndexStore(base_path=store_path)
    store._refs_path("octocat", "backfill").unlink()
    assert store.load_refs("octocat", "backfill") is None

    version["v"] = 2
    result = await index_repo(
        "octocat/backfill", use_ai_summaries=False, storage_path=store_path, incremental=True
    )
    assert result["success"] is True

    callers = find_callers("octocat/backfill", "helper", storage_path=store_path)
    assert callers["total_refs"] >= 1


# ── Fix 2A regression: constants show "unreferenced" not "not declared" ────


def test_constant_with_no_refs_warns_unreferenced_not_undeclared(tmp_path):
    """A declared constant with 0 refs should say 'unreferenced', not 'not declared'."""
    src = tmp_path / "src"
    src.mkdir()
    store_path = str(tmp_path / "store")

    _write_file(src, "config.py", "MAX_RETRIES = 3\n\ndef dummy():\n    pass\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    refs = find_references("local/src", "MAX_RETRIES", storage_path=store_path)
    warning = refs.get("warning", "")
    assert refs["total_refs"] == 0
    assert "not declared" not in warning
    assert "unreferenced" in warning


# ── Fix 2B regression: field-only queries suppress "not declared" warning ──


def test_field_query_suppresses_not_declared_warning(tmp_path):
    """find_field_reads for a name with no reads should not warn 'not declared'."""
    src = tmp_path / "src"
    src.mkdir()
    store_path = str(tmp_path / "store")

    # 'value' is only ever written, never read
    _write_file(src, "model.py", "class Config:\n    def set(self, obj):\n        obj.value = 42\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    refs = find_field_reads("local/src", "value", storage_path=store_path)
    warning = refs.get("warning", "")
    assert "not declared" not in warning


# ── Fix 3 regression: method call must not emit spurious field_read ─────────


def test_method_call_not_emitted_as_field_read(tmp_path):
    """obj.helper() should register as a call, not as a field_read of 'helper'."""
    src = tmp_path / "src"
    src.mkdir()
    store_path = str(tmp_path / "store")

    _write_file(
        src,
        "main.py",
        "class Obj:\n    def helper(self):\n        pass\n\ndef run(obj):\n    obj.helper()\n",
    )

    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    refs = find_field_reads("local/src", "helper", storage_path=store_path)
    assert refs["total_refs"] == 0


# ── Fix 4 regression: augmented assignment is classified as field_write ─────


def test_augmented_assignment_classified_as_field_write(tmp_path):
    """obj.count += 1 should be recorded as a field_write, not a field_read."""
    src = tmp_path / "src"
    src.mkdir()
    store_path = str(tmp_path / "store")

    _write_file(src, "counter.py", "class Counter:\n    def inc(self, obj):\n        obj.count += 1\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=store_path)
    assert result["success"] is True

    writes = find_field_writes("local/src", "count", storage_path=store_path)
    assert writes["total_refs"] == 1
