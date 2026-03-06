"""Parser package for extracting symbols from source code."""

from .symbols import Symbol, slugify, make_symbol_id, compute_content_hash
from .languages import LanguageSpec, LANGUAGE_REGISTRY, LANGUAGE_EXTENSIONS, PYTHON_SPEC
from .extractor import parse_file, extract_refs
from .hierarchy import SymbolNode, build_symbol_tree, flatten_tree

__all__ = [
    "Symbol",
    "slugify",
    "make_symbol_id",
    "compute_content_hash",
    "LanguageSpec",
    "LANGUAGE_REGISTRY",
    "LANGUAGE_EXTENSIONS",
    "PYTHON_SPEC",
    "parse_file",
    "extract_refs",
    "SymbolNode",
    "build_symbol_tree",
    "flatten_tree",
]
