"""Cross-reference query tools: find_references, find_callers, find_constructors, find_field_reads, find_field_writes."""

import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def _load_refs(repo: str, storage_path: Optional[str]) -> tuple[Optional[str], Optional[list[dict]], Optional[dict]]:
    """Load refs for a repo. Returns (error_str, refs, owner_name_dict)."""
    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return str(e), None, None

    store = IndexStore(base_path=storage_path)
    refs = store.load_refs(owner, name)
    if refs is None:
        return (
            f"Cross-reference index not available for {owner}/{name}. "
            "Re-index with index_folder or index_repo to build it.",
            None,
            None,
        )
    return None, refs, {"owner": owner, "name": name}


def _query_refs(
    repo: str,
    name: str,
    ref_types: Optional[list[str]],
    production_only: bool,
    test_only: bool,
    storage_path: Optional[str],
) -> dict:
    """Shared implementation for all find_* tools."""
    start = time.perf_counter()

    err, refs, meta = _load_refs(repo, storage_path)
    if err:
        return {"error": err}

    owner = meta["owner"]
    repo_name = meta["name"]

    # Filter by callee name (case-sensitive — Rust types are PascalCase)
    matches = [r for r in refs if r.get("callee") == name]

    # Filter by ref_type if specified
    if ref_types:
        matches = [r for r in matches if r.get("ref_type") in ref_types]

    # Test/production filter
    if production_only:
        matches = [r for r in matches if not r.get("is_test", False)]
    elif test_only:
        matches = [r for r in matches if r.get("is_test", False)]

    # Summarise
    production_count = sum(1 for r in matches if not r.get("is_test", False))
    test_count = sum(1 for r in matches if r.get("is_test", False))

    elapsed = (time.perf_counter() - start) * 1000

    response = {
        "repo": f"{owner}/{repo_name}",
        "symbol": name,
        "total_refs": len(matches),
        "production_refs": production_count,
        "test_refs": test_count,
        "refs": matches,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }

    if production_count == 0 and test_count > 0:
        response["warning"] = (
            f"WARNING: '{name}' is referenced only in test code ({test_count} test ref(s), "
            "0 production refs). It may be declared but not wired in production."
        )
    elif len(matches) == 0:
        response["warning"] = (
            f"WARNING: '{name}' has no recorded references. "
            "It may be unreferenced, or calls may go through dynamic dispatch (dyn Trait, cx.emit). "
            "Verify with exhaustive search_text if needed."
        )

    return response


def find_references(
    repo: str,
    symbol_name: str,
    production_only: bool = False,
    test_only: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all references to a symbol (calls, constructions, field accesses).

    Args:
        repo: Repository identifier.
        symbol_name: Name to search for (case-sensitive).
        production_only: If True, exclude test-context references.
        test_only: If True, return only test-context references.
        storage_path: Custom storage path.
    """
    return _query_refs(repo, symbol_name, None, production_only, test_only, storage_path)


def find_callers(
    repo: str,
    symbol_name: str,
    production_only: bool = False,
    test_only: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all call sites for a function or method.

    Args:
        repo: Repository identifier.
        symbol_name: Function/method name (case-sensitive).
        production_only: If True, exclude test-context callers.
        test_only: If True, return only test-context callers.
        storage_path: Custom storage path.
    """
    return _query_refs(repo, symbol_name, ["call"], production_only, test_only, storage_path)


def find_constructors(
    repo: str,
    type_name: str,
    production_only: bool = False,
    test_only: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all construction sites for a struct or class (::new calls and struct literals).

    Args:
        repo: Repository identifier.
        type_name: Struct/class name (case-sensitive, e.g. 'SpectralAnalyzer').
        production_only: If True, exclude test-context constructions.
        test_only: If True, return only test-context constructions.
        storage_path: Custom storage path.
    """
    return _query_refs(repo, type_name, ["construct"], production_only, test_only, storage_path)


def find_field_reads(
    repo: str,
    field_name: str,
    production_only: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all read sites for a struct field or object attribute.

    Args:
        repo: Repository identifier.
        field_name: Field/attribute name (case-sensitive).
        production_only: If True, exclude test-context reads.
        storage_path: Custom storage path.
    """
    return _query_refs(repo, field_name, ["field_read"], production_only, False, storage_path)


def find_field_writes(
    repo: str,
    field_name: str,
    production_only: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all write sites for a struct field or object attribute.

    Args:
        repo: Repository identifier.
        field_name: Field/attribute name (case-sensitive).
        production_only: If True, exclude test-context writes.
        storage_path: Custom storage path.
    """
    return _query_refs(repo, field_name, ["field_write"], production_only, False, storage_path)
