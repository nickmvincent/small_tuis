#!/usr/bin/env python3
"""
aipulse — tiny TUI for checking AI coding assistant usage & rate limits.

Monitors:
  - Claude Code: tokens, messages, sessions + live rate limits via API
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
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RateLimit:
    name: str
    utilization: float  # 0-100 percentage used
    resets_at: Optional[str] = None  # ISO timestamp or human-readable

    @property
    def remaining(self) -> float:
        return max(0, 100 - self.utilization)


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
    # Rate limits
    rate_limits: List[RateLimit] = field(default_factory=list)
    rate_limit_error: Optional[str] = None
    # Extra info
    model: str = ""
    extra: Dict[str, str] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    @property
    def status_char(self) -> str:
        if not self.available:
            return "?"
        # Check rate limits
        for rl in self.rate_limits:
            if rl.utilization >= 90:
                return "X"
            if rl.utilization >= 70:
                return "!"
        if self.today_messages > 0:
            return "*"
        return "~"


@dataclass
class AppState:
    tools: List[ToolStats] = field(default_factory=list)
    selected_idx: int = 0
    detail_view: bool = False
    last_msg: str = ""
    last_refresh: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# API Helpers
# ─────────────────────────────────────────────────────────────────────────────


def fetch_json(url: str, headers: dict, timeout: float = 5.0) -> dict:
    """Fetch JSON from URL with headers."""
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def parse_reset_time(iso_str: Optional[str]) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    if not iso_str:
        return ""
    try:
        # Parse ISO format
        if "+" in iso_str:
            iso_str = iso_str.split("+")[0]
        if "." in iso_str:
            iso_str = iso_str.split(".")[0]
        dt = datetime.fromisoformat(iso_str.replace("Z", ""))
        now = datetime.utcnow()
        diff = dt - now

        if diff.total_seconds() < 0:
            return "now"
        elif diff.total_seconds() < 3600:
            return f"{int(diff.total_seconds() / 60)}m"
        elif diff.total_seconds() < 86400:
            return f"{int(diff.total_seconds() / 3600)}h"
        else:
            return f"{int(diff.total_seconds() / 86400)}d"
    except Exception:
        return iso_str[:16] if iso_str else ""


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code
# ─────────────────────────────────────────────────────────────────────────────


CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def refresh_claude_token(creds_path: Path, refresh_token: str) -> Optional[str]:
    """Refresh Claude OAuth token and update credentials file."""
    try:
        data = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
        }).encode()

        req = urllib.request.Request(
            "https://console.anthropic.com/v1/oauth/token",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "aipulse/1.0",
                "Accept": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())

        new_token = result.get("access_token")
        new_refresh = result.get("refresh_token")
        expires_in = result.get("expires_in", 28800)

        if new_token:
            # Update credentials file
            with open(creds_path) as f:
                creds = json.load(f)

            creds["claudeAiOauth"]["accessToken"] = new_token
            creds["claudeAiOauth"]["expiresAt"] = int((time.time() + expires_in) * 1000)
            if new_refresh:
                creds["claudeAiOauth"]["refreshToken"] = new_refresh

            with open(creds_path, "w") as f:
                json.dump(creds, f)

            return new_token

    except urllib.error.HTTPError as e:
        # 400 = refresh token expired/invalid, need to re-auth
        return None
    except Exception:
        return None


def get_claude_rate_limits() -> tuple[List[RateLimit], Optional[str]]:
    """Fetch Claude rate limits from API."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return [], "no credentials"

    try:
        with open(creds_path) as f:
            creds = json.load(f)

        oauth = creds.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        refresh_token = oauth.get("refreshToken")
        expires_at = oauth.get("expiresAt", 0)

        if not token:
            return [], "no token"

        # Check if expired and try to refresh
        if expires_at and expires_at / 1000 < time.time():
            if refresh_token:
                new_token = refresh_claude_token(creds_path, refresh_token)
                if new_token:
                    token = new_token
                else:
                    return [], "re-auth needed (claude logout)"
            else:
                return [], "token expired"

        # Fetch usage
        data = fetch_json(
            "https://api.anthropic.com/api/oauth/usage",
            {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )

        limits = []
        if "five_hour" in data and data["five_hour"]:
            fh = data["five_hour"]
            limits.append(RateLimit(
                name="5h limit",
                utilization=fh.get("utilization", 0),
                resets_at=parse_reset_time(fh.get("resets_at")),
            ))
        if "seven_day" in data and data["seven_day"]:
            sd = data["seven_day"]
            limits.append(RateLimit(
                name="Weekly",
                utilization=sd.get("utilization", 0),
                resets_at=parse_reset_time(sd.get("resets_at")),
            ))
        if "seven_day_opus" in data and data["seven_day_opus"]:
            op = data["seven_day_opus"]
            if op.get("utilization", 0) > 0 or op.get("resets_at"):
                limits.append(RateLimit(
                    name="Opus",
                    utilization=op.get("utilization", 0),
                    resets_at=parse_reset_time(op.get("resets_at")),
                ))

        return limits, None

    except urllib.error.HTTPError as e:
        if e.code == 401:
            return [], "token expired"
        return [], f"HTTP {e.code}"
    except Exception as e:
        return [], str(e)[:30]


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

        # Get rate limits
        stats.rate_limits, stats.rate_limit_error = get_claude_rate_limits()

    except (json.JSONDecodeError, KeyError, IOError) as e:
        stats.error = str(e)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────────────────────────────────────


def get_gemini_rate_limits() -> tuple[List[RateLimit], Optional[str]]:
    """Try to get Gemini rate limits from Google AI API."""
    creds_path = Path.home() / ".gemini" / "oauth_creds.json"
    if not creds_path.exists():
        return [], "no credentials"

    try:
        with open(creds_path) as f:
            creds = json.load(f)

        token = creds.get("access_token")
        if not token:
            return [], "no token"

        # Try Google AI usage endpoint (may not exist)
        # This is speculative - Google may not expose this
        try:
            data = fetch_json(
                "https://generativelanguage.googleapis.com/v1beta/usage",
                {"Authorization": f"Bearer {token}"},
                timeout=3.0,
            )
            # Parse if successful
            return [], None
        except urllib.error.HTTPError:
            return [], "no API"
        except Exception:
            return [], "no API"

    except Exception as e:
        return [], str(e)[:20]


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

        stats.extra["data_source"] = "session files"

        # Try to get rate limits
        stats.rate_limits, stats.rate_limit_error = get_gemini_rate_limits()

    except (IOError, OSError) as e:
        stats.error = str(e)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Codex CLI
# ─────────────────────────────────────────────────────────────────────────────


def get_codex_session_data(codex_dir: Path) -> tuple[int, int, List[RateLimit], Optional[str]]:
    """Parse Codex session files for token counts and rate limits."""
    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.exists():
        return 0, 0, [], "no sessions dir"

    total_tokens = 0
    today_tokens = 0
    latest_rate_limits = []
    latest_timestamp = ""
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        # Find all session files, sorted by date (newest first)
        # Structure: sessions/YYYY/MM/DD/*.jsonl
        session_files = []
        for year_dir in sorted(sessions_dir.iterdir(), reverse=True):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir(), reverse=True):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    for f in sorted(day_dir.iterdir(), reverse=True):
                        if f.suffix == ".jsonl":
                            session_files.append(f)

        # Process session files (limit to recent ones for performance)
        for session_file in session_files[:50]:
            try:
                with open(session_file) as f:
                    last_tokens = 0
                    session_date = ""
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("type") != "event_msg":
                                continue
                            payload = entry.get("payload", {})
                            if payload.get("type") != "token_count":
                                continue

                            timestamp = entry.get("timestamp", "")

                            # Get token info (last one wins - they accumulate)
                            info = payload.get("info")
                            if info:
                                last_tokens = info.get("total_token_usage", {}).get("total_tokens", 0)
                                session_date = timestamp[:10]

                            # Get rate limits from most recent entry
                            if timestamp > latest_timestamp:
                                latest_timestamp = timestamp
                                rate_limits_data = payload.get("rate_limits", {})
                                if rate_limits_data:
                                    latest_rate_limits = []
                                    primary = rate_limits_data.get("primary", {})
                                    if primary:
                                        resets_at = primary.get("resets_at")
                                        reset_str = ""
                                        if resets_at:
                                            diff = resets_at - time.time()
                                            if diff > 0:
                                                if diff < 3600:
                                                    reset_str = f"{int(diff/60)}m"
                                                else:
                                                    reset_str = f"{int(diff/3600)}h"
                                        latest_rate_limits.append(RateLimit(
                                            name="5h limit",
                                            utilization=primary.get("used_percent", 0),
                                            resets_at=reset_str,
                                        ))
                                    secondary = rate_limits_data.get("secondary", {})
                                    if secondary:
                                        resets_at = secondary.get("resets_at")
                                        reset_str = ""
                                        if resets_at:
                                            diff = resets_at - time.time()
                                            if diff > 0:
                                                days = int(diff / 86400)
                                                if days > 0:
                                                    reset_str = f"{days}d"
                                                else:
                                                    reset_str = f"{int(diff/3600)}h"
                                        latest_rate_limits.append(RateLimit(
                                            name="Weekly",
                                            utilization=secondary.get("used_percent", 0),
                                            resets_at=reset_str,
                                        ))

                        except json.JSONDecodeError:
                            continue

                    # Add this session's tokens to totals
                    total_tokens += last_tokens
                    if session_date == today:
                        today_tokens += last_tokens
            except IOError:
                continue

        return total_tokens, today_tokens, latest_rate_limits, None

    except Exception as e:
        return 0, 0, [], str(e)[:30]


def get_codex_stats() -> ToolStats:
    """Parse Codex usage from session files and history."""
    stats = ToolStats(name="Codex CLI")
    codex_dir = Path.home() / ".codex"

    if not codex_dir.exists():
        stats.error = "~/.codex not found"
        return stats

    # Get session/message counts from history
    history_path = codex_dir / "history.jsonl"
    if history_path.exists():
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

                        # Check if today (using ts field which is unix timestamp)
                        ts = entry.get("ts", 0)
                        if ts:
                            entry_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                            if entry_date == today:
                                if session_id:
                                    today_sessions.add(session_id)
                                today_messages += 1
                    except (json.JSONDecodeError, ValueError):
                        continue

            stats.available = True
            stats.total_sessions = len(sessions)
            stats.total_messages = messages
            stats.today_sessions = len(today_sessions)
            stats.today_messages = today_messages
        except IOError:
            pass

    # Get token counts and rate limits from session files
    total_tokens, today_tokens, rate_limits, error = get_codex_session_data(codex_dir)
    stats.total_tokens = total_tokens
    stats.today_tokens = today_tokens
    stats.rate_limits = rate_limits
    if error and not rate_limits:
        stats.rate_limit_error = error
    stats.available = True

    # Check config for model
    config_path = codex_dir / "config.toml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                for line in f:
                    if line.startswith("model"):
                        stats.model = line.split("=")[1].strip().strip('"')
                        break
        except IOError:
            pass

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
    # Check rate limits first
    for rl in tool.rate_limits:
        if rl.utilization >= 90:
            return curses.color_pair(C_BAD)
        if rl.utilization >= 70:
            return curses.color_pair(C_WARN)
    if tool.today_messages > 100 or tool.today_tokens > 100000:
        return curses.color_pair(C_WARN)
    if tool.today_messages > 0 or tool.rate_limits:
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


def draw_progress_bar(pct_remaining: float, width: int = 20) -> str:
    """Draw a progress bar showing remaining capacity."""
    filled = int(width * pct_remaining / 100)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def limit_color(rl: RateLimit) -> int:
    """Get color for a rate limit based on utilization."""
    if rl.utilization >= 90:
        return curses.color_pair(C_BAD)
    if rl.utilization >= 70:
        return curses.color_pair(C_WARN)
    return curses.color_pair(C_OK)


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

    # Tool list - dynamic height per tool
    y = 2
    for i, tool in enumerate(state.tools):
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
        y += 1

        if not tool.available:
            put(y, 5, tool.error or "not available", curses.A_DIM)
            y += 2
            continue

        # Rate limits (most important info)
        if tool.rate_limits:
            for rl in tool.rate_limits:
                bar_width = min(15, w - 30)
                bar = draw_progress_bar(rl.remaining, bar_width)
                reset_str = f" (resets {rl.resets_at})" if rl.resets_at else ""
                limit_str = f"{rl.name}: {bar} {rl.remaining:.0f}%{reset_str}"
                put(y, 5, limit_str[:w - 6], limit_color(rl))
                y += 1
        elif tool.rate_limit_error:
            put(y, 5, f"limits: {tool.rate_limit_error}", curses.A_DIM)
            y += 1

        # Stats line
        stats_parts = []
        if tool.total_sessions > 0:
            stats_parts.append(f"S:{tool.total_sessions}")
        if tool.total_messages > 0:
            stats_parts.append(f"M:{tool.total_messages}")
        if tool.total_tokens > 0:
            stats_parts.append(f"T:{format_tokens(tool.total_tokens)}")
        if stats_parts:
            put(y, 5, " ".join(stats_parts), curses.A_DIM)
            y += 1

        y += 1  # Spacing between tools

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

    # Rate limits section
    y += 1
    put(y, 2, "Rate Limits:", curses.A_BOLD)
    y += 1
    if tool.rate_limits:
        for rl in tool.rate_limits:
            bar_width = min(20, w - 35)
            bar = draw_progress_bar(rl.remaining, bar_width)
            put(y, 4, f"{rl.name:12} {bar} {rl.remaining:5.1f}% left", limit_color(rl))
            y += 1
            if rl.resets_at:
                put(y, 18, f"resets in {rl.resets_at}", curses.A_DIM)
                y += 1
    elif tool.rate_limit_error:
        put(y, 4, tool.rate_limit_error, curses.A_DIM)
        y += 1
    else:
        put(y, 4, "No data", curses.A_DIM)
        y += 1

    # All time stats
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

    # Today stats
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
    if tool.extra and y < h - 5:
        y += 1
        put(y, 2, "Details:", curses.A_BOLD)
        y += 1
        for key, val in tool.extra.items():
            if y >= h - 3:
                break
            label = key.replace("_", " ").title()
            put(y, 4, f"{label}: {val}")
            y += 1

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
