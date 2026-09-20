#!/usr/bin/env python3
"""
bulk_rename.py — rename many files at once, safely.

Usage:
    # See what would happen, change nothing:
    python3 bulk_rename.py ~/Pictures --prefix "trip_" --dry-run

    # Add a prefix and a suffix:
    python3 bulk_rename.py ~/Pictures --prefix "trip_" --suffix "_2024"

    # Replace text in every name:
    python3 bulk_rename.py ~/Music --replace "Live at "="" --replace " "="_"

    # Number them: photo_001.jpg, photo_002.jpg, ...
    python3 bulk_rename.py ~/Pictures --pattern "*.jpg" --base photo --number

    # Number by the order they were modified, oldest first:
    python3 bulk_rename.py ~/Pictures --number --sort mtime

    # Change case:
    python3 bulk_rename.py ~/Documents --lower

    # Put back the names from the last run:
    python3 bulk_rename.py --undo
    python3 bulk_rename.py --undo --dry-run

How a new name is built
-----------------------
A filename is split into a "stem" and an "extension": photo.jpg is the stem
"photo" plus the extension ".jpg". The extension is kept as it is, so --upper
turns photo.jpg into PHOTO.jpg, not PHOTO.JPG.

The stem is then changed in this order:

    1. --replace        text is swapped  ("holiday" -> "vacation")
    2. --base           the whole stem is replaced with one name
    3. --lower/--upper/--title    the case is changed
    4. --prefix/--suffix          text is added around the stem
    5. --number         the counter is inserted

So this command:

    --replace " "="_" --base photo --upper --prefix "trip_" --number

turns "my first photo.jpg" into "trip_PHOTO_001.jpg".

--replace works on the stem by default. --replace-scope picks which part of
the name the text options act on — ext for extensions, full for the whole
name — and the case options follow the same scope. So:

    --replace "jpeg=jpg" --replace-scope ext    a.jpeg      -> a.jpg
    --lower --replace-scope ext                 PHOTO.JPEG  -> PHOTO.jpeg

--replace is case-sensitive, so "jpeg" does not match ".JPEG".

Safety
------
This script is built so that it cannot quietly mess up your files:

  * --dry-run shows a before/after table and changes nothing. Always start
    with it.
  * It REFUSES to run if two files would end up with the same name, or if a
    new name is already taken by a file it is not renaming. Nothing at all
    is renamed in that case — not even the files that were fine.
  * It never overwrites a file. If the target name is taken, that is a
    collision, and a collision stops the whole run.
  * Every rename is written to a log BEFORE it happens, so --undo can put
    things back even if the script is interrupted half way through.
  * Renaming happens in two steps (each file first gets a temporary name,
    then its real new name). That is what makes names able to swap places
    safely, and it means a failure part way through can be rolled back.

The undo log lives at ~/.local/state/bulk-rename/history.log

Only the standard library is used. Python 3.8+.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time

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
# Small helpers
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


# ===========================================================================
# Splitting a filename into stem and extension
# ===========================================================================
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


# ===========================================================================
# Reading the command line values
# ===========================================================================
def positive_int(text):
    """argparse type: a whole number of 1 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, got {}".format(value))
    return value


def zero_or_more_int(text):
    """argparse type: a whole number of 0 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if value < 0:
        raise argparse.ArgumentTypeError("cannot be negative, got {}".format(value))
    return value


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


def parse_arguments():
    """Read the command line options."""
    parser = argparse.ArgumentParser(
        description="Rename many files at once, safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The new name is built in this order:\n"
            "  1. --replace    2. --base    3. case    4. --prefix/--suffix"
            "    5. --number\n"
            "The extension is kept as-is unless --replace-scope says otherwise.\n"
            "\n"
            "Start with --dry-run: it shows a before/after table and changes\n"
            "nothing. The run is refused outright if any two files would end up\n"
            "with the same name, or if a new name is already taken.\n"
        ),
    )

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
        help="skip the 'Proceed?' question. Needed when running from a script, "
             "since there is no terminal to answer on.",
    )

    return parser.parse_args()


# ===========================================================================
# Finding the files
# ===========================================================================
def matches_any_pattern(filename, patterns):
    """True if the filename matches any of the glob patterns."""
    return any(fnmatch.fnmatchcase(filename, pattern) for pattern in patterns)


def collect_files(paths, patterns, recursive, include_hidden):
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
    """
    files = []
    stats = {"hidden": 0, "no_match": 0, "symlinks": 0, "empty_dirs": 0, "bad_paths": 0}
    patterns = patterns or ["*"]

    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))

        if os.path.islink(path):
            stats["symlinks"] += 1
            print("Skipping symlink: {}".format(path))
            continue

        if os.path.isfile(path):
            # Named directly, so it is always included.
            try:
                info = os.stat(path)
            except OSError as error:
                print("Skipping {}: {}".format(path, error))
                continue
            files.append((path, info.st_mtime_ns, info.st_size))
            continue

        if not os.path.isdir(path):
            stats["bad_paths"] += 1
            print("Warning: not a file or folder, skipping: {}".format(raw_path))
            continue

        cleaned = scan_folder(path, patterns, recursive, include_hidden, stats)
        if not cleaned:
            stats["empty_dirs"] += 1
        files.extend(cleaned)

    return files, stats


def scan_folder(folder, patterns, recursive, include_hidden, stats):
    """
    Collect the matching files inside one folder.

    os.scandir is used rather than os.walk because it hands back the file's
    type information in the same call that reads the directory, which avoids
    a second round trip to the disk for every entry.
    """
    found = []

    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as error:
        # An unreadable folder is a warning, not a reason to give up.
        print("Warning: cannot read {}: {}".format(folder, error))
        return found

    for entry in entries:
        try:
            if entry.is_symlink():
                stats["symlinks"] += 1
                continue

            if entry.is_dir():
                if recursive:
                    found.extend(
                        scan_folder(entry.path, patterns, recursive,
                                    include_hidden, stats))
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
            print("Warning: cannot read {}: {}".format(entry.path, error))

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


# ===========================================================================
# Building the new names
# ===========================================================================
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


def build_plan(files, args, replacements):
    """
    Work out the old name and the new name for every file.

    Returns (plan, unchanged, invalid) where:
      plan      is a list of Rename namedtuples, only for files that change
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


class Rename:
    """One file's old and new name. A plain class so the table can label it."""

    __slots__ = ("old", "new")

    def __init__(self, old, new):
        self.old = old
        self.new = new


# ===========================================================================
# Collision checking — the part that must never get this wrong
# ===========================================================================
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


# ===========================================================================
# Showing the plan
# ===========================================================================
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


# ===========================================================================
# The log
# ===========================================================================
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


def read_log(log_path):
    """
    Read every record from the log.

    A damaged line is reported and skipped rather than crashing: a log that
    is 99% readable is far more useful than no log at all.
    """
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
                    print("Warning: unreadable line {} in {} — skipped.".format(
                        number, log_path))
    except OSError as error:
        print("Error: cannot read the log {}: {}".format(log_path, error))

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


# ===========================================================================
# Doing the rename
# ===========================================================================
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
    one. For this script that would be data loss, so the target is checked
    first and the move is refused if it is occupied.

    There is an unavoidable sliver of a moment between the check and the
    rename in which another program could create that name. Nothing in the
    standard library closes it completely, and the collision check earlier in
    the run makes it very unlikely, so the target is simply checked as close
    to the rename as possible.
    """
    if os.path.lexists(target):
        raise FileExistsError(
            "{} already exists, and this script never overwrites a file".format(target))
    os.rename(source, target)


def two_phase_move(moves, run_id, note_stage=None):
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
    staged = []
    problems = []

    for index, (source, target) in enumerate(moves):
        temporary = temp_path(os.path.dirname(source), run_id, index)

        if note_stage is not None:
            note_stage(source, target, temporary)

        if not os.path.lexists(source):
            problems.append((source, "disappeared before it could be moved"))
            continue

        try:
            os.rename(source, temporary)
        except OSError as error:
            problems.append((source, "could not be moved: {}".format(error)))
            continue

        staged.append((source, temporary, target))

    done = []
    for source, temporary, target in staged:
        try:
            move_to(temporary, target)
            done.append((source, target))
        except OSError as error:
            problems.append((target, "could not take its new name: {}".format(error)))
            # Something went wrong part way through, so put everything back
            # rather than leaving a half-finished folder behind.
            _rollback(staged, done, problems)
            return [], problems

    return done, problems


def _rollback(staged, done, problems):
    """
    Put every staged file back where it started.

    `done` are the pairs that already reached their target; the rest are
    still at their temporary names. Both are moved back, so the folder ends
    up exactly as it was before the run.
    """
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
        print("Rolled back {} file(s) to where they started.".format(restored))

    return restored


def execute_plan(plan, log_path, run_id):
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
                          run_id, note_stage=log_stage)


# ===========================================================================
# Undo
# ===========================================================================
def cmd_undo(args):
    """
    Put back the names recorded in the log.

    Undoing is the same problem as renaming, in reverse: several files may
    need to move at once with their target names still occupied by each
    other. It therefore uses the same two-phase move, for the same reason.
    """
    log_path = os.path.abspath(os.path.expanduser(args.log_file))
    records = read_log(log_path)

    if not records:
        print("No history found at {}.".format(log_path))
        print("Nothing to undo.")
        return 1

    run_id = last_run_id(records)
    if run_id is None:
        print("The log has no completed run to undo.")
        return 1

    entries = [record for record in records
               if record.get("type") == "staged" and record.get("run") == run_id]

    if not entries:
        print("Run {} has no renames recorded.".format(run_id))
        return 1

    # A run that has already been undone should not be undone twice. The
    # 'restore' records say which files are already back, which is more
    # reliable than guessing from what is on disk: in a chain, a name being
    # present does not mean the file that used to have it is back.
    already_restored = {record.get("dst") for record in records
                        if record.get("type") == "restore"
                        and record.get("run") == run_id}

    todo = [entry for entry in entries if entry.get("src") not in already_restored]

    if not todo:
        print("Run {} has already been undone.".format(run_id))
        print("Nothing to do.")
        return 0

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
        print()
        print("Nothing could be restored:")
        for path, reason in problems:
            print("  - {}: {}".format(os.path.basename(path), reason))
        return 1

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
        print()
        print("Nothing could be restored:")
        for path, reason in problems:
            print("  - {}: {}".format(os.path.basename(path), reason))
        return 1

    # --- Show, or do ------------------------------------------------------
    print()
    print("Undoing run {} — {} file(s).".format(run_id, len(safe_moves)))

    if args.dry_run:
        print()
        for current, source in safe_moves:
            print("  [dry-run] {}  <-  {}".format(source, current))
        print()
        print("Dry run — nothing was renamed.")
        print("Re-run without --dry-run to put the names back.")
        return 0

    undo_run_id = new_run_id()

    def log_restore(current, source, temporary):
        # Written before the move, so an interruption during an undo can
        # still be worked out from the log afterwards.
        append_log(log_path, {
            "type": "restore",
            "run": run_id,
            "undo_run": undo_run_id,
            "src": current,
            "tmp": temporary,
            "dst": source,
            "time": now_stamp(),
        })

    restored, move_problems = two_phase_move(
        safe_moves, undo_run_id, note_stage=log_restore)
    problems.extend(move_problems)

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

    print("The names from run {} are back.".format(run_id))
    return 0


# ===========================================================================
# Main
# ===========================================================================
def main():
    args = parse_arguments()

    if args.undo:
        return cmd_undo(args)

    if not args.paths:
        print("Error: give me at least one file or folder to rename.")
        print("       (Or use --undo to put back the last run.)")
        return 1

    log_path = os.path.abspath(os.path.expanduser(args.log_file))

    # --- Work out which files, and what they would be called ---------------
    try:
        replacements = parse_replacements(args.replace)
    except ValueError as error:
        print("Error: {}".format(error))
        return 1

    print("Looking for files...")
    files, stats = collect_files(
        args.paths, args.pattern, args.recursive, args.include_hidden)

    if not files:
        print()
        print("No files found to rename.")
        print_skips(stats)
        # A folder with nothing matching in it is a normal, successful result.
        # Every path being wrong is a mistake worth an error code, so a script
        # does not read it as success.
        if stats["bad_paths"] and stats["bad_paths"] == len(args.paths):
            print()
            print("Error: none of the paths given exist.")
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

    run_id = new_run_id()
    append_log(log_path, {
        "type": "run",
        "run": run_id,
        "started": now_stamp(),
        "folders": sorted({os.path.dirname(item.old) for item in plan}),
        "planned": len(plan),
    })

    print()
    print("Renaming {} file(s)...".format(len(plan)))
    renamed, problems = execute_plan(plan, log_path, run_id)

    append_log(log_path, {
        "type": "done",
        "run": run_id,
        "time": now_stamp(),
        "renamed": len(renamed),
        "failed": len(problems),
    })

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
    else:
        print("Done. To put the old names back:")
        print("    python3 {} --undo".format(os.path.basename(sys.argv[0])))
        if not args.yes:
            print()
            print("(Undo reads {}.)".format(log_path))

    return 1 if problems and not renamed else 0


def confirm(prompt, assume_yes=False):
    """
    Ask before doing anything. Returns True only on a clear yes.

    An answer typed at the terminal and an answer piped in both count — that
    is what makes 'printf "y\\n" | bulk_rename.py ...' work, and it is still
    an explicit confirmation.

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


if __name__ == "__main__":
    sys.exit(main())
