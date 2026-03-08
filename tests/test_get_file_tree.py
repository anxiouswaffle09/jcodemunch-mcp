"""Tests for get_file_tree compact text format output."""

from jcodemunch_mcp.parser import Symbol
from jcodemunch_mcp.storage import IndexStore
from jcodemunch_mcp.tools.get_file_tree import get_file_tree


def _make_symbol(file: str, name: str, kind: str = "function",
                 language: str = "python", offset: int = 0) -> Symbol:
    """Helper to create a Symbol with minimal boilerplate."""
    return Symbol(
        id=f"{file}::{name}#{kind}",
        file=file,
        name=name,
        qualified_name=name,
        kind=kind,
        language=language,
        signature=f"def {name}()" if kind == "function" else name,
        byte_offset=offset,
        byte_length=20,
    )


def test_compact_output_contains_symbol_counts(tmp_path):
    """Output should be compact text with symbol counts in parentheses."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [
        _make_symbol("src/server.py", "handle_request"),
        _make_symbol("src/server.py", "start_server"),
        _make_symbol("src/utils.py", "log_message"),
    ]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["src/server.py", "src/utils.py"],
        symbols=syms,
        raw_files={
            "src/server.py": "def handle_request(): ...\ndef start_server(): ...\n",
            "src/utils.py": "def log_message(): ...\n",
        },
        languages={"python": 2},
    )

    result = get_file_tree("test_owner/test_repo", storage_path=str(tmp_path))
    assert "error" not in result
    tree = result["tree"]

    # Core assertion: tree is compact text, not a nested list
    assert isinstance(tree, str), f"Expected str, got {type(tree).__name__}"

    # Symbol counts in parentheses format
    assert "(2)" in tree
    assert "(1)" in tree

    # File names appear
    assert "server.py" in tree
    assert "utils.py" in tree

    # Monolingual repo: language labels should be suppressed
    assert "python" not in tree


def test_compact_output_hides_empty_files_by_default(tmp_path):
    """Files with zero symbols should be hidden when show_empty=False (default)."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [_make_symbol("src/main.py", "main")]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["src/main.py", "src/empty.py"],
        symbols=syms,
        raw_files={
            "src/main.py": "def main(): ...\n",
            "src/empty.py": "# nothing here\n",
        },
        languages={"python": 2},
    )

    result = get_file_tree("test_owner/test_repo", storage_path=str(tmp_path))
    assert "error" not in result
    tree = result["tree"]
    assert isinstance(tree, str)

    # File with symbols should appear
    assert "main.py" in tree

    # File with zero symbols should be hidden by default
    assert "empty.py" not in tree


def test_compact_output_shows_empty_files_when_requested(tmp_path):
    """Files with zero symbols should appear when show_empty=True."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [_make_symbol("src/main.py", "main")]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["src/main.py", "src/empty.py"],
        symbols=syms,
        raw_files={
            "src/main.py": "def main(): ...\n",
            "src/empty.py": "# nothing here\n",
        },
        languages={"python": 2},
    )

    result = get_file_tree(
        "test_owner/test_repo", show_empty=True, storage_path=str(tmp_path)
    )
    assert "error" not in result
    tree = result["tree"]
    assert isinstance(tree, str)

    # Both files should appear
    assert "main.py" in tree
    assert "empty.py" in tree


def test_compact_output_sorts_by_symbol_count_descending(tmp_path):
    """Within a directory, files should be sorted by symbol count descending."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [
        # alpha.py gets 1 symbol
        _make_symbol("src/alpha.py", "func_a"),
        # beta.py gets 3 symbols
        _make_symbol("src/beta.py", "func_b1"),
        _make_symbol("src/beta.py", "func_b2"),
        _make_symbol("src/beta.py", "func_b3"),
        # gamma.py gets 2 symbols
        _make_symbol("src/gamma.py", "func_g1"),
        _make_symbol("src/gamma.py", "func_g2"),
    ]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["src/alpha.py", "src/beta.py", "src/gamma.py"],
        symbols=syms,
        raw_files={
            "src/alpha.py": "def func_a(): ...\n",
            "src/beta.py": "def func_b1(): ...\ndef func_b2(): ...\ndef func_b3(): ...\n",
            "src/gamma.py": "def func_g1(): ...\ndef func_g2(): ...\n",
        },
        languages={"python": 3},
    )

    result = get_file_tree("test_owner/test_repo", storage_path=str(tmp_path))
    assert "error" not in result
    tree = result["tree"]
    assert isinstance(tree, str)

    # beta (3) should appear before gamma (2), which appears before alpha (1)
    beta_pos = tree.index("beta.py")
    gamma_pos = tree.index("gamma.py")
    alpha_pos = tree.index("alpha.py")
    assert beta_pos < gamma_pos < alpha_pos, (
        f"Expected beta ({beta_pos}) < gamma ({gamma_pos}) < alpha ({alpha_pos})"
    )


def test_compact_output_shows_language_in_mixed_repos(tmp_path):
    """In mixed-language repos, each file should show its language label."""
    store = IndexStore(base_path=str(tmp_path))
    c_sym = Symbol(
        id="include/api.h::only_c#function",
        file="include/api.h",
        name="only_c",
        qualified_name="only_c",
        kind="function",
        language="c",
        signature="int only_c(void)",
        byte_offset=0,
        byte_length=20,
    )
    py_sym = _make_symbol("src/main.py", "main", language="python")
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["include/api.h", "src/main.py"],
        symbols=[c_sym, py_sym],
        raw_files={
            "include/api.h": "int only_c(void) { return 0; }\n",
            "src/main.py": "def main(): ...\n",
        },
        languages={"c": 1, "python": 1},
    )

    result = get_file_tree("test_owner/test_repo", storage_path=str(tmp_path))
    assert "error" not in result
    tree = result["tree"]
    assert isinstance(tree, str)

    # Mixed repo: language labels should appear
    for line in tree.splitlines():
        if "api.h" in line:
            assert " c" in line, f"Expected language 'c' in line: {line!r}"
            break
    else:
        raise AssertionError("api.h not found in tree output")

    for line in tree.splitlines():
        if "main.py" in line:
            assert " python" in line, f"Expected language 'python' in line: {line!r}"
            break
    else:
        raise AssertionError("main.py not found in tree output")


def test_compact_output_suppresses_language_in_monolingual_repos(tmp_path):
    """In monolingual repos (>80% one language), language labels are suppressed."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [
        _make_symbol("src/a.py", "func_a"),
        _make_symbol("src/b.py", "func_b"),
        _make_symbol("src/c.py", "func_c"),
    ]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=["src/a.py", "src/b.py", "src/c.py"],
        symbols=syms,
        raw_files={
            "src/a.py": "def func_a(): ...\n",
            "src/b.py": "def func_b(): ...\n",
            "src/c.py": "def func_c(): ...\n",
        },
        languages={"python": 3},
    )

    result = get_file_tree("test_owner/test_repo", storage_path=str(tmp_path))
    assert "error" not in result
    tree = result["tree"]

    # All files should appear with counts
    assert "(1)" in tree

    # Language should NOT appear — repo is 100% python
    assert "python" not in tree


def test_path_prefix_filters_output(tmp_path):
    """path_prefix should limit output to files matching the prefix."""
    store = IndexStore(base_path=str(tmp_path))
    syms = [
        _make_symbol("src/core/engine.py", "run"),
        _make_symbol("src/core/engine.py", "stop"),
        _make_symbol("src/utils/helpers.py", "format_str"),
        _make_symbol("tests/test_engine.py", "test_run"),
    ]
    store.save_index(
        owner="test_owner",
        name="test_repo",
        source_files=[
            "src/core/engine.py",
            "src/utils/helpers.py",
            "tests/test_engine.py",
        ],
        symbols=syms,
        raw_files={
            "src/core/engine.py": "def run(): ...\ndef stop(): ...\n",
            "src/utils/helpers.py": "def format_str(): ...\n",
            "tests/test_engine.py": "def test_run(): ...\n",
        },
        languages={"python": 3},
    )

    result = get_file_tree(
        "test_owner/test_repo", path_prefix="src/core", storage_path=str(tmp_path)
    )
    assert "error" not in result
    tree = result["tree"]
    assert isinstance(tree, str)

    # engine.py should be in the filtered output
    assert "engine.py" in tree

    # Files outside the prefix should not appear
    assert "helpers.py" not in tree
    assert "test_engine.py" not in tree
