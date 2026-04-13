[中文](README.zh-CN.md) | English

# hotfiles

Track AI coding tool file access (reads + searches), export lcov, generate line-level HTML heatmaps.

See which files and lines your AI assistant actually looks at — visualized as a coverage-style heatmap.

## Why

When working with AI coding assistants (Claude Code, Cursor, CodeBuddy, etc.), you often wonder:
- Which files did the AI actually read?
- How thoroughly did it examine the code?
- Did it miss important files?

**hotfiles** records every `read_file`, `search_content`, `codebase_search` and other tool calls via the `PostToolUse` hook, stores them in SQLite, and generates a visual heatmap using [lcov](https://github.com/linux-test-project/lcov) / `genhtml`.

## How it works

```mermaid
flowchart LR
    A[AI calls tool] -->|PostToolUse Hook| B[hotfiles.py]
    B --> C[(hotfiles.db)]
    C -->|"--html"| D[genhtml → HTML heatmap]
```

Tracked tools: `read_file` · `Read` · `search_content` · `Grep` · `search_file` · `codebase_search` · `task`/`Task` (subagent inner calls)

### Supported IDEs

| IDE | Config dir | Hook format |
|---|---|---|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `.claude/` | ✅ identical |
| [CodeBuddy](https://www.codebuddy.ai/) | `.codebuddy/` | ✅ identical |
| [Cursor](https://cursor.com/) | `.cursor/` | ✅ identical |
| [Cline](https://docs.cline.bot/) | `.cline/` | ✅ identical |
| [Augment](https://www.augmentcode.com/) | `.augment/` | ✅ identical |
| [Windsurf](https://docs.windsurf.com/) | `.windsurf/` | ✅ identical |

All listed IDEs use the same Anthropic-originated hook specification. `install.py` auto-detects which one to use. You can also specify `--ide` manually.

## Prerequisites

- Python 3.8+
- [`lcov`](https://github.com/linux-test-project/lcov) for HTML generation: `brew install lcov`

## Quick start

```bash
# Clone the repo
git clone https://github.com/tivnantu/hotfiles.git

# cd to your project, then install
cd /path/to/your/project
python3 /path/to/hotfiles/install.py

# Start a new AI session, work as usual...

# Generate heatmap
python3 .claude/hooks/hotfiles/hotfiles.py --html --open
```

The install script auto-detects your IDE. To specify one explicitly:

```bash
python3 install.py --ide cursor
python3 install.py --ide codebuddy
python3 install.py --ide claude       # default
```

## Usage

### install.py

Run from the **hotfiles repo**, targeting your project:

```bash
python3 install.py                      # install (auto-detect IDE)
python3 install.py --ide cursor         # install for specific IDE
python3 install.py --project /path/to   # install to specific project
python3 install.py --debug              # install with debug logging
python3 install.py --status             # show status
python3 install.py --uninstall          # uninstall
```

### hotfiles.py

Run from your **project** (auto-deployed during install):

```bash
# no args = hook mode (called automatically by PostToolUse)
python3 hotfiles.py

# manual export
python3 hotfiles.py --export       # export lcov only
python3 hotfiles.py --html         # export + generate HTML
python3 hotfiles.py --html --open  # export + generate + open browser
python3 hotfiles.py --verify       # 3-source verification
```

## Debug & verification

Install with `--debug` to record raw hook JSON:

```bash
python3 install.py --debug

# After a session:
python3 hotfiles.py --export    # export lcov first
python3 hotfiles.py --verify    # 3-source check: debug log vs DB vs lcov
```

Turn off debug: re-run `install.py` without `--debug`.

## Screenshots

Line-level heatmap — see exactly which lines the AI read:

<img src="hotfiles-sip-sh.png" alt="sip.sh line-level heatmap" width="80%">

<img src="hotfiles-sip-readme.png" alt="README.md full-file read heatmap" width="80%">

### 3-source verification report (`--verify`)

<img src="hotfiles-verify-new.png" alt="3-source verification" width="60%">

## Design decisions

### Why lcov?

lcov is the standard Linux code coverage format. `genhtml` produces beautiful, interactive HTML reports with file trees, line highlighting, and hit counts — all for free, with zero custom frontend code.

### Why SQLite?

- WAL mode supports concurrent writes from multiple hook invocations
- Zero config, zero dependencies (Python stdlib)
- Queryable — you can run custom SQL on `hotfiles.db` for deeper analysis

### Why project-level install?

Each project gets its own `hotfiles.py` + `hotfiles.db`. This means:
- Data is scoped to the project
- Multiple projects don't interfere
- Easy to clean up: `rm -rf .claude/hooks/hotfiles/`

### Line range extraction priority

When recording which lines were accessed, hotfiles uses a 6-level priority chain:
1. `offset + limit` (most precise)
2. `offset + content last line` (IDE may drop limit)
3. `content first line + limit` (IDE may drop offset)
4. `content first & last line` (both missing)
5. `1 ~ limit` (conservative, no content)
6. `1 ~ totalLineCount` (full-file read)

This handles all the quirks of different IDE tool response formats.

## Files

```
hotfiles/
├── hotfiles.py     # core: hook handler + lcov export + HTML generation
├── install.py      # installer: multi-IDE support, auto-detect
├── .gitignore
├── README.md       # English
├── README.zh-CN.md # 中文
└── LICENSE
```

After install (example for Claude Code):
```
your-project/
└── .claude/
    ├── settings.json              # PostToolUse hook registered
    └── hooks/hotfiles/
        ├── hotfiles.py            # deployed copy
        ├── hotfiles.db            # created after first session
        ├── hotfiles.lcov          # created by --export
        └── hotfiles_html/         # created by --html
```

## License

[MIT](LICENSE)
