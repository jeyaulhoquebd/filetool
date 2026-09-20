#!/usr/bin/env bash
#
# install.sh — install the Download Organizer watcher as a systemd *user*
# service, so it starts on login and runs in the background.
#
# It does NOT run as root and does NOT use sudo: it is a user service, living
# entirely inside your own home folder.
#
# Usage:
#   ./install.sh                     # watch ~/Downloads
#   ./install.sh ~/Desktop           # watch a different folder
#   ./install.sh --dry-run           # print what it would do, change nothing
#   ./install.sh --uninstall         # stop and remove the service
#   ./install.sh --purge             # remove the service AND the config/history
#   PYTHON=~/.venvs/org/bin/python ./install.sh    # use a virtualenv
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SERVICE_NAME="download-organizer"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME.service"

# Where the python script and this installer live (the folder this script is in).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/organize_downloads.py"
UNIT_TEMPLATE="$SCRIPT_DIR/$SERVICE_NAME.service"

# Where the script keeps its own files. These are NOT deleted by --uninstall.
CONFIG_PATH="$HOME/.config/download-organizer/config.json"
HISTORY_PATH="$HOME/.local/state/download-organizer/history.log"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
FOLDER=""
DO_UNINSTALL=0
DO_PURGE=0
DRY_RUN=0
WATCHDOG_MISSING=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --uninstall)  DO_UNINSTALL=1 ;;
        --purge)      DO_PURGE=1; DO_UNINSTALL=1 ;;
        -h|--help)    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)           die "unknown option: $1 (try --help)" ;;
        *)            FOLDER="$1" ;;
    esac
    shift
done

# This is a *user* service. Running it with sudo would install it for root,
# which is not what anyone wants here.
if [ "$(id -u)" -eq 0 ]; then
    die "do not run this with sudo — it installs a user service for $(whoami)"
fi

# systemd --user needs a working session bus. Without one, systemctl fails
# with a clearer message than we could invent, but a hint helps.
if ! systemctl --user show-environment >/dev/null 2>&1; then
    die "no systemd user session available. Are you in a desktop session
       (or a login shell with XDG_RUNTIME_DIR set)?"
fi

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
    step "Stopping and disabling $SERVICE_NAME"
    if [ "$DRY_RUN" -eq 1 ]; then
        say "(dry run) systemctl --user disable --now $SERVICE_NAME.service"
    else
        systemctl --user disable --now "$SERVICE_NAME.service" 2>/dev/null \
            || warn "service was not running or not enabled — continuing"
    fi

    step "Removing the service file"
    if [ -f "$UNIT_PATH" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            say "(dry run) rm $UNIT_PATH"
        else
            rm -f "$UNIT_PATH"
            say "Removed $UNIT_PATH"
        fi
    else
        say "No service file at $UNIT_PATH — nothing to remove."
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        systemctl --user daemon-reload
        systemctl --user reset-failed "$SERVICE_NAME.service" 2>/dev/null || true
    fi

    # Deliberately left in place: your config and your undo history.
    step "Your data was kept"
    say "  Config:  $CONFIG_PATH"
    say "  History: $HISTORY_PATH"
    say ""
    say "These were not deleted, so a reinstall keeps your settings and your"
    say "ability to run --undo on past runs."
    say "To remove them too, re-run:  $0 --purge"

    if [ "$DO_PURGE" -eq 1 ]; then
        step "Purge requested"
        say "This will permanently delete:"
        say "  $CONFIG_PATH"
        say "  $HISTORY_PATH"
        say ""
        say "The history log is what --undo reads, so past moves can no longer"
        say "be reversed afterwards."
        if [ "$DRY_RUN" -eq 1 ]; then
            say "(dry run) nothing deleted"
        elif [ -t 0 ]; then
            printf 'Type exactly "delete" to confirm: '
            read -r answer
            if [ "$answer" = "delete" ]; then
                rm -f "$CONFIG_PATH" "$HISTORY_PATH"
                say "Deleted config and history."
            else
                say "Not confirmed — nothing was deleted."
            fi
        else
            warn "no terminal available to confirm on; nothing was deleted."
            warn "Delete the two files listed above by hand if you want them gone."
        fi
    fi

    say ""
    say "Done. The watcher will not start on your next login."
    exit 0
fi

# ---------------------------------------------------------------------------
# Checks before installing
# ---------------------------------------------------------------------------
step "Checking the script and its dependencies"

[ -f "$SCRIPT_PATH" ] || die "cannot find organize_downloads.py next to this installer (looked in $SCRIPT_DIR)"
say "  Script: $SCRIPT_PATH"

# Pick the interpreter: $PYTHON if you set it, otherwise the system python3.
# A virtualenv python is fine as long as it can import watchdog.
PYTHON_BIN="${PYTHON:-$(command -v python3 || true)}"
[ -n "$PYTHON_BIN" ] || die "python3 not found on PATH"
PYTHON_BIN="$(command -v "$PYTHON_BIN")"
[ -x "$PYTHON_BIN" ] || die "python interpreter is not executable: $PYTHON_BIN"
say "  Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

if ! "$PYTHON_BIN" -c "import watchdog" 2>/dev/null; then
    if [ "$DRY_RUN" -eq 1 ]; then
        # A dry run should still show you the plan, so warn instead of dying.
        warn "watchdog is not installed for $PYTHON_BIN."
        warn "A real install would stop here. Fix it with:"
        warn "    sudo apt install python3-watchdog"
        WATCHDOG_MISSING=1
    else
        die "the 'watchdog' package is not installed for $PYTHON_BIN

       The watcher cannot run without it. Install it with:

           sudo apt install python3-watchdog

       Or, to use a virtualenv instead:

           python3 -m venv ~/.venvs/org
           ~/.venvs/org/bin/pip install watchdog
           PYTHON=~/.venvs/org/bin/python $0"
    fi
else
    say "  watchdog: OK"
fi

# The folder we will watch.
[ -n "$FOLDER" ] || FOLDER="$HOME/Downloads"
if [ ! -d "$FOLDER" ]; then
    die "folder to watch does not exist: $FOLDER"
fi
FOLDER="$(cd "$FOLDER" && pwd)"   # make it absolute
say "  Watching: $FOLDER"

[ -f "$UNIT_TEMPLATE" ] || die "service template missing: $UNIT_TEMPLATE"

# ---------------------------------------------------------------------------
# Show the plan
# ---------------------------------------------------------------------------
# systemd does not expand ~ in ExecStart arguments, so we write the absolute
# path. -y is required because a service has no terminal for the prompt.
EXEC_LINE="ExecStart=$PYTHON_BIN $SCRIPT_PATH --watch $FOLDER -y"

step "This unit will be written to $UNIT_PATH"
# Show the template with the ExecStart line we are about to substitute in.
sed "s|^ExecStart=.*|$EXEC_LINE|" "$UNIT_TEMPLATE" | sed 's/^/    /'

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run — nothing was written"
    if [ "$WATCHDOG_MISSING" -eq 1 ]; then
        say "Note: a real install would stop until watchdog is installed:"
        say "      sudo apt install python3-watchdog"
    fi
    say "Re-run without --dry-run to install the service."
    exit 0
fi

# ---------------------------------------------------------------------------
# Write the unit file
# ---------------------------------------------------------------------------
step "Installing the service"
mkdir -p "$UNIT_DIR"

# Never silently clobber a file you may have edited by hand: keep a backup.
if [ -f "$UNIT_PATH" ]; then
    BACKUP="$UNIT_PATH.backup-$(date +%Y%m%d-%H%M%S)"
    cp -p "$UNIT_PATH" "$BACKUP"
    say "Existing unit backed up to: $BACKUP"
fi

sed "s|^ExecStart=.*|$EXEC_LINE|" "$UNIT_TEMPLATE" > "$UNIT_PATH"
say "Wrote $UNIT_PATH"

# ---------------------------------------------------------------------------
# Enable and start
# ---------------------------------------------------------------------------
step "Enabling and starting the service"
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"

# Give it a moment, then report whether it actually came up.
sleep 1
if systemctl --user is-active --quiet "$SERVICE_NAME.service"; then
    step "Running"
    say "The watcher is now active and will start automatically on login."
    say "Drop a file into $FOLDER to see it get sorted."
else
    warn "the service was installed but is not running."
    warn "Find out why with:  journalctl --user -u $SERVICE_NAME -n 50 --no-pager"
    systemctl --user status "$SERVICE_NAME.service" --no-pager || true
    exit 1
fi

# ---------------------------------------------------------------------------
# Cheat sheet
# ---------------------------------------------------------------------------
step "Commands you will want"
cat <<EOF
  Status (is it running, and why not)
      systemctl --user status $SERVICE_NAME

  Watch the log live  (Ctrl+C to stop watching — does not stop the service)
      journalctl --user -u $SERVICE_NAME -f

  Recent activity
      journalctl --user -u $SERVICE_NAME -n 50 --no-pager

  Everything since today
      journalctl --user -u $SERVICE_NAME --since today

  Stop / start / restart
      systemctl --user stop $SERVICE_NAME
      systemctl --user start $SERVICE_NAME
      systemctl --user restart $SERVICE_NAME

  Stop it starting on future logins (keeps the unit file)
      systemctl --user disable --now $SERVICE_NAME

  Undo the most recent run (any run, watch or batch)
      $PYTHON_BIN $SCRIPT_PATH --undo

  Change which folder is watched
      $0 /path/to/other/folder        # re-run the installer

  Uninstall (keeps your config and history)
      $0 --uninstall

Not sure about these? Run:  $0 --uninstall --dry-run
EOF

# ---------------------------------------------------------------------------
# Linger note
# ---------------------------------------------------------------------------
# A user service normally stops when your last session ends. If you want the
# watcher to keep running when you are logged out (and to start at boot
# instead of at login), you need "lingering". That changes a system-wide
# setting, so this installer leaves it to you.
step "Optional: run even when logged out"
say "By default the watcher runs while you are logged in. To keep it running"
say "after you log out, enable lingering (asks for your password):"
say ""
say "    sudo loginctl enable-linger $(whoami)"
