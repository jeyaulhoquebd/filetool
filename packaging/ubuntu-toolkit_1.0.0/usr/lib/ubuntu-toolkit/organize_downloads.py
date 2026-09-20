#!/usr/bin/env python3
"""
organize_downloads.py — sort loose files into subfolders by type.

Scans a folder (default: ~/Downloads) and moves each file into a subfolder
based on its extension: Images, Documents, Videos, Audio, Archives, Programs,
or Others. It can also sit and watch the folder, sorting each file as soon as
its download finishes.

Safety rules built in:
  * Nothing is ever overwritten. If the destination name is taken, the file
    is renamed to "name (1).ext", "name (2).ext", and so on.
  * Nothing is ever deleted. The script only moves files.
  * --dry-run prints exactly what would happen and touches nothing.
  * Without --dry-run you get a confirmation prompt before anything moves.
  * Every real move is written to a log, so --undo can put the files back.

Files this script uses (both paths are shown when it starts, and both can be
overridden with --config / --log-file):

  Config:  ~/.config/download-organizer/config.json
           The category-to-extension mapping. Created with defaults on the
           first run, then yours to edit.

  Log:     ~/.local/state/download-organizer/history.log
           One JSON object per line recording every move. Powers --undo.

Usage:
    python3 organize_downloads.py --dry-run      # look, change nothing
    python3 organize_downloads.py                # sort (asks to confirm)
    python3 organize_downloads.py ~/Desktop -y   # different folder, no prompt
    python3 organize_downloads.py --undo         # put the last run back
    python3 organize_downloads.py --watch        # sort new files as they land

--watch needs the third-party "watchdog" package. On Ubuntu:
    sudo apt install python3-watchdog
"""

import argparse
import copy
import json
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime

# ---------------------------------------------------------------------------
# Default locations. Change these two lines if you want the files elsewhere.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/download-organizer/config.json")
DEFAULT_LOG_PATH = os.path.expanduser("~/.local/state/download-organizer/history.log")

# How often the watch-mode settle checker looks at pending files, in seconds.
# This is not the same as --settle-seconds: this is how often we *check*,
# --settle-seconds is how long a file must stay unchanged before we act.
POLL_INTERVAL = 0.5

# ---------------------------------------------------------------------------
# The default configuration. This is what gets written to the config file the
# first time the script runs. Edit the config file, not this dict, once it
# exists — but keeping it here means a deleted config is rebuilt correctly.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "categories": {
        "Images": [
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
            ".tiff", ".tif", ".heic", ".ico", ".raw",
        ],
        "Documents": [
            ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".ods", ".odp",
            ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".md", ".epub", ".tex",
        ],
        "Videos": [
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
            ".m4v", ".mpg", ".mpeg", ".3gp",
        ],
        "Audio": [
            ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma",
            ".opus", ".aiff",
        ],
        "Archives": [
            ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
            ".zst", ".iso",
        ],
        "Programs": [
            ".deb", ".rpm", ".appimage", ".sh", ".run", ".bin", ".snap",
        ],
    },
    # Extensionless or unrecognised files land here.
    "fallback_category": "Others",
    # Files ending with these are treated as still-downloading and left alone.
    "incomplete_suffixes": [
        ".part", ".crdownload", ".download", ".opdownload", ".partial", ".tmp",
    ],
}

# Files that look like one extension but are really two. Handled so that
# "backup.tar.gz" becomes "backup (1).tar.gz" instead of "backup.tar (1).gz".
DOUBLE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


# ===========================================================================
# Configuration file handling
# ===========================================================================
def write_default_config(path):
    """Create the config file (and its folder) from DEFAULT_CONFIG."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULT_CONFIG, handle, indent=2)
        handle.write("\n")  # trailing newline keeps text editors happy


def load_config(path):
    """
    Read the config file, creating it from defaults if it is missing.

    Returns a config dict. Exits with an error if the file exists but is
    broken — silently falling back to defaults could mis-sort your files,
    which is worse than stopping.
    """
    if not os.path.exists(path):
        try:
            write_default_config(path)
        except OSError as error:
            print("Error: could not create config file {}: {}".format(path, error))
            sys.exit(1)
        print("Created default config: {}".format(path))

    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as error:
        print("Error: config file is not valid JSON: {}".format(path))
        print("       {}".format(error))
        print("       Fix the file, or delete it to regenerate the defaults.")
        sys.exit(1)
    except OSError as error:
        print("Error: could not read config file {}: {}".format(path, error))
        sys.exit(1)

    if not isinstance(config, dict):
        print("Error: config file must contain a JSON object: {}".format(path))
        sys.exit(1)

    # Optional keys fall back to their defaults if the user removed them.
    if "categories" not in config:
        print("Warning: config has no \"categories\" key — using the defaults.")
        config["categories"] = copy.deepcopy(DEFAULT_CONFIG["categories"])
    if "fallback_category" not in config:
        config["fallback_category"] = DEFAULT_CONFIG["fallback_category"]
    if "incomplete_suffixes" not in config:
        config["incomplete_suffixes"] = copy.deepcopy(
            DEFAULT_CONFIG["incomplete_suffixes"])

    # Tidy up the categories so the rest of the script can trust them:
    # extensions are lowercased and start with a dot.
    cleaned_categories = {}
    for name, extensions in config["categories"].items():
        if not isinstance(extensions, list):
            print("Warning: category \"{}\" is not a list — skipping it.".format(name))
            continue
        cleaned = set()
        for extension in extensions:
            if not isinstance(extension, str):
                continue
            extension = extension.strip().lower()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = "." + extension
            cleaned.add(extension)
        cleaned_categories[name] = cleaned
    config["categories"] = cleaned_categories

    config["fallback_category"] = str(config["fallback_category"])
    config["incomplete_suffixes"] = tuple(
        suffix.strip().lower()
        for suffix in config["incomplete_suffixes"]
        if isinstance(suffix, str) and suffix.strip()
    )

    return config


def build_extension_lookup(config):
    """Turn {"Images": {".jpg", ...}} into {".jpg": "Images", ...}."""
    lookup = {}
    for folder_name, extensions in config["categories"].items():
        for extension in extensions:
            lookup[extension] = folder_name
    return lookup


# ===========================================================================
# Log file handling
# ===========================================================================
def append_log_entry(log_path, entry):
    """
    Append one JSON object as a single line to the log file.

    JSON Lines format means the log is both readable by eye and easy for
    --undo to parse. Returns True on success, False if the write failed.
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError as error:
        print("Warning: could not write to log file {}: {}".format(log_path, error))
        return False


def read_log(log_path):
    """
    Read the log back into a list of dicts.

    Unreadable lines (a crash mid-write, a hand edit) are skipped with a
    warning rather than aborting — a single bad line should not cost you
    the ability to undo everything else.
    """
    if not os.path.exists(log_path):
        return []

    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    print("Warning: skipping unreadable log line {}.".format(line_number))
    except OSError as error:
        print("Error: could not read log file {}: {}".format(log_path, error))
        sys.exit(1)

    return entries


def new_run_id():
    """A short unique id linking a run to the moves it performed."""
    return uuid.uuid4().hex[:8]


def now():
    """Timestamp used in log entries and printed messages."""
    return datetime.now().isoformat(timespec="seconds")


def start_logged_run(log_path, folder, action):
    """Write a run header to the log and return its run_id."""
    run_id = new_run_id()
    append_log_entry(log_path, {
        "type": "run",
        "run_id": run_id,
        "timestamp": now(),
        "folder": folder,
        "action": action,
    })
    return run_id


# ===========================================================================
# Filename helpers
# ===========================================================================
def classify(filename, lookup, fallback):
    """Return the destination folder name for a filename."""
    extension = os.path.splitext(filename)[1].lower()
    return lookup.get(extension, fallback)


def is_incomplete(filename, incomplete_suffixes):
    """True if the file looks like a partially finished download."""
    lowered = filename.lower()
    return lowered.endswith(incomplete_suffixes)


def split_name(filename):
    """Split a filename into (base, extension), handling .tar.gz style names."""
    lowered = filename.lower()
    for double_ext in DOUBLE_EXTENSIONS:
        if lowered.endswith(double_ext):
            return filename[:-len(double_ext)], filename[-len(double_ext):]
    return os.path.splitext(filename)


def make_unique_path(directory, filename):
    """
    Return a full path inside `directory` that does not exist yet.

    If "report.pdf" is already there, this returns ".../report (1).pdf".
    If that is taken too, it returns "report (2).pdf", and so on.
    This is what guarantees we never overwrite an existing file.
    """
    base, extension = split_name(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate):
        new_name = "{} ({}){}".format(base, counter, extension)
        candidate = os.path.join(directory, new_name)
        counter += 1
    return candidate


def find_files_to_sort(folder, script_path, incomplete_suffixes):
    """
    Return a sorted list of filenames to consider.

    Skips: subfolders, hidden files, incomplete downloads, and this script
    itself (in case it is sitting in the folder being organized).
    """
    files = []
    for name in sorted(os.listdir(folder), key=str.lower):
        full_path = os.path.join(folder, name)

        # Skip subfolders (including the category folders we create).
        if os.path.isdir(full_path):
            continue

        # Skip anything that is not a regular file (broken symlinks, sockets...).
        if not os.path.isfile(full_path):
            continue

        # Skip hidden files such as .DS_Store or .~lock.doc#
        if name.startswith("."):
            continue

        # Skip half-finished downloads.
        if is_incomplete(name, incomplete_suffixes):
            continue

        # Skip the running script itself.
        if os.path.abspath(full_path) == script_path:
            continue

        files.append(name)

    return files


def count_skipped(folder, script_path, config):
    """Count the items we intentionally ignored (for the summary line)."""
    count = 0
    category_names = set(config["categories"])
    for name in os.listdir(folder):
        full_path = os.path.join(folder, name)
        if os.path.isdir(full_path):
            # Category folders only exist because we created them, so they
            # are not "skipped items".
            if name not in category_names and name != config["fallback_category"]:
                count += 1
        elif name.startswith(".") or is_incomplete(name, config["incomplete_suffixes"]):
            count += 1
        elif os.path.abspath(full_path) == script_path:
            count += 1
    return count


def confirm(message):
    """Ask the user a yes/no question. Returns True only on an explicit yes."""
    print()
    print(message)
    print("Nothing will be deleted or overwritten.")
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # No terminal to answer on (e.g. run from cron) — refuse to guess.
        print()
        return False
    return answer in ("y", "yes")


def make_move(source, destination):
    """
    Move one file, creating the destination folder if needed.

    os.makedirs(..., exist_ok=True) is a no-op when the folder already
    exists, so it is safe to call every time.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.move(source, destination)


def sort_one_file(source, base_folder, config, extension_lookup,
                  log_path, run_id, dry_run):
    """
    Move a single file into its category subfolder, and log it.

    Shared by the normal batch mode and by --watch, so both behave
    identically. Prints what it did.

    Returns (category, destination_path, renamed). Raises OSError if the
    move fails, which the caller is expected to catch and report.
    """
    name = os.path.basename(source)
    category = classify(name, extension_lookup, config["fallback_category"])
    destination_dir = os.path.join(base_folder, category)

    # In a dry run we must not create folders either, so we only create
    # directories when we are actually moving.
    if not dry_run:
        os.makedirs(destination_dir, exist_ok=True)

    destination = make_unique_path(destination_dir, name)
    renamed = os.path.basename(destination) != name

    if dry_run:
        if renamed:
            print("[dry-run] Would move: {}  ->  {}/{} (renamed to avoid overwrite)".format(
                name, category, os.path.basename(destination)))
        else:
            print("[dry-run] Would move: {}  ->  {}/".format(name, category))
        return category, destination, renamed

    make_move(source, destination)

    # Log immediately after each successful move, so even if the script is
    # interrupted the log stays accurate for --undo.
    append_log_entry(log_path, {
        "type": "move",
        "run_id": run_id,
        "timestamp": now(),
        "src": source,
        "dst": destination,
        "category": category,
        "renamed": renamed,
    })

    if renamed:
        print("Moved: {}  ->  {}/{}  (renamed to avoid overwrite)".format(
            name, category, os.path.basename(destination)))
    else:
        print("Moved: {}  ->  {}/".format(name, category))

    return category, destination, renamed


# ===========================================================================
# The organize action
# ===========================================================================
def cmd_organize(args, config, extension_lookup):
    """Sort the files in a folder. Returns a process exit code."""
    folder = os.path.abspath(os.path.expanduser(args.folder))
    script_path = os.path.abspath(__file__)

    if not os.path.exists(folder):
        print("Error: folder does not exist: {}".format(folder))
        return 1
    if not os.path.isdir(folder):
        print("Error: not a folder: {}".format(folder))
        return 1

    mode = "DRY RUN (nothing will be changed)" if args.dry_run else "LIVE"
    print("Organizing: {}".format(folder))
    print("Mode:       {}".format(mode))
    print("Config:     {}".format(args.config))
    print("Log:        {}".format(args.log_file))
    print("-" * 60)

    files = find_files_to_sort(folder, script_path, config["incomplete_suffixes"])

    if not files:
        print("Nothing to do — no loose files found.")
        return 0

    if not args.dry_run and not args.yes:
        if not confirm("About to move {} file(s) inside {}.".format(len(files), folder)):
            print("Cancelled — nothing was changed.")
            return 0

    # In a dry run we never write to the log, otherwise a later --undo would
    # try to reverse moves that never happened.
    run_id = None
    if not args.dry_run:
        run_id = start_logged_run(args.log_file, folder, "organize")

    moved_per_category = {}
    errors = []

    for name in files:
        source = os.path.join(folder, name)
        try:
            category, _destination, _renamed = sort_one_file(
                source, folder, config, extension_lookup,
                args.log_file, run_id, args.dry_run)
            moved_per_category[category] = moved_per_category.get(category, 0) + 1
        except OSError as error:
            # Permission denied, disk full, file vanished mid-run, etc.
            print("ERROR: could not move {}: {}".format(name, error))
            errors.append((name, str(error)))

    print_summary(moved_per_category, config, args.dry_run,
                  count_skipped(folder, script_path, config), errors)

    if args.dry_run:
        print()
        print("Re-run without --dry-run to actually move these files.")
    elif moved_per_category:
        print()
        print("To put these files back: python3 {} --undo".format(
            os.path.basename(script_path)))

    return 1 if errors else 0


def print_summary(moved_per_category, config, dry_run, skipped, errors):
    """Print the end-of-run summary."""
    all_categories = list(config["categories"].keys()) + [config["fallback_category"]]
    total_moved = sum(moved_per_category.values())

    print()
    print("=" * 60)
    print("SUMMARY" + ("  (dry run — no files were moved)" if dry_run else ""))
    print("=" * 60)

    for category in all_categories:
        count = moved_per_category.get(category, 0)
        if count:
            print("  {:<12} {:>4} file(s)".format(category, count))
    if total_moved == 0:
        print("  (no files to move)")
    print("  {:<12} {:>4} file(s)".format("TOTAL", total_moved))

    if skipped:
        print()
        print("Skipped {} item(s): subfolders, hidden files, incomplete downloads.".format(
            skipped))

    if errors:
        print()
        print("{} file(s) failed:".format(len(errors)))
        for filename, reason in errors:
            print("  - {}: {}".format(filename, reason))


# ===========================================================================
# The watch action
# ===========================================================================
def cmd_watch(args, config, extension_lookup):
    """
    Watch a folder and sort each new file once its download settles.

    watchdog tells us when files appear; a background thread then waits until
    the file's size stops changing for --settle-seconds before moving it. That
    pause is what stops us from moving a half-downloaded 2 GB film.
    """
    # Imported here rather than at the top so that --undo and normal sorting
    # still work on a machine without watchdog installed.
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("Error: --watch needs the \"watchdog\" package, which is not installed.")
        print()
        print("Install it with:")
        print("    sudo apt install python3-watchdog")
        print()
        print("Or, if you use a virtual environment:")
        print("    pip install watchdog")
        return 1

    folder = os.path.abspath(os.path.expanduser(args.folder))
    script_path = os.path.abspath(__file__)

    if not os.path.exists(folder):
        print("Error: folder does not exist: {}".format(folder))
        return 1
    if not os.path.isdir(folder):
        print("Error: not a folder: {}".format(folder))
        return 1

    if args.settle_seconds <= 0:
        print("Error: --settle-seconds must be greater than 0.")
        return 1

    print("Watching:   {}".format(folder))
    print("Mode:       {}".format(
        "DRY RUN (nothing will be changed)" if args.dry_run else "LIVE"))
    print("Settle time: {} second(s) of no size change".format(args.settle_seconds))
    print("Config:     {}".format(args.config))
    print("Log:        {}".format(args.log_file))
    print("-" * 60)
    print("Incomplete downloads ({}) are ignored.".format(
        ", ".join(config["incomplete_suffixes"])))
    print("Hidden files and subfolders are ignored.")
    print("Files already in the folder are NOT touched.")

    if not args.dry_run and not args.yes:
        if not confirm("New files in {} will be moved automatically.".format(folder)):
            print("Cancelled — nothing was changed.")
            return 0

    # --- Shared state between the watchdog thread and the settle thread ----
    # pending maps a file path to (last known size, time of last size change).
    pending = {}
    lock = threading.Lock()
    stop_event = threading.Event()

    # Totals for the goodbye summary. A dict rather than plain variables so
    # the nested handler below can update them.
    session_counts = {}
    session_errors = []
    ignored_incomplete = {"count": 0}

    # --- The watchdog event handler ---------------------------------------
    # Defined inside this function because FileSystemEventHandler has to be
    # imported first (see the lazy import above).
    class NewFileHandler(FileSystemEventHandler):
        def _consider(self, path):
            """Remember a newly seen file so the settle thread can check it."""
            if not path:
                return
            name = os.path.basename(path)
            # Ignore anything that is not a plain file in the watched folder
            # itself: no subfolders, no hidden files, no half-finished
            # downloads (those get renamed to their real name when done,
            # which fires its own event).
            if name.startswith("."):
                return
            if is_incomplete(name, config["incomplete_suffixes"]):
                # Counted so the shutdown summary can explain why a .part file
                # was never sorted. (Hidden temp files are not counted — your
                # desktop creates those constantly and it would just be noise.)
                with lock:
                    ignored_incomplete["count"] += 1
                return
            if os.path.abspath(path) == script_path:
                return
            if os.path.dirname(os.path.abspath(path)) != folder:
                return

            try:
                size = os.path.getsize(path)
            except OSError:
                # Already gone (cancelled download, or renamed by the browser).
                return

            with lock:
                pending[path] = (size, time.time())

        def on_created(self, event):
            if not event.is_directory:
                self._consider(event.src_path)

        def on_modified(self, event):
            # Downloads that write straight to the final filename only fire
            # "modified" after the first "created", so this is a safety net.
            if not event.is_directory:
                self._consider(event.src_path)

        def on_moved(self, event):
            # A .part file being renamed to its finished name lands here.
            if not event.is_directory:
                self._consider(event.dest_path)

    def handle_settled_file(path):
        """A file has stopped growing — sort it."""
        name = os.path.basename(path)

        # Re-check at the last moment: things can change while we wait.
        if not os.path.isfile(path):
            return
        if os.path.dirname(os.path.abspath(path)) != folder:
            return
        if name.startswith(".") or is_incomplete(name, config["incomplete_suffixes"]):
            return

        try:
            run_id = None
            if not args.dry_run:
                # One run per file, so --undo walks back file by file rather
                # than trying to reverse an entire evening's watching at once.
                run_id = start_logged_run(args.log_file, folder, "watch")

            category, _destination, _renamed = sort_one_file(
                path, folder, config, extension_lookup,
                args.log_file, run_id, args.dry_run)
            session_counts[category] = session_counts.get(category, 0) + 1
        except OSError as error:
            print("ERROR: could not move {}: {}".format(name, error), flush=True)
            session_errors.append((name, str(error)))

    def settle_worker():
        """Poll pending files until each one's size holds still."""
        while not stop_event.is_set():
            time.sleep(POLL_INTERVAL)
            current_time = time.time()

            with lock:
                candidates = list(pending.keys())

            for path in candidates:
                ready = False
                with lock:
                    if path not in pending:
                        continue
                    last_size, last_change = pending[path]

                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        # Vanished (cancelled or renamed). Stop tracking it.
                        pending.pop(path, None)
                        continue

                    if size != last_size:
                        # Still growing. Restart its countdown.
                        pending[path] = (size, current_time)
                        continue

                    if current_time - last_change < args.settle_seconds:
                        continue

                    # Size has held steady for long enough.
                    pending.pop(path, None)
                    ready = True

                # Move the file *outside* the lock so the watcher thread can
                # keep registering new arrivals while this one is moved.
                if ready:
                    handle_settled_file(path)

    # --- Start watching ---------------------------------------------------
    observer = Observer()
    observer.schedule(NewFileHandler(), folder, recursive=False)
    observer.start()

    settle_thread = threading.Thread(target=settle_worker, daemon=True)
    settle_thread.start()

    # --- Shut down cleanly on Ctrl+C *or* on a systemd stop ----------------
    # Ctrl+C sends SIGINT, which Python turns into KeyboardInterrupt. But
    # "systemctl --user stop" sends SIGTERM, whose default action kills the
    # process instantly — the closing summary would never reach the journal.
    # So we catch SIGTERM (and SIGHUP, for a closing terminal) too, and route
    # them through the same tidy shutdown path.
    stop_reason = {"signal": None}

    def request_stop(signum, _frame):
        # Only sets the flag. Printing from a signal handler can misbehave if
        # it lands in the middle of a write, so the message is printed after
        # the loop instead.
        stop_reason["signal"] = signum
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)

    print()
    if args.dry_run:
        print("Dry run: files will be reported but not moved.")
    print("Watching for new files. Press Ctrl+C to stop.", flush=True)

    try:
        # Waiting on an Event (instead of observer.join()) means Ctrl+C is
        # handled by us, immediately and cleanly.
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        print()
        print("Ctrl+C received — shutting down...", flush=True)
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)
        settle_thread.join(timeout=5)

    if stop_reason["signal"] is not None:
        print()
        print("Signal {} received (a systemctl stop) — shutting down...".format(
            stop_reason["signal"]), flush=True)

    # --- Goodbye summary --------------------------------------------------
    print()
    print("=" * 60)
    print("WATCH SESSION SUMMARY" + ("  (dry run)" if args.dry_run else ""))
    print("=" * 60)

    total = sum(session_counts.values())
    all_categories = list(config["categories"].keys()) + [config["fallback_category"]]
    if total == 0:
        print("  No files were sorted during this session.")
    else:
        for category in all_categories:
            count = session_counts.get(category, 0)
            if count:
                print("  {:<12} {:>4} file(s)".format(category, count))
        print("  {:<12} {:>4} file(s)".format("TOTAL", total))

    if session_errors:
        print()
        print("{} file(s) failed:".format(len(session_errors)))
        for filename, reason in session_errors:
            print("  - {}: {}".format(filename, reason))

    with lock:
        leftover = len(pending)
    if leftover:
        print()
        print("{} file(s) were still downloading and were left alone.".format(leftover))
    if ignored_incomplete["count"]:
        print()
        print("{} incomplete download(s) ({}) were ignored.".format(
            ignored_incomplete["count"], ", ".join(config["incomplete_suffixes"])))

    if total and not args.dry_run:
        print()
        print("To put these files back: python3 {} --undo".format(
            os.path.basename(script_path)))

    # Ctrl+C (or a systemctl stop) is a normal way to end a watch session,
    # so this is a success, not an error.
    return 1 if session_errors else 0


# ===========================================================================
# The undo action
# ===========================================================================
def find_undo_target(entries):
    """
    Find the most recent organize run that can still be undone.

    A run stops being undoable once an "undo" entry names it, so running
    --undo twice walks backwards through your history instead of undoing
    the same run over and over.
    """
    already_undone = {
        entry.get("undoes")
        for entry in entries
        if entry.get("type") == "undo"
    }

    runs = [entry for entry in entries if entry.get("type") == "run"]

    for run in reversed(runs):
        run_id = run.get("run_id")
        if not run_id or run_id in already_undone:
            continue
        moves = [
            entry for entry in entries
            if entry.get("type") == "move" and entry.get("run_id") == run_id
        ]
        if moves:
            return run, moves

    return None, []


def cmd_undo(args, config):
    """Reverse the last logged run. Returns a process exit code."""
    if args.folder is not None:
        print("Note: --undo works from the log, so the folder argument is ignored.")

    entries = read_log(args.log_file)
    run, moves = find_undo_target(entries)

    if run is None:
        print("Nothing to undo — no earlier run with moves was found in the log.")
        print("Log checked: {}".format(args.log_file))
        return 0

    folder = run.get("folder", "?")
    print("Undoing run {} from {}".format(run["run_id"], run.get("timestamp", "?")))
    print("Original folder: {}".format(folder))
    print("It moved {} file(s).".format(len(moves)))
    print("Log:             {}".format(args.log_file))
    print("-" * 60)

    if not args.dry_run and not args.yes:
        if not confirm("About to move these {} file(s) back.".format(len(moves))):
            print("Cancelled — nothing was changed.")
            return 0

    undo_id = None
    if not args.dry_run:
        undo_id = new_run_id()

    restored = 0
    skipped = []
    errors = []

    # Reverse order: the last file moved is the first one returned, which
    # mirrors what happened and keeps things predictable.
    for move in reversed(moves):
        original = move.get("src")
        current = move.get("dst")

        if not original or not current:
            skipped.append(("(unreadable log entry)", "missing src or dst"))
            continue

        if not os.path.exists(current):
            skipped.append((current, "no longer in the sorted location"))
            continue

        # The original spot may be occupied again by a newer download. In that
        # case make_unique_path gives us "name (1).ext" instead of clobbering it.
        original_dir = os.path.dirname(original)
        try:
            if not args.dry_run:
                os.makedirs(original_dir, exist_ok=True)

            target = make_unique_path(original_dir, os.path.basename(original))
            renamed = target != original

            if args.dry_run:
                if renamed:
                    print("[dry-run] Would restore: {}  ->  {} (renamed to avoid overwrite)".format(
                        current, target))
                else:
                    print("[dry-run] Would restore: {}  ->  {}".format(current, original))
            else:
                make_move(current, target)
                append_log_entry(args.log_file, {
                    "type": "undo_move",
                    "run_id": undo_id,
                    "timestamp": now(),
                    "src": original,
                    "dst": target,
                })
                if renamed:
                    print("Restored: {}  ->  {}  (renamed to avoid overwrite)".format(
                        current, target))
                else:
                    print("Restored: {}  ->  {}".format(current, original))

            restored += 1

        except OSError as error:
            print("ERROR: could not restore {}: {}".format(current, error))
            errors.append((current, str(error)))

    print()
    print("=" * 60)
    print("UNDO SUMMARY" + ("  (dry run — no files were moved)" if args.dry_run else ""))
    print("=" * 60)
    print("  {:<12} {:>4} file(s)".format("Restored", restored))
    if skipped:
        print("  {:<12} {:>4} file(s)".format("Skipped", len(skipped)))
        for path, reason in skipped:
            print("    - {}: {}".format(path, reason))
    if errors:
        print("  {:<12} {:>4} file(s)".format("Failed", len(errors)))

    if args.dry_run:
        print()
        if restored:
            print("Re-run without --dry-run to actually put these files back.")
    elif restored:
        # Only mark the run as undone if something actually moved, so a run
        # that failed entirely can be retried.
        append_log_entry(args.log_file, {
            "type": "undo",
            "run_id": undo_id,
            "undoes": run["run_id"],
            "timestamp": now(),
            "restored": restored,
        })
    else:
        print()
        print("No files were restored, so the run was left undone — you can retry.")

    return 1 if errors else 0


# ===========================================================================
# Command line
# ===========================================================================
def parse_arguments():
    """Read the command line options."""
    parser = argparse.ArgumentParser(
        description="Sort files in a folder into subfolders by file type.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="folder to organize (default: ~/Downloads)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without moving anything",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip the confirmation prompt before moving files",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="reverse the most recent logged run",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep running and sort new files as they finish downloading",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        help="with --watch: how long a file's size must stay unchanged "
             "before it is considered finished downloading",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="path to the JSON config file",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_PATH,
        help="path to the move log file",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.folder is None:
        args.folder = os.path.expanduser("~/Downloads")

    if args.watch and args.undo:
        print("Error: --watch and --undo cannot be combined.")
        return 1

    # Load the config once, and build the extension lookup from it.
    args.config = os.path.abspath(os.path.expanduser(args.config))
    args.log_file = os.path.abspath(os.path.expanduser(args.log_file))
    config = load_config(args.config)

    if args.undo:
        return cmd_undo(args, config)
    if args.watch:
        return cmd_watch(args, config, build_extension_lookup(config))

    return cmd_organize(args, config, build_extension_lookup(config))


if __name__ == "__main__":
    sys.exit(main())
