#!/usr/bin/env python3
"""
hotfiles · AI file access tracker

Two modes:
  Hook mode  — called by PostToolUse hook (no args), stdin → JSON → SQLite
  Manual mode — user runs with args, SQLite → lcov → HTML

Manual usage:
    python3 hotfiles.py --export       # export lcov only
    python3 hotfiles.py --html         # export + generate HTML heatmap
    python3 hotfiles.py --html --open  # export + generate + open browser
    python3 hotfiles.py --verify       # 3-source verification (debug log vs DB vs lcov)

Debug mode:
    Install with --debug to set HOTFILES_DEBUG=1 env var in hook command.
    Each tool call's raw JSON is appended to hook_debug.log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── Constants ────────────────────────────────────────────

_DIR = Path(__file__).parent
_DB_PATH = _DIR / "hotfiles.db"
_LCOV_PATH = _DIR / "hotfiles.lcov"
_HTML_DIR = _DIR / "hotfiles_html"
_DEBUG_LOG = _DIR / "hook_debug.log"
_DEBUG_SEP = "<<<HOTFILES_DEBUG_RECORD>>>"

_MAX_LINE_SPAN = 50000  # max line range per record, prevents OOM on bad data

# debug mode: controlled by HOTFILES_DEBUG=1 env var set in hook command
_DEBUG = os.environ.get("HOTFILES_DEBUG") == "1"

# hook stdout — signal IDE to continue (Anthropic hook protocol)
# Note: {"continue": True} is part of the Anthropic-originated hook specification
# used by Claude Code, CodeBuddy, Cursor, Cline, Augment, Windsurf, etc.
# Other IDEs without this protocol will simply ignore the stdout.
_CONTINUE = json.dumps({"continue": True})

# ─── Tracked tools (read + search only) ──────────────────
#
# key   = tool name (IDE style / CLI style)
# value = candidate path field names in tool_input, by priority

TOOL_PATH_FIELDS: Dict[str, List[str]] = {
    "read_file":       ["filePath"],
    "Read":            ["filePath", "file_path"],
    "search_content":  ["path"],
    "Grep":            ["path"],
    "search_file":     ["target_directory"],
    "codebase_search": ["path"],
}

TOOL_CATEGORIES: Dict[str, str] = {
    "read_file": "read",        "Read": "read",
    "search_content": "search", "Grep": "search",
    "search_file": "search",    "codebase_search": "search",
}

# subagent tools: extract inner calls from tool_response.toolInfo[]
_SUBAGENT_TOOLS = {"task", "Task"}

# only read tools record 1~totalLineCount for full-file reads
_READ_TOOLS = {"read_file", "Read"}


# ═══════════════════════════════════════════════════════════
#  Database
# ═══════════════════════════════════════════════════════════

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS file_access (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    session_id TEXT,
    tool_name  TEXT NOT NULL,
    category   TEXT NOT NULL,  -- read | search
    file_path  TEXT NOT NULL,  -- absolute path
    rel_path   TEXT,           -- relative to project root
    line_start INTEGER,        -- start line (inclusive)
    line_end   INTEGER,        -- end line (inclusive)
    source     TEXT DEFAULT 'main'  -- main | subagent
)"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_fa_path ON file_access(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_fa_ts   ON file_access(timestamp)",
]


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute(_SCHEMA)
    for sql in _INDEXES:
        conn.execute(sql)
    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════
#  Hook mode: PostToolUse → SQLite
# ═══════════════════════════════════════════════════════════

def _extract_path(tool_name: str, tool_input: dict) -> Optional[str]:
    """Extract file path from tool_input, filtering control chars to prevent lcov injection."""
    def _clean(p: str) -> Optional[str]:
        if any(c in p for c in ("\n", "\r", "\0")):
            return None
        return p

    for field in TOOL_PATH_FIELDS.get(tool_name, []):
        v = tool_input.get(field)
        if v and isinstance(v, str):
            return _clean(v)
    # fallback: first value that looks like an absolute path
    for v in tool_input.values():
        if isinstance(v, str) and v.startswith("/") and len(v) > 2:
            return _clean(v)
    return None


def _parse_content_lines(resp: dict) -> Tuple[Optional[int], Optional[int]]:
    """Parse line range from line-number prefixes in tool_response.content.

    AI tools return content as "     1:code\\n     2:code\\n...",
    parse the first and last line number prefix to get the exact range.
    """
    content = resp.get("content")
    if not content or not isinstance(content, str):
        return None, None
    line_nums = re.findall(r"^\s*(\d+):", content, re.MULTILINE)
    if not line_nums:
        return None, None
    return int(line_nums[0]), int(line_nums[-1])


def _extract_lines(tool_name: str, inp: dict, resp: dict
                   ) -> Tuple[Optional[int], Optional[int]]:
    """Extract line range (start, end) from tool_input / tool_response.

    Priority:
      1. offset + limit (most precise)
      2. offset + content last line (IDE may drop limit)
      3. content first line + limit (IDE may drop offset)
      4. content first & last line (both offset and limit missing)
      5. 1 ~ limit (conservative fallback without content)
      6. 1 ~ totalLineCount (full-file read)
    """
    offset = inp.get("offset")
    limit = inp.get("limit")
    total = resp.get("totalLineCount")

    if offset is not None:
        s = max(1, int(offset))  # lcov lines are 1-based
        if limit is not None:
            return s, s + int(limit) - 1
        # IDE dropped limit → infer end from content
        _, ce = _parse_content_lines(resp)
        if ce is not None:
            return s, ce
        if total is not None:
            return s, int(total)
        return s, None

    # no offset → recover from content line prefixes
    cs, ce = _parse_content_lines(resp)
    if cs is not None:
        if limit is not None:
            return cs, cs + int(limit) - 1
        return cs, ce

    # no offset, no content + has limit → conservative: start at line 1
    if limit is not None:
        return 1, int(limit)

    # no offset, no limit + has total → full-file read
    if total is not None and tool_name in _READ_TOOLS:
        return 1, int(total)

    return None, None


def _to_rel(path: str, project_dir: str) -> Optional[str]:
    """Absolute path → project-relative path, None on failure."""
    if not project_dir:
        return None
    try:
        return str(Path(path).relative_to(project_dir))
    except ValueError:
        return None


def _record(conn: sqlite3.Connection, session_id: Optional[str],
            tool_name: str, file_path: str, project_dir: str,
            line_start: Optional[int], line_end: Optional[int],
            source: str) -> None:
    conn.execute(
        "INSERT INTO file_access"
        " (timestamp,session_id,tool_name,category,file_path,rel_path,line_start,line_end,source)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), session_id, tool_name,
         TOOL_CATEGORIES.get(tool_name, "other"),
         file_path, _to_rel(file_path, project_dir),
         line_start, line_end, source),
    )


def _process_tool(conn: sqlite3.Connection, session_id: Optional[str],
                  tool_name: str, inp: dict, resp: dict,
                  project_dir: str, source: str = "main") -> None:
    path = _extract_path(tool_name, inp)
    if not path:
        return
    ls, le = _extract_lines(tool_name, inp, resp)
    _record(conn, session_id, tool_name, path, project_dir, ls, le, source)


def _process_subagent(conn: sqlite3.Connection, session_id: Optional[str],
                      resp: dict, project_dir: str) -> None:
    """Extract inner tool calls from task/Task tool_response.toolInfo[]."""
    for info in (resp.get("toolInfo") or []):
        if not isinstance(info, dict):
            continue
        name = info.get("toolName", "")
        if name in TOOL_PATH_FIELDS:
            _process_tool(conn, session_id, name,
                          info.get("args") or {}, info.get("result") or {},
                          project_dir, source="subagent")


def _debug_log(data: dict) -> None:
    """Append raw hook JSON to debug log file."""
    if not _DEBUG:
        return
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n{_DEBUG_SEP}\n")
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.write("\n")
    except Exception:
        pass


def _hook_mode() -> None:
    """Called automatically by PostToolUse hook. stdin → JSON → SQLite → stdout."""
    try:
        raw = sys.stdin.read(10 * 1024 * 1024)  # 10MB cap
        if not raw.strip():
            return

        data = json.loads(raw)
        _debug_log(data)

        tool_name = data.get("tool_name", "")
        if tool_name not in TOOL_PATH_FIELDS and tool_name not in _SUBAGENT_TOOLS:
            return

        project_dir = (data.get("cwd", "")
                       or os.environ.get("CLAUDE_PROJECT_DIR", "")
                       or os.environ.get("CODEBUDDY_PROJECT_DIR", "")
                       or os.environ.get("CURSOR_PROJECT_DIR", ""))

        conn = _init_db()
        try:
            if tool_name in _SUBAGENT_TOOLS:
                _process_subagent(conn, data.get("session_id"),
                                  data.get("tool_response") or {}, project_dir)
            else:
                _process_tool(conn, data.get("session_id"), tool_name,
                              data.get("tool_input") or {},
                              data.get("tool_response") or {},
                              project_dir)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        try:
            sys.stderr.write(f"[hotfiles] {e}\n")
        except Exception:
            pass
    finally:
        try:
            print(_CONTINUE)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  Manual mode: SQLite → lcov → HTML
# ═══════════════════════════════════════════════════════════

def _export_lcov() -> bool:
    """Export lcov file from hotfiles.db."""
    if not _DB_PATH.exists():
        print(f"⚠️  database not found: {_DB_PATH}")
        return False

    conn = sqlite3.connect(str(_DB_PATH))
    try:
        rows = conn.execute(
            "SELECT file_path, line_start, line_end FROM file_access"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("⚠️  no access records.")
        return False

    # count hits per file per line
    hits: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    no_lines: set = set()

    for path, ls, le in rows:
        if ls is not None:
            start = int(ls)
            end = int(le if le is not None else ls)
            if end - start > _MAX_LINE_SPAN:
                end = start + _MAX_LINE_SPAN
            for ln in range(start, end + 1):
                hits[path][ln] += 1
        else:
            no_lines.add(path)

    # generate lcov
    out: list[str] = []
    for fp in sorted(hits):
        lc = hits[fp]
        out.append(f"SF:{fp}")
        for ln in sorted(lc):
            out.append(f"DA:{ln},{lc[ln]}")
        out.append(f"LH:{len(lc)}")
        # LF must be >= max line number, otherwise genhtml reports "not long enough"
        out.append(f"LF:{max(lc)}")
        out.append("end_of_record")

    for fp in sorted(no_lines - set(hits)):
        out.append(f"SF:{fp}")
        out.append("LH:0")
        out.append("LF:1")
        out.append("end_of_record")

    _LCOV_PATH.write_text("\n".join(out) + "\n")
    print(f"✅ lcov → {_LCOV_PATH}  ({len(hits)} files with line data)")
    return True


def _gen_html(open_browser: bool) -> None:
    """Run genhtml to convert lcov into an HTML report."""
    genhtml = shutil.which("genhtml")
    if not genhtml:
        print("⚠️  genhtml not found. Install it: brew install lcov")
        return

    result = subprocess.run([
        genhtml, str(_LCOV_PATH), "-o", str(_HTML_DIR),
        "--title", "AI File Access Heatmap",
        "--no-function-coverage", "--no-branch-coverage",
        "--ignore-errors", "range,source",
    ], capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        print(f"⚠️  genhtml failed:\n{result.stderr}")
        return

    index = _HTML_DIR / "index.html"
    print(f"✅ HTML → {index}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{index.resolve()}")


# ═══════════════════════════════════════════════════════════
#  Verify mode: debug log vs DB vs lcov 3-source comparison
# ═══════════════════════════════════════════════════════════

def _verify() -> None:
    """Read debug log + DB + lcov, compare and output diff report."""
    if not _DEBUG_LOG.exists():
        print(f"⚠️  debug log not found: {_DEBUG_LOG}")
        print("   install with --debug first, then run a session.")
        return

    # 1. parse debug log
    log_entries = []
    for block in _DEBUG_LOG.read_text("utf-8").split(_DEBUG_SEP):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
            ti = data.get("tool_input", {})
            tr = data.get("tool_response", {})
            name = data.get("tool_name", "")
            fp = ti.get("filePath", "")

            content = tr.get("content", "")
            cl = re.findall(r"^\s*(\d+):", content, re.MULTILINE) if content else []
            content_range = (int(cl[0]), int(cl[-1])) if cl else (None, None)

            log_entries.append({
                "tool": name,
                "file": fp.split("/")[-1] if fp else "(task)",
                "offset": ti.get("offset"),
                "limit": ti.get("limit"),
                "total": tr.get("totalLineCount"),
                "content_start": content_range[0],
                "content_end": content_range[1],
            })
        except Exception:
            continue

    # 2. read DB
    db_entries = []
    if _DB_PATH.exists():
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            for row in conn.execute(
                "SELECT tool_name, file_path, line_start, line_end, source"
                " FROM file_access ORDER BY id"
            ):
                db_entries.append({
                    "tool": row[0],
                    "file": row[1].split("/")[-1],
                    "start": row[2],
                    "end": row[3],
                    "source": row[4],
                })
        finally:
            conn.close()

    # 3. read lcov
    lcov_entries: dict[str, Tuple[Optional[int], Optional[int]]] = {}
    if _LCOV_PATH.exists():
        cur_file = ""
        cur_min: Optional[int] = None
        cur_max: Optional[int] = None
        for line in _LCOV_PATH.read_text().splitlines():
            if line.startswith("SF:"):
                cur_file = line[3:].split("/")[-1]
                cur_min = cur_max = None
            elif line.startswith("DA:"):
                ln = int(line.split(":")[1].split(",")[0])
                if cur_min is None or ln < cur_min:
                    cur_min = ln
                if cur_max is None or ln > cur_max:
                    cur_max = ln
            elif line == "end_of_record":
                lcov_entries[cur_file] = (cur_min, cur_max)

    # 4. output comparison
    print()
    print("=" * 90)
    print("  3-Source Verification Report")
    print("=" * 90)
    print()
    hdr = f"  {'#':>2}  {'Tool':6}  {'File':25}  {'Hook params':20}  {'content':10}  {'DB':10}  {'lcov':10}  Result"
    sep = f"  {'─'*2}  {'─'*6}  {'─'*25}  {'─'*20}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*6}"
    print(hdr)
    print(sep)

    all_ok = True
    for i, log in enumerate(log_entries):
        o = log["offset"]
        lm = log["limit"]
        hook_str = f"off={o if o is not None else '-':>4} lim={lm if lm is not None else '-':>4}"

        cs, ce = log["content_start"], log["content_end"]
        content_str = f"L{cs}-{ce}" if cs is not None else "-"

        db_str = "-"
        db_start = db_end = None
        if i < len(db_entries):
            db = db_entries[i]
            db_start, db_end = db["start"], db["end"]
            db_str = f"L{db_start}-{db_end}" if db_start is not None else "-"

        lcov_range = lcov_entries.get(log["file"], (None, None))
        lcov_str = f"L{lcov_range[0]}-{lcov_range[1]}" if lcov_range[0] is not None else "-"

        ok = True
        note = "✅"
        if cs is not None and db_start is not None:
            if db_start != cs or db_end != ce:
                ok = False
                note = "❌ DB≠content"
        if log["tool"] in ("Task", "task"):
            note = "📦 subagent"

        if not ok:
            all_ok = False

        print(f"  {i+1:>2}  {log['tool']:6}  {log['file']:25}  "
              f"{hook_str:20}  {content_str:10}  {db_str:10}  {lcov_str:10}  {note}")

    print()
    print(f"  {'✅ All 3 sources consistent.' if all_ok else '❌ Differences found, check marked rows above.'}")
    print()


# ═══════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════

def main() -> None:
    if len(sys.argv) > 1:
        p = argparse.ArgumentParser(description="AI file access heatmap → lcov → HTML")
        p.add_argument("--export", action="store_true", help="export lcov only")
        p.add_argument("--html", action="store_true", help="generate HTML heatmap (requires: brew install lcov)")
        p.add_argument("--open", action="store_true", help="open browser after generating")
        p.add_argument("--verify", action="store_true", help="3-source verification (debug log vs DB vs lcov)")
        args = p.parse_args()

        if args.verify:
            _verify()
            return

        if not _export_lcov():
            sys.exit(1)
        if args.html or args.open:
            _gen_html(args.open)
    else:
        _hook_mode()


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        print(f"❌ Python 3.8+ required (current: {sys.version.split()[0]})", file=sys.stderr)
        sys.exit(1)
    main()
