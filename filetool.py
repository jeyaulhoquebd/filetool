#!/usr/bin/env python3
"""
filetool.py — one program for renaming and reshaping lots of files at once.

Commands
--------
    rename   rename many files at once (prefix, replace, number, case, undo)
    image    compress, resize and convert images, never touching the originals

Usage
-----
    # See what a rename would do, change nothing:
    python3 filetool.py rename ~/Pictures --prefix "trip_" --dry-run

    # Number a folder of photos: photo_001.jpg, photo_002.jpg, ...
    python3 filetool.py rename ~/Pictures --pattern "*.jpg" --base photo --number

    # Put back the names from the last rename:
    python3 filetool.py rename --undo

    # Shrink images into a new folder, at most 1600 pixels wide:
    python3 filetool.py image ~/Pictures --quality 80 --max-width 1600

    # Convert a folder of PNGs to WebP:
    python3 filetool.py image ~/Pictures --format webp --quality 82

    # See what the image command would do, write nothing:
    python3 filetool.py image ~/Pictures --max-width 1200 --dry-run

Run "python3 filetool.py rename --help" or "python3 filetool.py image --help"
for every option each command takes.

How the two commands work
-------------------------
rename  builds a new name for each file from the options you give it, shows
        you a before/after table, and refuses the whole run if any two files
        would end up with the same name. Every rename is written to a log
        before it happens, so --undo can put the names back.

image   reads each image, optionally rotates it upright, optionally scales
        it down, and writes the result into a NEW folder. The input files are
        only ever opened for reading.

Nothing here overwrites a file it was not explicitly told to overwrite, and
both commands have a --dry-run that changes nothing at all. Start there.

The progress bar
----------------
Both commands show a progress bar on stderr while they work, so it never
mixes into the results on stdout. It needs no package: with tqdm installed
you get a proper moving bar, and without it you get a plain counter. Turn it
off with --no-progress.

Dependencies
------------
rename  needs nothing but the standard library.
image   needs Pillow, which is already installed on most Ubuntu desktops:
            sudo apt install python3-pil
        If it is missing, only the image command stops — rename still works.

tqdm is optional and only makes the progress bar prettier:
            sudo apt install python3-tqdm

This file replaces the earlier bulk_rename.py and image_tool.py, which are
left on disk untouched. It needs Python 3.8 or newer.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time

# Pillow is needed by the image command only. It is imported here rather than
# at the point of use so that a missing Pillow produces a helpful message
# instead of a bare traceback — and so that `filetool rename` still works
# perfectly well without it.
try:
    from PIL import Image, ImageOps
except ImportError as error:      # pragma: no cover - depends on the machine
    PILLOW_IMPORT_ERROR = error
    Image = ImageOps = None
    RESAMPLE_FILTER = None
else:
    PILLOW_IMPORT_ERROR = None

    # Pillow renamed the resampling filters, so support both spellings. LANCZOS
    # is the slowest and the best looking, which is what you want for photos.
    #
    # This has to be worked out inside the guard, not further down the file: a
    # module-level `Image.Resampling` would be looked up every time the program
    # starts, and with no Pillow installed it would raise NameError before a
    # single argument was read — so `filetool rename`, which wants nothing from
    # Pillow, would die too.
    try:
        RESAMPLE_FILTER = Image.Resampling.LANCZOS
    except AttributeError:        # Pillow older than 9.1
        RESAMPLE_FILTER = Image.LANCZOS


# How the program was typed, used in hints like "run ... rename --undo".
PROGRAM = os.path.basename(sys.argv[0]) or "filetool.py"

# Without tqdm there is no timer to redraw on, so the plain counter only
# prints every so often. Without this a scan of 100,000 files would print
# 100,000 lines.
FALLBACK_REPORT_EVERY = 2000


# ===========================================================================
# Settings for the rename command
# ===========================================================================
# Where every rename is recorded, so --undo can reverse it. --log-file
# overrides it (handy for testing).
DEFAULT_LOG_PATH = os.path.expanduser("~/.local/state/bulk-rename/history.log")

# Every file being renamed is parked at a temporary name while the run is in
# progress. The leading dot keeps these hidden, and the name is distinctive
# enough that it will not collide with a real file.
TEMP_PREFIX = ".bulkrename-tmp-"

# Extensions that are really two extensions. ".tar.gz" is the extension of
# "backup.tar.gz"; without this the stem would be "backup.tar" and an --upper
# would give you "BACKUP.tar.gz". Same list as organize_downloads.py.
DOUBLE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

# Used between a number and the rest of the name: "photo_001".
NUMBER_SEPARATOR = "_"

# Longest a single filename may be, in bytes, on Linux.
MAX_NAME_BYTES = 255


# ===========================================================================
# Settings for the image command
# ===========================================================================
# Quality used when --quality is not given.
DEFAULT_QUALITY = 85

# The output formats that can be asked for by name.
#   name -> (Pillow's format name, the file extension to use)
REQUESTED_FORMATS = {
    "jpeg": ("JPEG", ".jpg"),
    "png": ("PNG", ".png"),
    "webp": ("WEBP", ".webp"),
}

# What a file's own format maps to when --format keep is used. Anything a
# camera or phone produces is here; an exotic format not in this table is
# reported and skipped rather than guessed at.
SOURCE_FORMATS = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "GIF": ("GIF", ".gif"),
    "BMP": ("BMP", ".bmp"),
    "TIFF": ("TIFF", ".tif"),
}

# The same mapping, looked up by file extension. This is used before the file
# is opened, so that the destination path is final by the time the
# does-it-already-exist check runs. Deciding the extension later would mean
# checking one path and writing to another, which is how a script quietly
# overwrites something it promised not to touch.
EXTENSION_FORMATS = {
    ".jpg": ("JPEG", ".jpg"),
    ".jpeg": ("JPEG", ".jpg"),
    ".jpe": ("JPEG", ".jpg"),
    ".jfif": ("JPEG", ".jpg"),
    ".png": ("PNG", ".png"),
    ".webp": ("WEBP", ".webp"),
    ".gif": ("GIF", ".gif"),
    ".bmp": ("BMP", ".bmp"),
    ".tif": ("TIFF", ".tif"),
    ".tiff": ("TIFF", ".tif"),
}

# Extensions worth trying to open. This is only a quick first filter — what
# actually matters is whether Pillow can read the file, which is found out
# when it is opened.
IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".jfif", ".jpe",
    ".png", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".ppm", ".pgm",
)


# ===========================================================================
# Shared helpers
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


def positive_int(text):
    """argparse type: a whole number of 1 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, got {}".format(value))
    return value


def warn_to(warn):
    """
    Where a warning message should go.

    The command line lets warnings go to the screen with print(). A front end
    with a window of its own passes its own function instead — a log pane, a
    list, anything callable with one line of text. Every warning in the core
    goes through here, so neither front end has to capture stdout to see one.
    """
    return print if warn is None else warn


def tqdm_available():
    """
    True if the tqdm package can be imported.

    tqdm draws a proper moving bar with a time estimate. It is entirely
    optional: the fallback below prints a plain counter instead, so the
    program never fails just because a package is missing.
    """
    try:
        import tqdm          # noqa: F401  (imported only to test for it)
        return True
    except ImportError:
        return False


class Progress:
    """
    A progress bar for a long loop, written to stderr.

    stderr, not stdout, so that redirecting the results to a file
    ("... > results.txt") does not fill that file with progress lines.

    Use it as a context manager:

        with Progress(total, "Processing") as bar:
            for item in items:
                ...
                bar.update()

    `total` may be None when the number of items is not known in advance, as
    when scanning a folder tree; the bar then counts without a percentage.
    """

    def __init__(self, total, label, enabled=True):
        self.total = None if total is None else int(total)
        self.label = label
        self.enabled = bool(enabled) and (self.total is None or self.total > 0)
        self.count = 0
        self._bar = None          # the tqdm object, when tqdm is installed
        self._drawn = False       # True once we have written to stderr
        self._last_width = 0      # length of the last fallback line drawn

    def __enter__(self):
        if not self.enabled:
            return self

        try:
            from tqdm import tqdm
        except ImportError:
            # No tqdm: use the plain counter, and print a first line straight
            # away so a long silent wait never looks like a hang.
            self._plain_report()
            return self

        # leave=False removes the bar when it finishes, so it does not sit
        # above the results for ever.
        self._bar = tqdm(total=self.total, desc=self.label, unit="file",
                         leave=False, file=sys.stderr)

        # tqdm works out how wide to draw itself from the terminal size. If
        # it cannot — a terminal that will not report its size gives ncols
        # -1 — it draws a zero-width bar, which is to say nothing at all. A
        # plain counter beats a progress bar that never appears.
        ncols = getattr(self._bar, "ncols", None)
        if isinstance(ncols, int) and ncols <= 0:
            self._bar.close()
            self._bar = None
            self._plain_report()

        return self

    def update(self, step=1):
        """Record that `step` more items are done."""
        if not self.enabled:
            return

        self.count += step

        if self._bar is not None:
            self._bar.update(step)
            return

        # Without tqdm, print sparingly, and always print the final line.
        if self.total is None:
            if self.count % FALLBACK_REPORT_EVERY == 0:
                self._plain_report()
        elif self.count >= self.total or self.count % FALLBACK_REPORT_EVERY == 0:
            self._plain_report()

    def _plain_report(self):
        """One line of the no-tqdm fallback bar."""
        if self.total:
            body = "{}/{} ({}%)".format(
                self.count, self.total, int(self.count / self.total * 100))
        else:
            body = "{} so far".format(self.count)

        line = "  {} {}".format(self.label, body)
        self._last_width = len(line)

        # On a terminal the line is rewritten in place. Anywhere else (a log
        # file, a pipe) a carriage return would just be litter, so a fresh
        # line is printed instead — which is why update() calls this rarely.
        if sys.stderr.isatty():
            sys.stderr.write("\r" + line)
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()
        self._drawn = True

    def clear(self):
        """
        Take the bar off the line so ordinary output can be printed there.

        Without this, a result row printed while the bar is showing lands on
        top of it and both become unreadable. The bar comes back by itself
        the next time update() is called.
        """
        if not self.enabled:
            return

        if self._bar is not None:
            # tqdm can erase its own line, but clear() only appeared in
            # tqdm 4.66, so an older one is left to redraw over itself.
            clear = getattr(self._bar, "clear", None)
            if clear is not None:
                clear()
            return

        if self._drawn and sys.stderr.isatty():
            # Overwrite the line with spaces, then return to its start. No
            # escape sequences, so this works on any terminal.
            sys.stderr.write("\r" + " " * self._last_width + "\r")
            sys.stderr.flush()

    def __exit__(self, exc_type, exc_value, traceback):
        if self._bar is not None:
            self._bar.close()
            self._bar = None

        # Finish the rewritten line so later output starts on a clean line.
        if self._drawn and sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()

        return False              # never swallow an exception


class HelpFormatter(argparse.RawDescriptionHelpFormatter,
                    argparse.ArgumentDefaultsHelpFormatter):
    """
    Help formatting for the image command.

    Two things are wanted at once, and argparse offers them separately:

      * the examples in the epilog kept exactly as they were written, rather
        than re-flowed into one paragraph
        (that is RawDescriptionHelpFormatter)
      * "(default: 85)" added to each option, so no default has to be
        repeated by hand in the help text
        (that is ArgumentDefaultsHelpFormatter)

    The one adjustment is that nothing is said about options with no real
    default. "(default: None)" tells the reader nothing, and SUPPRESS is
    internal bookkeeping that should never be printed.
    """

    def _get_help_string(self, action):
        if action.default is None or action.default is argparse.SUPPRESS:
            return action.help
        return super()._get_help_string(action)


# ===========================================================================
# THE RENAME COMMAND
# ===========================================================================
def now_stamp():
    """A readable timestamp for the log."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_run_id():
    """
    A short unique id for this run, e.g. '20260919-234512-4321'.

    The pid on the end means two runs started in the same second still get
    different ids.
    """
    return "{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), os.getpid())


def zero_or_more_int(text):
    """argparse type: a whole number of 0 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if value < 0:
        raise argparse.ArgumentTypeError("cannot be negative, got {}".format(value))
    return value


# ---------------------------------------------------------------------------
# Splitting a filename into stem and extension
# ---------------------------------------------------------------------------
def split_name(filename):
    """
    Split 'photo.jpg' into ('photo', '.jpg').

    Handles the awkward cases:
      'backup.tar.gz'  -> ('backup', '.tar.gz')   two-part extension
      'notes'          -> ('notes', '')           no extension
      '.gitignore'     -> ('.gitignore', '')      a dotfile, not an extension
      '.hidden.txt'    -> ('.hidden', '.txt')
    """
    lowered = filename.lower()
    for double in DOUBLE_EXTENSIONS:
        if lowered.endswith(double):
            return filename[:-len(double)], filename[-len(double):]

    stem, extension = os.path.splitext(filename)

    # os.path.splitext('.gitignore') gives ('.gitignore', ''), which is what
    # we want. But splitext('.hidden.txt') gives ('.hidden', '.txt') — also
    # what we want. The only case to guard is a name that is *only* a dot
    # plus an extension, like '.jpg', which splitext leaves alone entirely.
    return stem, extension


def smart_title(text):
    """
    Capitalise each word, leaving the inside of words alone.

    Python's built-in str.title() turns "don't" into "Don'T" and "3rd" into
    "3Rd". This does not: a letter is only uppercased at the start of a word,
    a digit or an apostrophe counts as part of the word it is in.
    """
    result = []
    at_word_start = True

    for character in text:
        if character.isalpha():
            result.append(character.upper() if at_word_start else character.lower())
            at_word_start = False
        else:
            result.append(character)
            if character.isdigit():
                at_word_start = False      # "3rd" stays "3rd", not "3Rd"
            elif character in "'’":
                at_word_start = False      # "don't" stays "don't", not "Don'T"
            else:
                at_word_start = True       # a space, dash or bracket ends a word

    return "".join(result)


def apply_case(text, args):
    """Apply whichever of --lower/--upper/--title was asked for."""
    if args.lower:
        return text.lower()
    if args.upper:
        return text.upper()
    if args.title:
        return smart_title(text)
    return text


def natural_key(text):
    """
    A sort key that counts numbers properly: photo2 comes before photo10.

    Each piece is wrapped in a (0, ...) or (1, ...) pair. That matters: it
    keeps numbers and words from ever being compared directly, which would
    raise a TypeError as soon as one name had a digit where another had a
    letter.
    """
    return [
        (1, int(piece)) if piece.isdigit() else (0, piece.lower())
        for piece in re.split(r"(\d+)", text)
    ]


def parse_replacements(values):
    """
    Turn ['Live at =', ' =_'] into [('Live at ', ''), (' ', '_')].

    Each --replace is written OLD=NEW and split on the FIRST '=' only, so
    the new text may itself contain '='. If there is no '=' at all, the
    matched text is simply removed — --replace "draft_" deletes "draft_".

    They are applied in the order given on the command line.
    """
    replacements = []
    for value in values or []:
        if "=" in value:
            old, new = value.split("=", 1)
        else:
            old, new = value, ""
        if not old:
            raise ValueError(
                "an empty OLD text in --replace {!r} would match everywhere".format(value))
        replacements.append((old, new))
    return replacements


# ---------------------------------------------------------------------------
# The rename command line
# ---------------------------------------------------------------------------
def add_rename_arguments(parser):
    """Add every option the rename command takes to `parser`."""
    parser.add_argument(
        "paths",
        nargs="*",
        help="files or folders to rename. A folder is scanned for files; a "
             "file named directly is always included, whatever --pattern says.",
    )

    # --- Which files ------------------------------------------------------
    which = parser.add_argument_group("choosing files")
    which.add_argument(
        "--pattern",
        action="append",
        metavar="GLOB",
        help="only rename files matching this glob, e.g. --pattern '*.jpg'. "
             "Repeatable. (default: *)",
    )
    which.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="also look inside subfolders",
    )
    which.add_argument(
        "--include-hidden",
        action="store_true",
        help="also rename dotfiles like .gitignore. Off by default, because "
             "renaming those can break things that look for them.",
    )

    # --- How to change the name -------------------------------------------
    naming = parser.add_argument_group("changing the name")
    naming.add_argument(
        "--prefix",
        default="",
        metavar="TEXT",
        help="put this in front of the stem",
    )
    naming.add_argument(
        "--suffix",
        default="",
        metavar="TEXT",
        help="put this after the stem, before the extension",
    )
    naming.add_argument(
        "--base",
        metavar="NAME",
        help="replace the whole stem with this name. Usually used together "
             "with --number, otherwise every file would end up called NAME.",
    )
    naming.add_argument(
        "--replace",
        action="append",
        metavar="OLD=NEW",
        help="swap text in each name. Split on the first '=', so NEW may "
             "contain '='. With no '=', the text is removed. Repeatable, and "
             "applied in the order given.",
    )
    naming.add_argument(
        "--replace-scope",
        choices=("stem", "ext", "full"),
        default="stem",
        help="which part of the name the text options act on: the stem only, "
             "the extension only, or the whole filename. It applies to both "
             "--replace and the case options, so '--lower --replace-scope ext' "
             "lowercases extensions. Note --replace is case-sensitive: "
             "'jpeg' will not match '.JPEG'.",
    )

    case = naming.add_mutually_exclusive_group()
    case.add_argument("--lower", action="store_true", help="make the stem lowercase")
    case.add_argument("--upper", action="store_true", help="MAKE THE STEM UPPERCASE")
    case.add_argument("--title", action="store_true", help="Make The Stem Title Case")

    # --- Numbering --------------------------------------------------------
    numbering = parser.add_argument_group("numbering")
    numbering.add_argument(
        "--number",
        action="store_true",
        help="add a counter: photo_001.jpg, photo_002.jpg, ...",
    )
    numbering.add_argument(
        "--number-start",
        type=zero_or_more_int,
        default=1,
        metavar="N",
        help="the first number",
    )
    numbering.add_argument(
        "--number-step",
        type=positive_int,
        default=1,
        metavar="N",
        help="how much to add each time",
    )
    numbering.add_argument(
        "--number-pad",
        type=positive_int,
        default=3,
        metavar="N",
        help="pad the number with zeros to this width. Widened automatically "
             "if there are more files than it can hold.",
    )
    numbering.add_argument(
        "--number-position",
        choices=("prefix", "suffix"),
        default="suffix",
        help="whether the counter goes before or after the stem",
    )

    # --- Order ------------------------------------------------------------
    order = parser.add_argument_group("order")
    order.add_argument(
        "--sort",
        choices=("name", "mtime", "size", "none"),
        default="name",
        help="the order files are handled in. This decides which file gets "
             "which number, so it matters when you use --number. 'mtime' is "
             "oldest first. Folders are always kept together.",
    )
    order.add_argument(
        "--reverse",
        action="store_true",
        help="reverse the sort order (newest first, largest first, ...)",
    )

    # --- Running ----------------------------------------------------------
    running = parser.add_argument_group("running")
    running.add_argument(
        "--dry-run",
        action="store_true",
        help="show the before/after table and stop. Nothing is renamed and "
             "nothing is written to the log.",
    )
    running.add_argument(
        "--undo",
        action="store_true",
        help="put back the names from a previous run, using the log. This "
             "ignores every naming option above.",
    )
    running.add_argument(
        "--log-file",
        default=DEFAULT_LOG_PATH,
        metavar="PATH",
        help="where to record renames, for --undo",
    )
    running.add_argument(
        "--yes", "-y",
        action="store_true",
        help="skip the 'Rename N file(s)?' question. Needed when running from "
             "a script, since there is no terminal to answer on.",
    )
    # SUPPRESS keeps argparse from printing "(default: False)" for a flag
    # whose name already says what the default is.
    running.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="hide the progress bar",
    )


# ---------------------------------------------------------------------------
# Finding the files
# ---------------------------------------------------------------------------
def matches_any_pattern(filename, patterns):
    """True if the filename matches any of the glob patterns."""
    return any(fnmatch.fnmatchcase(filename, pattern) for pattern in patterns)


def collect_files(paths, patterns, recursive, include_hidden, progress=None,
                  warn=None):
    """
    Work out which files are involved.

    A path given on the command line is treated differently depending on
    what it is:

      * a file  — always included. Naming it was the filter, so --pattern and
                  the dotfile rule do not apply to it.
      * a folder — scanned for files matching --pattern, one level deep, or
                  all the way down with --recursive.

    Returns (files, stats) where files is a list of (path, mtime_ns, size) and
    stats counts what was deliberately skipped. Symlinks are never included:
    renaming one renames the link rather than the file it points at, which is
    almost never what someone means.

    How many files a folder holds is not known before walking it, so the
    `progress` bar counts entries examined rather than showing a percentage.
    """
    warn = warn_to(warn)
    files = []
    stats = {"hidden": 0, "no_match": 0, "symlinks": 0, "empty_dirs": 0, "bad_paths": 0}
    patterns = patterns or ["*"]

    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))

        if os.path.islink(path):
            stats["symlinks"] += 1
            warn("Skipping symlink: {}".format(path))
            continue

        if os.path.isfile(path):
            # Named directly, so it is always included.
            try:
                info = os.stat(path)
            except OSError as error:
                warn("Skipping {}: {}".format(path, error))
                continue
            files.append((path, info.st_mtime_ns, info.st_size))
            if progress is not None:
                progress.update()
            continue

        if not os.path.isdir(path):
            stats["bad_paths"] += 1
            warn("Warning: not a file or folder, skipping: {}".format(raw_path))
            continue

        cleaned = scan_folder(path, patterns, recursive, include_hidden, stats,
                              progress, warn)
        if not cleaned:
            stats["empty_dirs"] += 1
        files.extend(cleaned)

    return files, stats


def scan_folder(folder, patterns, recursive, include_hidden, stats, progress=None,
                warn=None):
    """
    Collect the matching files inside one folder.

    os.scandir is used rather than os.walk because it hands back the file's
    type information in the same call that reads the directory, which avoids
    a second round trip to the disk for every entry.
    """
    warn = warn_to(warn)
    found = []

    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as error:
        # An unreadable folder is a warning, not a reason to give up.
        warn("Warning: cannot read {}: {}".format(folder, error))
        return found

    for entry in entries:
        if progress is not None:
            progress.update()
        try:
            if entry.is_symlink():
                stats["symlinks"] += 1
                continue

            if entry.is_dir():
                if recursive:
                    found.extend(
                        scan_folder(entry.path, patterns, recursive,
                                    include_hidden, stats, progress, warn))
                continue

            if not entry.is_file():
                continue    # sockets, pipes, devices

            if entry.name.startswith(".") and not include_hidden:
                stats["hidden"] += 1
                continue

            if not matches_any_pattern(entry.name, patterns):
                stats["no_match"] += 1
                continue

            info = entry.stat()
            found.append((entry.path, info.st_mtime_ns, info.st_size))

        except OSError as error:
            warn("Warning: cannot read {}: {}".format(entry.path, error))

    return found


def sort_files(files, mode, reverse):
    """
    Put the files in the order they will be handled.

    Everything is sorted by folder first, then by the chosen key. That keeps
    one folder's files together instead of interleaving them with another
    folder's, so with --number each folder gets a consecutive block:

        one/a.jpg -> img_001     two/c.jpg -> img_003
        one/b.jpg -> img_002     two/d.jpg -> img_004

    The counter does not restart in each folder; it runs the whole way
    through the run. (Use --sort none to keep whatever order the folder
    itself reports, or run the folders one at a time to number each from 1.)
    """
    if mode == "none":
        ordered = list(files)
    elif mode == "name":
        ordered = sorted(files, key=lambda item: (
            os.path.dirname(item[0]), natural_key(os.path.basename(item[0]))))
    elif mode == "mtime":
        ordered = sorted(files, key=lambda item: (
            os.path.dirname(item[0]), item[1], natural_key(os.path.basename(item[0]))))
    else:   # size
        ordered = sorted(files, key=lambda item: (
            os.path.dirname(item[0]), item[2], natural_key(os.path.basename(item[0]))))

    # --reverse flips the sort key, but folders stay in place: reversing the
    # whole list would shuffle the folders into reverse order too, which is
    # not what "newest first" means.
    if reverse:
        grouped = {}
        for item in ordered:
            grouped.setdefault(os.path.dirname(item[0]), []).append(item)
        ordered = []
        for directory in sorted(grouped):
            ordered.extend(reversed(grouped[directory]))

    return ordered


# ---------------------------------------------------------------------------
# Building the new names
# ---------------------------------------------------------------------------
def number_width(args, count):
    """
    How many digits the numbers need.

    --number-pad is the minimum. If there are more files than the padding can
    hold, the width grows so that every number is the same length — otherwise
    999 would sort before 1000 and the sequence would look broken.
    """
    if count < 1:
        return args.number_pad
    biggest = args.number_start + (count - 1) * args.number_step
    return max(args.number_pad, len(str(biggest)))


def build_new_name(filename, args, replacements, index, width):
    """
    Build one new filename. `index` is the file's position, counted from 0.
    """
    stem, extension = split_name(filename)

    # 1. --replace, in the scope that was asked for.
    if replacements:
        if args.replace_scope == "ext":
            for old, new in replacements:
                extension = extension.replace(old, new)
        elif args.replace_scope == "full":
            whole = stem + extension
            for old, new in replacements:
                whole = whole.replace(old, new)
            stem, extension = split_name(whole)
        else:
            for old, new in replacements:
                stem = stem.replace(old, new)

    # 2. --base swaps out the whole stem.
    if args.base is not None:
        stem = args.base

    # 3. Case. The case options follow --replace-scope, so that "the part of
    #    the name I am editing" means the same thing for both.
    if args.replace_scope == "ext":
        extension = apply_case(extension, args)
    elif args.replace_scope == "full":
        stem = apply_case(stem, args)
        extension = apply_case(extension, args)
    else:
        stem = apply_case(stem, args)

    # 4. Prefix and suffix.
    stem = args.prefix + stem + args.suffix

    # 5. The counter.
    if args.number:
        value = args.number_start + index * args.number_step
        label = str(value).zfill(width)
        if args.number_position == "prefix":
            stem = label + NUMBER_SEPARATOR + stem
        else:
            stem = stem + NUMBER_SEPARATOR + label

    return stem + extension


def check_name(stem, extension, original):
    """
    Check a new name is something we are willing to create.

    Returns a message explaining the problem, or None if the name is fine.
    """
    name = stem + extension

    if not name:
        return "the new name would be empty"
    if not stem:
        # A name like ".jpg" is legal on Linux but is almost certainly a
        # mistake, and it turns the file into a hidden file.
        return ("nothing would be left before the extension "
                "(the name would be {!r})".format(name))
    if "/" in name:
        return "the new name contains a '/' ({!r})".format(name)
    if "\0" in name:
        return "the new name contains a null byte"
    if name in (".", ".."):
        return "the new name would be {!r}, which is a folder".format(name)

    length = len(name.encode("utf-8"))
    if length > MAX_NAME_BYTES:
        return ("the new name is {} bytes long; the limit is {} "
                "({!r}...)".format(length, MAX_NAME_BYTES, name[:40]))

    return None


class Rename:
    """One file's old and new name. A plain class so the table can label it."""

    __slots__ = ("old", "new")

    def __init__(self, old, new):
        self.old = old
        self.new = new


def build_plan(files, args, replacements):
    """
    Work out the old name and the new name for every file.

    Returns (plan, unchanged, invalid) where:
      plan      is a list of Rename objects, only for files that change
      unchanged is a list of paths whose new name equals their old name
      invalid   is a list of (path, problem) for names we will not create
    """
    plan = []
    unchanged = []
    invalid = []

    count = len(files)
    width = number_width(args, count)

    for index, (path, _mtime, _size) in enumerate(files):
        directory = os.path.dirname(path)
        old_name = os.path.basename(path)
        new_name = build_new_name(old_name, args, replacements, index, width)

        if new_name == old_name:
            unchanged.append(path)
            continue

        new_stem, new_ext = split_name(new_name)
        problem = check_name(new_stem, new_ext, old_name)
        if problem:
            invalid.append((path, problem))
            continue

        plan.append(Rename(old=path, new=os.path.join(directory, new_name)))

    return plan, unchanged, invalid


# ---------------------------------------------------------------------------
# Collision checking — the part that must never get this wrong
# ---------------------------------------------------------------------------
def find_collisions(plan):
    """
    Find every reason the plan would damage something.

    Two kinds of collision:

      * two files in the plan whose new names are the same
      * a new name that is already used on disk by a file this run is NOT
        renaming. A file that IS being renamed away does not count, because
        by the time the second file takes that name the first one has moved.

    Returns a list of (message, [paths]) — empty when the plan is safe.
    """
    collisions = []
    by_target = {}

    for item in plan:
        by_target.setdefault(item.new, []).append(item.old)

    # Two files wanting the same new name.
    for target, sources in sorted(by_target.items()):
        if len(sources) > 1:
            collisions.append((
                "{} files would all be renamed to {!r}".format(
                    len(sources), os.path.basename(target)),
                sorted(sources),
            ))

    # A new name that something else already has.
    moved_away = {item.old for item in plan}
    for target, sources in sorted(by_target.items()):
        if len(sources) > 1:
            continue     # already reported above
        if os.path.lexists(target) and target not in moved_away:
            collisions.append((
                "{!r} is already taken by a file that is not part of this "
                "run".format(os.path.basename(target)),
                sources,
            ))

    return collisions


# ---------------------------------------------------------------------------
# Showing the plan
# ---------------------------------------------------------------------------
def print_plan_table(plan, unchanged, invalid, collisions, show_collisions):
    """
    Print the before/after table.

    Rows are grouped by folder with a 'Folder:' line, so a run over several
    folders stays readable without a wide extra column.
    """
    bad_paths = set()
    for _message, sources in collisions:
        bad_paths.update(sources)

    width_old = len("CURRENT NAME")
    width_new = len("NEW NAME")
    for item in plan:
        width_old = max(width_old, len(os.path.basename(item.old)))
        width_new = max(width_new, len(os.path.basename(item.new)))

    print()
    if not plan:
        print("Nothing would be renamed.")
    else:
        print("  {:<6} {:<{ow}}  {:<{nw}}".format(
            "#", "CURRENT NAME", "NEW NAME", ow=width_old, nw=width_new))
        print("  {}  {}".format("-" * width_old, "-" * width_new))

        current_folder = None
        for index, item in enumerate(plan, start=1):
            folder = os.path.dirname(item.old)
            if folder != current_folder:
                if current_folder is not None:
                    print()
                print("  Folder: {}".format(folder))
                current_folder = folder

            marker = ""
            if show_collisions and item.old in bad_paths:
                marker = "   <-- COLLISION"

            print("  {:<6} {:<{ow}}  {:<{nw}}{}".format(
                "{}/{}".format(index, len(plan)),
                os.path.basename(item.old),
                os.path.basename(item.new),
                marker, ow=width_old, nw=width_new))

    if unchanged:
        print()
        print("{} file(s) would keep their current name and are not listed.".format(
            len(unchanged)))

    if invalid:
        print()
        print("{} file(s) have a problem and would stop the run:".format(len(invalid)))
        for path, problem in invalid:
            print("  - {}: {}".format(os.path.basename(path), problem))


def print_skips(stats):
    """Mention what was left out, so a missing file is never a mystery."""
    skipped = []
    if stats["hidden"]:
        skipped.append("{} hidden file(s) (use --include-hidden)".format(stats["hidden"]))
    if stats["no_match"]:
        skipped.append("{} not matching --pattern".format(stats["no_match"]))
    if stats["symlinks"]:
        skipped.append("{} symlink(s)".format(stats["symlinks"]))
    if skipped:
        print()
        print("Skipped: {}.".format(", ".join(skipped)))


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------
def append_log(log_path, record):
    """
    Add one record to the log, and make sure it is really on the disk.

    The flush and fsync matter. A rename is logged BEFORE it happens, and
    that is only useful if the record has actually reached the disk by then —
    otherwise a power cut could leave files renamed with nothing recording
    what they used to be called.
    """
    folder = os.path.dirname(log_path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_log(log_path, warn=None):
    """
    Read every record from the log.

    A damaged line is reported and skipped rather than crashing: a log that
    is 99% readable is far more useful than no log at all.
    """
    warn = warn_to(warn)
    records = []

    if not os.path.exists(log_path):
        return records

    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    warn("Warning: unreadable line {} in {} — skipped.".format(
                        number, log_path))
    except OSError as error:
        warn("Error: cannot read the log {}: {}".format(log_path, error))

    return records


def last_run_id(records):
    """
    The id of the most recent run that actually renamed something.

    'restore' records written by --undo are ignored, so undo cannot end up
    chasing its own tail.
    """
    for record in reversed(records):
        if record.get("type") == "staged" and record.get("run"):
            return record["run"]
    return None


# ---------------------------------------------------------------------------
# Doing the rename
# ---------------------------------------------------------------------------
def temp_path(directory, run_id, index):
    """A free temporary name in `directory` for one file."""
    base = os.path.join(directory, "{}{}-{}".format(TEMP_PREFIX, run_id, index))
    candidate = base
    counter = 0
    while os.path.lexists(candidate):
        counter += 1
        candidate = "{}-{}".format(base, counter)
    return candidate


def move_to(source, target):
    """
    Rename source to target, and refuse to overwrite anything.

    os.rename on Linux silently replaces the file at the target if there is
    one. For this program that would be data loss, so the target is checked
    first and the move is refused if it is occupied.

    There is an unavoidable sliver of a moment between the check and the
    rename in which another program could create that name. Nothing in the
    standard library closes it completely, and the collision check earlier in
    the run makes it very unlikely, so the target is simply checked as close
    to the rename as possible.
    """
    if os.path.lexists(target):
        raise FileExistsError(
            "{} already exists, and this program never overwrites a file".format(target))
    os.rename(source, target)


def two_phase_move(moves, run_id, note_stage=None, progress=None, warn=None):
    """
    Move every (source, target) pair, in two steps.

    Step 1 parks each file at a temporary name. Step 2 gives it its real new
    name.

    The two steps are what make a chain or a swap work. Moving a.txt to
    b.txt while b.txt is itself becoming c.txt fails in one pass, because
    b.txt is still occupied at that moment. With everything parked at a
    temporary name first, no target is ever held by a file that is still
    waiting its turn.

    `note_stage` is called with (source, target, temporary) just before the
    file is parked, which is where the caller writes its log record. Logging
    before the move means an interruption can always be worked out from disk.

    Returns (done, problems) where done is a list of the (source, target)
    pairs that completed.
    """
    warn = warn_to(warn)
    staged = []
    problems = []

    for index, (source, target) in enumerate(moves):
        temporary = temp_path(os.path.dirname(source), run_id, index)

        if note_stage is not None:
            note_stage(source, target, temporary)

        if not os.path.lexists(source):
            problems.append((source, "disappeared before it could be moved"))
            if progress is not None:
                progress.update(2)     # this file will never reach step 2
            continue

        try:
            os.rename(source, temporary)
        except OSError as error:
            problems.append((source, "could not be moved: {}".format(error)))
            if progress is not None:
                progress.update(2)     # ... nor will this one
            continue

        staged.append((source, temporary, target))
        if progress is not None:
            progress.update()          # step 1 of 2 done for this file

    done = []
    for source, temporary, target in staged:
        try:
            move_to(temporary, target)
            done.append((source, target))
        except OSError as error:
            problems.append((target, "could not take its new name: {}".format(error)))
            # Something went wrong part way through, so put everything back
            # rather than leaving a half-finished folder behind.
            _rollback(staged, done, problems, warn)
            return [], problems
        if progress is not None:
            progress.update()          # step 2 of 2 done for this file

    return done, problems


def _rollback(staged, done, problems, warn=None):
    """
    Put every staged file back where it started.

    `done` are the pairs that already reached their target; the rest are
    still at their temporary names. Both are moved back, so the folder ends
    up exactly as it was before the run.
    """
    warn = warn_to(warn)
    restored = 0

    for source, temporary, target in reversed(staged):
        try:
            if (source, target) in done and os.path.lexists(target):
                os.rename(target, source)
                restored += 1
            elif os.path.lexists(temporary):
                os.rename(temporary, source)
                restored += 1
        except OSError as error:
            problems.append((source, "COULD NOT BE PUT BACK: {}".format(error)))

    if restored:
        warn("Rolled back {} file(s) to where they started.".format(restored))

    return restored


def execute_plan(plan, log_path, run_id, progress=None, warn=None):
    """Rename every file in the plan. Returns (renamed, problems)."""
    def log_stage(source, target, temporary):
        append_log(log_path, {
            "type": "staged",
            "run": run_id,
            "src": source,
            "tmp": temporary,
            "dst": target,
            "time": now_stamp(),
        })

    return two_phase_move([(item.old, item.new) for item in plan],
                          run_id, note_stage=log_stage, progress=progress,
                          warn=warn)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------
def blocked_message(problems):
    """The lines shown when not one file could be put back."""
    lines = ["", "Nothing could be restored:"]
    for path, reason in problems:
        lines.append("  - {}: {}".format(os.path.basename(path), reason))
    return lines


class UndoPlan:
    """
    What an undo would do.

    `moves` is the list of (where the file is now, where it should go back
    to). `message` is what to tell the user — empty when there is work to do,
    and otherwise the reason there is not. `exit_code` is None while there is
    work to do, and the code to finish with when there is not.
    """

    __slots__ = ("log_path", "run_id", "moves", "problems", "message", "exit_code")

    def __init__(self, log_path, run_id=None, moves=None, problems=None,
                 message=None, exit_code=None):
        self.log_path = log_path
        self.run_id = run_id
        self.moves = moves or []
        self.problems = problems or []
        self.message = message or []
        self.exit_code = exit_code

    @property
    def ready(self):
        """True when there is something to put back."""
        return self.exit_code is None and bool(self.moves)


def plan_undo(log_path, warn=None):
    """
    Work out what --undo would put back, without moving anything.

    Reading the log and deciding where each file currently is are the parts
    worth sharing; showing the result is not. The command line prints
    `message` and then lists `moves`, and a window can do the same in a table.
    """
    warn = warn_to(warn)
    log_path = os.path.abspath(os.path.expanduser(log_path))
    records = read_log(log_path, warn)

    if not records:
        return UndoPlan(log_path, message=[
            "No history found at {}.".format(log_path),
            "",
            "Undo reads the log of past renames, and there is nothing in it",
            "yet. It finds it automatically, so you do not need to pass",
            "--log-file unless you moved it yourself.",
        ], exit_code=1)

    run_id = last_run_id(records)
    if run_id is None:
        return UndoPlan(log_path, message=[
            "The log has no completed run to undo."], exit_code=1)

    entries = [record for record in records
               if record.get("type") == "staged" and record.get("run") == run_id]

    if not entries:
        return UndoPlan(log_path, run_id, message=[
            "Run {} has no renames recorded.".format(run_id)], exit_code=1)

    # A run that has already been undone should not be undone twice. The
    # 'restore' records say which files are already back, which is more
    # reliable than guessing from what is on disk: in a chain, a name being
    # present does not mean the file that used to have it is back.
    already_restored = {record.get("dst") for record in records
                        if record.get("type") == "restore"
                        and record.get("run") == run_id}

    todo = [entry for entry in entries if entry.get("src") not in already_restored]

    if not todo:
        return UndoPlan(log_path, run_id, message=[
            "Run {} has already been undone.".format(run_id),
            "Nothing to do."], exit_code=0)

    # --- Work out where each file is now, and where it should go ----------
    moves = []
    problems = []

    for entry in todo:
        source = entry["src"]          # the name it had before the run
        temporary = entry.get("tmp")
        target = entry["dst"]          # the name the run gave it

        if os.path.lexists(target):
            current = target
        elif temporary and os.path.lexists(temporary):
            # The run was interrupted between its two steps, so the file is
            # still parked at its temporary name.
            current = temporary
        elif os.path.lexists(source):
            continue                   # already back where it belongs
        else:
            problems.append((source, "not found anywhere — moved or deleted since?"))
            continue

        moves.append((current, source))

    if not moves:
        return UndoPlan(log_path, run_id, problems=problems,
                        message=blocked_message(problems), exit_code=1)

    # A file's old name may currently be held by another file that this undo
    # is about to move out of the way. That is normal in a chain. A name held
    # by something that is NOT part of this undo is a real conflict, and the
    # file is left alone rather than overwriting it.
    being_vacated = {current for current, _source in moves}
    safe_moves = []
    for current, source in moves:
        if os.path.lexists(source) and source not in being_vacated:
            problems.append((source, "the original name is now taken by "
                                     "another file, so this one was left alone"))
            continue
        safe_moves.append((current, source))

    if not safe_moves:
        return UndoPlan(log_path, run_id, problems=problems,
                        message=blocked_message(problems), exit_code=1)

    return UndoPlan(log_path, run_id, safe_moves, problems)


def run_undo(plan, progress=None, warn=None):
    """
    Carry out the moves an UndoPlan describes.

    Returns (restored, problems), where problems includes everything that was
    already wrong with the plan, so one call gives the whole picture.
    """
    warn = warn_to(warn)
    undo_run_id = new_run_id()

    def log_restore(current, source, temporary):
        # Written before the move, so an interruption during an undo can
        # still be worked out from the log afterwards.
        append_log(plan.log_path, {
            "type": "restore",
            "run": plan.run_id,
            "undo_run": undo_run_id,
            "src": current,
            "tmp": temporary,
            "dst": source,
            "time": now_stamp(),
        })

    restored, move_problems = two_phase_move(
        plan.moves, undo_run_id, note_stage=log_restore, progress=progress,
        warn=warn)

    return restored, list(plan.problems) + move_problems


# ---------------------------------------------------------------------------
# The rename command itself
# ---------------------------------------------------------------------------
def cmd_undo(args):
    """
    Put back the names recorded in the log.

    Undoing is the same problem as renaming, in reverse: several files may
    need to move at once with their target names still occupied by each
    other. It therefore uses the same two-phase move, for the same reason.
    """
    plan = plan_undo(args.log_file)

    for line in plan.message:
        print(line)
    if plan.exit_code is not None:
        return plan.exit_code

    # --- Show, or do ------------------------------------------------------
    print()
    print("Undoing run {} — {} file(s).".format(plan.run_id, len(plan.moves)))

    if args.dry_run:
        print()
        for current, source in plan.moves:
            print("  [dry-run] {}  <-  {}".format(source, current))
        print()
        print("Dry run — nothing was renamed.")
        print("Re-run without --dry-run to put the names back.")
        return 0

    restored, problems = run_undo(plan)

    # --- Result ----------------------------------------------------------
    print()
    print("=" * 70)
    print("UNDO SUMMARY")
    print("=" * 70)
    print("  {:<22} {}".format("Restored", len(restored)))
    if problems:
        print("  {:<22} {}".format("Could not restore", len(problems)))
        print()
        for path, reason in problems:
            print("  - {}: {}".format(os.path.basename(path), reason))

    print()
    if problems:
        # Never say the names are back when some of them are not.
        print("Some names could not be restored — see the list above.")
        print("The files themselves were not deleted.")
        return 1

    print("The names from run {} are back.".format(plan.run_id))
    return 0


# ---------------------------------------------------------------------------
# The rename command itself
# ---------------------------------------------------------------------------
def cmd_rename(args):
    """Run the rename command. Returns the exit code."""
    if args.undo:
        return cmd_undo(args)

    if not args.paths:
        print("Error: no files or folders were given to rename.")
        print()
        print("  For example:")
        print("      python3 {} rename ~/Pictures --prefix \"trip_\" --dry-run".format(
            PROGRAM))
        print()
        print("  To put back the names from the last rename instead:")
        print("      python3 {} rename --undo".format(PROGRAM))
        return 2

    log_path = os.path.abspath(os.path.expanduser(args.log_file))
    show_progress = not getattr(args, "no_progress", False)

    # --- Work out which files, and what they would be called ---------------
    try:
        replacements = parse_replacements(args.replace)
    except ValueError as error:
        print("Error: {}".format(error))
        return 2

    print("Looking for files...")
    # How many files a folder holds is unknown until it has been walked, so
    # this bar counts entries examined and shows no percentage.
    with Progress(None, "Scanning", enabled=show_progress) as scan_bar:
        files, stats = collect_files(args.paths, args.pattern, args.recursive,
                                     args.include_hidden, progress=scan_bar)

    if not files:
        print()
        print("No files found to rename.")
        print_skips(stats)
        # A folder with nothing matching in it is a normal, successful result.
        # Every path being wrong is a mistake worth an error code, so a script
        # does not read it as success.
        if stats["bad_paths"] and stats["bad_paths"] == len(args.paths):
            print()
            print("Error: none of the paths given exist. Check the spelling:")
            for raw_path in args.paths:
                print("    {}".format(raw_path))
            return 1
        return 0

    files = sort_files(files, args.sort, args.reverse)
    plan, unchanged, invalid = build_plan(files, args, replacements)

    if not plan and not invalid:
        print()
        print("Nothing to rename: all {} file(s) already have the name "
              "these options would give them.".format(len(files)))
        print_skips(stats)
        return 0

    # --- Check for anything that would cause damage ------------------------
    collisions = find_collisions(plan)
    report = RenamePlan(files, stats, plan, unchanged, invalid, collisions)

    # Show the table with the bad rows marked, so --dry-run tells you not
    # just that it would fail but which lines are the problem.
    print()
    print("{} file(s) found. {} would be renamed:".format(len(files), len(plan)))
    print_plan_table(plan, unchanged, invalid, collisions, show_collisions=True)
    print_skips(stats)

    if invalid or collisions:
        print()
        print("=" * 70)
        print("REFUSED — NOTHING WAS RENAMED")
        print("=" * 70)

        if collisions:
            print()
            print("{} name collision(s) would damage files:".format(len(collisions)))
            for message, sources in collisions:
                print()
                print("  {}".format(message))
                for source in sources:
                    print("      {}".format(source))
            if args.base is not None and not args.number:
                print()
                print("  Hint: --base gives every file the same stem. Add")
                print("        --number so each one gets its own counter.")

        if invalid:
            print()
            print("{} file(s) would get an unusable name.".format(len(invalid)))

        print()
        print("No files were touched. Adjust the options and try again.")
        return 1

    # --- Stop here if this is a dry run -----------------------------------
    if args.dry_run:
        print()
        print("=" * 70)
        print("DRY RUN — NOTHING WAS RENAMED")
        print("=" * 70)
        print()
        print("{} file(s) would be renamed.".format(len(plan)))
        print("Nothing was changed, and nothing was written to the log.")
        print()
        print("Re-run without --dry-run to do it for real.")
        return 0

    # --- Confirm and do it ------------------------------------------------
    print()
    if not confirm("Rename {} file(s)? [y/N] ".format(len(plan)), args.yes):
        print("Cancelled — nothing was renamed.")
        return 0

    print()
    print("Renaming {} file(s)...".format(len(plan)))
    # Two steps per file (park it, then give it its name), so the bar counts
    # steps rather than files.
    with Progress(len(plan) * 2, "Renaming", enabled=show_progress) as bar:
        _run_id, renamed, problems = run_rename(report, log_path, progress=bar)

    # --- Result -----------------------------------------------------------
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("  {:<22} {}".format("Renamed", len(renamed)))
    if unchanged:
        print("  {:<22} {}".format("Already correct", len(unchanged)))
    if problems:
        print("  {:<22} {}".format("Failed", len(problems)))
        print()
        for path, reason in problems:
            print("  - {}: {}".format(os.path.basename(path), reason))

    print()
    if not renamed:
        print("Nothing was renamed. Your files are exactly as they were.")
    elif problems:
        print("Some files were left untouched — see the failures above.")
        print("To put back the names that did change:")
        print("    python3 {} rename --undo".format(PROGRAM))
    else:
        print("Done. To put the old names back:")
        print("    python3 {} rename --undo".format(PROGRAM))
        if not args.yes:
            print()
            print("(Undo reads {}.)".format(log_path))

    return 1 if problems and not renamed else 0


def confirm(prompt, assume_yes=False):
    """
    Ask before doing anything. Returns True only on a clear yes.

    An answer typed at the terminal and an answer piped in both count — that
    is what makes 'printf "y\\n" | filetool.py rename ...' work, and it is
    still an explicit confirmation.

    What does not count is silence. If there is nothing to read (EOF), or the
    input is not a 'yes', this returns False, so a script that forgot to say
    yes renames nothing instead of guessing.
    """
    if assume_yes:
        return True

    try:
        answer = input(prompt)
    except EOFError:
        print()
        print("No answer was given, so nothing was renamed.")
        print("Re-run with --yes if you meant to do this.")
        return False
    except KeyboardInterrupt:
        print()
        return False

    return answer.strip().lower() in ("y", "yes")


# ===========================================================================
# THE IMAGE COMMAND
# ===========================================================================
def quality_argument(text):
    """argparse type: a JPEG/WebP quality between 1 and 100."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError(
            "quality must be between 1 and 100, got {}".format(value))
    return value


def format_change(before, after):
    """
    The percentage the file shrank by, e.g. '-67%'.

    A negative result means the output came out LARGER than the input, which
    does happen — re-encoding an already-optimised JPEG at high quality, or
    turning a small flat-colour PNG into a JPEG. It is reported honestly
    rather than hidden.
    """
    if before <= 0:
        return "n/a"
    change = (after - before) / before * 100
    if change > -0.5 and change < 0.5:
        return "0%"
    return "{:+.0f}%".format(change)


def add_image_arguments(parser):
    """Add every option the image command takes to `parser`."""
    parser.add_argument(
        "paths",
        nargs="+",
        help="image files or folders to process",
    )

    what = parser.add_argument_group("what to do")
    # default=SUPPRESS keeps argparse from appending "(default: None)" to the
    # help; the real default is stated in the help text itself.
    what.add_argument(
        "--quality",
        type=quality_argument,
        metavar="1-100",
        default=argparse.SUPPRESS,
        help="JPEG and WebP quality. Lower is smaller and lossier. PNG is "
             "lossless and ignores this. (default: {})".format(DEFAULT_QUALITY),
    )
    what.add_argument(
        "--max-width",
        type=positive_int,
        metavar="PX",
        help="scale images down so they are no wider than this, keeping the "
             "shape. Images that are already smaller are left alone.",
    )
    what.add_argument(
        "--max-height",
        type=positive_int,
        metavar="PX",
        help="the same, for height. Use both to fit images inside a box.",
    )
    what.add_argument(
        "--allow-upscale",
        action="store_true",
        help="let --max-width/--max-height make small images bigger. Off by "
             "default: enlarging wastes space and never adds detail.",
    )
    what.add_argument(
        "--format",
        choices=("keep", "jpeg", "png", "webp"),
        default="keep",
        help="convert everything to this format, or keep each file's own "
             "format",
    )

    where = parser.add_argument_group("where things go")
    where.add_argument(
        "--output",
        metavar="FOLDER",
        help="folder for the results. Created if missing. Defaults to a "
             "sibling of the first input folder, named <folder>_optimized.",
    )
    where.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="also look inside subfolders. The subfolder layout is copied "
             "into the output folder.",
    )
    where.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing files that are already in the output folder. "
             "Without this they are skipped, so a previous run is never "
             "damaged by accident.",
    )
    where.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be produced and write nothing",
    )
    where.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="hide the progress bar",
    )


# ---------------------------------------------------------------------------
# Finding the images
# ---------------------------------------------------------------------------
def collect_images(paths, recursive, warn=None):
    """
    Return a list of (source_path, base_folder) for every image found.

    `base_folder` is the folder the file's relative path is measured from, so
    that a recursive run can rebuild the same folder layout inside the output
    folder.

    Returns (images, skipped_non_images, bad_paths) where skipped_non_images
    counts files that were passed over because their extension is not an image
    one, and bad_paths counts arguments that named nothing on disk at all.
    """
    warn = warn_to(warn)
    images = []
    skipped = 0
    bad_paths = 0
    seen = set()

    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))

        if os.path.isfile(path):
            # Named directly, so take it whatever it is called; Pillow will
            # say whether it can actually read it.
            if path not in seen:
                seen.add(path)
                images.append((path, os.path.dirname(path)))
            continue

        if not os.path.isdir(path):
            warn("Warning: not a file or folder, skipping: {}".format(raw_path))
            bad_paths += 1
            continue

        if recursive:
            for folder, subfolders, filenames in os.walk(
                    path, onerror=lambda error: warn("Warning: {}".format(error))):
                subfolders.sort()
                for name in sorted(filenames):
                    full = os.path.join(folder, name)
                    if not is_image_name(name):
                        skipped += 1
                        continue
                    if full in seen:
                        continue
                    seen.add(full)
                    images.append((full, path))
        else:
            try:
                entries = sorted(os.scandir(path), key=lambda entry: entry.name)
            except OSError as error:
                warn("Warning: cannot read {}: {}".format(path, error))
                continue

            for entry in entries:
                if not entry.is_file():
                    continue
                if not is_image_name(entry.name):
                    skipped += 1
                    continue
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                images.append((entry.path, path))

    return images, skipped, bad_paths


def is_image_name(filename):
    """True if the filename ends in something Pillow can probably open."""
    return filename.lower().endswith(IMAGE_EXTENSIONS)


# ---------------------------------------------------------------------------
# The output folder
# ---------------------------------------------------------------------------
def resolve_output_folder(args, images, warn=None):
    """
    Work out where the results go, and refuse to accept anywhere unsafe.

    The one rule that matters: the output folder must not be the folder the
    images are being read from, and must not be inside it. Otherwise a run
    could write on top of the very files it is supposed to be protecting.

    Returns the folder, or None if it was refused — the reason for a refusal
    is explained through `warn`.
    """
    warn = warn_to(warn)

    if args.output:
        output = os.path.abspath(os.path.expanduser(args.output))
    else:
        first = os.path.abspath(os.path.expanduser(args.paths[0]))
        base = first if os.path.isdir(first) else os.path.dirname(first)
        output = base + "_optimized"

    # Every folder that images are being read from.
    read_from = {base for _path, base in images}
    for _path, base in images:
        read_from.add(os.path.realpath(base))

    # Folders that are scanned as a whole, i.e. the ones named on the command
    # line that are directories. A file named on the command line contributes
    # its parent folder to `read_from` so the "same folder" test below still
    # works, but that parent is never listed, so an output folder inside it
    # cannot be picked up by the run.
    scanned = set()
    for path in args.paths:
        full = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(full):
            scanned.add(full)
            scanned.add(os.path.realpath(full))

    for folder in sorted(read_from):
        if output == folder:
            warn("Error: the output folder is the same folder as the one")
            warn("       being read: {}".format(folder))
            warn("")
            warn("       That would write over your originals. Give a")
            warn("       different --output, for example a folder beside it:")
            warn("           python3 {} image {} --output {}{}".format(
                PROGRAM, args.paths[0], folder.rstrip(os.sep), "_small"))
            return None
        if folder in scanned and output.startswith(folder.rstrip(os.sep) + os.sep):
            warn("Error: the output folder is inside the folder being read:")
            warn("         output: {}".format(output))
            warn("         input:  {}".format(folder))
            warn("")
            warn("       A recursive run could pick up its own output, and")
            warn("       the results would be mixed in with your originals.")
            warn("       Put the output folder somewhere else:")
            warn("           python3 {} image {} --output ~/{}_small".format(
                PROGRAM, args.paths[0],
                os.path.basename(folder.rstrip(os.sep)) or "images"))
            return None

    return output


def format_for_keep(source):
    """
    Look up (Pillow format, extension) from a file's extension.

    Returns None when the extension is not one this program knows how to keep,
    so the caller can report it instead of guessing.
    """
    return EXTENSION_FORMATS.get(os.path.splitext(source)[1].lower())


def destination_for(source, base, output_folder, extension):
    """
    Where one image's result should be written.

    The path relative to the input folder is kept, so a recursive run
    produces the same folder layout under the output folder.
    """
    relative = os.path.relpath(source, base)
    stem = os.path.splitext(relative)[0]
    return os.path.join(output_folder, stem + extension)


def decide_destinations(images, settings, output_folder):
    """
    Work out the destination of every image, and which ones cannot be done.

    Returns a list of Result objects, in the same order as `images`. One with
    .error set is a file that will be skipped, and the message says why —
    either its format cannot be kept, or two inputs map to the same output
    name, or something is already there and --overwrite was not given.

    Both front ends use this. It is the one place that decides where a result
    goes, which matters because the path that is checked for clashes has to be
    the same path that is later written to.
    """
    results = []
    taken = {}

    for source, base in images:
        # The destination extension is settled here, before any
        # does-it-exist check, so the path that is checked is the path that
        # gets written. Nothing later in the run is allowed to change it.
        if settings.format == "keep":
            known = format_for_keep(source)
            if known is None:
                extension = os.path.splitext(source)[1].lower() or "(no extension)"
                results.append(Result(
                    source, error="{} files cannot be kept; use --format "
                                  "jpeg, png or webp".format(extension)))
                continue
            extension = known[1]
        else:
            extension = REQUESTED_FORMATS[settings.format][1]

        dest = destination_for(source, base, output_folder, extension)
        result = Result(source, dest)

        # Two inputs that would land on the same output file.
        if dest in taken:
            result.error = ("would be written over {} (both map to the same "
                            "output name)".format(os.path.basename(taken[dest])))
        else:
            taken[dest] = source
            # Never damage a file already sitting in the output folder.
            if os.path.exists(dest) and not settings.overwrite:
                result.error = ("already in the output folder — use "
                                "--overwrite to replace it")

        results.append(result)

    return results


# ---------------------------------------------------------------------------
# The image work
# ---------------------------------------------------------------------------
def plan_size(width, height, max_width, max_height, allow_upscale):
    """
    Work out the new size for an image.

    The shape is never changed: one scale factor is applied to both sides, so
    the image shrinks until the wider and taller limits are both satisfied.

    Images already inside the limits are left as they are unless
    --allow-upscale is given.
    """
    if not max_width and not max_height:
        return (width, height)

    # Take the tightest of the two limits. Note that the running value starts
    # out empty rather than at 1.0: seeding it with 1.0 and then calling min()
    # on every limit would quietly cap the scale at 1.0, which would make
    # --allow-upscale impossible to honour.
    scale = None
    if max_width:
        scale = max_width / width
    if max_height:
        height_scale = max_height / height
        scale = height_scale if scale is None else min(scale, height_scale)

    # Enlarging wastes disk space and never adds real detail, so it is opt-in.
    if scale > 1.0 and not allow_upscale:
        return (width, height)

    # Never let a side round away to nothing.
    new_width = max(1, int(round(width * scale)))
    new_height = max(1, int(round(height * scale)))
    return (new_width, new_height)


def prepare_for_format(image, save_format):
    """
    Return the image in a mode the target format can actually store.

    JPEG has no transparency, so an image with an alpha channel is flattened
    onto white first. Without that, transparent areas would turn black.

    PNG keeps whatever it has. WebP handles transparency but not palette
    images, so those are expanded.
    """
    if save_format == "JPEG":
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info)
        if has_alpha:
            flattened = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            flattened.paste(rgba, mask=rgba.split()[-1])
            return flattened
        if image.mode != "RGB":
            return image.convert("RGB")
        return image

    if save_format == "WEBP":
        if image.mode not in ("RGB", "RGBA"):
            return image.convert("RGBA")
        return image

    if save_format == "PNG":
        if image.mode in ("CMYK", "YCbCr", "HSV", "LAB"):
            return image.convert("RGB")
        return image

    return image


def save_options(save_format, quality):
    """The extra settings each format's save() wants."""
    if save_format == "JPEG":
        # optimize makes the encoder try harder for a smaller file, and costs
        # only a little time. It does not change the picture.
        return {"quality": quality, "optimize": True}
    if save_format == "WEBP":
        # method goes up to 6; 4 is a good balance of speed and size.
        return {"quality": quality, "method": 4}
    if save_format == "PNG":
        return {"optimize": True}
    if save_format == "GIF":
        return {"optimize": True}
    return {}


class Result:
    """What happened to one image."""

    __slots__ = ("source", "dest", "before_bytes", "after_bytes",
                 "before_size", "after_size", "note", "error")

    def __init__(self, source, dest=None, error=""):
        self.source = source
        self.dest = dest
        self.before_bytes = 0
        self.after_bytes = 0
        self.before_size = None
        self.after_size = None
        self.note = ""
        self.error = error


def process_one(source, dest, args, quality):
    """
    Read one image, do the requested work, and write the result.

    Nothing here ever opens the source for writing. The result is written
    straight to its destination file, which is in the output folder.

    Returns a Result. A file that cannot be processed comes back with .error
    set rather than raising, so one bad image never stops the run.
    """
    result = Result(source, dest)

    try:
        result.before_bytes = os.path.getsize(source)
    except OSError as error:
        result.error = "cannot read it: {}".format(error)
        return result

    try:
        with Image.open(source) as opened:
            source_format = opened.format or ""
            animated = getattr(opened, "n_frames", 1) > 1

            # Work out which format the result should be in. The destination
            # path was fixed before this function was called and is never
            # changed here — see the note on EXTENSION_FORMATS.
            if args.format == "keep":
                if source_format not in SOURCE_FORMATS:
                    result.error = ("{} files are not supported; use --format "
                                    "to convert them".format(source_format or "unknown"))
                    return result
                save_format = SOURCE_FORMATS[source_format][0]
            else:
                save_format = REQUESTED_FORMATS[args.format][0]

            # Ask the JPEG decoder for a smaller image straight away when we
            # are going to shrink it anyway. This can skip most of the work on
            # a big photo, and uses far less memory.
            if args.max_width or args.max_height:
                try:
                    opened.draft("RGB", (args.max_width or 10 ** 9,
                                         args.max_height or 10 ** 9))
                except Exception:
                    # draft is only a hint; if it is not supported the image
                    # is simply decoded at full size below.
                    pass

            # Was there an orientation tag? Read it before transposing, since
            # exif_transpose removes it.
            try:
                orientation_tag = opened.getexif().get(0x0112, 1)
            except Exception:
                orientation_tag = 1
            if orientation_tag not in (None, 1):
                result.note = "was rotated by EXIF"

            # Apply the EXIF rotation to the actual pixels, and drop the tag.
            # This has to happen before resizing, or a portrait photo stored
            # sideways would be resized to the wrong shape.
            rotated = ImageOps.exif_transpose(opened)

            result.before_size = rotated.size

            # Resize, if asked and if it would actually make it smaller.
            new_size = plan_size(rotated.size[0], rotated.size[1],
                                 args.max_width, args.max_height,
                                 args.allow_upscale)

            if new_size != rotated.size:
                working = rotated.resize(new_size, RESAMPLE_FILTER)
            else:
                working = rotated

            result.after_size = working.size

            working = prepare_for_format(working, save_format)

            # EXIF travels with the output for the formats that store it.
            #
            # This reads the EXIF from `rotated`, NOT from `opened`. That is
            # the whole point: exif_transpose removed the orientation tag from
            # `rotated`'s copy, while `opened` still carries the original tag.
            # Saving `opened`'s version would rotate the pixels here and then
            # ask every viewer to rotate them again.
            exif_bytes = None
            if save_format in ("JPEG", "WEBP", "PNG"):
                try:
                    exif = rotated.getexif()
                    if exif:
                        exif_bytes = exif.tobytes()
                except Exception:
                    exif_bytes = None

            options = save_options(save_format, quality)
            if exif_bytes:
                options["exif"] = exif_bytes

            if animated:
                note = "animated; first frame only"
                result.note = "{}; {}".format(result.note, note) if result.note else note

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            working.save(dest, save_format, **options)

    except Exception as error:
        # One unreadable or unusual file must not end the run. The exception
        # type is included so a real bug is still identifiable.
        result.error = "{}: {}".format(type(error).__name__, error)
        return result

    try:
        result.after_bytes = os.path.getsize(dest)
    except OSError as error:
        result.error = "the result was written but cannot be read back: {}".format(error)

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_row(result, name_width):
    """Print one image's line of the table."""
    name = os.path.basename(result.source)

    if result.error:
        print("  {:<{w}}  FAILED — {}".format(name, result.error, w=name_width))
        return

    dimensions = "{}x{} -> {}x{}".format(
        result.before_size[0], result.before_size[1],
        result.after_size[0], result.after_size[1])

    print("  {:<{w}}  {:>9}  {:>9}  {:>6}  {:<22}  {}".format(
        name,
        format_size(result.before_bytes),
        format_size(result.after_bytes),
        format_change(result.before_bytes, result.after_bytes),
        dimensions,
        result.note,
        w=name_width))


def print_summary(results, output_folder, args, quality):
    """Print the totals, including how much space was actually saved."""
    done = [r for r in results if not r.error and r.after_bytes]
    failed = [r for r in results if r.error]

    before_total = sum(r.before_bytes for r in done)
    after_total = sum(r.after_bytes for r in done)
    saved = before_total - after_total
    grew = [r for r in done if r.after_bytes > r.before_bytes]

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print("  {:<26} {}".format("Images processed", len(done)))
    if failed:
        print("  {:<26} {}".format("Failed", len(failed)))
    print()
    print("  {:<26} {}".format("Total before", format_size(before_total)))
    print("  {:<26} {}".format("Total after", format_size(after_total)))

    if before_total:
        percent = saved / before_total * 100
        if saved >= 0:
            print("  {:<26} {} ({:.0f}% smaller)".format(
                "Space saved", format_size(saved), percent))
        else:
            print("  {:<26} {} ({:.0f}% LARGER)".format(
                "Space change", format_size(-saved), -percent))
            print()
            print("  The results are bigger than the originals. That happens")
            print("  when an image is already well compressed, or when a")
            print("  simple picture is turned into a lossy format. Try a lower")
            print("  --quality, or --format webp.")

    if grew and saved >= 0:
        print()
        print("  {} image(s) came out larger than they went in.".format(len(grew)))

    if failed:
        print()
        print("  These could not be processed:")
        for result in failed:
            print("    - {}: {}".format(os.path.basename(result.source), result.error))

    print()
    print("  Originals: untouched ({} file(s) read, none written)".format(len(results)))
    print("  Results:   {}".format(output_folder))

    if args.format == "keep":
        print("  Format:    kept as-is")
    else:
        print("  Format:    converted to {}".format(args.format))
    if args.max_width or args.max_height:
        limits = []
        if args.max_width:
            limits.append("max width {}".format(args.max_width))
        if args.max_height:
            limits.append("max height {}".format(args.max_height))
        print("  Resize:    {}".format(", ".join(limits)))
    if args.format != "png":
        print("  Quality:   {}".format(quality))


def print_plan(results, name_width):
    """The dry-run listing: what would be written, and where."""
    print()
    print("  {:<{w}}  {}".format("IMAGE", "WOULD BE WRITTEN TO", w=name_width))
    print("  {}  {}".format("-" * name_width, "-" * 40))
    for result in results:
        if result.error or not result.dest:
            continue        # listed separately under "would be skipped"
        print("  {:<{w}}  {}".format(
            os.path.basename(result.source), result.dest, w=name_width))


# ---------------------------------------------------------------------------
# The image command itself
# ---------------------------------------------------------------------------
def cmd_image(args):
    """Run the image command. Returns the exit code."""
    if PILLOW_IMPORT_ERROR is not None:
        print("Error: Pillow is needed to read and write images, and it is")
        print("       not installed for this Python ({}).".format(sys.executable))
        print()
        print("       Install it with:")
        print("           sudo apt install python3-pil")
        print()
        print("       (Pillow is the 'PIL' module. The rename command does not")
        print("       need it, so 'python3 {} rename ...' works as it is.)".format(
            PROGRAM))
        return 1

    explicit_quality = getattr(args, "quality", None)
    quality = explicit_quality if explicit_quality is not None else DEFAULT_QUALITY
    show_progress = not getattr(args, "no_progress", False)

    # --- Find the images ---------------------------------------------------
    print("Looking for images...")
    plan = plan_images(args.paths, recursive=args.recursive,
                       max_width=args.max_width, max_height=args.max_height,
                       allow_upscale=args.allow_upscale, format=args.format,
                       output=args.output, overwrite=args.overwrite,
                       quality=quality)

    if not plan.images:
        print()
        print("No images found.")
        if plan.skipped:
            print("({} file(s) skipped: not image extensions.)".format(plan.skipped))
        # Nothing was found and the arguments themselves were wrong: that is a
        # failure, not an empty success. A mistyped folder must not look like
        # a run that worked.
        if plan.bad_paths:
            print("None of the paths given exist. Check the spelling:")
            for raw_path in args.paths:
                print("    {}".format(raw_path))
            return 1
        if not args.recursive:
            print("If the images are inside subfolders, add --recursive (-r).")
        return 0

    # --- Work out the output folder, and make sure it is safe --------------
    # plan_images refused to settle on one, and has already explained why.
    if plan.output_folder is None:
        return 1

    output_folder = plan.output_folder
    results = plan.results
    ready = plan.ready
    blocked = plan.blocked

    name_width = max(len(os.path.basename(r.source)) for r in results)
    name_width = min(max(name_width, len("IMAGE")), 46)

    print()
    print("{} image(s) found. Writing to:".format(len(plan.images)))
    print("  {}".format(output_folder))
    if args.format != "keep":
        print("  converting to {}".format(args.format))
    if args.max_width or args.max_height:
        if args.max_width:
            print("  max width {}".format(args.max_width))
        if args.max_height:
            print("  max height {}".format(args.max_height))
    if args.format != "png":
        print("  quality {}".format(quality))
    if args.format == "png" and explicit_quality is not None:
        print()
        print("  Note: PNG is lossless, so --quality has no effect on it.")
        print("        Use --format webp or --format jpeg for smaller files.")

    if args.dry_run:
        print_plan(results, name_width)
        if blocked:
            print()
            print("  {} would be skipped:".format(len(blocked)))
            for result in blocked:
                print("    - {}: {}".format(
                    os.path.basename(result.source), result.error))
        print()
        print("=" * 72)
        print("DRY RUN — NOTHING WAS WRITTEN")
        print("=" * 72)
        print()
        print("{} image(s) would be written, one per file listed above."
              .format(len(ready)))
        print()
        print("This listing comes from filenames alone — no image is opened —")
        print("so a file that cannot really be decoded will still fail on a")
        print("real run, and no before/after sizes can be shown in advance.")
        print("No files or folders were created, changed, or opened.")
        return 0

    if not ready:
        print()
        print("Nothing to do:")
        for result in blocked:
            print("  - {}: {}".format(os.path.basename(result.source), result.error))
        return 1

    # --- Process -----------------------------------------------------------
    print()
    print("  {:<{w}}  {:>9}  {:>9}  {:>6}  {:<22}  {}".format(
        "IMAGE", "BEFORE", "AFTER", "CHANGE", "DIMENSIONS", "NOTES", w=name_width))
    print("  {}  {}  {}  {}  {}  {}".format(
        "-" * name_width, "-" * 9, "-" * 9, "-" * 6, "-" * 22, "-" * 5))

    processed = []
    with Progress(len(ready), "Processing", enabled=show_progress) as bar:
        def show(result):
            # Move the bar out of the way first, so the row is readable.
            bar.clear()
            print_row(result, name_width)

        processed = run_images(plan, progress=bar, on_result=show)

    print_summary(processed, output_folder, args, quality)

    failures = [r for r in processed if r.error]
    return 1 if failures and not any(not r.error for r in processed) else 0


# ===========================================================================
# THE CORE, AS A FRONT END SEES IT
# ===========================================================================
# Everything above does the work. Nothing above is a front end: no function
# there knows whether it is being driven from a terminal or a window, except
# through the two channels that are handed to it — `warn`, where messages go,
# and `progress`, which counts the work.
#
# This section is the doorway into all of that. One function per command,
# taking ordinary arguments and handing back data:
#
#     plan_rename(paths, **settings)   what a rename would do — touches nothing
#     run_rename(report, ...)          do it, logging every step so it can be undone
#     plan_undo(log_path)              what undo would put back — touches nothing
#     run_undo(plan, ...)              put it back
#     plan_images(paths, **settings)   what an image run would write — writes nothing
#     run_images(plan, ...)            write the results
#
# The command line at the bottom of this file is one front end; filetool_gui.py
# is another. Neither decides how a name is built, where a result goes, or what
# counts as a clash — all of that happens above, once.
class Cancelled(Exception):
    """
    Raised to stop a run early, from a progress callback.

    A window with a Cancel button gets out this way: its progress object
    raises this the next time the core reports progress. Travelling out
    through the middle of a move is safe, because every rename is written to
    the log before it happens — so a run stopped this way can always be put
    back with undo.
    """


# --- the rename command, as a function -------------------------------------
# Every setting the naming code reads, at the value it has when the matching
# option is not given on the command line.
RENAME_DEFAULTS = {
    "pattern": None,
    "recursive": False,
    "include_hidden": False,
    "prefix": "",
    "suffix": "",
    "base": None,
    "replace_scope": "stem",
    "lower": False,
    "upper": False,
    "title": False,
    "number": False,
    "number_start": 1,
    "number_step": 1,
    "number_pad": 3,
    "number_position": "suffix",
    "sort": "name",
    "reverse": False,
}


# The same idea for the image command.
IMAGE_DEFAULTS = {
    "recursive": False,
    "max_width": None,
    "max_height": None,
    "allow_upscale": False,
    "format": "keep",
    "output": None,
    "overwrite": False,
    "quality": DEFAULT_QUALITY,
}


def options_from(defaults, what, overrides):
    """
    A bag of settings, with the defaults filled in and typos refused.

    The naming code reads its settings from an object it can reach with a
    dot. That is how argparse hands them over, and it suits a window just as
    well: both build one object per run, the same way, from the same table of
    defaults.

    A name that is not in the table is an error rather than a silent no-op,
    so a front end cannot quietly ask for a setting that does not exist and
    be told everything is fine.
    """
    settings = dict(defaults)
    for key, value in overrides.items():
        if key not in settings:
            raise ValueError("unknown {} setting: {!r}".format(what, key))
        settings[key] = value
    return argparse.Namespace(**settings)


def rename_options(**overrides):
    """The rename settings, as one object. See options_from."""
    return options_from(RENAME_DEFAULTS, "rename", overrides)


def image_options(**overrides):
    """The image settings, as one object. See options_from."""
    return options_from(IMAGE_DEFAULTS, "image", overrides)


class RenamePlan:
    """
    What a rename would do, worked out but not done.

        files       every file that was found: (path, mtime_ns, size)
        stats       what the scan left out, and why
        plan        the files that would change: Rename objects
        unchanged   paths that already have the name these options give them
        invalid     (path, reason) for names that must not be created
        collisions  (message, paths) for names that clash or are already taken
    """

    __slots__ = ("files", "stats", "plan", "unchanged", "invalid", "collisions")

    def __init__(self, files, stats, plan, unchanged, invalid, collisions):
        self.files = files
        self.stats = stats
        self.plan = plan
        self.unchanged = unchanged
        self.invalid = invalid
        self.collisions = collisions

    @property
    def blocked(self):
        """True when the run has to be refused, because it would damage files."""
        return bool(self.invalid or self.collisions)

    def collision_paths(self):
        """Every path that a listing should mark as part of a clash."""
        paths = set()
        for _message, sources in self.collisions:
            paths.update(sources)
        return paths

    def reasons(self):
        """(path, reason) for every file that is holding the run up."""
        rows = list(self.invalid)
        for message, sources in self.collisions:
            for source in sources:
                rows.append((source, message))
        return rows


def plan_rename(paths, replace=None, progress=None, warn=None, **options):
    """
    Work out what a rename would do. Nothing on disk is touched.

    `paths` are the files and folders to look at, and `replace` is a list of
    (old, new) text pairs applied in order. Every other setting comes from
    RENAME_DEFAULTS — pass any of them by name, e.g. prefix="trip_".
    """
    settings = rename_options(**options)

    files, stats = collect_files(paths, settings.pattern, settings.recursive,
                                 settings.include_hidden, progress=progress,
                                 warn=warn)
    files = sort_files(files, settings.sort, settings.reverse)
    plan, unchanged, invalid = build_plan(files, settings, list(replace or []))
    collisions = find_collisions(plan)

    return RenamePlan(files, stats, plan, unchanged, invalid, collisions)


def run_rename(report, log_path=None, progress=None, warn=None):
    """
    Carry out a plan, writing each step to the log before it happens.

    Returns (run_id, renamed, problems). The log records are exactly the ones
    the command line writes, because this is what the command line runs.
    """
    log_path = os.path.abspath(os.path.expanduser(log_path or DEFAULT_LOG_PATH))
    run_id = new_run_id()

    append_log(log_path, {
        "type": "run",
        "run": run_id,
        "started": now_stamp(),
        "folders": sorted({os.path.dirname(item.old) for item in report.plan}),
        "planned": len(report.plan),
    })

    renamed, problems = execute_plan(report.plan, log_path, run_id,
                                     progress=progress, warn=warn)

    append_log(log_path, {
        "type": "done",
        "run": run_id,
        "time": now_stamp(),
        "renamed": len(renamed),
        "failed": len(problems),
    })

    return run_id, renamed, problems


class ImagePlan:
    """
    What an image run would write, worked out but not written.

        images          (source, folder) for every image found
        skipped         files passed over because they are not image extensions
        bad_paths       arguments that named nothing on disk
        output_folder   where the results go, or None if it was refused
        results         Result objects, one per image, in the same order
    """

    __slots__ = ("settings", "images", "skipped", "bad_paths", "output_folder",
                 "results")

    def __init__(self, settings, images, skipped, bad_paths, output_folder,
                 results):
        self.settings = settings
        self.images = images
        self.skipped = skipped
        self.bad_paths = bad_paths
        self.output_folder = output_folder
        self.results = results

    @property
    def ready(self):
        """The images that will actually be written."""
        return [result for result in self.results if not result.error]

    @property
    def blocked(self):
        """The images that will be skipped, each with its reason."""
        return [result for result in self.results if result.error]


def plan_images(paths, warn=None, **options):
    """
    Work out what an image run would write. Nothing is written or created.

    Every setting comes from IMAGE_DEFAULTS — pass any of them by name, e.g.
    format="webp", max_width=1600.

    A refusal to use the output folder is reported through `warn`, and leaves
    `output_folder` as None. When no images were found, that check is not
    reached at all, so the caller can report the empty search first.
    """
    settings = image_options(**options)
    settings.paths = list(paths)

    images, skipped, bad_paths = collect_images(paths, settings.recursive,
                                                warn=warn)

    if not images:
        return ImagePlan(settings, images, skipped, bad_paths, None, [])

    output_folder = resolve_output_folder(settings, images, warn=warn)
    if output_folder is None:
        return ImagePlan(settings, images, skipped, bad_paths, None, [])

    results = decide_destinations(images, settings, output_folder)
    return ImagePlan(settings, images, skipped, bad_paths, output_folder, results)


def run_images(plan, progress=None, on_result=None, warn=None):
    """
    Write the results an ImagePlan describes.

    Returns the results in the plan's order. A file that fails comes back with
    .error set rather than raising, so one bad image never stops the run.
    `on_result` is called with each result as it is finished, which is how a
    window fills in its table while the work is still going.
    """
    warn = warn_to(warn)
    quality = plan.settings.quality
    finished = []

    for result in plan.results:
        if result.error:
            # Rejected while planning, so there is nothing to do but report it.
            finished.append(result)
            if on_result is not None:
                on_result(result)
            continue

        outcome = process_one(result.source, result.dest, plan.settings, quality)
        finished.append(outcome)

        if on_result is not None:
            on_result(outcome)
        if progress is not None:
            progress.update()

    return finished


# ===========================================================================
# THE PROGRAM
# ===========================================================================
EXAMPLES = """\
examples:
  See what a rename would do, changing nothing:
      %(prog)s rename ~/Pictures --prefix "trip_" --dry-run

  Number a folder of photos (photo_001.jpg, photo_002.jpg, ...):
      %(prog)s rename ~/Pictures --pattern "*.jpg" --base photo --number

  Rename text inside every filename, and lowercase the extensions:
      %(prog)s rename ~/Music --replace "Live at "="" --lower \\
          --replace-scope ext

  Put back the names from the last rename:
      %(prog)s rename --undo

  Shrink images into a new folder, no wider than 1600 pixels:
      %(prog)s image ~/Pictures --quality 80 --max-width 1600

  Convert a folder of PNGs to WebP, keeping the subfolder layout:
      %(prog)s image ~/Shots --format webp --recursive

  See what the image command would write, changing nothing:
      %(prog)s image ~/Pictures --max-width 1200 --dry-run

Each command has its own help with every option:
      %(prog)s rename --help
      %(prog)s image --help
"""

RENAME_EXAMPLES = """\
examples:
  See what would happen first — this changes nothing:
      %(prog)s ~/Pictures --prefix "trip_" --dry-run

  Add a prefix and a suffix:
      %(prog)s ~/Pictures --prefix "trip_" --suffix "_2024"

  Swap text in every name (repeatable, applied in order):
      %(prog)s ~/Music --replace "Live at "="" --replace " "="_"

  Number them and sort by when they were modified, oldest first:
      %(prog)s ~/Pictures --pattern "*.jpg" --base photo --number --sort mtime

  Lowercase the extensions only:
      %(prog)s ~/Documents --lower --replace-scope ext

  Put back the names from the last run:
      %(prog)s --undo
      %(prog)s --undo --dry-run

The new name is built in this order:
  1. --replace    2. --base    3. case    4. --prefix/--suffix    5. --number
The extension is kept as-is unless --replace-scope says otherwise.

A run is refused outright, with nothing renamed, if any two files would end
up with the same name or if a new name is already taken by another file.
"""

IMAGE_EXAMPLES = """\
examples:
  Compress a folder of photos into a new folder:
      %(prog)s ~/Pictures --quality 80 --output ~/Pictures_small

  Scale down for the web, keeping the shape:
      %(prog)s ~/Pictures --max-width 1600 --output ~/Pictures_web

  Convert everything to WebP and mirror the subfolder layout:
      %(prog)s ~/Shots --format webp --quality 82 --recursive

  See what would be written, changing nothing:
      %(prog)s ~/Pictures --max-width 1200 --dry-run

Results always go to a separate folder. The originals are only ever opened
for reading, and they are never written to, moved, or deleted.

EXIF orientation is handled: the rotation is applied to the pixels and the
orientation tag is then removed, so the result is upright on its own and no
viewer will rotate it a second time. The rest of the EXIF is carried over.

PNG is lossless, so --quality does nothing to it. Use --format webp or
--format jpeg when you want smaller files.
"""


def build_parser():
    """Build the whole command line: the program, then one parser per command."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Rename and reshape lots of files at once, without "
                    "damaging anything.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
        title="commands",
    )

    rename_parser = subparsers.add_parser(
        "rename",
        help="rename many files at once (prefix, replace, number, case, undo)",
        description="Rename many files at once, safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RENAME_EXAMPLES,
    )
    add_rename_arguments(rename_parser)
    rename_parser.set_defaults(func=cmd_rename)

    image_parser = subparsers.add_parser(
        "image",
        help="compress, resize and convert images, keeping the originals",
        description="Compress, resize, and convert images. Never touches "
                    "the originals.",
        formatter_class=HelpFormatter,
        epilog=IMAGE_EXAMPLES,
    )
    add_image_arguments(image_parser)
    image_parser.set_defaults(func=cmd_image)

    return parser


def main(argv=None):
    """Parse the command line and run the chosen command."""
    parser = build_parser()

    # parse_known_args rather than parse_args, so that an option typed before
    # the command ("filetool.py --dry-run image ...") can be answered with a
    # helpful line instead of argparse's bare "unrecognized arguments". What
    # is left over is still an error, so a typo is never quietly ignored.
    args, extra = parser.parse_known_args(argv)

    if extra:
        print("Error: did not understand: {}".format(" ".join(extra)))
        print()
        print("       Either that is a typo, or the option was put before the")
        print("       command. Options belong after it:")
        print("           python3 {} image ~/Pictures --max-width 1600".format(PROGRAM))
        print("           python3 {} rename ~/Pictures --prefix \"trip_\" --dry-run".format(
            PROGRAM))
        return 2

    if getattr(args, "command", None) is None or not hasattr(args, "func"):
        # No command, or an option given before the command. Printing the
        # help is far more useful than argparse's bare "invalid choice".
        parser.print_help()
        print()
        print("Error: no command given.", file=sys.stderr)
        print("       Pick one of: rename, image.", file=sys.stderr)
        print("       For example:", file=sys.stderr)
        print("           python3 {} image ~/Pictures --max-width 1600".format(
            PROGRAM), file=sys.stderr)
        return 2

    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Ctrl+C. What is left behind differs between the two commands, so
        # say what to do about it rather than just dying.
        print()
        print("Stopped by Ctrl+C.", file=sys.stderr)
        if args.command == "rename":
            print("Every rename is recorded before it happens, so any file", file=sys.stderr)
            print("left parked at a temporary name can be put back with:", file=sys.stderr)
            print("    python3 {} rename --undo".format(PROGRAM), file=sys.stderr)
        else:
            print("Any image being written when you stopped may be incomplete.", file=sys.stderr)
            print("The originals were not touched. Re-run with --overwrite to", file=sys.stderr)
            print("replace the incomplete results.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
