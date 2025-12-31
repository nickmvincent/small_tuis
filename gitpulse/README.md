# gitpulse

Tiny TUI for monitoring git repos. Designed for a small tmux pane in your dev setup.

```
┌────────────────────────────────────┐
│ gitpulse (5)              14:32    │
│ ──────────────────────────────────│
│ > ! agentwatch   main     *3 ?2   │
│   ↓ granular     feat-x   -2      │
│   ↑ extenote     main     +5      │
│   ✓ small_tuis   main             │
│   ? ulttrack     main             │
│                                    │
│ q:quit r:refresh f:fetch j/k:nav   │
└────────────────────────────────────┘
```

## Features

- **Multi-repo monitoring**: Scan a directory for all git repos
- **Compact list view**: See all repos at a glance
- **Status indicators**: `✓` clean, `↑` ahead, `↓` behind, `!` dirty, `?` no upstream
- **Detailed counts**: `+N` ahead, `-N` behind, `*N` modified, `?N` untracked, `SN` stashes
- **Auto-refresh**: Polls every 30s, auto-fetches every 5m
- **Smart sorting**: Repos needing attention float to top
- **GitHub Desktop**: Press `g` to open selected repo

## Status Icons

| Icon | Meaning |
|------|---------|
| `✓` | Clean and synced with upstream |
| `↑` | Ahead of upstream (need to push) |
| `↓` | Behind upstream (need to pull) |
| `⇅` | Both ahead and behind (diverged) |
| `!` | Dirty working tree |
| `?` | No upstream tracking branch |
| `✗` | Error reading repo |

## Keys

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh (local only) |
| `f` | Fetch all remotes + refresh |
| `g` | Open selected repo in GitHub Desktop |
| `j/k` | Navigate up/down |
| `Enter` | Detailed view of selected repo |
| `Esc` | Back to list / quit |

## Install

```bash
# Symlink to PATH
ln -sf "$(pwd)/gitpulse.py" ~/.local/bin/gitpulse

# Or copy
cp gitpulse.py ~/.local/bin/gitpulse
chmod +x ~/.local/bin/gitpulse
```

## Usage

```bash
# In a git repo: shows detailed single-repo view
cd ~/project && gitpulse

# In a parent directory: scans for all repos
gitpulse ~/Documents/GitHub

# Custom scan depth
gitpulse ~/code --depth 3
```

## Configuration

Create `~/.config/gitpulse.json`:

```json
{
  "auto_refresh_seconds": 30,
  "auto_fetch_seconds": 300,
  "scan_depth": 2,
  "ignore_repos": ["node_modules", "vendor"]
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `auto_refresh_seconds` | 30 | Local refresh interval (0 to disable) |
| `auto_fetch_seconds` | 300 | Remote fetch interval (0 to disable) |
| `scan_depth` | 2 | How deep to scan for .git directories |
| `ignore_repos` | [] | Repo names to skip |

## tmuxp Integration

Point gitpulse at your projects directory:

```yaml
- shell_command:
    - tmux select-pane -t "$TMUX_PANE" -T "GIT"
    - gitpulse ~/Documents/GitHub
```

Or for single-repo monitoring in a project-specific session:

```yaml
- shell_command:
    - tmux select-pane -t "$TMUX_PANE" -T "GIT"
    - gitpulse
```

## Requirements

- Python 3.8+
- git
- curses (included in Python stdlib on Unix)
- Optional: GitHub Desktop with CLI tool installed
