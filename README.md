# small_tuis

A collection of tiny terminal UIs for embedding in tmux panes.

## Tools

| Tool | Description |
|------|-------------|
| [gitpulse](./gitpulse/) | Git push/pull status dashboard |

## Philosophy

These tools are designed to be:
- **Small** — fit in a narrow tmux pane (40-60 chars wide)
- **Passive** — show status at a glance, auto-refresh
- **Simple** — single-file Python, no dependencies beyond stdlib
- **Keyboard-driven** — minimal keybindings, easy to remember

## Install all

```bash
# Symlink all tools to ~/.local/bin
for tool in */; do
    name="${tool%/}"
    if [ -f "$tool/$name.py" ]; then
        ln -sf "$(pwd)/$tool/$name.py" ~/.local/bin/"$name"
    fi
done
```
