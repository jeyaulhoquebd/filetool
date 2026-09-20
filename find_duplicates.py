#!/usr/bin/env python3
"""
find_duplicates.py — find duplicate files across one or more folders.

Usage:
    # Report only (never touches anything):
    python3 find_duplicates.py ~/Downloads
    python3 find_duplicates.py ~/Downloads ~/Pictures ~/Documents
    python3 find_duplicates.py ~/Downloads --min-size 1MB
    python3 find_duplicates.py ~/Downloads --ext jpg,png,heic
    python3 find_duplicates.py ~/Downloads --exclude node_modules --exclude '*.cache'
    python3 find_duplicates.py ~/Downloads --report duplicates.csv
    python3 find_duplicates.py ~/Downloads --no-progress

    # Interactive cleanup (moves files to the Trash):
    python3 find_duplicates.py ~/Downloads --cleanup
    python3 find_duplicates.py ~/Downloads --cleanup --dry-run   # rehearse it

How it works (three passes, cheapest first):

  1. Walk the folders and record every file's size. Nothing is read yet —
     this is just one stat() per file.
  2. Throw away any file whose size is unique. Two files can only be
     duplicates if they are exactly the same length, so this usually removes
     almost everything without reading a single byte of content.
  3. For the files that are left, compute a SHA-256 hash and group by it.
     Files are read in 1 MiB chunks, so memory use stays flat no matter how
     large the files are.

  Only step 3 reads file contents, and only for files that survived step 2.

Two modes
---------
  Default (no --cleanup)
      Report only. Nothing is written, moved, or deleted. Files are opened
      for reading and nothing else.

  --cleanup
      Interactive. For each duplicate group you pick which copy to keep, and
      the others are moved to the system Trash. This script NEVER deletes
      anything permanently: the only thing it ever does to a file is hand it
      to `gio trash` (or send2trash), which is the same mechanism the GNOME
      Files app uses. You can restore from Trash any time.

      Moving files to the Trash does NOT free disk space. The files are
      still there until you empty the Trash. That is the point — it is
      undoable — but it means "wasted space" does not become "free space"
      until you empty it.

Dependencies
------------
  Everything here uses the standard library except the progress bar, which
  uses tqdm if it is installed:

      sudo apt install python3-tqdm

  Without tqdm the script still works — it just prints a plain count to
  stderr now and then instead of a bar. Nothing else changes.

Sizes are printed 1024-based and labelled KiB / MiB / GiB, so "1 MiB" is
1,048,576 bytes. --min-size accepts the same units: 1MB, 500 KiB, 2G, or a
plain number of bytes.
"""

import argparse
import csv
import fnmatch
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from collections import defaultdict

# How much of a file to read at once when hashing. 1 MiB keeps memory use
# tiny while still being large enough that big files are read efficiently.
# Change this if you like — it only affects speed, not the result.
CHUNK_SIZE = 1024 * 1024

# How many paths to hand `gio trash` in one go. A few hundred keeps us well
# clear of the system's argument-length limit while still being much faster
# than one process per file.
TRASH_BATCH_SIZE = 200

# With tqdm missing, a fallback line is printed to stderr this often.
FALLBACK_REPORT_EVERY = 2000

# Multipliers for --min-size. 1024-based, matching how sizes are printed.
SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
    "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4,
}


# ===========================================================================
# Command line
# ===========================================================================
def parse_size(text):
    """
    Turn a size like '1MB', '500 KiB', '2G' or '1048576' into a byte count.

    Multipliers are 1024-based, matching how the script prints sizes, so
    '1MB' and '1MiB' both mean 1,048,576 bytes. Raises ValueError on
    anything it cannot read.
    """
    cleaned = str(text).strip().lower()
    if not cleaned:
        raise ValueError("empty size")

    # Allow one space between the number and its unit ("500 KB"), but do not
    # quietly glue extra whitespace together: "1 2 MB" would otherwise become
    # "12mb" and silently mean 12 MiB instead of whatever was intended.
    parts = cleaned.split()
    if len(parts) == 1:
        cleaned = parts[0]
    elif len(parts) == 2:
        cleaned = parts[0] + parts[1]
    else:
        raise ValueError("could not read {!r} as a size (did you mean "
                         "'1MB' or '1 MB'?)".format(text))

    # Split the leading number from the trailing unit letters.
    number_part = cleaned.rstrip("abcdefghijklmnopqrstuvwxyz")
    unit_part = cleaned[len(number_part):]

    if not number_part:
        raise ValueError("no number in {!r}".format(text))
    try:
        number = float(number_part)
    except ValueError:
        raise ValueError("not a number: {!r}".format(text))

    if unit_part not in SIZE_UNITS:
        raise ValueError("unknown unit {!r} in {!r} (try B, KB, MB, GB, TB)".format(
            unit_part, text))
    if number < 0:
        raise ValueError("size cannot be negative")
    return int(number * SIZE_UNITS[unit_part])


def size_argument(text):
    """Wrapper so argparse reports parse_size problems as clean CLI errors."""
    try:
        return parse_size(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def parse_extensions(values):
    """
    Turn ['jpg,png', 'PDF'] into {'.jpg', '.png', '.pdf'}.

    Accepts commas, spaces, or repeated --ext flags, and copes with the dot
    being present or absent. Returns None when no filter was given, which
    means "every extension".
    """
    if not values:
        return None

    extensions = set()
    for value in values:
        for piece in value.replace(",", " ").split():
            piece = piece.strip().lower()
            if not piece:
                continue
            if not piece.startswith("."):
                piece = "." + piece
            extensions.add(piece)

    return extensions or None


def parse_arguments():
    """Read the command line options."""
    parser = argparse.ArgumentParser(
        description="Find duplicate files in one or more folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "folders",
        nargs="+",
        help="one or more folders to scan, recursively",
    )
    parser.add_argument(
        "--min-size",
        type=size_argument,
        default=1,
        metavar="SIZE",
        help="ignore files smaller than this. Accepts a plain number of "
             "bytes or a unit: 500KB, 1MB, 2GiB. The default of 1 skips "
             "empty files, which are all trivially identical to each other. "
             "Use --min-size 0 to include them.",
    )
    parser.add_argument(
        "--ext",
        action="append",
        metavar="LIST",
        help="only consider these extensions, e.g. --ext jpg,png or "
             "--ext .pdf --ext .docx. Everything else is ignored.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="PATTERN",
        help="skip folders matching this name or path pattern, e.g. "
             "--exclude node_modules --exclude '*.cache' "
             "--exclude /home/j/Videos. Repeatable; glob wildcards work.",
    )
    parser.add_argument(
        "--report",
        metavar="CSV",
        help="write the duplicates to a CSV file, one row per file",
    )
    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        # SUPPRESS keeps "(default: True)" out of --help, where it would read
        # backwards for a flag that switches something OFF. main() falls back
        # to True itself (see show_progress there).
        default=argparse.SUPPRESS,
        help="hide the progress bar (a progress bar is shown by default)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="interactively pick a copy to keep in each group and move the "
             "rest to the Trash (never deletes permanently)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --cleanup: go through the whole process but move nothing",
    )
    return parser.parse_args()

# ===========================================================================
# Formatting helpers
# ===========================================================================
def format_size(num_bytes):
    """Turn a byte count into something readable, e.g. 1536 -> '1.5 KiB'."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            if unit == "B":
                return "{} B".format(int(size))
            return "{:.1f} {}".format(size, unit)
        size /= 1024
    return "{:.1f} PiB".format(size)


def format_count(number):
    """Thousands separators, e.g. 1234567 -> '1,234,567'."""
    return "{:,}".format(number)


# ===========================================================================
# Progress display
# ===========================================================================
def tqdm_available():
    """True if the tqdm package can be imported."""
    try:
        import tqdm  # noqa: F401  (only checking that it imports)
        return True
    except ImportError:
        return False


class Progress:
    """
    A progress display for the slow phases.

    Uses tqdm when it is installed, which gives a real bar with a percentage
    and an ETA. Otherwise it prints a plain count to stderr every couple of
    thousand items, so the script still tells you it is alive on a machine
    without tqdm.

    Everything goes to stderr. stdout stays clean, which matters when you
    are piping the report somewhere or writing a CSV with --report.

    Used as a context manager:

        with Progress(enabled, total=100, description="Hashing") as bar:
            for item in items:
                bar.update()
    """

    def __init__(self, enabled, total=None, description="Working", unit="items"):
        self.enabled = enabled
        self.total = total
        self.description = description
        self.unit = unit
        self.count = 0
        self.bar = None
        self._next_report = FALLBACK_REPORT_EVERY

        if not enabled:
            return

        try:
            from tqdm import tqdm
        except ImportError:
            # Fall back to the plain counter below.
            return

        self.bar = tqdm(
            total=total,
            desc=description,
            unit=unit,
            file=sys.stderr,
            dynamic_ncols=True,
            leave=True,
        )

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()
        return False

    def update(self, amount=1):
        """Record that `amount` more items have been dealt with."""
        if not self.enabled:
            return
        self.count += amount

        if self.bar is not None:
            self.bar.update(amount)
            return

        if self.count >= self._next_report:
            self._next_report += FALLBACK_REPORT_EVERY
            self._print_fallback()

    def _print_fallback(self):
        """The no-tqdm version: a plain line on stderr, no rewriting."""
        if self.total:
            message = "  {}: {} of {} {}".format(
                self.description, format_count(self.count),
                format_count(self.total), self.unit)
        else:
            message = "  {}: {} {}".format(
                self.description, format_count(self.count), self.unit)
        print(message, file=sys.stderr, flush=True)

    def close(self):
        """Finish the bar, or print a final count in fallback mode."""
        if not self.enabled:
            return
        if self.bar is not None:
            self.bar.close()
        else:
            self._print_fallback()


# ===========================================================================
# Pass 1 — walk the folders and collect (path, size, mtime) triples
# ===========================================================================
def normalise_folders(folders):
    """
    Clean up the list of folders before scanning.

    Drops folders that do not exist, removes exact duplicates, and removes
    any folder that sits inside another one in the list — otherwise a nested
    folder would be scanned twice and every one of its files would look like
    a duplicate of itself.

    Returns a list of absolute folder paths.
    """
    resolved = []
    for folder in folders:
        path = os.path.realpath(os.path.expanduser(folder))
        if not os.path.isdir(path):
            print("Warning: not a folder, skipping: {}".format(folder))
            continue
        if path in resolved:
            print("Note: {} listed twice, scanning once.".format(path))
            continue
        resolved.append(path)

    # Drop any folder that lives inside another folder we are already scanning.
    kept = []
    for path in resolved:
        nested = [other for other in resolved
                  if other != path and path.startswith(other + os.sep)]
        if nested:
            print("Note: {} is inside {}, skipping it (would be scanned twice).".format(
                path, nested[0]))
            continue
        kept.append(path)

    return kept


def is_excluded(full_path, name, patterns):
    """
    True if this folder should be skipped.

    Each --exclude pattern is tried against the folder's own name
    ('node_modules', '*.cache') and against its full path
    ('/home/j/Videos'), so either style works. Matching is case-sensitive.
    """
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return True
        if fnmatch.fnmatchcase(full_path, pattern):
            return True
    return False


def collect_files(folders, min_size, extensions, exclude_patterns, show_progress):
    """
    Walk every folder and return a list of (path, size, mtime_ns) for
    regular files.

    The mtime is kept so that --cleanup can check, just before moving a file
    to the Trash, that it has not been changed since the scan.

    Deliberately skipped:
      * symlinks      — a symlink is not a second copy of a file, and
                        following them can loop forever
      * non-files     — sockets, pipes and devices are not really files
      * extra hard links — two hard links are the same data on disk, not two
                        copies, so reporting them as duplicates would make
                        the "wasted space" figure wrong
      * excluded folders — anything matching an --exclude pattern
      * filtered files   — anything not matching --ext, or under --min-size

    Nothing here is allowed to abort the scan. Unreadable folders and files
    are recorded and skipped: a permission error on one directory should
    never cost you the whole run.

    Returns (files, stats, errors).
    """
    files = []
    stats = {
        "files": 0,
        "bytes": 0,
        "symlinks": 0,
        "hardlinks": 0,
        "too_small": 0,
        "excluded_dirs": 0,
        "wrong_ext": 0,
        "unreadable": 0,
    }
    errors = []
    seen_inodes = set()

    for folder in folders:
        # onerror collects "permission denied" style problems instead of
        # letting them crash the whole scan.
        with Progress(show_progress, description="Scanning",
                      unit="files") as progress:
            for dirpath, dirnames, filenames in os.walk(
                    folder, onerror=lambda error: errors.append(str(error))):

                # Prune the walk: remove symlinked directories (following
                # them can loop forever) and anything --exclude matched.
                kept_dirs = []
                for name in sorted(dirnames):
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):
                        stats["excluded_dirs"] += 1
                        continue
                    if exclude_patterns and is_excluded(full, name, exclude_patterns):
                        stats["excluded_dirs"] += 1
                        continue
                    kept_dirs.append(name)
                dirnames[:] = kept_dirs

                for name in sorted(filenames):
                    progress.update()
                    path = os.path.join(dirpath, name)

                    if os.path.islink(path):
                        stats["symlinks"] += 1
                        continue

                    if extensions is not None:
                        if os.path.splitext(name)[1].lower() not in extensions:
                            stats["wrong_ext"] += 1
                            continue

                    try:
                        info = os.stat(path)
                    except OSError as error:
                        stats["unreadable"] += 1
                        errors.append("{}: {}".format(path, error))
                        continue

                    # Regular files only: skips sockets, pipes, devices.
                    if not stat.S_ISREG(info.st_mode):
                        continue

                    if info.st_size < min_size:
                        stats["too_small"] += 1
                        continue

                    # Hard links: only the first path to a given inode counts.
                    if info.st_nlink > 1:
                        inode = (info.st_dev, info.st_ino)
                        if inode in seen_inodes:
                            stats["hardlinks"] += 1
                            continue
                        seen_inodes.add(inode)

                    files.append((path, info.st_size, info.st_mtime_ns))
                    stats["files"] += 1
                    stats["bytes"] += info.st_size

    return files, stats, errors


# ===========================================================================
# Pass 2 — group by size, and keep only the sizes that appear more than once
# ===========================================================================
def group_by_size(files):
    """
    Return {size: [path, ...]} for sizes shared by two or more files.

    Files with a unique size cannot possibly have a duplicate, so they are
    dropped here and never read from disk.
    """
    by_size = defaultdict(list)
    for path, size, _mtime in files:
        by_size[size].append(path)

    return {
        size: sorted(paths)
        for size, paths in by_size.items()
        if len(paths) > 1
    }


# ===========================================================================
# Pass 3 — hash the survivors, in chunks, and group by hash
# ===========================================================================
def hash_file(path, chunk_size=CHUNK_SIZE):
    """
    Return the SHA-256 hex digest of a file, reading it in chunks.

    Only `chunk_size` bytes are ever held in memory, so hashing a 4 GiB film
    uses the same amount of RAM as hashing a 4 KiB text file.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def find_duplicate_groups(size_groups, errors, show_progress):
    """
    Hash every file in `size_groups` and return the groups that match.

    `size_groups` is the {size: [paths]} dict from group_by_size().
    Returns a list of (size, [paths]) sorted by wasted space, biggest first.

    A file that cannot be read is recorded in `errors` and skipped — one
    locked or unreadable file must not bring down the whole run.
    """
    duplicates = []
    total_to_hash = sum(len(paths) for paths in size_groups.values())

    with Progress(show_progress, total=total_to_hash,
                  description="Hashing", unit="files") as progress:

        for size, paths in size_groups.items():
            by_hash = defaultdict(list)

            for path in paths:
                progress.update()

                try:
                    digest = hash_file(path)
                except OSError as error:
                    errors.append("{}: {}".format(path, error))
                    continue

                # If the file changed size while we were reading it, the
                # digest describes a file that no longer exists. Skipping is
                # safer than reporting a duplicate that is not one.
                try:
                    if os.path.getsize(path) != size:
                        errors.append(
                            "{}: changed size while being read, skipped".format(path))
                        continue
                except OSError as error:
                    errors.append("{}: {}".format(path, error))
                    continue

                by_hash[digest].append(path)

            for digest, matching in by_hash.items():
                if len(matching) > 1:
                    duplicates.append((size, sorted(matching)))

    # Biggest waste first — that is what you most want to see.
    duplicates.sort(key=lambda group: (len(group[1]) - 1) * group[0], reverse=True)
    return duplicates


# ===========================================================================
# CSV export
# ===========================================================================
def write_csv_report(path, duplicates, folders, stats):
    """
    Write the duplicate groups to a CSV file, one row per file.

    Uses the csv module rather than joining strings by hand, so a path
    containing a comma, a quote, or a newline is quoted correctly and the
    file still opens cleanly in a spreadsheet.

    Returns the number of data rows written, or None if the file could not
    be written.
    """
    path = os.path.abspath(os.path.expanduser(path))

    try:
        # newline="" is what the csv module documents; without it you get
        # blank lines between rows on some platforms. lineterminator="\n"
        # overrides the module's CRLF default, which is aimed at Windows and
        # leaves stray ^M characters in ordinary Linux tools like head, diff
        # and awk. Excel and LibreOffice read plain \n quite happily.
        # csv.writer also handles quoting, so a path containing a comma, a
        # quote, or a newline comes back out intact.
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow([
                "group", "path", "size_bytes", "size_human",
                "copies_in_group", "wasted_bytes",
            ])
            rows = 0
            for number, (size, paths) in enumerate(duplicates, start=1):
                wasted = (len(paths) - 1) * size
                for file_path in paths:
                    writer.writerow([
                        number, file_path, size, format_size(size),
                        len(paths), wasted,
                    ])
                    rows += 1
    except OSError as error:
        print("Error: could not write CSV report to {}: {}".format(path, error))
        return None

    return rows


# ===========================================================================
# Reporting (read-only mode)
# ===========================================================================
def print_report(folders, stats, size_groups, duplicates, duplicates_hashed, errors):
    """Print the duplicate groups and the summary. Returns an exit code."""
    total_hashed_bytes = sum(
        size * len(paths) for size, paths in size_groups.items())

    print()
    print("Scanned {} file(s) ({}) in {} folder(s).".format(
        format_count(stats["files"]), format_size(stats["bytes"]), len(folders)))

    if duplicates_hashed:
        print("{} of them share a size with another file, so {} of content "
              "was read to compare them.".format(
                  format_count(duplicates_hashed), format_size(total_hashed_bytes)))
    else:
        print("No two files share a size, so no content needed to be read.")

    # --- The duplicate groups themselves ---------------------------------
    if not duplicates:
        print()
        print("No duplicate files found.")
        print_skips(stats, errors)
        print_readonly_note()
        return 0

    total_wasted = sum((len(paths) - 1) * size for size, paths in duplicates)
    total_duplicate_files = sum(len(paths) for _size, paths in duplicates)

    print()
    print("Found {} duplicate group(s):".format(format_count(len(duplicates))))

    for number, (size, paths) in enumerate(duplicates, start=1):
        print_duplicate_group(number, len(duplicates), size, paths)

    # --- Summary ----------------------------------------------------------
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("  {:<22} {}".format("Folders scanned", len(folders)))
    print("  {:<22} {} ({})".format(
        "Files scanned", format_count(stats["files"]), format_size(stats["bytes"])))
    print("  {:<22} {}".format("Duplicate groups", format_count(len(duplicates))))
    print("  {:<22} {}".format(
        "Duplicate files", format_count(total_duplicate_files)))
    print("  {:<22} {}".format("Wasted space", format_size(total_wasted)))
    print()
    print("  \"Wasted space\" means what you would get back if you kept one")
    print("  copy of each group and removed the rest: (copies - 1) x size.")

    print_skips(stats, errors)
    print_readonly_note()
    return 0


def print_duplicate_group(number, total, size, paths):
    """Print one duplicate group in the report format."""
    wasted = (len(paths) - 1) * size
    print()
    print("-" * 70)
    print("Group {} of {} — {} copies of {} each — {} wasted".format(
        number, format_count(total), len(paths),
        format_size(size), format_size(wasted)))
    print("-" * 70)
    for path in paths:
        print("  {}".format(path))


def print_skips(stats, errors):
    """Report what was deliberately ignored, and anything that went wrong."""
    skipped = []
    if stats["symlinks"]:
        skipped.append("{} symlink(s)".format(format_count(stats["symlinks"])))
    if stats["hardlinks"]:
        skipped.append("{} extra hard link(s)".format(format_count(stats["hardlinks"])))
    if stats["excluded_dirs"]:
        skipped.append("{} excluded folder(s)".format(
            format_count(stats["excluded_dirs"])))
    if stats["wrong_ext"]:
        skipped.append("{} file(s) filtered out by --ext".format(
            format_count(stats["wrong_ext"])))
    if stats["too_small"]:
        skipped.append("{} file(s) under --min-size".format(
            format_count(stats["too_small"])))

    if skipped:
        print()
        print("Skipped: {}.".format(", ".join(skipped)))

    if errors:
        print()
        print("{} problem(s) encountered:".format(format_count(len(errors))))
        for message in errors[:20]:
            print("  - {}".format(message))
        if len(errors) > 20:
            print("  ... and {} more.".format(format_count(len(errors) - 20)))


def print_readonly_note():
    """Remind the user that nothing was changed."""
    print()
    print("Read-only: nothing was modified, moved, or deleted.")


# ===========================================================================
# The Trash
# ===========================================================================
def find_trash_backend():
    """
    Work out how to send files to the Trash.

    Prefers the `gio` command: it ships with GLib, so it is already present
    on any GNOME desktop, needs no Python package, and is the exact same
    mechanism the Files app uses. Falls back to the third-party send2trash
    package if gio is missing.

    Returns "gio", "send2trash", or None if neither is available.
    """
    if shutil.which("gio"):
        return "gio"
    try:
        import send2trash  # noqa: F401  (only checking that it imports)
        return "send2trash"
    except ImportError:
        return None


def explain_trash_failure(message):
    """
    Add a hint to gio's error message when it is the well-known one.

    gio follows the freedesktop Trash specification, which can only put files
    in the Trash when they are on the same filesystem as your home folder.
    Files on another drive (a USB stick, a second disk, /tmp on some systems)
    are refused, because there is nowhere for them to go.
    """
    if "system internal mounts" in message:
        return (message + "\n      (gio can only trash files on the same drive "
                "as your home folder. Files on other drives have to be moved "
                "by hand, or with rm if you are sure.)")
    return message


def trash_files(paths, backend):
    """
    Move files to the system Trash.

    Returns (trashed, failures) where trashed is the list of paths that are
    now in the Trash and failures is a list of (path, reason).
    """
    if not paths:
        return [], []

    if backend == "gio":
        return _trash_with_gio(paths)

    return _trash_with_send2trash(paths)


def _trash_with_gio(paths):
    """Send files to the Trash using the `gio trash` command."""
    trashed = []
    failures = []

    # Work in batches: one gio process for a few hundred files is far faster
    # than one process per file.
    for start in range(0, len(paths), TRASH_BATCH_SIZE):
        batch = paths[start:start + TRASH_BATCH_SIZE]

        # The "--" stops gio from reading a filename that begins with a dash
        # as a command line option.
        result = subprocess.run(
            ["gio", "trash", "--"] + batch,
            capture_output=True, text=True)

        if result.returncode == 0:
            trashed.extend(batch)
            continue

        # Something in the batch failed, and gio does not say which one, so
        # retry them one at a time to find out exactly which are the problem.
        for path in batch:
            if not os.path.exists(path):
                # The batch did move this one before hitting the bad file.
                trashed.append(path)
                continue

            one = subprocess.run(
                ["gio", "trash", "--", path],
                capture_output=True, text=True)

            if one.returncode == 0:
                trashed.append(path)
            else:
                reason = (one.stderr or one.stdout).strip() or "gio refused it"
                # gio prefixes messages with "gio: file:///the/path: ", which
                # is noise here because we already print the path. The real
                # message is whatever follows the last colon.
                reason = reason.rsplit(":", 1)[-1].strip() or reason
                failures.append((path, explain_trash_failure(reason)))

    return trashed, failures


def _trash_with_send2trash(paths):
    """Send files to the Trash using the send2trash package."""
    from send2trash import send2trash

    trashed = []
    failures = []
    for path in paths:
        try:
            send2trash(path)
            trashed.append(path)
        except Exception as error:
            # send2trash raises a variety of exception types depending on
            # what went wrong. For this one job, reporting the message is
            # more useful than letting an unusual exception type escape.
            failures.append((path, explain_trash_failure(str(error))))
    return trashed, failures


def other_device_paths(paths):
    """
    Return the paths that live on a different filesystem than your home
    folder, because gio will almost certainly refuse to trash those.

    Checking up front means we can warn before you spend ten minutes
    choosing files, rather than after.
    """
    try:
        home_device = os.stat(os.path.expanduser("~")).st_dev
    except OSError:
        return []

    on_other_device = []
    for path in paths:
        try:
            if os.stat(path).st_dev != home_device:
                on_other_device.append(path)
        except OSError:
            continue
    return on_other_device


def unchanged_since_scan(path, file_info):
    """
    Cheap check that a file still looks the way it did during the scan.

    Compares size and modification time — one stat() call, no content read.
    It cannot catch an edit that preserved both, but it does catch the
    realistic case of a file that has been changed since we scanned it, which
    is exactly when moving it to the Trash would be a mistake.
    """
    expected = file_info.get(path)
    if expected is None:
        return False
    expected_size, expected_mtime = expected
    try:
        info = os.stat(path)
    except OSError:
        return False
    return (info.st_size == expected_size
            and info.st_mtime_ns == expected_mtime)


# ===========================================================================
# Interactive cleanup
# ===========================================================================
def parse_keep_choice(answer, count):
    """
    Work out what the user typed.

    Returns:
        ("keep", [1-based numbers to keep])
        ("skip", None)   keep every copy in this group
        ("stop", None)   stop reviewing and move on to the confirmation

    Raises ValueError with a message for the caller to print if the answer
    makes no sense.
    """
    text = answer.strip().lower()

    if text in ("q", "quit", "stop"):
        return "stop", None
    if text in ("s", "skip"):
        return "skip", None
    if not text:
        raise ValueError("nothing entered")

    numbers = set()
    # Accept "1", "1,3", or "1 3".
    for piece in text.replace(",", " ").split():
        if not piece.isdigit():
            raise ValueError("not a number: {!r}".format(piece))
        number = int(piece)
        if number < 1 or number > count:
            raise ValueError("out of range: {} (choose 1-{})".format(number, count))
        numbers.add(number)

    if not numbers:
        raise ValueError("nothing entered")
    return "keep", sorted(numbers)


def build_cleanup_plan(duplicates, file_info):
    """
    Walk the user through each duplicate group and collect their choices.

    Returns (plan, groups_reviewed, groups_skipped, stopped_early).

    Each entry in `plan` is (size, [keepers], [paths_to_trash]).
    """
    plan = []
    groups_skipped = 0
    groups_reviewed = 0
    stopped_early = False

    on_other_device = set(other_device_paths(
        path for _size, paths in duplicates for path in paths))

    print()
    print("=" * 70)
    print("INTERACTIVE CLEANUP — {} group(s) to review".format(
        format_count(len(duplicates))))
    print("=" * 70)
    print()
    print("For each group, choose which copy to KEEP. The others are moved")
    print("to the Trash — never deleted, and always restorable.")
    print()
    print("  a number      keep that copy, trash the rest of the group")
    print("  1,3           keep more than one (trash the others)")
    print("  s             skip this group, keep everything")
    print("  q             stop reviewing, go to the final confirmation")
    print()
    if on_other_device:
        print("Note: some files are on a different drive than your home folder.")
        print("      gio cannot trash those; they are marked below.")
        print()

    for number, (size, paths) in enumerate(duplicates, start=1):
        print("-" * 70)
        print("Group {} of {} — {} copies of {} each — {} wasted".format(
            number, format_count(len(duplicates)), len(paths),
            format_size(size), format_size((len(paths) - 1) * size)))
        print("-" * 70)

        for index, path in enumerate(paths, start=1):
            marker = ""
            if path in on_other_device:
                marker = "   [another drive — gio may refuse this one]"
            print("  {}) {}{}".format(index, path, marker))

        # Keep asking until we get an answer we understand.
        while True:
            answer = ask("Keep which? "
                         "(number, 1,3, s=skip, q=finish): ")
            if answer is None:
                # No more input (Ctrl+D, or a closed pipe). Stop reviewing
                # and let the user confirm what has been chosen so far.
                print()
                print("No more input — finishing the review.")
                stopped_early = True
                break
            try:
                action, keep_numbers = parse_keep_choice(answer, len(paths))
            except ValueError as error:
                print("  Sorry, {}".format(error))
                continue
            break

        if answer is None:
            break

        groups_reviewed += 1

        if action == "stop":
            stopped_early = True
            break

        if action == "skip":
            groups_skipped += 1
            print("  Skipped — keeping all {} copies.".format(len(paths)))
            continue

        keepers = [paths[number - 1] for number in keep_numbers]
        to_trash = [path for path in paths if path not in keepers]

        if not to_trash:
            groups_skipped += 1
            print("  All {} copies kept — nothing to trash.".format(len(paths)))
            continue

        # Every copy the user chose to keep is recorded, not just the first:
        # before trashing anything we need to find at least one of them still
        # present and unmodified.
        plan.append((size, keepers, to_trash))
        print("  Keeping {} — {} file(s), {}, will be trashed.".format(
            ", ".join(os.path.basename(keeper) for keeper in keepers),
            len(to_trash), format_size(sum(size for _ in to_trash))))

    return plan, groups_reviewed, groups_skipped, stopped_early


def ask(prompt):
    """
    Read a line from the user.

    Returns None if there is no more input to read (Ctrl+D, or the script was
    run with its input piped from something that has ended), which callers
    treat as "stop asking".
    """
    try:
        return input(prompt)
    except EOFError:
        return None
    except KeyboardInterrupt:
        # Ctrl+C here means "get me out of here" — fall through to the
        # caller, which will abort without trashing anything.
        print()
        raise


def run_cleanup(args, duplicates, file_info, folders, stats, errors):
    """Interactive cleanup: choose keepers, confirm, then move to the Trash."""
    backend = find_trash_backend()
    if backend is None:
        print()
        print("Error: no way to send files to the Trash was found.")
        print()
        print("Install one of these, then try again:")
        print("    sudo apt install libglib2.0-bin     # the 'gio' command")
        print("    python3 -m pip install send2trash   # or the Python package")
        return 1

    print()
    print("Scanned {} file(s) ({}) in {} folder(s) — {} duplicate group(s).".format(
        format_count(stats["files"]), format_size(stats["bytes"]),
        len(folders), format_count(len(duplicates))))
    print("Trash method: {}{}".format(
        backend, "  (dry run — nothing will be moved)" if args.dry_run else ""))

    if errors:
        print("{} problem(s) came up during the scan; run without --cleanup "
              "to see them.".format(format_count(len(errors))))

    try:
        plan, reviewed, skipped, stopped_early = build_cleanup_plan(
            duplicates, file_info)
    except KeyboardInterrupt:
        print()
        print("Cancelled — nothing was moved.")
        return 0

    if stopped_early:
        print()
        print("Review ended early.")

    if not plan:
        print()
        print("Nothing selected, so there is nothing to do.")
        return 0

    # --- Final confirmation ----------------------------------------------
    files_to_trash = [path for _size, _keepers, paths in plan for path in paths]
    bytes_affected = sum(size for size, _keepers, paths in plan
                         for _ in paths)

    print()
    print("=" * 70)
    print("READY TO CLEAN UP")
    print("=" * 70)
    print("  {:<22} {}".format("Groups reviewed", format_count(reviewed)))
    print("  {:<22} {}".format("Groups skipped", format_count(skipped)))
    print("  {:<22} {}".format("Groups with files to trash",
                               format_count(len(plan))))
    print("  {:<22} {}".format("Files to move to Trash",
                               format_count(len(files_to_trash))))
    print("  {:<22} {}".format("Space affected", format_size(bytes_affected)))
    print()
    print("  These {} file(s) will be MOVED TO THE TRASH, not deleted.".format(
        format_count(len(files_to_trash))))
    print("  You can restore them from Files > Trash at any time.")
    print()
    print("  Note: this does NOT free {} of disk space yet. The files".format(
        format_size(bytes_affected)))
    print("  still use that space until you empty the Trash.")
    print()

    answer = ask("Proceed? [y/N] ")
    if answer is None or answer.strip().lower() not in ("y", "yes"):
        print("Cancelled — nothing was moved.")
        return 0

    # --- Safety check, then the move -------------------------------------
    print()
    print("Checking the files have not changed since the scan...")

    ready = []
    problems = []

    for size, keepers, paths in plan:
        # At least one of the copies being kept must still be there and
        # unmodified. If none is, we must not trash the others — that could
        # destroy the only good copy left.
        verified_keeper = None
        for keeper in keepers:
            if os.path.exists(keeper) and unchanged_since_scan(keeper, file_info):
                verified_keeper = keeper
                break

        if verified_keeper is None:
            reason = ("none of the copies you chose to keep is still intact "
                      "({}), so nothing in this group was touched".format(
                          ", ".join(keepers)))
            for path in paths:
                problems.append((path, reason))
            continue

        for path in paths:
            if not os.path.exists(path):
                problems.append((path, "no longer exists"))
            elif not unchanged_since_scan(path, file_info):
                problems.append((path, "changed since the scan, so it may no "
                                 "longer be a duplicate"))
            else:
                ready.append(path)

    if not ready:
        print()
        print("Nothing could be safely moved:")
        for path, reason in problems:
            print("  - {}: {}".format(path, reason))
        return 0

    if args.dry_run:
        print()
        print("[dry-run] Would move {} file(s) to the Trash:".format(
            format_count(len(ready))))
        for path in ready:
            print("  [dry-run] {}".format(path))
        trashed, failures = ready, []
    else:
        print("Moving {} file(s) to the Trash...".format(format_count(len(ready))))
        trashed, failures = trash_files(ready, backend)
        for path in trashed:
            print("  trashed: {}".format(path))

    failures = problems + failures

    # --- Result ----------------------------------------------------------
    trashed_set = set(trashed)
    moved_bytes = sum(size for size, _keepers, paths in plan
                      for path in paths if path in trashed_set)

    print()
    print("=" * 70)
    print("CLEANUP SUMMARY" + ("  (dry run)" if args.dry_run else ""))
    print("=" * 70)
    print("  {:<22} {}".format(
        "Files trashed", format_count(len(trashed))))
    print("  {:<22} {}".format("Space affected", format_size(moved_bytes)))

    if failures:
        print("  {:<22} {}".format("Skipped/failed", format_count(len(failures))))
        print()
        for path, reason in failures:
            print("  - {}: {}".format(path, reason))

    print()
    if args.dry_run:
        print("Dry run — nothing was actually moved.")
        print("Re-run without --dry-run to do it for real.")
    elif not trashed:
        # Never claim success when nothing actually moved. Saying "they are in
        # the Trash" here would be a lie about the state of your files, which
        # is the one thing this script must not do.
        print("Nothing was moved. Every file was skipped or refused — see the")
        print("problems listed above. Your files are exactly as they were.")
    else:
        print("Done. The {} file(s) listed as trashed are in the Trash and can".format(
            format_count(len(trashed))))
        print("be restored from Files > Trash. Empty the Trash to actually free")
        print("the space.")
        if failures:
            print()
            print("The {} file(s) listed as failed were left untouched.".format(
                format_count(len(failures))))

    return 0


# ===========================================================================
# Main
# ===========================================================================
def main():
    args = parse_arguments()

    if args.dry_run and not args.cleanup:
        print("Note: --dry-run only means something together with --cleanup;")
        print("      without --cleanup the script is already read-only.")

    folders = normalise_folders(args.folders)
    if not folders:
        print("Error: no valid folders to scan.")
        return 1

    extensions = parse_extensions(args.ext)
    exclude_patterns = args.exclude or []

    # Tell the user once if the progress bar will be the plain version.
    # --no-progress sets args.progress to False; when the flag is absent the
    # attribute does not exist at all (its default is SUPPRESS so that --help
    # does not claim "default: True" for a flag that turns something off).
    show_progress = getattr(args, "progress", True)
    if show_progress and not tqdm_available():
        print("Note: tqdm is not installed, so the progress bar is a plain")
        print("      counter. For a real bar: sudo apt install python3-tqdm")

    print("Scanning {} folder(s)...".format(len(folders)))
    for folder in folders:
        print("  {}".format(folder))
    if args.min_size:
        print("  skipping files under {}".format(format_size(args.min_size)))
    if extensions:
        print("  only these extensions: {}".format(
            ", ".join(sorted(extensions))))
    if exclude_patterns:
        print("  excluding folders matching: {}".format(
            ", ".join(exclude_patterns)))

    errors = []

    # Pass 1: one stat() per file. No content is read.
    files, stats, walk_errors = collect_files(
        folders, args.min_size, extensions, exclude_patterns, show_progress)
    errors.extend(walk_errors)

    # Look-up table of what we saw, used by --cleanup to spot changes.
    file_info = {path: (size, mtime) for path, size, mtime in files}

    if not files:
        print()
        print("No files found to compare.")
        print_skips(stats, errors)
        if args.report:
            rows = write_csv_report(args.report, [], folders, stats)
            if rows is None:
                return 1
            print("CSV report written to {} (header only, no duplicates).".format(
                os.path.abspath(os.path.expanduser(args.report))))
        print_readonly_note()
        return 0

    # Pass 2: sizes only. Unique sizes drop out here, unread.
    size_groups = group_by_size(files)
    duplicates_hashed = sum(len(paths) for paths in size_groups.values())

    # Pass 3: only now do we read any file content, in chunks.
    duplicates = find_duplicate_groups(size_groups, errors, show_progress)

    # Export before cleanup, so the CSV is a record of what was found
    # rather than of what is left.
    report_failed = False
    if args.report:
        rows = write_csv_report(args.report, duplicates, folders, stats)
        if rows is None:
            # The scan itself worked, so keep going and show the results —
            # but remember the failure for the exit code below.
            report_failed = True
        else:
            print()
            print("CSV report written to {} ({} row(s)).".format(
                os.path.abspath(os.path.expanduser(args.report)),
                format_count(rows)))

    if args.cleanup:
        exit_code = run_cleanup(args, duplicates, file_info, folders, stats, errors)
    else:
        exit_code = print_report(folders, stats, size_groups, duplicates,
                                 duplicates_hashed, errors)

    if report_failed:
        # You asked for a report and did not get one, so do not exit 0 and
        # let a script think everything went fine.
        print()
        print("Note: the CSV report could not be written (see the error above).")
        return 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
