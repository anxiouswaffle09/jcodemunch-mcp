# User Guide

## Installation

The recommended approach is `uvx`, which resolves and runs the package without requiring anything on your PATH:

```bash
uvx jcodemunch-mcp --help
```

Or install with pip:

```bash
pip install jcodemunch-mcp
jcodemunch-mcp --help
```

Or from source:

```bash
git clone https://github.com/jgravelle/jcodemunch-mcp.git
cd jcodemunch-mcp
pip install -e .
```

---

## Configuration

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jcodemunch": {
      "command": "jcodemunch-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_xxxxxxxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxxxxxxx"
      }
    }
  }
}
```

Both environment variables are optional:

* `GITHUB_TOKEN` enables private repositories and higher GitHub API rate limits.
* `ANTHROPIC_API_KEY` enables AI-generated summaries via Claude Haiku.
* `GOOGLE_API_KEY` enables AI-generated summaries via Gemini Flash (used if `ANTHROPIC_API_KEY` is not set).
* If neither key is set, summaries fall back to docstrings or signatures.

### VS Code

Add to `.vscode/settings.json`:

```json
{
  "mcp.servers": {
    "jcodemunch": {
      "command": "jcodemunch-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_xxxxxxxx"
      }
    }
  }
}
```

### Claude Code Status Line

If you use Claude Code, you can display a live token savings counter in the status bar:

```
Claude Sonnet 4.6 | my-project | ░░░░░░░░░░ 0% | 1,280,837 tkns saved · $6.40 saved on Opus
```

Ask Claude Code to set it up:

> "Add jcodemunch token savings to my status line"

Claude Code will add a segment that reads `~/.code-index/_savings.json` and calculates cost avoided at the Claude Opus rate ($25.00 / 1M tokens). The counter updates automatically after every jcodemunch tool call — no restart required.

To add it manually, read `~/.code-index/_savings.json` and extract `total_tokens_saved`:

```js
// Node.js snippet for a statusline hook
const f = path.join(os.homedir(), '.code-index', '_savings.json');
const total = fs.existsSync(f) ? JSON.parse(fs.readFileSync(f)).total_tokens_saved ?? 0 : 0;
const cost  = (total * 25.00 / 1_000_000).toFixed(2);
if (total > 0) output += ` │ ${total.toLocaleString()} tkns saved · $${cost} saved on Opus`;
```

---

### Google Antigravity

1. Open the Agent pane → click the `⋯` menu → **MCP Servers** → **Manage MCP Servers**
2. Click **View raw config** to open `mcp_config.json`
3. Add the entry below, save, then restart the MCP server from the Manage MCPs pane

```json
{
  "mcpServers": {
    "jcodemunch": {
      "command": "jcodemunch-mcp",
      "env": {
        "GITHUB_TOKEN": "ghp_xxxxxxxx",
        "ANTHROPIC_API_KEY": "sk-ant-xxxxxxxx"
      }
    }
  }
}
```

---

## Workflows

### Explore a New Repository

```
index_repo: { "url": "fastapi/fastapi" }
get_repo_outline: { "repo": "fastapi/fastapi" }
get_file_tree: { "repo": "fastapi/fastapi", "path_prefix": "fastapi" }
get_file_outline: { "repo": "fastapi/fastapi", "file_path": "fastapi/main.py" }
```

### Explore a Local Project

```
index_folder: { "path": "/home/user/myproject" }
get_repo_outline: { "repo": "local-myproject" }
search_symbols: { "repo": "local-myproject", "query": "main" }
```

### Find and Read a Function

```
search_symbols: { "repo": "owner/repo", "query": "authenticate", "kind": "function" }
get_symbol: { "repo": "owner/repo", "symbol_id": "src/auth.py::authenticate#function" }
```

### Understand a Class

```
get_file_outline: { "repo": "owner/repo", "file_path": "src/auth.py" }
get_symbols: {
  "repo": "owner/repo",
  "symbol_ids": [
    "src/auth.py::AuthHandler.login#method",
    "src/auth.py::AuthHandler.logout#method"
  ]
}
```

### Verify Source Hasn't Changed

```
get_symbol: {
  "repo": "owner/repo",
  "symbol_id": "src/main.py::process#function",
  "verify": true
}
```

The response `_meta.content_verified` will be `true` if the source matches the stored hash and `false` if it has drifted.

### Search for Non-Symbol Content

```
search_text: { "repo": "owner/repo", "query": "TODO", "file_pattern": "*.py" }
```

Use `search_text` for string literals, comments, configuration values, or anything that is not a symbol name.

For punctuation-heavy queries (macro invocations, `::new(` patterns, enum variants), use `exact=true` for case-sensitive exact matching:

```
search_text: { "repo": "owner/repo", "query": "Foo::new(", "exact": true }
```

If results are truncated (check `total_hits` vs `result_count` in the response), use `exhaustive=true` or paginate with `offset`:

```
search_text: { "repo": "owner/repo", "query": "TODO", "exhaustive": true }
search_text: { "repo": "owner/repo", "query": "TODO", "max_results": 20, "offset": 20 }
```

### Trace Cross-References

Find everywhere a function is called:

```
find_callers: { "repo": "owner/repo", "symbol_name": "authenticate" }
```

Find only production callers (exclude test code):

```
find_callers: { "repo": "owner/repo", "symbol_name": "authenticate", "production_only": true }
```

Verify a type is actually instantiated in production (not just in tests):

```
find_constructors: { "repo": "owner/repo", "type_name": "SpectralAnalyzer", "production_only": true }
```

Track all reads of a struct field:

```
find_field_reads: { "repo": "owner/repo", "field_name": "session_id" }
```

Find all writes to a field (useful for auditing mutation):

```
find_field_writes: { "repo": "owner/repo", "field_name": "session_id", "production_only": true }
```

Find all usages of a symbol across all reference types (calls + constructions + field accesses):

```
find_references: { "repo": "owner/repo", "symbol_name": "Config" }
```

> **Note:** Cross-reference results include `total_refs`, `production_refs`, `test_refs`, and a `refs` list with file, line, and caller information. If a symbol name is ambiguous (multiple in-repo declarations with the same short name), the tool withholds conflated results and returns `candidates` instead — inspect them with `search_symbols` or `get_symbol` first.

### Force Re-index

```
invalidate_cache: { "repo": "owner/repo" }
index_repo: { "url": "owner/repo" }
```

---

## Tool Reference

| Tool                 | Purpose                            | Key Parameters                                                                      |
| -------------------- | ---------------------------------- | ----------------------------------------------------------------------------------- |
| `index_repo`         | Index GitHub repository            | `url`, `use_ai_summaries`                                                           |
| `index_folder`       | Index local folder                 | `path`, `extra_ignore_patterns`, `follow_symlinks`                                  |
| `list_repos`         | List all indexed repositories      | —                                                                                   |
| `get_file_tree`      | Browse file structure              | `repo`, `path_prefix`, `show_empty`                                                 |
| `get_file_outline`   | Symbols in a file                  | `repo`, `file_path`                                                                 |
| `get_symbol`         | Full source of one symbol          | `repo`, `symbol_id`, `verify`, `context_lines`                                      |
| `get_symbols`        | Batch retrieve symbols             | `repo`, `symbol_ids`                                                                |
| `search_symbols`     | Search symbols                     | `repo`, `query`, `kind`, `language`, `file_pattern`, `max_results`, `offset`, `exhaustive` |
| `search_text`        | Full-text search                   | `repo`, `query`, `file_pattern`, `max_results`, `offset`, `exhaustive`, `exact`     |
| `get_repo_outline`   | High-level overview                | `repo`                                                                              |
| `invalidate_cache`   | Delete cached index                | `repo`                                                                              |
| `find_references`    | All usages of a symbol             | `repo`, `symbol_name`, `production_only`, `test_only`                               |
| `find_callers`       | Call sites for a function/method   | `repo`, `symbol_name`, `production_only`, `test_only`                               |
| `find_constructors`  | Construction sites for a type      | `repo`, `type_name`, `production_only`, `test_only`                                 |
| `find_field_reads`   | Read sites for a field/attribute   | `repo`, `field_name`, `production_only`                                             |
| `find_field_writes`  | Write sites for a field/attribute  | `repo`, `field_name`, `production_only`                                             |

---

## Symbol IDs

Symbol IDs follow the format:

```
{file_path}::{qualified_name}#{kind}
```

Examples:

```
src/main.py::UserService#class
src/main.py::UserService.login#method
src/utils.py::authenticate#function
config.py::MAX_RETRIES#constant
```

IDs are returned by `get_file_outline`, `search_symbols`, and `search_text`. Pass them to `get_symbol` or `get_symbols` to retrieve source code.

---

## Community Savings Meter

jCodeMunch contributes an anonymous token savings delta to a live global counter at [j.gravelle.us](https://j.gravelle.us) with each tool call. Only two values are ever sent: the tokens saved (a number) and a random anonymous install ID. No code, paths, repo names, or anything identifying is transmitted. Network failures are silent and never affect tool performance.

The anonymous install ID is generated once and stored locally in `~/.code-index/_savings.json`.

To disable, set `JCODEMUNCH_SHARE_SAVINGS=0` in your MCP server env:

```json
{
  "mcpServers": {
    "jcodemunch": {
      "command": "jcodemunch-mcp",
      "env": {
        "JCODEMUNCH_SHARE_SAVINGS": "0"
      }
    }
  }
}
```

---

## Troubleshooting

**"Repository not found"**
Check the URL format (`owner/repo` or full GitHub URL). For private repositories, set `GITHUB_TOKEN`.

**"No source files found"**
The repository may not contain supported language files (`.py`, `.js`, `.ts`, `.go`, `.rs`, `.java`, `.c`, `.h`, `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`), or files may be excluded by skip patterns.

**Rate limiting**
Set `GITHUB_TOKEN` to increase GitHub API limits (5,000 requests/hour vs 60 unauthenticated).

**AI summaries not working**
Set `ANTHROPIC_API_KEY` (Claude Haiku) or `GOOGLE_API_KEY` (Gemini Flash). Anthropic takes priority if both are set. Without either, summaries fall back to docstrings or signatures.

**Stale index**
Use `invalidate_cache` followed by `index_repo` or `index_folder` to force a clean re-index.

**Encoding issues**
Files with invalid UTF-8 are handled safely using replacement characters.

---

## Storage

Indexes are stored at `~/.code-index/` (override with the `CODE_INDEX_PATH` environment variable):

```
~/.code-index/
├── owner-repo.json       # Index metadata + symbols
└── owner-repo/           # Raw source files
    └── src/main.py
```

---

## Tips

1. Start with `get_repo_outline` to quickly understand the repository structure.
2. Use `get_file_outline` before reading source to understand the API surface first.
3. Narrow searches using `kind`, `language`, and `file_pattern`.
4. Batch-retrieve related symbols with `get_symbols` instead of repeated `get_symbol` calls.
5. Use `search_text` when symbol search does not locate the needed content.
6. Use `verify: true` on `get_symbol` to detect source drift since indexing.
7. Check `total_hits` in search responses — if it exceeds `result_count`, use `exhaustive: true` or paginate with `offset` before drawing conclusions.
8. Use `find_constructors` with `production_only: true` before claiming a type is wired in production — zero production hits means it is not, regardless of whether the symbol exists in the index.
9. Use `find_callers` / `find_field_writes` to quickly identify dead code or unintended mutation paths.
