"""Cross-reference query tools: find_references, find_callers, find_constructors, find_field_reads, find_field_writes."""

import time
from typing import Optional

from ..parser import SUPPORTED_REF_LANGUAGES
from ..storage import IndexStore
from ._utils import resolve_repo


def _load_refs(
    repo: str,
    storage_path: Optional[str],
) -> tuple[Optional[str], Optional[list[dict]], Optional[dict], Optional[object]]:
    """Load refs and index metadata for a repo."""
    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return str(e), None, None, None

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return f"Repository not indexed: {owner}/{name}", None, None, None
    refs = store.load_refs(owner, name)
    if refs is None:
        return (
            f"Cross-reference index not available for {owner}/{name}. "
            "Re-index with index_folder or index_repo to build it.",
            None,
            None,
            None,
        )
    return None, refs, {"owner": owner, "name": name}, index


def _coverage_warnings(index) -> list[str]:
    """Describe xref coverage for the repo's indexed languages."""
    repo_languages = set(index.languages)
    unsupported = sorted(repo_languages - SUPPORTED_REF_LANGUAGES)
    if not unsupported:
        return []

    supported_display = ", ".join(sorted(SUPPORTED_REF_LANGUAGES))
    repo_display = ", ".join(sorted(repo_languages)) or "unknown"
    if repo_languages & SUPPORTED_REF_LANGUAGES:
        return [
            "WARNING: cross-reference coverage is partial for "
            f"{index.repo}. Supported xref languages: {supported_display}. "
            f"Repo languages without xref support: {', '.join(unsupported)}."
        ]

    return [
        "WARNING: cross-reference extraction currently supports only "
        f"{supported_display}. Repo languages: {repo_display}. "
        "Results may be empty."
    ]


def _candidate_symbols(index, name: str, ref_types: Optional[list[str]]) -> list[dict]:
    """Find declared in-repo symbols that match the queried short name."""
    if ref_types and set(ref_types) <= {"field_read", "field_write"}:
        return []

    if ref_types == ["call"]:
        allowed_kinds = {"function", "method"}
    elif ref_types == ["construct"]:
        allowed_kinds = {"class", "type"}
    else:
        allowed_kinds = {"function", "method", "class", "type", "constant"}

    return [
        sym
        for sym in index.symbols
        if sym.get("name") == name and sym.get("kind") in allowed_kinds
    ]


def _format_candidates(candidates: list[dict]) -> list[dict]:
    """Trim candidate definitions for response payloads."""
    return [
        {
            "id": sym["id"],
            "name": sym["name"],
            "qualified_name": sym.get("qualified_name", sym["name"]),
            "kind": sym["kind"],
            "file": sym["file"],
            "line": sym["line"],
        }
        for sym in candidates
    ]


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

    err, refs, meta, index = _load_refs(repo, storage_path)
    if err:
        return {"error": err}

    owner = meta["owner"]
    repo_name = meta["name"]
    warnings = _coverage_warnings(index)
    unsupported_languages = set(index.languages) - SUPPORTED_REF_LANGUAGES

    candidates = _candidate_symbols(index, name, ref_types)
    response = {
        "repo": f"{owner}/{repo_name}",
        "symbol": name,
        "_meta": {
            "repo_languages": sorted(index.languages),
            "supported_ref_languages": sorted(SUPPORTED_REF_LANGUAGES),
            "unsupported_ref_languages": sorted(unsupported_languages),
        },
    }

    if len(candidates) > 1:
        elapsed = (time.perf_counter() - start) * 1000
        warnings.append(
            f"WARNING: '{name}' is ambiguous: {len(candidates)} in-repo declarations share this "
            "short name. Ref queries would conflate them, so results are withheld. "
            "Inspect candidates via search_symbols/get_symbol first."
        )
        response.update(
            {
                "total_refs": 0,
                "production_refs": 0,
                "test_refs": 0,
                "refs": [],
                "candidates": _format_candidates(candidates),
            }
        )
        response["_meta"]["timing_ms"] = round(elapsed, 1)
        response["warning"] = warnings[0]
        if len(warnings) > 1:
            response["warnings"] = warnings
        return response

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

    response.update(
        {
            "total_refs": len(matches),
            "production_refs": production_count,
            "test_refs": test_count,
            "refs": matches,
            "candidates": _format_candidates(candidates),
        }
    )
    response["_meta"]["timing_ms"] = round(elapsed, 1)

    if production_count == 0 and test_count > 0:
        warnings.append(
            f"WARNING: '{name}' is referenced only in test code ({test_count} test ref(s), "
            "0 production refs). It may be declared but not wired in production."
        )
    elif len(matches) == 0 and not unsupported_languages:
        is_field_query = bool(ref_types and set(ref_types) <= {"field_read", "field_write"})
        if candidates:
            warnings.append(
                f"WARNING: '{name}' has no recorded references. "
                "It may be unreferenced, or calls may go through dynamic dispatch (dyn Trait, cx.emit). "
                "Verify with exhaustive search_text if needed."
            )
        elif not is_field_query:
            warnings.append(
                f"WARNING: '{name}' is not declared in the indexed repo and has no recorded references."
            )
        # field queries: fields are not tracked as symbols — emit no declaration warning

    if warnings:
        response["warning"] = warnings[0]
        if len(warnings) > 1:
            response["warnings"] = warnings

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
