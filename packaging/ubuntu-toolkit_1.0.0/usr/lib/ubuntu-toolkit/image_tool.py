#!/usr/bin/env python3
"""
image_tool.py — compress, resize, and convert images without touching the
originals.

Usage:
    # Shrink images to a decent quality, writing the results to a new folder:
    python3 image_tool.py ~/Pictures --quality 80 --output ~/Pictures_small

    # Also scale them down so they are at most 1600 pixels wide:
    python3 image_tool.py ~/Pictures --max-width 1600 --output ~/Pictures_web

    # Convert a folder of PNGs to WebP:
    python3 image_tool.py ~/Pictures --format webp --quality 82

    # See what would happen, write nothing:
    python3 image_tool.py ~/Pictures --max-width 1200 --dry-run

How it works
------------
Every image found in the folders you name is read, optionally rotated
upright, optionally scaled down, and then written into the output folder.
The input files are only ever opened for reading — the script has no code
path that writes to, moves, or deletes an original.

Output always goes to a *new* folder, and the script refuses to start if the
output folder is the same as, or inside, a folder you are reading from. It
also refuses to overwrite a file that is already in the output folder unless
you pass --overwrite.

EXIF orientation
----------------
Phone photos are usually stored landscape, with a tag in their EXIF metadata
saying "rotate me 90 degrees when showing this". If that tag is copied to the
output as-is while the picture also gets resized, viewers can end up showing
it sideways, and the resize uses the wrong width and height.

This script applies the rotation to the actual pixels first
(ImageOps.exif_transpose), then removes the tag, so the output is upright on
its own and needs no tag. The rest of the EXIF — camera, date taken, and so
on — is carried over to the JPEG and WebP output.

Quality
-------
--quality affects JPEG and WebP, which are lossy: lower means smaller files
and more visible damage. 85 is a good default, 70-80 is usually fine for
photos, and below about 50 the artefacts become obvious.

PNG is lossless and has no quality setting, so --quality does nothing to a
PNG. If you want smaller files, convert to WebP or JPEG with --format.

Dependencies
------------
Pillow. On Ubuntu that is already installed on most desktops; if not:

    sudo apt install python3-pil

No other packages are needed.
"""

import argparse
import os
import sys

# Pillow is the one third-party package this script needs. It is imported
# here rather than at the top of the file so that a missing Pillow produces a
# helpful message instead of a bare traceback.
try:
    from PIL import Image, ImageOps
    PILLOW_IMPORT_ERROR = None
except ImportError as error:      # pragma: no cover - depends on the machine
    PILLOW_IMPORT_ERROR = error


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

# Pillow renamed the resampling filters, so support both spellings. LANCZOS
# is the slowest and the best looking, which is what you want for photos.
try:
    RESAMPLE_FILTER = Image.Resampling.LANCZOS
except AttributeError:            # Pillow older than 9.1
    RESAMPLE_FILTER = Image.LANCZOS


# ===========================================================================
# Formatting
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


# ===========================================================================
# Command line
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


def positive_int(text):
    """argparse type: a whole number of 1 or more."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: {!r}".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more, got {}".format(value))
    return value


def parse_arguments():
    """Read the command line options."""
    parser = argparse.ArgumentParser(
        description="Compress, resize, and convert images. Never touches "
                    "the originals.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Output always goes to a separate folder, and the script refuses\n"
            "to run if that folder is the same as, or inside, an input folder.\n"
            "Start with --dry-run to see what it would do.\n"
        ),
    )

    parser.add_argument(
        "paths",
        nargs="+",
        help="image files or folders to process",
    )

    what = parser.add_argument_group("what to do")
    what.add_argument(
        "--quality",
        type=quality_argument,
        metavar="1-100",
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

    return parser.parse_args()


# ===========================================================================
# Finding the images
# ===========================================================================
def collect_images(paths, recursive):
    """
    Return a list of (source_path, base_folder) for every image found.

    `base_folder` is the folder the file's relative path is measured from, so
    that a recursive run can rebuild the same folder layout inside the output
    folder.

    Returns (images, skipped_non_images, bad_paths) where skipped_non_images
    counts files that were passed over because their extension is not an image
    one, and bad_paths counts arguments that named nothing on disk at all.
    """
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
            print("Warning: not a file or folder, skipping: {}".format(raw_path))
            bad_paths += 1
            continue

        if recursive:
            for folder, subfolders, filenames in os.walk(
                    path, onerror=lambda error: print("Warning: {}".format(error))):
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
                print("Warning: cannot read {}: {}".format(path, error))
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


# ===========================================================================
# The output folder
# ===========================================================================
def resolve_output_folder(args, images):
    """
    Work out where the results go, and refuse to accept anywhere unsafe.

    The one rule that matters: the output folder must not be the folder the
    images are being read from, and must not be inside it. Otherwise a run
    could write on top of the very files it is supposed to be protecting.
    """
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
            print("Error: the output folder is the same folder as the one")
            print("       being read: {}".format(folder))
            print()
            print("       That would write over your originals. Give a")
            print("       different --output, for example a folder beside it.")
            return None
        if folder in scanned and output.startswith(folder.rstrip(os.sep) + os.sep):
            print("Error: the output folder is inside the folder being read:")
            print("         output: {}".format(output))
            print("         input:  {}".format(folder))
            print()
            print("       A recursive run could pick up its own output, and")
            print("       the results would be mixed in with your originals.")
            print("       Put the output folder somewhere else.")
            return None

    return output


def format_for_keep(source):
    """
    Look up (Pillow format, extension) from a file's extension.

    Returns None when the extension is not one this script knows how to keep,
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


# ===========================================================================
# The image work
# ===========================================================================
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


# ===========================================================================
# Reporting
# ===========================================================================
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


# ===========================================================================
# Main
# ===========================================================================
def main():
    if PILLOW_IMPORT_ERROR is not None:
        print("Pillow is needed to read and write images, and it is not")
        print("installed for this Python ({}).".format(sys.executable))
        print()
        print("Install it with:")
        print("    sudo apt install python3-pil")
        print()
        print("(Pillow is the 'PIL' module.)")
        return 1

    args = parse_arguments()
    quality = args.quality if args.quality is not None else DEFAULT_QUALITY

    # --- Find the images ---------------------------------------------------
    print("Looking for images...")
    images, skipped_non_images, bad_paths = collect_images(
        args.paths, args.recursive)

    if not images:
        print()
        print("No images found.")
        if skipped_non_images:
            print("({} file(s) skipped: not image extensions.)".format(skipped_non_images))
        # Nothing was found and the arguments themselves were wrong: that is a
        # failure, not an empty success. A mistyped folder must not look like
        # a run that worked.
        if bad_paths:
            print("None of the paths given exist. Check the spelling.")
            return 1
        return 0

    # --- Work out the output folder, and make sure it is safe --------------
    output_folder = resolve_output_folder(args, images)
    if output_folder is None:
        return 1

    # --- Decide where each result goes -------------------------------------
    results = []
    taken = {}
    for source, base in images:
        # The destination extension is settled here, before any
        # does-it-exist check, so the path that is checked is the path that
        # gets written. Nothing later in the run is allowed to change it.
        if args.format == "keep":
            known = format_for_keep(source)
            if known is None:
                extension = os.path.splitext(source)[1].lower() or "(no extension)"
                results.append(Result(
                    source, error="{} files cannot be kept; use --format "
                                  "jpeg, png or webp".format(extension)))
                continue
            extension = known[1]
        else:
            extension = REQUESTED_FORMATS[args.format][1]

        dest = destination_for(source, base, output_folder, extension)
        result = Result(source, dest)

        # Two inputs that would land on the same output file.
        if dest in taken:
            result.error = ("would be written over {} (both map to the same "
                            "output name)".format(os.path.basename(taken[dest])))
        else:
            taken[dest] = source
            # Never damage a file already sitting in the output folder.
            if os.path.exists(dest) and not args.overwrite:
                result.error = ("already in the output folder — use "
                                "--overwrite to replace it")

        results.append(result)

    ready = [r for r in results if not r.error]
    blocked = [r for r in results if r.error]

    name_width = max(len(os.path.basename(r.source)) for r in results)
    name_width = min(max(name_width, len("IMAGE")), 46)

    print()
    print("{} image(s) found. Writing to:".format(len(images)))
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
    if args.format == "png" and args.quality is not None:
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
    for result in results:
        if result.error:
            print_row(result, name_width)
            processed.append(result)
            continue
        outcome = process_one(result.source, result.dest, args, quality)
        print_row(outcome, name_width)
        processed.append(outcome)

    print_summary(processed, output_folder, args, quality)

    failures = [r for r in processed if r.error]
    return 1 if failures and not any(not r.error for r in processed) else 0


if __name__ == "__main__":
    sys.exit(main())
