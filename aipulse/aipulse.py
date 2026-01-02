#!/usr/bin/env python3
"""
aipulse — tiny TUI for checking AI coding assistant usage.

Monitors:
  - Claude Code: tokens, messages, sessions from stats-cache.json
  - Gemini CLI: session count from tmp directory
  - Codex: session count from history.jsonl

Keys:
  q      quit
  r      refresh
  j/k    navigate tools
  Enter  show detail view
  Esc    back to overview
"""

import curses
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ToolStats:
    name: str
    available: bool = False
    error: Optional[str] = None
    # Common stats
    total_sessions: int = 0
    total_messages: int = 0
    total_tokens: int = 0
    # Today's stats
    today_sessions: int = 0
    today_messages: int = 0
    today_tokens: int = 0
    # Extra info
    model: str = ""
    extra: Dict[str, str] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    @property
    def status_char(self) -> str:
        if not self.available:
            return "?"
        if self.today_messages > 0:
            return "!"
        return "~"


@dataclass
class AppState:
    tools: List[ToolStats] = field(default_factory=list)
    selected_idx: int = 0
    detail_view: bool = False
    last_msg: str = ""
    last_refresh: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Data Collection
# ─────────────────────────────────────────────────────────────────────────────


def get_claude_stats() -> ToolStats:
    """Parse Claude Code stats from ~/.claude/stats-cache.json"""
    stats = ToolStats(name="Claude Code")
    stats_path = Path.home() / ".claude" / "stats-cache.json"

    if not stats_path.exists():
        stats.error = "stats-cache.json not found"
        return stats

    try:
        with open(stats_path) as f:
            data = json.load(f)

        stats.available = True
        stats.total_sessions = data.get("totalSessions", 0)
        stats.total_messages = data.get("totalMessages", 0)

        # Sum up token usage across models
        model_usage = data.get("modelUsage", {})
        total_in = 0
        total_out = 0
        primary_model = ""
        max_tokens = 0

        for model, usage in model_usage.items():
            in_tok = usage.get("inputTokens", 0)
            out_tok = usage.get("outputTokens", 0)
            total_in += in_tok
            total_out += out_tok
            if in_tok + out_tok > max_tokens:
                max_tokens = in_tok + out_tok
                primary_model = model

        stats.total_tokens = total_in + total_out
        stats.model = primary_model.replace("claude-", "").replace("-20251101", "").replace("-20250929", "")

        # Today's stats
        today = datetime.now().strftime("%Y-%m-%d")
        daily_activity = data.get("dailyActivity", [])
        for day in daily_activity:
            if day.get("date") == today:
                stats.today_messages = day.get("messageCount", 0)
                stats.today_sessions = day.get("sessionCount", 0)
                break

        daily_tokens = data.get("dailyModelTokens", [])
        for day in daily_tokens:
            if day.get("date") == today:
                for tokens in day.get("tokensByModel", {}).values():
                    stats.today_tokens += tokens
                break

        # Extra info
        stats.extra["input_tokens"] = f"{total_in:,}"
        stats.extra["output_tokens"] = f"{total_out:,}"
        stats.extra["first_session"] = data.get("firstSessionDate", "")[:10]

        # Calculate cache stats
        cache_read = sum(u.get("cacheReadInputTokens", 0) for u in model_usage.values())
        cache_create = sum(u.get("cacheCreationInputTokens", 0) for u in model_usage.values())
        if cache_read > 0:
            stats.extra["cache_read"] = f"{cache_read:,}"
        if cache_create > 0:
            stats.extra["cache_created"] = f"{cache_create:,}"

    except (json.JSONDecodeError, KeyError, IOError) as e:
        stats.error = str(e)

    return stats


def get_gemini_stats() -> ToolStats:
    """Estimate Gemini CLI usage from tmp directory session files."""
    stats = ToolStats(name="Gemini CLI")
    gemini_dir = Path.home() / ".gemini"

    if not gemini_dir.exists():
        stats.error = "~/.gemini not found"
        return stats

    tmp_dir = gemini_dir / "tmp"
    if not tmp_dir.exists():
        stats.error = "No session data"
        return stats

    try:
        # Count session files (each hash is a session)
        session_files = list(tmp_dir.iterdir())
        stats.total_sessions = len(session_files)
        stats.available = True

        # Check settings for model
        settings_path = gemini_dir / "settings.json"
        if settings_path.exists():
            with open(settings_path) as f:
                settings = json.load(f)
                stats.model = settings.get("model", "gemini")

        # Count today's sessions by modification time
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for f in session_files:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime >= today_start:
                stats.today_sessions += 1

        stats.extra["data_source"] = "session file count"

    except (IOError, OSError) as e:
        stats.error = str(e)

    return stats


def get_codex_stats() -> ToolStats:
    """Parse Codex usage from ~/.codex/history.jsonl"""
    stats = ToolStats(name="Codex CLI")
    codex_dir = Path.home() / ".codex"

    if not codex_dir.exists():
        stats.error = "~/.codex not found"
        return stats

    history_path = codex_dir / "history.jsonl"
    if not history_path.exists():
        stats.error = "history.jsonl not found"
        return stats

    try:
        sessions = set()
        messages = 0
        today_sessions = set()
        today_messages = 0
        today = datetime.now().strftime("%Y-%m-%d")

        with open(history_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    session_id = entry.get("session_id", "")
                    if session_id:
                        sessions.add(session_id)
                    messages += 1

                    # Check if today
                    ts = entry.get("timestamp", "")
                    if ts and ts.startswith(today):
                        if session_id:
                            today_sessions.add(session_id)
                        today_messages += 1
                except json.JSONDecodeError:
                    continue

        stats.available = True
        stats.total_sessions = len(sessions)
        stats.total_messages = messages
        stats.today_sessions = len(today_sessions)
        stats.today_messages = today_messages

        # Check config for model
        config_path = codex_dir / "config.toml"
        if config_path.exists():
            with open(config_path) as f:
                for line in f:
                    if line.startswith("model"):
                        stats.model = line.split("=")[1].strip().strip('"')
                        break

        stats.extra["data_source"] = "history.jsonl"

    except (IOError, OSError) as e:
        stats.error = str(e)

    return stats


def refresh_all() -> List[ToolStats]:
    """Collect stats from all tools."""
    return [
        get_claude_stats(),
        get_gemini_stats(),
        get_codex_stats(),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# UI Drawing
# ─────────────────────────────────────────────────────────────────────────────

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


def status_color(tool: ToolStats) -> int:
    if not tool.available:
        return curses.color_pair(C_DIM)
    if tool.today_messages > 100 or tool.today_tokens > 100000:
        return curses.color_pair(C_WARN)
    if tool.today_messages > 0:
        return curses.color_pair(C_OK)
    return curses.color_pair(C_DIM)


def format_tokens(n: int) -> str:
    """Format token count in human-readable form."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def draw_overview(stdscr, state: AppState):
    """Draw compact overview of all tools."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y: int, x: int, s: str, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)

    # Header
    ts = time.strftime("%H:%M", time.localtime(state.last_refresh))
    put(0, 1, "aipulse", curses.A_BOLD)
    put(0, w - len(ts) - 1, ts, curses.A_DIM)

    # Separator
    put(1, 0, "-" * (w - 1), curses.A_DIM)

    # Tool list
    list_start = 2
    for i, tool in enumerate(state.tools):
        y = list_start + i * 3  # 3 lines per tool
        if y >= h - 3:
            break

        selected = i == state.selected_idx
        color = status_color(tool)
        attr = curses.A_REVERSE if selected else 0

        # Tool name line
        prefix = ">" if selected else " "
        status_ch = tool.status_char
        name_line = f"{prefix} {status_ch} {tool.name}"
        if tool.model:
            name_line += f" ({tool.model})"
        put(y, 0, name_line[:w - 1], attr | curses.A_BOLD)

        if not tool.available:
            put(y + 1, 5, tool.error or "not available", curses.A_DIM)
        else:
            # Stats line
            stats_parts = []
            if tool.total_sessions > 0:
                stats_parts.append(f"S:{tool.total_sessions}")
            if tool.total_messages > 0:
                stats_parts.append(f"M:{tool.total_messages}")
            if tool.total_tokens > 0:
                stats_parts.append(f"T:{format_tokens(tool.total_tokens)}")
            put(y + 1, 5, " ".join(stats_parts), color)

            # Today line
            today_parts = []
            if tool.today_sessions > 0:
                today_parts.append(f"s:{tool.today_sessions}")
            if tool.today_messages > 0:
                today_parts.append(f"m:{tool.today_messages}")
            if tool.today_tokens > 0:
                today_parts.append(f"t:{format_tokens(tool.today_tokens)}")
            if today_parts:
                put(y + 2, 5, "today: " + " ".join(today_parts), curses.A_DIM)

    # Footer
    if state.last_msg:
        put(h - 2, 1, state.last_msg[:w - 2], curses.A_DIM)
    put(h - 1, 1, "q:quit r:refresh j/k:nav Enter:detail", curses.A_DIM)

    stdscr.refresh()


def draw_detail(stdscr, tool: ToolStats, last_msg: str):
    """Draw detailed view of a single tool."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y: int, x: int, s: str, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)

    color = status_color(tool)

    put(0, 1, f"aipulse: {tool.name}", curses.A_BOLD)
    put(1, 0, "-" * (w - 1), curses.A_DIM)

    if not tool.available:
        put(3, 2, tool.error or "Not available", curses.A_DIM)
        put(h - 1, 1, "q:quit Esc:back", curses.A_DIM)
        stdscr.refresh()
        return

    y = 3
    if tool.model:
        put(y, 2, f"Model: {tool.model}")
        y += 1

    y += 1
    put(y, 2, "All time:", curses.A_BOLD)
    y += 1
    if tool.total_sessions > 0:
        put(y, 4, f"Sessions:  {tool.total_sessions:,}")
        y += 1
    if tool.total_messages > 0:
        put(y, 4, f"Messages:  {tool.total_messages:,}")
        y += 1
    if tool.total_tokens > 0:
        put(y, 4, f"Tokens:    {tool.total_tokens:,}")
        y += 1

    y += 1
    put(y, 2, "Today:", curses.A_BOLD | color)
    y += 1
    put(y, 4, f"Sessions:  {tool.today_sessions:,}")
    y += 1
    put(y, 4, f"Messages:  {tool.today_messages:,}")
    y += 1
    if tool.today_tokens > 0:
        put(y, 4, f"Tokens:    {tool.today_tokens:,}")
        y += 1

    # Extra info
    if tool.extra:
        y += 1
        put(y, 2, "Details:", curses.A_BOLD)
        y += 1
        for key, val in tool.extra.items():
            label = key.replace("_", " ").title()
            put(y, 4, f"{label}: {val}")
            y += 1
            if y >= h - 3:
                break

    ts = time.strftime("%H:%M:%S", time.localtime(tool.updated_at))
    put(h - 2, 2, f"Updated: {ts}", curses.A_DIM)

    if last_msg:
        put(h - 1, 1, last_msg[:w - 2], curses.A_DIM)
    else:
        put(h - 1, 1, "q:quit r:refresh Esc:back", curses.A_DIM)

    stdscr.refresh()


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────


def main_loop(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(500)
    init_colors()

    state = AppState()
    state.tools = refresh_all()
    state.last_refresh = time.time()

    auto_refresh = 60  # seconds

    while True:
        # Draw
        if state.detail_view and state.tools:
            draw_detail(stdscr, state.tools[state.selected_idx], state.last_msg)
        else:
            draw_overview(stdscr, state)

        # Handle input
        ch = stdscr.getch()

        # Auto-refresh
        now = time.time()
        if auto_refresh > 0 and (now - state.last_refresh) > auto_refresh:
            state.tools = refresh_all()
            state.last_refresh = now

        if ch == -1:
            continue

        state.last_msg = ""

        c = chr(ch).lower() if 0 <= ch < 256 else ""

        if c == "q":
            return
        elif ch == 27:  # Escape
            if state.detail_view:
                state.detail_view = False
            else:
                return
        elif c == "r":
            state.tools = refresh_all()
            state.last_refresh = time.time()
            state.last_msg = "Refreshed"
        elif c == "j" or ch == curses.KEY_DOWN:
            if state.tools and not state.detail_view:
                state.selected_idx = (state.selected_idx + 1) % len(state.tools)
        elif c == "k" or ch == curses.KEY_UP:
            if state.tools and not state.detail_view:
                state.selected_idx = (state.selected_idx - 1) % len(state.tools)
        elif ch == 10 or ch == curses.KEY_ENTER:
            if state.tools and not state.detail_view:
                state.detail_view = True


def main():
    try:
        curses.wrapper(main_loop)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
