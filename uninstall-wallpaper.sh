#!/usr/bin/env bash
#
# uninstall-wallpaper.sh — undo what install-wallpaper.sh did: stop and
# disable the timer and service, remove the units and the installed script.
#
# It does NOT run as root and does NOT use sudo.
#
# Usage:
#   ./uninstall-wallpaper.sh             # remove the service, keep your photos
#   ./uninstall-wallpaper.sh --dry-run   # print what it would do, change nothing
#   ./uninstall-wallpaper.sh --purge     # also delete the downloaded photos
#
# Your saved wallpapers are NOT deleted unless you ask with --purge, and the
# wallpaper currently on screen is left alone either way.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
UNIT_NAME="bing-wallpaper"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

WALLPAPER_DIR="$HOME/Pictures/Wallpapers"

# Where this script lives; used only to point at the installer in the closing
# message.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
DO_PURGE=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --purge)   DO_PURGE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)        die "unknown option: $1 (try --help)" ;;
        *)         die "unexpected argument: $1 (try --help)" ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    die "do not run this with sudo — it removes a user service from $(whoami)'s home"
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
    die "no systemd user session available. Are you in a desktop session
       (or a login shell with XDG_RUNTIME_DIR set)?"
fi

SERVICE_PATH="$UNIT_DIR/$UNIT_NAME.service"
TIMER_PATH="$UNIT_DIR/$UNIT_NAME.timer"

# ---------------------------------------------------------------------------
# Work out where the script was installed
# ---------------------------------------------------------------------------
# The installer can put the script anywhere (BIN_DIR), so ask the installed
# unit where it points rather than assuming the default. Falls back to the
# default if the unit is already gone.
INSTALLED_SCRIPT="$BIN_DIR/wallpaper_changer.py"
if [ -f "$SERVICE_PATH" ]; then
    # ExecStart=/usr/bin/python3 /path/to/wallpaper_changer.py [--notify]
    FROM_UNIT="$(sed -n 's|^ExecStart=[^ ]* *\([^ ]*wallpaper_changer\.py\).*|\1|p' "$SERVICE_PATH" | head -1)"
    if [ -n "$FROM_UNIT" ]; then
        INSTALLED_SCRIPT="$FROM_UNIT"
    fi
fi

# ---------------------------------------------------------------------------
# Stop and disable
# ---------------------------------------------------------------------------
step "Stopping and disabling the timer and service"
if [ "$DRY_RUN" -eq 1 ]; then
    say "(dry run) systemctl --user disable --now $UNIT_NAME.timer"
else
    # Disabling the timer is what stops future runs; the service is oneshot and
    # is not enabled on its own. Both are stopped in case one is running now.
    systemctl --user disable --now "$UNIT_NAME.timer" 2>/dev/null \
        || warn "timer was not running or not enabled — continuing"
    systemctl --user stop "$UNIT_NAME.service" 2>/dev/null \
        || warn "service was not running — continuing"
fi

# ---------------------------------------------------------------------------
# Remove the unit files
# ---------------------------------------------------------------------------
step "Removing the unit files"
for path in "$TIMER_PATH" "$SERVICE_PATH"; do
    if [ -f "$path" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            say "(dry run) rm $path"
        else
            rm -f "$path"
            say "Removed $path"
        fi
    else
        say "Not present: $path"
    fi
done

# Backups left behind by the installer. Reported rather than deleted, since a
# backup may be a hand-edited unit someone wants to keep.
BACKUPS="$(find "$UNIT_DIR" -maxdepth 1 -name "$UNIT_NAME.*.backup-*" 2>/dev/null || true)"
if [ -n "$BACKUPS" ]; then
    step "Backups were left in place"
    say "$BACKUPS"
    say "Delete them by hand if you do not want them."
fi

# ---------------------------------------------------------------------------
# Remove the installed script
# ---------------------------------------------------------------------------
step "Removing the installed script"
if [ -f "$INSTALLED_SCRIPT" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        say "(dry run) rm $INSTALLED_SCRIPT"
    else
        rm -f "$INSTALLED_SCRIPT"
        say "Removed $INSTALLED_SCRIPT"
    fi
else
    say "Not present: $INSTALLED_SCRIPT"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    systemctl --user daemon-reload
    systemctl --user reset-failed "$UNIT_NAME.service" 2>/dev/null || true
    systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# The wallpaper itself
# ---------------------------------------------------------------------------
step "Your wallpaper was left alone"
say "The current background is still the last photo that was set. To go back to"
say "your distribution's default, set it by hand, for example:"
say ""
say "    gsettings set org.gnome.desktop.background picture-uri \\"
say "        'file:///usr/share/backgrounds/mizuno-as-Winter_Grand_Triangle.jpg'"
say "    gsettings set org.gnome.desktop.background picture-uri-dark \\"
say "        'file:///usr/share/backgrounds/mizuno-as-Winter_Grand_Triangle.jpg'"

# ---------------------------------------------------------------------------
# Purge the photos
# ---------------------------------------------------------------------------
if [ "$DO_PURGE" -ne 1 ]; then
    step "Your photos were kept"
    say "  $WALLPAPER_DIR"
    say ""
    say "To delete them too, re-run:  $0 --purge"
else
    step "Purge requested"
    say "This will permanently delete every downloaded photo in:"
    say "  $WALLPAPER_DIR"

    # Only the files this tool writes, matching the script's own naming rule —
    # --dir can point at a folder that also holds your own pictures, and those
    # are not ours to delete.
    PHOTOS="$(find "$WALLPAPER_DIR" -maxdepth 1 -name 'bing-*.jpg' -type f 2>/dev/null || true)"
    if [ -z "$PHOTOS" ]; then
        say "Nothing to delete."
    else
        say ""
        say "$PHOTOS" | sed 's/^/  /'
        if [ "$DRY_RUN" -eq 1 ]; then
            say "(dry run) nothing deleted"
        elif [ -t 0 ]; then
            printf 'Type exactly "delete" to confirm: '
            read -r answer
            if [ "$answer" = "delete" ]; then
                find "$WALLPAPER_DIR" -maxdepth 1 -name 'bing-*.jpg' -type f -delete
                say "Deleted the photos listed above."
            else
                say "Not confirmed — nothing was deleted."
            fi
        else
            warn "no terminal available to confirm on; nothing was deleted."
            warn "Delete the files listed above by hand if you want them gone."
        fi
    fi
fi

say ""
say "Done. The wallpaper changer will not run again."
say "To put it back:  $SCRIPT_DIR/install-wallpaper.sh"
