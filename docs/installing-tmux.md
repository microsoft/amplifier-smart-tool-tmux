# Installing tmux

`tmux-fleet` is a tmux client: every verb speaks to a tmux server, so `tmux`
must be present on `PATH`. This tool never installs tmux for you (the manifest
is inert — it references this document, it does not run anything).

Install tmux with your platform's package manager, for example:

- **Debian / Ubuntu:** `sudo apt install tmux`
- **Fedora / RHEL:** `sudo dnf install tmux`
- **macOS (Homebrew):** `brew install tmux`
- **Arch:** `sudo pacman -S tmux`

Verify with `tmux -V`. tmux 3.x is expected; the tool was validated against
tmux 3.4.

Once tmux is on `PATH`, run `tmux-fleet doctor` to confirm the tool can see it
and can reach a writable socket directory.
