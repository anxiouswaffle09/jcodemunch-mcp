"""Additional regression tests for storage and discovery edge cases."""

import json
import os
import shutil
import threading
import unittest.mock as mock
from pathlib import Path

import pytest

from jcodemunch_mcp.parser import Symbol
from jcodemunch_mcp.server import AutoRefresher
from jcodemunch_mcp.storage import IndexStore
from jcodemunch_mcp.tools.index_folder import discover_local_files


def _make_symbol(name: str, file_path: str = "main.py") -> Symbol:
    return Symbol(
        id=f"{file_path}::{name}#function",
        file=file_path,
        name=name,
        qualified_name=name,
        kind="function",
        language="python",
        signature=f"def {name}():",
        byte_offset=0,
        byte_length=20,
    )


def test_detect_changes_fast_same_size_preserved_mtime_is_still_detected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    store = IndexStore(base_path=str(tmp_path / "store"))
    main_py = src / "main.py"
    main_py.write_text("def foo():\n    pass\n", encoding="utf-8")

    store.save_index(
        owner="local",
        name="src",
        source_files=["main.py"],
        symbols=[_make_symbol("foo")],
        raw_files={"main.py": main_py.read_text(encoding="utf-8")},
        languages={"python": 1},
        folder_path=src,
    )

    original = main_py.stat()
    main_py.write_text("def bar():\n    pass\n", encoding="utf-8")
    os.utime(main_py, ns=(original.st_atime_ns, original.st_mtime_ns))

    changed, added, deleted = store.detect_changes_fast(
        "local",
        "src",
        src,
        [main_py],
        source_path=None,
    )

    assert "main.py" in changed
    assert added == []
    assert deleted == []


def test_detect_changes_fast_non_git_unchanged_avoids_hashing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    store = IndexStore(base_path=str(tmp_path / "store"))
    main_py = src / "main.py"
    content = "def foo():\n    pass\n"
    main_py.write_text(content, encoding="utf-8")

    store.save_index(
        owner="local",
        name="src",
        source_files=["main.py"],
        symbols=[_make_symbol("foo")],
        raw_files={"main.py": content},
        languages={"python": 1},
        folder_path=src,
    )

    with mock.patch.object(
        Path,
        "read_text",
        autospec=True,
        side_effect=AssertionError("detect_changes_fast should not hash unchanged files"),
    ):
        changed, added, deleted = store.detect_changes_fast(
            "local",
            "src",
            src,
            [main_py],
            source_path=None,
        )

    assert changed == []
    assert added == []
    assert deleted == []


def test_save_index_invalid_raw_path_does_not_publish_index(tmp_path):
    store = IndexStore(base_path=str(tmp_path))

    with pytest.raises(ValueError, match="Unsafe file path"):
        store.save_index(
            owner="owner",
            name="repo",
            source_files=["main.py"],
            symbols=[_make_symbol("foo")],
            raw_files={"../evil.py": "oops"},
            languages={"python": 1},
        )

    assert store.load_index("owner", "repo") is None


def test_incremental_save_rolls_back_raw_files_on_copy_failure(tmp_path):
    store = IndexStore(base_path=str(tmp_path / "store"))
    store.save_index(
        owner="owner",
        name="repo",
        source_files=["main.py"],
        symbols=[_make_symbol("foo")],
        raw_files={"main.py": "def foo():\n    pass\n"},
        languages={"python": 1},
    )

    dest = store._content_dir("owner", "repo") / "main.py"
    original_copyfile = shutil.copyfile

    def flaky_copyfile(src, dst, *args, **kwargs):
        if Path(dst) == dest:
            raise OSError("disk full")
        return original_copyfile(src, dst, *args, **kwargs)

    with mock.patch("shutil.copyfile", side_effect=flaky_copyfile):
        with pytest.raises(OSError, match="disk full"):
            store.incremental_save(
                owner="owner",
                name="repo",
                changed_files=["main.py"],
                new_files=[],
                deleted_files=[],
                new_symbols=[_make_symbol("bar")],
                raw_files={"main.py": "def bar():\n    pass\n"},
                languages={},
            )

    loaded = store.load_index("owner", "repo")
    assert loaded is not None
    assert [sym["name"] for sym in loaded.symbols] == ["foo"]
    assert dest.read_text(encoding="utf-8") == "def foo():\n    pass\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported on this platform")
def test_discover_local_files_follow_symlinks_avoids_loops(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "f.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "a" / "loop").symlink_to(tmp_path, target_is_directory=True)

    files, _warnings, skip_counts = discover_local_files(
        tmp_path,
        max_files=20,
        follow_symlinks=True,
    )

    rel_paths = [p.relative_to(tmp_path).as_posix() for p in files]
    assert rel_paths == ["f.py"]
    assert skip_counts["file_limit"] == 0


def test_autorefresher_reload_replaces_removed_paths(tmp_path):
    config_path = tmp_path / "autorefresh.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_path.write_text(json.dumps({"paths": [str(first)]}), encoding="utf-8")

    refresher = AutoRefresher.__new__(AutoRefresher)
    refresher._lock = threading.Lock()
    refresher._last_refresh = {}
    refresher._paths = set()
    refresher._cooldown = 0.0
    refresher._cfg_mtime = None
    refresher.CONFIG_PATH = str(config_path)
    refresher._load_config()

    config_path.write_text(json.dumps({"paths": [str(second)]}), encoding="utf-8")
    refresher._cfg_mtime = None
    refresher._maybe_reload_config()

    assert refresher._paths == {str(second.resolve())}


def test_autorefresher_readded_path_is_not_blocked_by_stale_cooldown(tmp_path):
    config_path = tmp_path / "autorefresh.json"
    watched = tmp_path / "watched"
    watched.mkdir()
    watched_resolved = str(watched.resolve())
    config_path.write_text(
        json.dumps({"paths": [str(watched)], "cooldown_secs": 60}),
        encoding="utf-8",
    )

    refresher = AutoRefresher.__new__(AutoRefresher)
    refresher._lock = threading.Lock()
    refresher._last_refresh = {}
    refresher._paths = set()
    refresher._cooldown = 0.0
    refresher._cfg_mtime = None
    refresher.CONFIG_PATH = str(config_path)
    refresher._load_config()
    refresher._last_refresh[watched_resolved] = 123.0

    config_path.write_text(json.dumps({"paths": [], "cooldown_secs": 60}), encoding="utf-8")
    refresher._cfg_mtime = None
    refresher._maybe_reload_config()

    config_path.write_text(
        json.dumps({"paths": [str(watched)], "cooldown_secs": 60}),
        encoding="utf-8",
    )
    refresher._cfg_mtime = None
    refresher._maybe_reload_config()

    with mock.patch("jcodemunch_mcp.server.index_folder") as mock_index_folder:
        mock_index_folder.return_value = {"changed": 0, "new": 0, "deleted": 0}
        refresher.maybe_refresh(storage_path=None)

    mock_index_folder.assert_called_once_with(
        path=watched_resolved,
        use_ai_summaries=False,
        storage_path=None,
        incremental=True,
    )
