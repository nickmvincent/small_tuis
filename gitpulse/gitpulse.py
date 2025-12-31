#!/usr/bin/env python3
"""
gitpulse — tiny TUI for monitoring git repos in a small tmux pane.

Modes:
  - Single repo: Run inside a git repo for detailed view
  - Multi-repo: Run in a parent directory to scan all repos

Keys:
  q      quit
  r      refresh (no fetch)
  f      fetch all + refresh
  g      open selected repo in GitHub Desktop
  j/k    navigate repos (multi-repo mode)
  Enter  switch to detailed view of selected repo
  Esc    back to multi-repo view
"""

import argparse
import curses
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "auto_refresh_seconds": 30,
    "auto_fetch_seconds": 300,  # 0 to disable
    "scan_depth": 2,  # How deep to look for .git directories
    "ignore_repos": [],  # Repo names to skip
}


def load_config() -> dict:
    config_paths = [
        Path.home() / ".config" / "gitpulse.json",
        Path.home() / ".gitpulse.json",
    ]
    for p in config_paths:
        if p.exists():
            try:
                with open(p) as f:
                    user_config = json.load(f)
                return {**DEFAULT_CONFIG, **user_config}
            except (json.JSONDecodeError, IOError):
                pass
    return DEFAULT_CONFIG.copy()


CONFIG = load_config()

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RepoStatus:
    name: str
    path: str
    ok: bool = True
    branch: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: int = 0  # Count of modified/staged files
    untracked: int = 0  # Count of untracked files
    stashes: int = 0
    error: Optional[str] = None
    fetch_error: Optional[str] = None
    updated_at: float = field(default_factory=time.time)

    @property
    def needs_attention(self) -> bool:
        return self.ahead > 0 or self.behind > 0 or self.dirty > 0

    @property
    def status_char(self) -> str:
        """Single character status indicator."""
        if not self.ok:
            return "✗"
        if self.dirty > 0:
            return "!"
        if self.upstream is None:
            return "?"
        if self.ahead > 0 and self.behind > 0:
            return "⇅"
        if self.behind > 0:
            return "↓"
        if self.ahead > 0:
            return "↑"
        return "✓"


@dataclass
class AppState:
    repos: List[RepoStatus] = field(default_factory=list)
    selected_idx: int = 0
    detail_view: bool = False  # True = single repo detail, False = multi-repo list
    last_msg: str = ""
    last_refresh: float = 0.0
    last_fetch: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Git operations
# ─────────────────────────────────────────────────────────────────────────────


def run_git(repo_path: str, args: List[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["git", "-C", repo_path, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def find_repos(root: str, max_depth: int = 2) -> List[str]:
    """Find all git repositories under root, up to max_depth levels."""
    repos = []
    root_path = Path(root).resolve()

    def scan(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            git_dir = path / ".git"
            if git_dir.exists():
                repos.append(str(path))
                return  # Don't scan inside a repo
            for child in path.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    scan(child, depth + 1)
        except PermissionError:
            pass

    # Check if root itself is a repo
    if (root_path / ".git").exists():
        return [str(root_path)]

    scan(root_path, 0)
    return sorted(repos)


def get_repo_status(repo_path: str, do_fetch: bool = False) -> RepoStatus:
    """Get complete status for a single repo."""
    name = Path(repo_path).name
    st = RepoStatus(name=name, path=repo_path)

    # Branch
    code, out, _ = run_git(repo_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if code == 0 and out:
        st.branch = out
    else:
        code, sha, _ = run_git(repo_path, ["rev-parse", "--short", "HEAD"])
        st.branch = f"@{sha[:7]}" if sha else "???"

    # Fetch (if requested)
    if do_fetch:
        code, _, err = run_git(repo_path, ["fetch", "--quiet", "--all"])
        if code != 0:
            st.fetch_error = err or "fetch failed"

    # Upstream
    code, out, _ = run_git(
        repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    st.upstream = out if code == 0 and out else None

    # Ahead/behind
    if st.upstream:
        code, out, err = run_git(
            repo_path, ["rev-list", "--left-right", "--count", "HEAD...@{u}"]
        )
        if code == 0 and out:
            parts = out.replace("\t", " ").split()
            st.ahead = int(parts[0]) if len(parts) > 0 else 0
            st.behind = int(parts[1]) if len(parts) > 1 else 0

    # Working tree status (dirty + untracked)
    code, out, _ = run_git(repo_path, ["status", "--porcelain"])
    if code == 0 and out:
        for line in out.splitlines():
            if line.startswith("??"):
                st.untracked += 1
            else:
                st.dirty += 1

    # Stash count
    code, out, _ = run_git(repo_path, ["stash", "list"])
    if code == 0 and out:
        st.stashes = len(out.splitlines())

    st.updated_at = time.time()
    return st


def refresh_all(repos: List[str], do_fetch: bool = False) -> List[RepoStatus]:
    """Refresh status for all repos."""
    ignore = set(CONFIG.get("ignore_repos", []))
    results = []
    for repo_path in repos:
        name = Path(repo_path).name
        if name in ignore:
            continue
        results.append(get_repo_status(repo_path, do_fetch))
    # Sort: needs attention first, then alphabetically
    results.sort(key=lambda r: (not r.needs_attention, r.name.lower()))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Desktop integration
# ─────────────────────────────────────────────────────────────────────────────


def open_github_desktop(repo_path: str) -> Tuple[bool, str]:
    # Try `github .` CLI first
    try:
        p = subprocess.run(["github", "."], cwd=repo_path, capture_output=True)
        if p.returncode == 0:
            return True, "Opened in GitHub Desktop"
    except FileNotFoundError:
        pass  # Fall through to macOS fallback

    # Fallback for macOS: use `open -a`
    if platform.system() == "Darwin":
        try:
            p = subprocess.run(
                ["open", "-a", "GitHub Desktop", repo_path], capture_output=True
            )
            if p.returncode == 0:
                return True, "Opened in GitHub Desktop"
        except FileNotFoundError:
            pass

    return False, "GitHub Desktop not found"


# ─────────────────────────────────────────────────────────────────────────────
# UI Drawing
# ─────────────────────────────────────────────────────────────────────────────

# Color pairs
C_OK = 1
C_WARN = 2
C_BAD = 3
C_DIM = 4


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_BAD, curses.COLOR_RED, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)


def status_color(st: RepoStatus) -> int:
    if not st.ok or st.dirty > 0:
        return curses.color_pair(C_BAD)
    if st.behind > 0:
        return curses.color_pair(C_BAD)
    if st.ahead > 0:
        return curses.color_pair(C_WARN)
    if st.upstream is None:
        return curses.color_pair(C_WARN)
    return curses.color_pair(C_OK)


def draw_multi_repo(stdscr, state: AppState):
    """Draw compact multi-repo list view."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y: int, x: int, s: str, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)

    # Header
    ts = time.strftime("%H:%M", time.localtime(state.last_refresh))
    header = f"gitpulse ({len(state.repos)})"
    put(0, 1, header, curses.A_BOLD)
    put(0, w - len(ts) - 1, ts, curses.A_DIM)

    # Separator
    put(1, 0, "─" * (w - 1), curses.A_DIM)

    # Repo list
    list_start = 2
    list_height = h - 4  # Leave room for footer

    for i, repo in enumerate(state.repos):
        if i >= list_height:
            break

        y = list_start + i
        selected = i == state.selected_idx
        color = status_color(repo)

        # Selection indicator
        prefix = ">" if selected else " "
        attr = curses.A_REVERSE if selected else 0

        # Status character
        status_ch = repo.status_char

        # Build the line
        # Format: > ✓ reponame     main  +3 -2 *5 ?2 S1
        name_width = min(14, w - 25)  # Leave space for status info
        name = repo.name[:name_width].ljust(name_width)
        branch = repo.branch[:8].ljust(8)

        line = f"{prefix} {status_ch} {name} {branch}"

        # Add counts if any
        extras = []
        if repo.ahead > 0:
            extras.append(f"+{repo.ahead}")
        if repo.behind > 0:
            extras.append(f"-{repo.behind}")
        if repo.dirty > 0:
            extras.append(f"*{repo.dirty}")
        if repo.untracked > 0:
            extras.append(f"?{repo.untracked}")
        if repo.stashes > 0:
            extras.append(f"S{repo.stashes}")

        if extras:
            line += " " + " ".join(extras)

        put(y, 0, line[:w - 1], attr | color)

    # Footer
    footer_y = h - 2
    if state.last_msg:
        put(footer_y - 1, 1, state.last_msg[:w - 2], curses.A_DIM)

    keys = "q:quit r:refresh f:fetch g:github j/k:nav"
    put(footer_y, 1, keys[:w - 2], curses.A_DIM)

    stdscr.refresh()


def draw_detail(stdscr, repo: RepoStatus, last_msg: str):
    """Draw detailed single-repo view."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y: int, x: int, s: str, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)

    color = status_color(repo)

    put(0, 1, f"gitpulse: {repo.name}", curses.A_BOLD)
    put(1, 0, "─" * (w - 1), curses.A_DIM)

    if not repo.ok:
        put(3, 2, repo.error or "Error", curses.A_BOLD | curses.color_pair(C_BAD))
        put(h - 2, 1, "q:quit Esc:back", curses.A_DIM)
        stdscr.refresh()
        return

    put(3, 2, f"Path: {repo.path}")
    put(4, 2, f"Branch: {repo.branch}")
    put(5, 2, f"Upstream: {repo.upstream or '(none)'}")

    y = 7
    # Push/pull status
    if repo.ahead > 0:
        put(y, 2, f"↑ {repo.ahead} commits to push", curses.A_BOLD | curses.color_pair(C_WARN))
        y += 1
    if repo.behind > 0:
        put(y, 2, f"↓ {repo.behind} commits to pull", curses.A_BOLD | curses.color_pair(C_BAD))
        y += 1
    if repo.dirty > 0:
        put(y, 2, f"* {repo.dirty} modified files", curses.A_BOLD | curses.color_pair(C_BAD))
        y += 1
    if repo.untracked > 0:
        put(y, 2, f"? {repo.untracked} untracked files", curses.color_pair(C_WARN))
        y += 1
    if repo.stashes > 0:
        put(y, 2, f"S {repo.stashes} stashed changes", curses.color_pair(C_WARN))
        y += 1

    if repo.ahead == 0 and repo.behind == 0 and repo.dirty == 0:
        put(y, 2, "✓ Clean and synced", curses.color_pair(C_OK))
        y += 1

    if repo.fetch_error:
        put(y + 1, 2, f"Fetch error: {repo.fetch_error}", curses.A_DIM)

    ts = time.strftime("%H:%M:%S", time.localtime(repo.updated_at))
    put(h - 3, 2, f"Updated: {ts}", curses.A_DIM)

    if last_msg:
        put(h - 2, 1, last_msg[:w - 2], curses.A_DIM)
    else:
        put(h - 2, 1, "q:quit r:refresh f:fetch g:github Esc:back", curses.A_DIM)

    stdscr.refresh()


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────


def main_loop(stdscr, root_path: str):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    # Find repos
    repo_paths = find_repos(root_path, CONFIG.get("scan_depth", 2))
    if not repo_paths:
        stdscr.addstr(0, 0, f"No git repos found in {root_path}")
        stdscr.addstr(1, 0, "Press any key to exit")
        stdscr.nodelay(False)
        stdscr.getch()
        return

    state = AppState()
    state.repos = refresh_all(repo_paths, do_fetch=False)
    state.last_refresh = time.time()
    state.detail_view = len(repo_paths) == 1  # Auto detail view for single repo

    auto_refresh = CONFIG.get("auto_refresh_seconds", 30)
    auto_fetch = CONFIG.get("auto_fetch_seconds", 300)

    while True:
        # Draw
        if state.detail_view and state.repos:
            draw_detail(stdscr, state.repos[state.selected_idx], state.last_msg)
        else:
            draw_multi_repo(stdscr, state)

        # Handle input
        ch = stdscr.getch()

        # Auto-refresh
        now = time.time()
        if auto_refresh > 0 and (now - state.last_refresh) > auto_refresh:
            state.repos = refresh_all(repo_paths, do_fetch=False)
            state.last_refresh = now

        # Auto-fetch (less frequent)
        if auto_fetch > 0 and (now - state.last_fetch) > auto_fetch:
            state.repos = refresh_all(repo_paths, do_fetch=True)
            state.last_fetch = now
            state.last_refresh = now
            state.last_msg = "Auto-fetched"

        if ch == -1:
            continue

        state.last_msg = ""  # Clear message on any key

        c = chr(ch).lower() if 0 <= ch < 256 else ""

        if c == "q":
            return
        elif ch == 27:  # Escape
            if state.detail_view and len(repo_paths) > 1:
                state.detail_view = False
            else:
                return
        elif c == "r":
            state.repos = refresh_all(repo_paths, do_fetch=False)
            state.last_refresh = time.time()
            state.last_msg = "Refreshed"
        elif c == "f":
            state.last_msg = "Fetching..."
            if state.detail_view:
                draw_detail(stdscr, state.repos[state.selected_idx], state.last_msg)
            else:
                draw_multi_repo(stdscr, state)
            state.repos = refresh_all(repo_paths, do_fetch=True)
            state.last_refresh = time.time()
            state.last_fetch = time.time()
            state.last_msg = "Fetched all"
        elif c == "g":
            if state.repos:
                repo = state.repos[state.selected_idx]
                ok, msg = open_github_desktop(repo.path)
                state.last_msg = msg
        elif c == "j" or ch == curses.KEY_DOWN:
            if state.repos and not state.detail_view:
                state.selected_idx = (state.selected_idx + 1) % len(state.repos)
        elif c == "k" or ch == curses.KEY_UP:
            if state.repos and not state.detail_view:
                state.selected_idx = (state.selected_idx - 1) % len(state.repos)
        elif ch == 10 or ch == curses.KEY_ENTER:  # Enter
            if state.repos and not state.detail_view:
                state.detail_view = True


def main():
    parser = argparse.ArgumentParser(description="Git status TUI for tmux panes")
    parser.add_argument(
        "path",
        nargs="?",
        default=os.getcwd(),
        help="Directory to scan for repos (default: current directory)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help="How deep to scan for repos (default: from config or 2)",
    )
    args = parser.parse_args()

    if args.depth is not None:
        CONFIG["scan_depth"] = args.depth

    try:
        curses.wrapper(lambda stdscr: main_loop(stdscr, args.path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
