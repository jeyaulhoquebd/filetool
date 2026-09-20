# ubuntu-toolkit

Seven small file-management tools, two background jobs, and a desktop entry,
packaged together. Everything here is pure Python 3; the package itself
contains no compiled code, which is why it is `Architecture: all`.

## Commands

| Command | What it does |
| --- | --- |
| `bulk-rename` | Rename many files at once — prefixes, suffixes, find/replace, renumbering, case changes. Has `--undo`. |
| `filetool` | The same rename work plus image handling, behind subcommands: `filetool rename`, `filetool image`. Has `--undo`. |
| `filetool-gui` | The same engine as `filetool`, in a window: drag files in, preview a table of what would happen, then run it. |
| `find-duplicates` | Find duplicate files across several folders by hash, with `--cleanup` to send them to the Trash. |
| `image-tool` | Compress, resize and convert images without touching the originals. |
| `download-organizer` | Sort loose files into subfolders by type. |
| `bing-wallpaper` | Fetch today's Bing photo and set it as the desktop wallpaper. |

Each command has its own `--help`. `filetool-gui` also appears in your
application menu as **Filetool**.

`filetool` and `bulk-rename` deliberately share one undo log
(`~/.local/state/bulk-rename/history.log`), so a rename made with either one
can be reversed with `--undo` from either one. `image-tool` is standalone and
keeps no log.

`filetool` supersedes `bulk-rename` and `image-tool` in what it can do; all
three are installed because the older two have their own command lines. If you
only ever use `filetool`, the other two cost you nothing but a few hundred
kilobytes.

## Package relationships

| Relationship | Packages | Consequence |
| --- | --- | --- |
| **Depends** | `python3`, `python3-watchdog` | Hard requirements. |
| **Recommends** | `python3-pil`, `python3-tqdm`, `python3-send2trash`, `python3-pyqt6`, `libglib2.0-bin` | Installed by default with `apt install`. |
| **Suggests** | `libnotify-bin` | Only for `bing-wallpaper --notify`. |

Every Python dependency except `watchdog` is optional at runtime: each tool
checks for it and degrades rather than crashing. `watchdog` is the one hard
dependency because the packaged `download-organizer` unit runs with `--watch`,
which cannot work without it.

If you install with `--no-install-recommends`:

- `filetool-gui` will not start (it needs a Qt binding).
- `find-duplicates --cleanup` falls back from the `send2trash` module to the
  `gio` command; if `libglib2.0-bin` is also absent it will not be able to
  empty the Trash for you.
- `bulk-rename`, `filetool`, `image-tool` and `download-organizer` print a
  plain line counter instead of a `tqdm` progress bar.
- Image work in `filetool` and all of `image-tool` needs Pillow.

## The two background jobs

Both are systemd **user** units, installed to `/usr/lib/systemd/user/`. On
installation they are enabled **globally** (`systemctl --global enable`), which
means each user who logs in gets their own instance, running as that user and
touching only that user's files.

Nothing is started for you at install time. A package script runs as root with
no session to reach into, so it cannot start anything in a running desktop. To
start them **in the session you are in now**:

```sh
systemctl --user daemon-reload
systemctl --user start bing-wallpaper.timer
systemctl --user enable --now download-organizer.service
```

Check on them:

```sh
systemctl --user list-timers bing-wallpaper.timer
systemctl --user status download-organizer.service
journalctl --user -u bing-wallpaper -n 50
journalctl --user -u download-organizer -n 50
```

### Turning them off

For everyone, including future logins:

```sh
sudo systemctl --global disable download-organizer.service
```

For one session only — for example if you do not want the watcher rearranging
your Downloads folder:

```sh
systemctl --user disable --now download-organizer.service
```

### Changing how they behave

Do not edit the files in `/usr/lib/systemd/user/`; an upgrade will overwrite
them. Use a drop-in instead:

```sh
systemctl --user edit bing-wallpaper.service
```

That opens an override file in which you can add, for example:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/bing-wallpaper --keep 30
```

`ExecStart=` on its own resets the list before the next line appends to it.
After editing any unit, run `systemctl --user daemon-reload`.

Common changes:

- **Refresh time.** `bing-wallpaper.timer` runs daily at 16:20 and again two
  minutes after each login. Bing changes its photo at midnight US Pacific, so
  the 16:20 default suits timezones well east of that. Override `OnCalendar=`
  in a timer drop-in if your timezone does not.
- **Watched folder.** `download-organizer.service` watches `%h/Downloads`.
  Point it somewhere else with a `download-organizer.service` drop-in.
- **Notifications.** `bing-wallpaper --notify` pops up a desktop notification
  on each change; it needs `libnotify-bin`, and the flag has to go in the
  unit as shown above because systemd has nowhere else to put it.
- **Retries.** The wallpaper service gives up after six tries in ten minutes
  rather than retrying forever. Clear a failed state early with
  `systemctl --user reset-failed bing-wallpaper.service`.

### Requirements for the wallpaper job

Setting a GNOME background goes through `gsettings`, so this job needs a GNOME
(or compatible) session; it does nothing on a bare X11 or non-GNOME desktop.
`systemd --user` must also be running, which it is on a normal desktop login.

## Where your data lives

Nothing in this package writes inside `/usr`, and removing the package removes
none of it:

| Path | Written by |
| --- | --- |
| `~/.config/download-organizer/config.json` | folder rules and settings |
| `~/.local/state/download-organizer/` | undo history for sorting |
| `~/.local/state/bulk-rename/history.log` | undo history for renames |
| `~/Pictures/Wallpapers/` | photos fetched by `bing-wallpaper` |
| `~/Downloads/` | rearranged by `download-organizer` — it only moves loose files into subfolders there, and never deletes |

All of these are created on first use. `bing-wallpaper` keeps the last 15
photos and deletes older ones (change the count with `--keep`); it never
deletes anything it did not download itself.

## Ubuntu 24.04 (noble) and the Qt bindings

`filetool-gui` works with either **PySide6** or **PyQt6**. PySide6 is preferred
and is what Ubuntu ships from 26.04 onwards; 24.04 has no PySide6 packages at
all, and ships PyQt6 instead, so the package recommends `python3-pyqt6` and the
program falls back to it automatically. The other six commands need no Qt and
are unaffected.

## Removing it

```sh
sudo apt remove ubuntu-toolkit      # keeps your settings and undo history
sudo apt purge ubuntu-toolkit       # same — this package has no data to purge
```

Neither removes anything listed under *Where your data lives* above; see the
message printed during removal for the paths to delete by hand if you want
them gone.

## manual-install/ in this directory

`/usr/share/doc/ubuntu-toolkit/manual-install/` holds the shell installers that
predate this package, kept for reference. **Do not run them alongside the
package.** They write a second copy of `wallpaper_changer.py` into
`~/.local/bin` and units into `~/.config/systemd/user`, and both of those win
over the packaged versions — `~/.local/bin` comes first on `PATH`, and user
units in `~/.config` take precedence over the ones in `/usr/lib`. If you have
run them before, run their `--uninstall` first.
