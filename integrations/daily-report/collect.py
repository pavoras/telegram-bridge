#!/usr/bin/env python3
"""Collect the facts for the daily report as plain text (the scheduled task turns them into a report).

Usage: collect.py --projects DIR [--tg PATH_TO_TG_PY]
Sections: window, commits per repository (after `git fetch`), Codex weekly quota, relay status
(open questions, presence, host status). Anything that cannot be read is reported as "unknown".
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_WINDOW = 24 * 3600
MAX_WINDOW = 48 * 3600
MAX_COMMITS = 12
QUOTA = re.compile(r'"used_percent":\s*([\d.]+),\s*"window_minutes":\s*10080,\s*"resets_at":\s*(\d+)')


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def when(ts: float) -> str:
    moment = datetime.fromtimestamp(ts)
    return f"{WEEKDAYS[moment.weekday()]} {moment:%Y-%m-%d %H:%M}"


def run(args: list, cwd: Optional[Path] = None, timeout: int = 90) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200])
    return result.stdout


def repositories(projects: Path) -> list:
    repos = [projects] if (projects / ".git").exists() else []
    return repos + sorted(p for p in projects.iterdir() if p.is_dir() and (p / ".git").exists())


def commits(repo: Path, since: float) -> list:
    """Commits on any branch (local or remote) since the timestamp, newest first, without duplicates."""
    try:
        run(["git", "fetch", "-q", "--all", "--prune"], cwd=repo)
        fetched = True
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        fetched = False
    out = run(["git", "log", "--all", f"--since=@{int(since)}", "--format=%h\t%ad\t%s",
               "--date=format:%Y-%m-%d %H:%M"], cwd=repo)
    lines = [line for line in out.splitlines() if line.strip()]
    return lines if fetched else lines + ["(git fetch failed: local state only)"]


def codex_quota(home: Path) -> str:
    sessions = home / ".codex" / "sessions"
    files = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime) if sessions.exists() else []
    for path in reversed(files[-5:]):
        matches = QUOTA.findall(path.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace"))
        if matches:
            used, resets = matches[-1]
            return f"{float(used):.0f} % of the week used, resets {when(int(resets))} (as of {when(path.stat().st_mtime)})"
    return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--projects", required=True)
    parser.add_argument("--tg", default=str(Path.home() / ".claude" / "skills" / "telegram" / "tg.py"))
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    now = time.time()

    try:
        status = json.loads(run([sys.executable, args.tg, "status"], timeout=60))
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        status = None
        print(f"Relay status: unknown ({e})")
    last = (status or {}).get("last_daily_at")
    since = max(now - MAX_WINDOW, last) if last else now - DEFAULT_WINDOW
    print(f"Window: since {when(since)}")

    print("\n## Commits (all branches, after git fetch)")
    for repo in repositories(Path(args.projects)):
        try:
            found = commits(repo, since)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            print(f"- {repo.name}: unknown ({e})")
            continue
        if found:
            print(f"- {repo.name}: {len([c for c in found if not c.startswith('(')])} commits")
            for line in found[:MAX_COMMITS]:
                print(f"    {line}")

    print("\n## Codex quota")
    print(codex_quota(Path.home()))

    print("\n## Relay")
    if status is not None:
        print(f"Presence: {status.get('presence')}")
        open_questions = status.get("open") or []
        print(f"Open questions: {len(open_questions)}")
        for q in open_questions:
            age = int((now - q["created_at"]) // 3600)
            print(f"- #{q['code']} {q.get('project') or ''} · {q.get('title') or ''} ({age} h ago)")
        host = status.get("host_status")
        if host:
            print(f"Host status ({host['age_s'] // 60} min ago):")
            for key, value in host["items"].items():
                print(f"- {key}: {value}")
        else:
            print("Host status: unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
