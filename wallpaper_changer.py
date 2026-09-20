#!/usr/bin/env python3
"""
wallpaper_changer.py — fetch today's Bing photo and set it as the GNOME
desktop background.

Usage:
    # Download today's image and apply it:
    python3 wallpaper_changer.py

    # Set a random wallpaper from the ones already saved (no network needed):
    python3 wallpaper_changer.py --random

    # Pop up a desktop notification when the wallpaper actually changes:
    python3 wallpaper_changer.py --notify

    # Keep 30 images around instead of the default 15:
    python3 wallpaper_changer.py --keep 30

    # Pick a different edition of the Bing homepage (different photos):
    python3 wallpaper_changer.py --market en-GB

    # Download but leave the current background alone:
    python3 wallpaper_changer.py --no-set

    # Fetch again even if today's file is already on disk:
    python3 wallpaper_changer.py --force

    # Keep the images somewhere else:
    python3 wallpaper_changer.py --dir ~/Pictures/Backgrounds

How it works
------------
Bing publishes the photo from its homepage as JSON. We ask for the newest
entry, build the URL of the UHD (3840x2160) version of that picture, and save
it to ~/Pictures/Wallpapers as bing-YYYY-MM-DD.jpg. Then we point the GNOME
background schema at the file with gsettings, setting both the light and dark
variants so the wallpaper does not change when you switch the system theme.

Running it more than once a day is free
---------------------------------------
If the file for today's photo is already on disk, the download is skipped and
the existing image is simply re-applied — which is what makes this safe to run
again after a reboot, or from both a login hook and a timer. --force overrides
that and fetches the bytes again.

Old images are cleaned up
-------------------------
Pruning keeps the newest --keep images (default 15) and deletes the rest, so
the folder does not grow without bound. Two rules make that safe:

  * Only files whose names look exactly like the ones this script writes
    (bing-YYYY-MM-DD.jpg) are ever candidates. Anything else you keep in the
    folder — your own photos, other wallpapers — is left alone.
  * The file the desktop is currently showing is never deleted, and neither is
    the one --random just picked. Without that, a run could delete the image
    out from under the running desktop and leave you with a blank background.

Notifications
-------------
With --notify, a desktop notification pops up after the wallpaper changes,
titled with Bing's headline for the photo and credited with the copyright
line, using the image itself as the icon. It is deliberately quiet about runs
that change nothing: a daily timer plus a run at every login means the same
picture gets re-applied often, and a notification each time would be noise.
The notification is sent only when the file being applied differs from the one
the desktop is already showing.

Offline and API failures
------------------------
A missing network connection, a DNS failure, a timeout, or a reply that is not
JSON all end in the same way: a single line saying what went wrong and a
non-zero exit, with nothing on disk touched. If some wallpapers are already
saved, the message points at --random, which needs no network at all.

Why only the standard library
-----------------------------
The whole job is two GET requests. urllib does that in a few lines, and
keeping to the standard library means this keeps working on a machine with no
pip packages installed and no virtualenv activated — which is exactly the
situation a login or timer unit runs in.

Nothing is ever left half-written
---------------------------------
Downloads land in a temporary file next to the destination and are moved into
place with os.replace() only after the transfer finished and the bytes were
checked to actually be an image. So an interrupted run can never leave a
truncated .jpg where the wallpaper is supposed to be.

Exit status
-----------
0 on success (with --no-set, that means the download succeeded), 1 on any
failure.
"""

import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# The archived-homepage endpoint. idx=0 is today's image, n=1 asks for just
# that one. format=js gives us JSON instead of the XML that this endpoint
# returns when you leave it off.
API_URL = "https://www.bing.com/HPImageArchive.aspx"
IMAGE_HOST = "https://www.bing.com"

# Bing serves an empty 200 or a redirect to a bot-check page for requests with
# urllib's default User-Agent, so identify ourselves as an ordinary browser.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# A wallpaper that small is a server error page, not a photograph.
MIN_IMAGE_BYTES = 10 * 1024

# urllib's own default is to block forever, which would hang a timer unit.
# Every request gets this many seconds unless --timeout says otherwise.
DEFAULT_TIMEOUT = 30

BACKGROUND_SCHEMA = "org.gnome.desktop.background"

# GNOME adopted picture-uri-dark in 42. Setting it on an older schema is an
# error, so it is treated as optional (see set_wallpaper).
WALLPAPER_KEYS = ("picture-uri", "picture-uri-dark")

# How many images to keep before pruning. A fortnight of Bing photos is a few
# hundred megabytes at UHD sizes, which is a reasonable ceiling.
DEFAULT_KEEP = 15

# The shape of every filename this script writes, and the only shape pruning
# will agree to delete. Deliberately narrow: it is the difference between
# tidying up after ourselves and eating whatever else lives in the folder.
NAME_PREFIX = "bing-"


class FetchError(Exception):
    """
    Anything that went wrong talking to the network.

    Raised for DNS failures, refused connections, timeouts, HTTP error statuses,
    replies that are not JSON, and image URLs that hand back an HTML error page.
    The message is written to be shown to the user as-is.
    """


def _describe_url_error(error, timeout):
    """Turn a urllib/socket failure into one plain sentence."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, socket.gaierror):
        # gaierror means the name did not resolve, which in practice means no
        # DNS — i.e. no internet.
        return "cannot reach bing.com — no internet connection?"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"bing.com did not reply within {timeout:g}s"
    if isinstance(reason, ConnectionRefusedError):
        return "bing.com refused the connection"
    return f"could not reach bing.com ({reason})"


def _open(url, timeout):
    """
    urlopen() with every failure mode translated into FetchError.

    Kept in one place so that the JSON call and the image download report the
    same way, and so HTTPError is always checked before URLError — the former
    is a subclass of the latter, and the order of except clauses decides which
    one wins.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise FetchError(f"Bing replied with HTTP {error.code} {error.reason}") from error
    except urllib.error.URLError as error:
        raise FetchError(_describe_url_error(error, timeout)) from error
    except (TimeoutError, OSError) as error:
        # Read timeouts surface here rather than as URLError.
        raise FetchError(_describe_url_error(error, timeout)) from error


def fetch_json(timeout, market):
    """Ask Bing for the current homepage images and return the parsed JSON."""
    url = f"{API_URL}?format=js&idx=0&n=1&mkt={market}"
    with _open(url, timeout) as response:
        raw = response.read()
    try:
        # utf-8-sig: the endpoint has been known to send a UTF-8 BOM, which
        # json.loads() chokes on if you decode the raw bytes as plain utf-8.
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FetchError(f"Bing sent a reply that is not JSON ({error})") from error


def parse_image(payload):
    """
    Pull the image we want out of the API payload.

    The interesting field is urlbase, e.g. "/th?id=OHR.SomePhoto_EN-US123".
    The full-size version is that plus "_UHD.jpg" — 3840x2160, the largest
    Bing publishes. The url field holds a 1920x1080 version that is always
    present, and it is what we fall back to if the UHD one has been pruned.
    """
    images = payload.get("images") or []
    if not images:
        raise FetchError("Bing returned no images for today")

    image = images[0]
    urlbase = image.get("urlbase")
    fallback = image.get("url")

    candidates = []
    if urlbase:
        candidates.append(f"{IMAGE_HOST}{urlbase}_UHD.jpg")
    if fallback:
        # Usually site-relative ("/th?id=..."), but an absolute URL is left
        # alone rather than having the host glued on twice.
        candidates.append(
            fallback if fallback.startswith("http") else f"{IMAGE_HOST}{fallback}"
        )

    # dict.fromkeys() dedupes while keeping the order (UHD first), in case the
    # API ever hands back the same URL in both fields.
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise FetchError("Bing's response had neither urlbase nor url")

    # startdate is the day Bing published the photo, and it is the date the
    # filename should carry: run this twice in one evening and the second run
    # must land on the same filename, not create a duplicate. A payload
    # without it falls back to the local date.
    published = _parse_compact_date(image.get("startdate")) or date.today()

    return {
        "urls": candidates,
        "date": published,
        "copyright": image.get("copyright", ""),
        "title": image.get("title", ""),
    }


def _parse_compact_date(value):
    """Turn Bing's "20260920" into a date object, or None if it is unusable."""
    if not value or len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def _date_from_name(path):
    """Read the date back out of bing-YYYY-MM-DD.jpg, or None."""
    stem = path.stem
    if not stem.startswith(NAME_PREFIX):
        return None
    try:
        return date.fromisoformat(stem[len(NAME_PREFIX):])
    except ValueError:
        return None


def download(url, dest, timeout):
    """
    Save url to dest atomically. Returns the number of bytes written.

    The bytes go to a temporary file in the destination directory first, so
    that the rename at the end is atomic: dest either does not exist or is a
    complete image, never a partial one. The temporary file has to be in the
    same directory for os.replace() to be a same-filesystem rename.
    """
    with _open(url, timeout) as response:
        content_type = response.headers.get_content_type()
        # A missing image comes back as an HTML error page rather than a
        # useful status code often enough to check this rather than trust it.
        if not content_type.startswith("image/"):
            raise FetchError(f"expected an image, server sent {content_type}")

        fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=dest.name + ".", suffix=".part"
        )
        tmp = Path(tmp_name)
        try:
            # mkstemp deliberately creates the file as 0600. That is right for
            # a secret, but wrong for a wallpaper: the saved file should end up
            # with the same permissions as any other file written here, so ask
            # the umask what that is.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
            with os.fdopen(fd, "wb") as handle:
                written = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
        except BaseException:
            # Includes KeyboardInterrupt: never leave a .part file behind.
            tmp.unlink(missing_ok=True)
            raise

    if written < MIN_IMAGE_BYTES:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"only {written} bytes — that is not a photograph")

    os.replace(tmp, dest)
    return written


def download_image(image, dest, timeout):
    """
    Download the best available version of image into dest.

    Tries the UHD URL first and falls back to the 1080p one, which is worth a
    retry because Bing retires the UHD renditions of older photos. Only if
    every candidate fails does this raise, with the last error as the message.
    """
    last_error = None
    for url in image["urls"]:
        try:
            return download(url, dest, timeout)
        except (FetchError, ValueError) as error:
            last_error = error
            print(f"Warning: {url} failed ({error})", file=sys.stderr)
    raise FetchError(str(last_error) if last_error else "no image URL to try")


def list_wallpapers(directory):
    """
    The wallpapers this script saved in directory, oldest first.

    Only files named bing-YYYY-MM-DD.jpg are returned, so a folder that also
    holds the user's own pictures is safe to point --dir at. Sorting by name
    works because the date is ISO-8601: newest last, and picking the newest N
    is just a slice from the end.
    """
    found = [p for p in directory.glob(f"{NAME_PREFIX}*.jpg") if p.is_file()]
    dated = [(p, _date_from_name(p)) for p in found]
    return sorted((p for p, when in dated if when), key=lambda p: p.name)


def prune(directory, keep, protect=()):
    """
    Delete the oldest saved wallpapers, leaving the newest `keep` in place.

    protect is a set of paths that must survive regardless of age — the image
    the desktop is showing right now, typically, which may well be older than
    the cutoff. Returns the paths that were actually deleted.
    """
    saved = list_wallpapers(directory)
    safe = {p.resolve() for p in protect if p}
    doomed = saved[:-keep] if keep < len(saved) else []

    deleted = []
    for path in doomed:
        if path.resolve() in safe:
            continue
        try:
            path.unlink()
        except OSError as error:
            print(f"Warning: could not delete {path}: {error}", file=sys.stderr)
            continue
        deleted.append(path)
    return deleted


def current_wallpaper(directory):
    """
    The file the desktop is showing, if gsettings will tell us.

    Used only to keep pruning from deleting the wallpaper out from under the
    running desktop. Returns None whenever that cannot be determined — no
    gsettings, no session bus, no answer — because failing to protect a file is
    a far smaller problem than refusing to run at all.
    """
    if shutil.which("gsettings") is None:
        return None
    try:
        result = subprocess.run(
            ["gsettings", "get", BACKGROUND_SCHEMA, "picture-uri"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    raw = result.stdout.strip().strip("'\"")
    if raw.startswith("file://"):
        # url2pathname undoes the percent-encoding as_uri() applied, so a path
        # with spaces in it round-trips.
        return Path(urllib.request.url2pathname(raw[len("file://"):]))
    if raw.startswith("/"):
        # Older GNOME stored a plain path here instead of a URI.
        return Path(raw)
    return None


def set_wallpaper(path):
    """
    Point GNOME's background settings at path.

    Both picture-uri (light theme) and picture-uri-dark (dark theme) are set
    to the same file. GNOME keeps them as separate settings, so setting only
    the first one means the background silently reverts to the default the
    moment the user switches to a dark theme.
    """
    if shutil.which("gsettings") is None:
        raise RuntimeError(
            "gsettings not found — this script needs a GNOME desktop "
            "(or a compatible settings daemon) to change the wallpaper"
        )

    # Path.as_uri() percent-encodes the path for us, which matters as soon as
    # a folder name contains a space or a non-ASCII character.
    uri = path.resolve().as_uri()

    for key in WALLPAPER_KEYS:
        try:
            subprocess.run(
                ["gsettings", "set", BACKGROUND_SCHEMA, key, uri],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"Set {key} to {uri}")
        except subprocess.CalledProcessError as error:
            # picture-uri-dark does not exist before GNOME 42. Not being able
            # to set it is not a reason to fail the whole run, but not being
            # able to set picture-uri is.
            detail = (error.stderr or "").strip() or "gsettings failed"
            if key == "picture-uri":
                raise RuntimeError(detail) from error
            print(f"Warning: could not set {key} ({detail})", file=sys.stderr)


def notify(summary, body, icon=None):
    """
    Show a desktop notification. Returns True if one was sent.

    A nicety, never a requirement: by the time this runs the wallpaper has
    already been changed, so a missing notify-send or a notification daemon
    that is not listening must not turn into a failed run. Both cases are
    reported on stderr and swallowed.
    """
    if shutil.which("notify-send") is None:
        print("Warning: notify-send not found — no notification shown",
              file=sys.stderr)
        return False

    command = ["notify-send", "--app-name=Bing wallpaper"]
    if icon:
        # Pointing the icon at the downloaded file makes the notification show
        # a thumbnail of the wallpaper itself, which is more use than a
        # generic icon in a notification you are trying to identify at a glance.
        command += ["--icon", str(icon)]
    command += [summary, body]

    try:
        # A timeout, because a wedged notification daemon should not hold up a
        # timer run.
        subprocess.run(command, check=True, capture_output=True, text=True,
                       timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        detail = getattr(error, "stderr", None) or error
        print(f"Warning: could not show a notification: {detail}",
              file=sys.stderr)
        return False
    # Logged so that a silent run in the journal can be told apart from one
    # that never got as far as notifying.
    print(f"Notified: {summary}")
    return True


def apply_and_prune(args, chosen, summary=None, body=None):
    """Set the wallpaper, drop the oldest images, and report what happened."""
    # Whether this run changes anything has to be asked *before* the setting is
    # updated. Re-applying the picture that is already up — which is what the
    # login run does most days — is not a change, and should not notify.
    showing = current_wallpaper(args.dir)
    changed = showing is None or showing.resolve() != chosen.resolve()

    if not args.no_set:
        try:
            set_wallpaper(chosen)
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr)
            print(
                "Hint: gsettings needs a running desktop session — run this "
                "from a terminal in your GNOME session, not from a root shell "
                "or cron.",
                file=sys.stderr,
            )
            return 1

        if args.notify and changed:
            notify(summary or "Wallpaper changed", body or chosen.name, icon=chosen)

    # Protect the file that is on screen *after* the run: the one just applied,
    # plus whatever gsettings is pointing at, which still matters under
    # --no-set. Age alone is not a good enough reason to delete the wallpaper
    # the desktop is currently rendering.
    protect = [chosen, current_wallpaper(args.dir)]
    deleted = prune(args.dir, args.keep, protect=protect)
    for path in deleted:
        print(f"Deleted {path.name}")
    if deleted:
        print(f"Kept the newest {args.keep} image(s)")

    return 0


def run_random(args):
    """Set a random wallpaper from the ones already saved. Needs no network."""
    saved = list_wallpapers(args.dir)
    if not saved:
        print(
            f"Error: no wallpapers saved in {args.dir} yet — run this script "
            "without --random first to download one.",
            file=sys.stderr,
        )
        return 1

    # Avoid re-picking the wallpaper that is already up, when there is a
    # choice: asking for a random wallpaper means asking for a different one.
    showing = current_wallpaper(args.dir)
    choices = [p for p in saved if showing is None or p.resolve() != showing]
    chosen = random.choice(choices or saved)

    print(f"Chose {chosen.name} out of {len(saved)} saved image(s)")
    # Pruning still runs here, and is still bounded by --keep: the pick is
    # protected by apply_and_prune, so even when it is the oldest file in the
    # folder it survives, at the cost of keeping one more image than --keep
    # until the next normal run.
    #
    # There is no title to show here — that only comes from the API, and this
    # path never touches the network — so the notification names the file.
    when = _date_from_name(chosen)
    summary = f"Bing wallpaper {when.isoformat()}" if when else "Wallpaper changed"
    return apply_and_prune(args, chosen, summary=summary, body=chosen.name)


def run_today(args):
    """Download today's Bing photo and apply it."""
    try:
        image = parse_image(fetch_json(args.timeout, args.market))
    except FetchError as error:
        print(f"Error: {error}", file=sys.stderr)
        # Nothing on disk was touched, so say what that means for the desktop
        # and point at the one mode that works without a network.
        saved = list_wallpapers(args.dir)
        if saved:
            print(
                f"The desktop background is unchanged. {len(saved)} image(s) "
                f"are saved in {args.dir}; --random can pick one of those "
                "offline.",
                file=sys.stderr,
            )
        else:
            print(
                "The desktop background is unchanged, and no images have been "
                "saved yet. Try again once you are online.",
                file=sys.stderr,
            )
        return 1

    if image["copyright"]:
        print(f"Today's photo: {image['copyright']}")

    dest = args.dir / f"{NAME_PREFIX}{image['date'].isoformat()}.jpg"

    if dest.exists() and not args.force:
        # Same photo as last run: re-applying it is cheap, and doing so repairs
        # a desktop whose background was changed by something else in between.
        print(f"Already downloaded: {dest}")
    else:
        try:
            size = download_image(image, dest, args.timeout)
        except FetchError as error:
            print(f"Error: could not download the wallpaper: {error}",
                  file=sys.stderr)
            return 1
        print(f"Saved {size / 1024 / 1024:.1f} MB to {dest}")

    # Bing's headline for the photo ("The Alpine sound of Oktoberfest") makes a
    # much better notification title than the filename; the copyright line
    # carries the credit.
    return apply_and_prune(
        args,
        dest,
        summary=image["title"] or f"Bing wallpaper {image['date'].isoformat()}",
        body=image["copyright"] or dest.name,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Set today's Bing photo as the GNOME desktop wallpaper.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path.home() / "Pictures" / "Wallpapers",
        help="where to save the images (default: %(default)s)",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="apply a random image from the saved ones instead of downloading "
        "today's (works offline)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help="how many images to keep before deleting the oldest "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--market",
        default="en-US",
        help="Bing market code, which selects the edition of the photo "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds to wait for each request (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if this date's file already exists",
    )
    parser.add_argument(
        "--no-set",
        action="store_true",
        help="download only; do not change the current wallpaper",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="show a desktop notification (needs notify-send) when the "
        "wallpaper actually changes",
    )
    args = parser.parse_args(argv)

    # Zero would mean deleting every wallpaper including the one just applied,
    # which is never what someone typing --keep 0 has in mind.
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    try:
        args.dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"Error: cannot use {args.dir}: {error}", file=sys.stderr)
        return 1

    if args.random:
        return run_random(args)
    return run_today(args)


if __name__ == "__main__":
    sys.exit(main())
