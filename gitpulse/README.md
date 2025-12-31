# gitpulse

Tiny TUI showing git push/pull status at a glance. Designed for a small tmux pane.

```
┌─────────────────────────────────────────┐
│  gitpulse                               │
│                                         │
│  Repo: /Users/you/project               │
│  Branch: main                           │
│  Upstream: origin/main                  │
│                                         │
│  Pending push: no   (ahead 0)           │
│  Pending pull: YES  (behind 3)          │
│  Working tree: DIRTY                    │
│                                         │
│  Last update: 14:32:05                  │
│                                         │
│  q quit   r refresh   f fetch   g GitHub│
└─────────────────────────────────────────┘
```

## Features

- Shows ahead/behind counts vs upstream
- Color-coded status (green=clean, yellow=ahead, red=behind/dirty)
- Auto-refreshes every 30 seconds
- Quick `g` key to open in GitHub Desktop

## Keys

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh (local only) |
| `f` | Fetch from remote + refresh |
| `g` | Open repo in GitHub Desktop |

## Install

```bash
# Copy to your PATH
cp gitpulse.py ~/.local/bin/gitpulse
chmod +x ~/.local/bin/gitpulse

# Or symlink for development
ln -sf "$(pwd)/gitpulse.py" ~/.local/bin/gitpulse
```

## Requirements

- Python 3.8+
- git
- curses (included in Python stdlib on Unix)
- Optional: GitHub Desktop with CLI tool installed (for `g` key)

## tmuxp integration

Replace `gitui` or `lazygit` in your tmuxp config:

```yaml
- shell_command:
    - tmux select-pane -t "$TMUX_PANE" -T "GIT"
    - gitpulse
```
