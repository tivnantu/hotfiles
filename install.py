#!/usr/bin/env python3
"""
hotfiles · install / uninstall

Deploy hotfiles.py into a project and register the PostToolUse hook.

Supports multiple AI coding tools by auto-detecting the settings directory:
  - Claude Code:  .claude/settings.json  (default)
  - CodeBuddy:    .codebuddy/settings.json
  - Cursor:       .cursor/settings.json
  - Cline:        .cline/settings.json
  - Augment:      .augment/settings.json
  - Windsurf:     .windsurf/settings.json

Usage:
    python3 install.py                      # install (auto-detect IDE)
    python3 install.py --ide claude         # install for specific IDE
    python3 install.py --debug              # install with debug logging
    python3 install.py --project /path/to   # install to specific project
    python3 install.py --status             # show status
    python3 install.py --uninstall          # uninstall
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SRC_DIR = Path(__file__).parent.resolve()
_DEPLOY_FILES = ["hotfiles.py", ".gitignore"]
_HOOK_DIR = "hotfiles"
_HOOK_TAG = "# managed by hotfiles"

# ─── IDE definitions ─────────────────────────────────────

# Each IDE: (name, config_dir_name, settings_relative_path)
# All listed IDEs share the same Anthropic-originated hook specification.
_IDES: List[Tuple[str, str, str]] = [
    ("claude",    ".claude",    ".claude/settings.json"),
    ("codebuddy", ".codebuddy", ".codebuddy/settings.json"),
    ("cursor",    ".cursor",    ".cursor/settings.json"),
    ("cline",     ".cline",     ".cline/settings.json"),
    ("augment",   ".augment",   ".augment/settings.json"),
    ("windsurf",  ".windsurf",  ".windsurf/settings.json"),
]

# Legacy dir names to recognise during uninstall / status
_LEGACY_HOOK_DIRS = ["codebuddy_hotfiles"]


def _detect_ide(project: Path) -> Optional[Tuple[str, str, str]]:
    """Auto-detect which IDE config dir exists in the project."""
    for ide in _IDES:
        if (project / ide[1]).is_dir():
            return ide
    return None


def _detect_all_ides(project: Path) -> List[Tuple[str, str, str]]:
    """Return all IDEs whose config dir exists in the project."""
    return [ide for ide in _IDES if (project / ide[1]).is_dir()]


def _get_ide(name: str) -> Optional[Tuple[str, str, str]]:
    """Lookup IDE by name."""
    for ide in _IDES:
        if ide[0] == name:
            return ide
    return None


# ─── Path helpers ────────────────────────────────────────

def _hooks_dir(project: Path, config_dir: str) -> Path:
    return project / config_dir / "hooks" / _HOOK_DIR


def _settings_path(project: Path, settings_rel: str) -> Path:
    return project / settings_rel


def _load_json(p: Path) -> Dict[str, Any]:
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_json(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def _is_our_hook(entry: dict) -> bool:
    """Check if a hook entry was registered by hotfiles (via unique tag)."""
    return any(_HOOK_TAG in h.get("command", "")
               for h in entry.get("hooks", []))


# ─── install ─────────────────────────────────────────────

def _install(project: Path, ide: Tuple[str, str, str], debug: bool = False) -> None:
    ide_name, config_dir, settings_rel = ide
    hdir = _hooks_dir(project, config_dir)
    hdir.mkdir(parents=True, exist_ok=True)

    # deploy files
    for name in _DEPLOY_FILES:
        src = _SRC_DIR / name
        if not src.exists():
            if name.endswith(".py"):
                print(f"❌ missing: {src}")
                return
            continue
        dst = hdir / name
        shutil.copy2(src, dst)
        if name.endswith(".py"):
            dst.chmod(dst.stat().st_mode | stat.S_IEXEC)

    # build hook command
    tracker = hdir / "hotfiles.py"
    base_cmd = f"HOTFILES_DEBUG=1 python3 {tracker}" if debug else f"python3 {tracker}"
    command = f"{base_cmd} {_HOOK_TAG}"

    # register hook
    sp = _settings_path(project, settings_rel)
    settings = _load_json(sp)
    ptu = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    ptu[:] = [e for e in ptu if not _is_our_hook(e)]  # remove old
    ptu.append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    })
    _save_json(sp, settings)

    rel_hdir = hdir.relative_to(project)
    rel_tracker = tracker.relative_to(project)

    print(f"✅ installed for {ide_name}! (debug: {'on' if debug else 'off'})")
    print(f"   → {rel_hdir}/")
    print()
    print("   restart your IDE to activate.")
    print()
    print(f"   heatmap:  python3 {rel_tracker} --html --open")
    if debug:
        print(f"   verify:   python3 {rel_tracker} --verify")


# ─── uninstall ───────────────────────────────────────────

def _uninstall(project: Path, ide: Tuple[str, str, str]) -> None:
    ide_name, config_dir, settings_rel = ide
    sp = _settings_path(project, settings_rel)

    if sp.exists():
        settings = _load_json(sp)
        ptu = settings.get("hooks", {}).get("PostToolUse", [])
        before = len(ptu)
        ptu[:] = [e for e in ptu if not _is_our_hook(e)]

        if len(ptu) < before:
            if not ptu:
                settings.get("hooks", {}).pop("PostToolUse", None)
            if not settings.get("hooks"):
                settings.pop("hooks", None)
            if settings:
                _save_json(sp, settings)
            else:
                sp.unlink(missing_ok=True)
            print(f"✅ hook removed ({ide_name})")
        else:
            print(f"ℹ️  no hook found ({ide_name})")

    # remove deployed files (keep database and logs)
    dirs_to_check = [_hooks_dir(project, config_dir)]
    for legacy in _LEGACY_HOOK_DIRS:
        ld = project / config_dir / "hooks" / legacy
        if ld.exists():
            dirs_to_check.append(ld)

    for hdir in dirs_to_check:
        if not hdir.exists():
            continue
        for name in _DEPLOY_FILES:
            (hdir / name).unlink(missing_ok=True)
        remaining = [f.name for f in hdir.iterdir()]
        if remaining:
            print(f"   data kept: {', '.join(remaining)}")
            print(f"   full cleanup: rm -rf {hdir}")
        else:
            hdir.rmdir()
            print(f"✅ cleaned {hdir}")


# ─── status ──────────────────────────────────────────────

def _status(project: Path) -> None:
    found_any = False
    for ide_name, config_dir, settings_rel in _IDES:
        hdir = _hooks_dir(project, config_dir)
        sp = _settings_path(project, settings_rel)
        db = hdir / "hotfiles.db"

        deployed = (hdir / "hotfiles.py").exists()

        # also check legacy dir
        if not deployed:
            for legacy in _LEGACY_HOOK_DIRS:
                ld = project / config_dir / "hooks" / legacy
                if (ld / "hotfiles.py").exists():
                    deployed = True
                    hdir = ld
                    db = ld / "hotfiles.db"
                    break

        hooked = False
        debug = False
        if sp.exists():
            ptu = _load_json(sp).get("hooks", {}).get("PostToolUse", [])
            for e in ptu:
                if _is_our_hook(e):
                    hooked = True
                    cmd = e.get("hooks", [{}])[0].get("command", "")
                    debug = "HOTFILES_DEBUG=1" in cmd
                    break

        if not deployed and not hooked and not db.exists():
            continue

        found_any = True
        print(f"  [{ide_name}]")
        print(f"    project: {project}")
        print(f"    script:  {'✅' if deployed else '❌'}  {hdir}")
        print(f"    hook:    {'✅' if hooked else '❌'}")
        print(f"    debug:   {'✅ on' if debug else 'off'}")

        if db.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(str(db))
                n = conn.execute("SELECT COUNT(*) FROM file_access").fetchone()[0]
                f = conn.execute("SELECT COUNT(DISTINCT file_path) FROM file_access").fetchone()[0]
                conn.close()
                print(f"    data:    {n} records, {f} files")
            except Exception:
                print("    data:    (read failed)")
        else:
            print("    data:    none yet (created after first session)")
        print()

    if not found_any:
        print("  not installed for any IDE")
        print(f"  supported: {', '.join(ide[0] for ide in _IDES)}")


# ─── main ────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="hotfiles — install / uninstall AI file access tracker")
    p.add_argument("--project", metavar="DIR",
                   help="target project directory (default: cwd)")
    p.add_argument("--ide", choices=[ide[0] for ide in _IDES],
                   help="target IDE (default: auto-detect)")
    p.add_argument("--debug", action="store_true",
                   help="enable debug logging (raw hook JSON → hook_debug.log)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true", help="uninstall")
    g.add_argument("--status", action="store_true", help="show status")
    args = p.parse_args()

    project = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    if not project.is_dir():
        print(f"❌ not a directory: {project}")
        sys.exit(1)

    if args.status:
        _status(project)
        return

    # resolve IDE
    if args.ide:
        ide = _get_ide(args.ide)
    else:
        ide = _detect_ide(project)
        if not ide:
            # no existing config dir — default to claude
            ide = _get_ide("claude")
            print(f"ℹ️  no IDE config found, defaulting to {ide[0]}")
        else:
            print(f"ℹ️  auto-detected IDE: {ide[0]}")

    if args.uninstall:
        if args.ide:
            _uninstall(project, ide)
        else:
            # uninstall from all detected IDEs
            detected = _detect_all_ides(project)
            if not detected:
                detected = [ide]
            for d in detected:
                _uninstall(project, d)
    else:
        _install(project, ide, debug=args.debug)


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        print(f"❌ Python 3.8+ required (current: {sys.version.split()[0]})", file=sys.stderr)
        sys.exit(1)
    main()
