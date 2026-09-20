#!/usr/bin/env bash
#
# install-wallpaper.sh — install the Bing wallpaper changer as a systemd *user*
# service and timer, so it runs once a day and again shortly after you log in.
#
# It does NOT run as root and does NOT use sudo: everything it touches lives
# inside your own home folder.
#
# Usage:
#   ./install-wallpaper.sh              # install and enable
#   ./install-wallpaper.sh --notify     # also pop up a notification on change
#   ./install-wallpaper.sh --dry-run    # print what it would do, change nothing
#   ./install-wallpaper.sh --no-run     # install but do not fetch a photo now
#   BIN_DIR=~/bin ./install-wallpaper.sh         # install the script elsewhere
#   PYTHON=~/.venvs/wp/bin/python ./install-wallpaper.sh
#
# To remove it again, use the companion script:
#   ./uninstall-wallpaper.sh
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
UNIT_NAME="bing-wallpaper"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Where the python script is installed. ~/.local/bin is on PATH on this
# machine, which means the installed copy can also be run by hand:
#   wallpaper_changer.py --random
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

# Where the photos are saved. This is the script's own default; change it here
# and in the units below if you want them somewhere else.
WALLPAPER_DIR="$HOME/Pictures/Wallpapers"

# The folder this installer lives in, which is where the templates are.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_SRC="$SCRIPT_DIR/wallpaper_changer.py"
SERVICE_TEMPLATE="$SCRIPT_DIR/$UNIT_NAME.service"
TIMER_TEMPLATE="$SCRIPT_DIR/$UNIT_NAME.timer"

INSTALLED_SCRIPT="$BIN_DIR/wallpaper_changer.py"
SERVICE_DEST="$UNIT_DIR/$UNIT_NAME.service"
TIMER_DEST="$UNIT_DIR/$UNIT_NAME.timer"

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
NOTIFY=0
DRY_RUN=0
RUN_NOW=1

while [ $# -gt 0 ]; do
    case "$1" in
        --notify)   NOTIFY=1 ;;
        --dry-run)  DRY_RUN=1 ;;
        --no-run)   RUN_NOW=0 ;;
        -h|--help)  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)         die "unknown option: $1 (try --help)" ;;
        *)          die "unexpected argument: $1 (try --help)" ;;
    esac
    shift
done

# This is a *user* service. Installing it as root would put it in root's home,
# where it could never touch your desktop.
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
# Checks
# ---------------------------------------------------------------------------
step "Checking the script and its dependencies"

[ -f "$SCRIPT_SRC" ]       || die "cannot find wallpaper_changer.py next to this installer (looked in $SCRIPT_DIR)"
[ -f "$SERVICE_TEMPLATE" ] || die "service template missing: $SERVICE_TEMPLATE"
[ -f "$TIMER_TEMPLATE" ]   || die "timer template missing: $TIMER_TEMPLATE"
say "  Script:      $SCRIPT_SRC"
say "  Will install to: $INSTALLED_SCRIPT"

# The script is pure standard library, so any python3 recent enough will do.
# 3.8 is the floor because of Path.unlink(missing_ok=True).
PYTHON_BIN="${PYTHON:-$(command -v python3 || true)}"
[ -n "$PYTHON_BIN" ] || die "python3 not found on PATH"
PYTHON_BIN="$(command -v "$PYTHON_BIN")"
[ -x "$PYTHON_BIN" ] || die "python interpreter is not executable: $PYTHON_BIN"

if "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    say "  Python:      $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
else
    die "$PYTHON_BIN is too old ($("$PYTHON_BIN" --version 2>&1)).

       This script needs Python 3.8 or newer. Point the installer at a newer
       interpreter with:

           PYTHON=/usr/bin/python3 $0"
fi

# Without gsettings there is no way to set a GNOME background, so the whole
# thing would be pointless.
if ! command -v gsettings >/dev/null 2>&1; then
    die "gsettings not found.

       This script changes the wallpaper through GNOME's settings, so it needs
       a GNOME (or compatible) desktop session. Nothing was installed."
fi
say "  gsettings:   OK"

# notify-send is only needed for --notify, and the script degrades gracefully
# without it, so this is a warning rather than a hard stop.
if [ "$NOTIFY" -eq 1 ]; then
    if command -v notify-send >/dev/null 2>&1; then
        say "  notify-send: OK (--notify requested)"
    else
        warn "notify-send is not installed, so --notify will not show anything."
        warn "Install it with:  sudo apt install libnotify-bin"
    fi
fi

# The downloaded script is checked with the same interpreter the unit will use,
# so a syntax error is caught here rather than at 16:20 in a journal nobody
# is reading.
"$PYTHON_BIN" -m py_compile "$SCRIPT_SRC" \
    || die "wallpaper_changer.py failed to compile with $PYTHON_BIN"
say "  Compiles:    OK"

# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
# --notify is baked into the unit's ExecStart rather than passed at runtime,
# because systemd has nowhere else to put it.
NOTIFY_FLAG=""
[ "$NOTIFY" -eq 1 ] && NOTIFY_FLAG=" --notify"

EXEC_LINE="ExecStart=$PYTHON_BIN $INSTALLED_SCRIPT$NOTIFY_FLAG"

step "These units will be written to $UNIT_DIR"
say "  $UNIT_NAME.service (comments stripped here; the file keeps them all)"
# Show the effective settings rather than all seventy lines of explanation.
sed -e "s|^ExecStart=.*|$EXEC_LINE|" \
    -e "s|^Documentation=.*|Documentation=file:$INSTALLED_SCRIPT|" \
    "$SERVICE_TEMPLATE" | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' | sed 's/^/    /'
say ""
say "  $UNIT_NAME.timer (copied unchanged)"
say ""
say "  And the script itself to $INSTALLED_SCRIPT"
say "  Photos are saved to $WALLPAPER_DIR"

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run — nothing was written"
    say "Re-run without --dry-run to install."
    exit 0
fi

# ---------------------------------------------------------------------------
# Install the script
# ---------------------------------------------------------------------------
step "Installing the script"
mkdir -p "$BIN_DIR"

# Back up a modified copy rather than silently clobbering it. An unchanged one
# is left alone, so re-running the installer does not litter backups.
if [ -f "$INSTALLED_SCRIPT" ] && ! cmp -s "$SCRIPT_SRC" "$INSTALLED_SCRIPT"; then
    BACKUP="$INSTALLED_SCRIPT.backup-$(date +%Y%m%d-%H%M%S)"
    cp -p "$INSTALLED_SCRIPT" "$BACKUP"
    say "Existing script differed and was backed up to: $BACKUP"
fi
install -m 0755 "$SCRIPT_SRC" "$INSTALLED_SCRIPT"
say "Wrote $INSTALLED_SCRIPT"

# ---------------------------------------------------------------------------
# Install the units
# ---------------------------------------------------------------------------
step "Installing the systemd units"
mkdir -p "$UNIT_DIR"

# The timer has no paths baked into it, so it is copied as-is. The service is
# templated, because systemd does not expand ~ in ExecStart and because the
# script path is a choice this installer makes.
write_unit() {
    local generated="$1" dest="$2"
    if [ -f "$dest" ] && cmp -s "$generated" "$dest"; then
        say "Unchanged: $dest"
        rm -f "$generated"
        return 0
    fi
    if [ -f "$dest" ]; then
        local backup="$dest.backup-$(date +%Y%m%d-%H%M%S)"
        cp -p "$dest" "$backup"
        say "Existing unit differed and was backed up to: $backup"
    fi
    mv "$generated" "$dest"
    say "Wrote $dest"
}

SERVICE_TMP="$(mktemp)"
sed -e "s|^ExecStart=.*|$EXEC_LINE|" \
    -e "s|^Documentation=.*|Documentation=file:$INSTALLED_SCRIPT|" \
    "$SERVICE_TEMPLATE" > "$SERVICE_TMP"
write_unit "$SERVICE_TMP" "$SERVICE_DEST"

TIMER_TMP="$(mktemp)"
cp "$TIMER_TEMPLATE" "$TIMER_TMP"
write_unit "$TIMER_TMP" "$TIMER_DEST"

# ---------------------------------------------------------------------------
# Enable
# ---------------------------------------------------------------------------
step "Enabling the timer"
systemctl --user daemon-reload
# Only the timer is enabled. It pulls the service in on its own, and enabling
# both would just add a second, redundant trigger at login.
systemctl --user enable --now "$UNIT_NAME.timer"
say "Timer enabled: it will run daily at 16:20 and again 2 minutes after login."

# ---------------------------------------------------------------------------
# Prove it works
# ---------------------------------------------------------------------------
if [ "$RUN_NOW" -eq 1 ]; then
    step "Running it once now"
    if systemctl --user start "$UNIT_NAME.service"; then
        say "The service ran successfully. Your wallpaper should have changed."
    else
        warn "the service did not run cleanly."
        warn "Find out why with:  journalctl --user -u $UNIT_NAME -n 50 --no-pager"
        systemctl --user status "$UNIT_NAME.service" --no-pager || true
    fi
fi

step "Check it later with"
say "  systemctl --user list-timers $UNIT_NAME.timer      # when it runs next"
say "  systemctl --user status $UNIT_NAME.timer $UNIT_NAME.service"
say "  journalctl --user -u $UNIT_NAME.service -n 50     # what it did"

# ---------------------------------------------------------------------------
# Cheat sheet
# ---------------------------------------------------------------------------
step "Commands you will want"
cat <<EOF
  Run it right now (without waiting for the timer)
      systemctl --user start $UNIT_NAME.service

  Use a random photo from the ones already saved (works offline)
      $INSTALLED_SCRIPT --random

  Watch the log live  (Ctrl+C stops watching, not the service)
      journalctl --user -u $UNIT_NAME -f

  Keep a different number of photos (default 15), then reload
      systemctl --user edit $UNIT_NAME.service     # add: ExecStart=... --keep 30
      systemctl --user daemon-reload

  Add or remove notifications (re-running the installer rewrites the unit)
      $0 --notify       # turn them on
      $0                # turn them back off

  Stop it starting on future logins (keeps everything installed)
      systemctl --user disable --now $UNIT_NAME.timer

  Uninstall
      $SCRIPT_DIR/uninstall-wallpaper.sh
EOF
