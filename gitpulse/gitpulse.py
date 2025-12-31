#!/usr/bin/env python3
"""
gitpulse — tiny TUI for "pending push/pull?" + open repo in GitHub Desktop.

Keys:
  q  quit
  r  refresh (no fetch)
  f  fetch + refresh
  g  open this repo in GitHub Desktop (uses `github .`)
"""

import curses
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Tuple

AUTO_REFRESH_INTERVAL = 30  # seconds


@dataclass
class RepoStatus:
    ok: bool
    repo_root: Optional[str] = None
    branch: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    fetch_error: Optional[str] = None
    error: Optional[str] = None
    updated_at: float = 0.0


def run_git(repo_root: str, args: list[str]) -> Tuple[int, str, str]:
    p = subprocess.run(
        ["git", "-C", repo_root, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def discover_repo_root(cwd: str) -> Optional[str]:
    p = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def get_branch(repo_root: str) -> str:
    code, out, _ = run_git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if code == 0 and out:
        return out
    # detached HEAD
    _, sha, _ = run_git(repo_root, ["rev-parse", "--short", "HEAD"])
    return f"(detached @ {sha})" if sha else "(detached)"


def get_upstream(repo_root: str) -> Optional[str]:
    code, out, _ = run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return out if code == 0 and out else None


def get_ahead_behind(repo_root: str) -> Tuple[int, int]:
    code, out, err = run_git(repo_root, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if code != 0 or not out:
        raise RuntimeError(err or "Unable to compute ahead/behind.")
    parts = out.replace("\t", " ").split()
    ahead = int(parts[0])
    behind = int(parts[1])
    return ahead, behind


def get_dirty(repo_root: str) -> bool:
    code, out, _ = run_git(repo_root, ["status", "--porcelain"])
    return code == 0 and bool(out.strip())


def fetch(repo_root: str) -> Optional[str]:
    code, _, err = run_git(repo_root, ["fetch", "--quiet"])
    return None if code == 0 else (err or "git fetch failed")


def status_snapshot(cwd: str, do_fetch: bool) -> RepoStatus:
    repo_root = discover_repo_root(cwd)
    if not repo_root:
        return RepoStatus(ok=False, error="Not inside a git repository.", updated_at=time.time())

    st = RepoStatus(ok=True, repo_root=repo_root, updated_at=time.time())
    st.branch = get_branch(repo_root)
    st.dirty = get_dirty(repo_root)

    if do_fetch:
        st.fetch_error = fetch(repo_root)

    st.upstream = get_upstream(repo_root)
    if st.upstream:
        try:
            st.ahead, st.behind = get_ahead_behind(repo_root)
        except Exception as e:
            st.ok = False
            st.error = str(e)
    else:
        st.ahead = st.behind = 0

    return st


def open_github_desktop(repo_root: str) -> Tuple[bool, str]:
    try:
        p = subprocess.run(["github", "."], cwd=repo_root)
        if p.returncode == 0:
            return True, "Opened in GitHub Desktop via `github .`"
        return False, "Tried `github .` but it returned a non-zero exit code."
    except FileNotFoundError:
        if platform.system() == "Darwin":
            p = subprocess.run(["open", "-a", "GitHub Desktop", repo_root])
            if p.returncode == 0:
                return True, "Opened in GitHub Desktop via macOS `open -a`"
        return (
            False,
            "GitHub Desktop CLI not found. Install via: GitHub Desktop > Menu > Install Command Line Tool",
        )


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)   # clean/ok
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # ahead (pending push)
    curses.init_pair(3, curses.COLOR_RED, -1)     # behind/dirty


def draw(stdscr, st: RepoStatus, last_msg: str):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    COLOR_OK = curses.color_pair(1)
    COLOR_WARN = curses.color_pair(2)
    COLOR_BAD = curses.color_pair(3)

    def put(y: int, x: int, s: str, attr=0):
        if y < 0 or y >= h:
            return
        stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)

    title = "gitpulse"
    put(0, 2, title, curses.A_BOLD)

    if not st.ok:
        put(2, 2, st.error or "Error", curses.A_BOLD | COLOR_BAD)
        put(h - 2, 2, "q quit", curses.A_DIM)
        stdscr.refresh()
        return

    put(2, 2, f"Repo: {st.repo_root}")
    put(3, 2, f"Branch: {st.branch}")
    put(4, 2, f"Upstream: {st.upstream or '(none set)'}")

    push_pending = st.ahead > 0
    pull_pending = st.behind > 0
    dirty = st.dirty

    push_attr = (curses.A_BOLD | COLOR_WARN) if push_pending else COLOR_OK
    pull_attr = (curses.A_BOLD | COLOR_BAD) if pull_pending else COLOR_OK
    dirty_attr = (curses.A_BOLD | COLOR_BAD) if dirty else COLOR_OK

    put(6, 2, f"Pending push: {'YES' if push_pending else 'no '}  (ahead {st.ahead})", push_attr)
    put(7, 2, f"Pending pull: {'YES' if pull_pending else 'no '}  (behind {st.behind})", pull_attr)
    put(8, 2, f"Working tree: {'DIRTY' if dirty else 'clean'}", dirty_attr)

    if st.fetch_error:
        put(10, 2, f"Fetch: {st.fetch_error}", curses.A_DIM)

    put(12, 2, f"Last update: {time.strftime('%H:%M:%S', time.localtime(st.updated_at))}", curses.A_DIM)

    if last_msg:
        put(h - 3, 2, last_msg[: max(0, w - 4)], curses.A_DIM)

    put(h - 2, 2, "q quit   r refresh   f fetch   g GitHub Desktop", curses.A_DIM)
    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(150)
    init_colors()

    cwd = os.getcwd()
    st = status_snapshot(cwd, do_fetch=False)
    last_msg = ""
    last_auto = time.time()

    while True:
        draw(stdscr, st, last_msg)
        ch = stdscr.getch()

        # Auto-refresh every N seconds
        if time.time() - last_auto > AUTO_REFRESH_INTERVAL:
            st = status_snapshot(cwd, do_fetch=False)
            last_auto = time.time()

        if ch == -1:
            continue

        c = chr(ch).lower() if 0 <= ch < 256 else ""
        if c == "q":
            return
        elif c == "r":
            st = status_snapshot(cwd, do_fetch=False)
            last_msg = "Refreshed."
            last_auto = time.time()
        elif c == "f":
            st = status_snapshot(cwd, do_fetch=True)
            last_msg = "Fetched + refreshed."
            last_auto = time.time()
        elif c == "g":
            if st.repo_root:
                ok, msg = open_github_desktop(st.repo_root)
                last_msg = msg if ok else f"Could not open: {msg}"
            else:
                last_msg = "No repo root detected."
        else:
            last_msg = "Keys: q r f g"


if __name__ == "__main__":
    curses.wrapper(main)
