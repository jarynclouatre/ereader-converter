"""File watching, conversion dispatch, and output handling for books and comics."""

import os
import re
import sys
import time
import uuid
import json
import shutil
import threading
import subprocess
from collections import deque
from datetime import datetime, timezone

from config import DEFAULT_CONFIG, load_config, profile_overrides, ConfigDict

COMICS_IN      = '/Comics_in'
COMICS_OUT     = '/Comics_out'
COMICS_ARCHIVE = os.path.join(COMICS_IN, '.archive')
BOOKS_IN       = '/Books_in'
BOOKS_OUT      = '/Books_out'

BOOK_EXTS  = {'.epub'}
COMIC_EXTS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
BOOK_OUTPUT_EXTS = frozenset({'kepub', 'kepub.epub', 'epub'})
BOOK_RESCANNED_OUTPUT_EXTS = frozenset({'kepub.epub', 'epub'})

# Archive types 7z can extract for chapter bundling. PDFs are deliberately
# absent: they can't join an image-directory job and always convert alone.
BUNDLE_EXTS = {'.cbz', '.cbr', '.zip', '.rar'}
IMAGE_EXTS  = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

# Minimum seconds since the last file modification inside a folder before
# treating it as ready for KCC. Prevents processing mid-upload.
FOLDER_STABILITY_SECS = 30

# In inotify mode, a full scan still runs at this interval. Events alone can't
# finish the job: a folder dropped into Comics_in fires its events while still
# unstable, and network mounts fire no events at all.
BACKSTOP_SCAN_SECS = 60

PROCESSING_LOCKS = set()
lock_mutex        = threading.Lock()
LOG_BUFFER        = deque(maxlen=300)
log_lock          = threading.Lock()

# KCC cannot safely run multiple instances concurrently.
# This semaphore ensures only one comic conversion runs at a time.
# Books (kepubify) are unaffected and run in parallel.
kcc_semaphore = threading.Semaphore(1)

LOG_FILE  = '/app/config/bindery.log'
JOBS_FILE = '/app/config/jobs.json'

JOB_REGISTRY: dict[str, dict] = {}
job_registry_lock = threading.Lock()
MAX_JOBS = 500

# Sources converted in Keep-in-place mode stay in the watch folder; their
# path+signature is remembered here so the scanner does not re-convert them.
CONVERTED_FILE = '/app/config/converted.json'
CONVERTED_LEDGER: dict[str, str] = {}
converted_lock = threading.Lock()

# Lifetime counters for the /api/stats dashboard endpoint.
STATS_FILE = '/app/config/stats.json'
STATS: dict[str, int] = {'converted': 0, 'bytes_saved': 0}
stats_lock = threading.Lock()


def log(msg: str) -> None:
    line = msg.rstrip()
    with log_lock:
        LOG_BUFFER.append(line)
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line + '\n')
        except OSError:
            pass
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def _load_log_history() -> None:
    """Pre-populate LOG_BUFFER from the persistent log file on startup.
    Trims the file to the last 5000 lines to prevent unbounded growth."""
    try:
        with open(LOG_FILE) as f:
            lines = f.read().splitlines()
        if len(lines) > 5000:
            lines = lines[-5000:]
            try:
                with open(LOG_FILE, 'w') as f:
                    f.write('\n'.join(lines) + '\n')
            except OSError:
                pass
        with log_lock:
            for line in lines[-300:]:
                LOG_BUFFER.append(line)
    except OSError:
        pass


def _load_job_registry() -> None:
    """Load persisted job registry from disk on startup.

    Uses .update() on the existing dict so that references imported by other
    modules (e.g. app.py) continue to point at the same object.

    Jobs persisted as queued/processing belonged to threads that died with the
    previous process — their sources are still in the watch folders and get
    picked up as fresh jobs, so the stale entries are dropped rather than left
    as permanent "processing" rows in the UI.
    """
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with job_registry_lock:
                JOB_REGISTRY.update(
                    (k, v) for k, v in data.items()
                    if isinstance(v, dict) and v.get('state') in ('success', 'failed')
                )
                _save_job_registry()
    except (OSError, json.JSONDecodeError):
        pass


def _save_job_registry() -> None:
    """Atomically write job registry to disk. Caller must hold job_registry_lock."""
    try:
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        tmp = JOBS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(JOB_REGISTRY, f)
        os.replace(tmp, JOBS_FILE)
    except OSError:
        pass


def _ledger_signature(path: str) -> str:
    """A cheap fingerprint used to tell whether a source changed since it was
    last converted. Files use size and mtime; folders aggregate the count, total
    size, and newest mtime beneath them, skipping dot-dirs so Syncthing and
    .uploading scratch never shift the result. Empty string if it can't be read."""
    try:
        if os.path.isdir(path):
            count = total = latest = 0
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    try:
                        st = os.stat(os.path.join(root, f))
                    except OSError:
                        continue
                    count  += 1
                    total  += st.st_size
                    latest  = max(latest, int(st.st_mtime))
            return f'{count}:{total}:{latest}'
        st = os.stat(path)
        return f'{st.st_size}:{int(st.st_mtime)}'
    except OSError:
        return ''


def _load_converted_ledger() -> None:
    """Load the keep-in-place ledger from disk on startup (see _load_job_registry
    for the .update() rationale). Entries are {'sig': ..., 'outputs': [...]};
    plain-string entries from 4.1.x are accepted and upgraded on the next mark."""
    try:
        with open(CONVERTED_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with converted_lock:
                CONVERTED_LEDGER.update(
                    (k, v) for k, v in data.items()
                    if isinstance(v, str)
                    or (isinstance(v, dict) and isinstance(v.get('sig'), str)))
    except (OSError, json.JSONDecodeError):
        pass


def _save_converted_ledger() -> None:
    """Atomically write the ledger to disk. Caller must hold converted_lock."""
    try:
        os.makedirs(os.path.dirname(CONVERTED_FILE), exist_ok=True)
        tmp = CONVERTED_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(CONVERTED_LEDGER, f)
        os.replace(tmp, CONVERTED_FILE)
    except OSError:
        pass


def _entry_sig(entry) -> str:
    """Signature stored in a ledger entry, whatever its format."""
    return entry['sig'] if isinstance(entry, dict) else entry


def _already_converted(path: str) -> bool:
    """True if path was converted before and has not changed since. Entries are
    never dropped for a missing path: a NAS mount blip must not wipe the ledger
    and re-convert a whole library.

    A changed timestamp with identical size does not count as changed — library
    managers (metadata refreshes, cover passes) touch files without altering
    them, and re-converting on every touch loops forever when Comics_out is the
    same folder. The stored signature is refreshed so the entry stays current."""
    sig = _ledger_signature(path)
    if not sig:
        return False
    with converted_lock:
        entry = CONVERTED_LEDGER.get(path)
        if entry is None:
            return False
        known = _entry_sig(entry)
        if known == sig:
            return True
        if known.rsplit(':', 1)[0] == sig.rsplit(':', 1)[0]:
            if isinstance(entry, dict):
                entry['sig'] = sig
            else:
                CONVERTED_LEDGER[path] = sig
            _save_converted_ledger()
            return True
        return False


def _mark_converted(path: str, outputs: list[str] | None = None) -> None:
    """Record path+signature so the scanner skips it while it sits in place.
    The output paths are kept so a genuine re-convert can replace its own
    previous outputs instead of stacking _2/_3 copies beside them."""
    sig = _ledger_signature(path)
    if not sig:
        return
    with converted_lock:
        CONVERTED_LEDGER[path] = {'sig': sig, 'outputs': sorted(set(outputs or []))}
        _save_converted_ledger()


def _discard_previous_outputs(path: str) -> None:
    """Delete the outputs an earlier keep-mode conversion of path produced.
    Only files recorded in the ledger are touched — anything the user placed
    beside them is left alone."""
    with converted_lock:
        entry = CONVERTED_LEDGER.get(path)
        outputs = list(entry.get('outputs') or []) if isinstance(entry, dict) else []
    for out in outputs:
        try:
            os.remove(out)
        except OSError:
            pass


def _load_stats() -> None:
    """Load the lifetime conversion counters from disk on startup."""
    try:
        with open(STATS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with stats_lock:
                for k in STATS:
                    if isinstance(data.get(k), int):
                        STATS[k] = data[k]
    except (OSError, json.JSONDecodeError):
        pass


def _save_stats() -> None:
    """Atomically write the counters. Caller must hold stats_lock."""
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        tmp = STATS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(STATS, f)
        os.replace(tmp, STATS_FILE)
    except OSError:
        pass


def _bump_stats(converted: int = 0, saved: int = 0) -> None:
    """Add to the lifetime counters after a successful conversion."""
    with stats_lock:
        STATS['converted']   += converted
        STATS['bytes_saved'] += max(0, saved)
        _save_stats()


def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _register_job(filepath: str, c_type: str) -> str:
    """Create a new QUEUED job entry and return its ID."""
    job_id = uuid.uuid4().hex
    entry: dict = {
        'id':       job_id,
        'filename': os.path.basename(filepath),
        'filepath': filepath,
        'type':     c_type,
        'state':    'queued',
        'created':  _now(),
        'started':  None,
        'finished': None,
        'error':    None,
    }
    with job_registry_lock:
        JOB_REGISTRY[job_id] = entry
        if len(JOB_REGISTRY) > MAX_JOBS:
            # Prune oldest completed jobs first, then by created time
            candidates = sorted(
                JOB_REGISTRY,
                key=lambda k: (
                    0 if JOB_REGISTRY[k]['state'] in ('success', 'failed') else 1,
                    JOB_REGISTRY[k].get('created') or '',
                )
            )
            for k in candidates[:len(JOB_REGISTRY) - MAX_JOBS]:
                del JOB_REGISTRY[k]
        _save_job_registry()
    return job_id


def _tree_bytes(path: str) -> int:
    """Total size of a file, or of every file under a directory."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _update_job(job_id: str | None, **kwargs: object) -> None:
    """Update fields on a job entry and persist. No-op if job_id is None or unknown."""
    if job_id is None:
        return
    with job_registry_lock:
        if job_id in JOB_REGISTRY:
            JOB_REGISTRY[job_id].update(kwargs)
            _save_job_registry()


def _notify(event: str, filename: str, error: str | None = None) -> None:
    """Send an Apprise notification if configured for this event type."""
    try:
        config = load_config()
        urls   = config.get('apprise_urls', '').strip()
        if not urls:
            return
        if event == 'success' and not config.get('notify_on_success', DEFAULT_CONFIG['notify_on_success']):
            return
        if event == 'failure' and not config.get('notify_on_failure', DEFAULT_CONFIG['notify_on_failure']):
            return
        import apprise
        ap = apprise.Apprise()
        for url in urls.splitlines():
            url = url.strip()
            if url:
                ap.add(url)
        if event == 'success':
            title = 'Bindery: Conversion complete'
            body  = f'\u2713 {filename}'
        else:
            title = 'Bindery: Conversion failed'
            body  = f'\u2717 {filename}' + (f'\n{error}' if error else '')
        ap.notify(title=title, body=body)
    except Exception as e:
        log(f'>>> NOTIFY ERROR: {e}')


def _book_extension(config: ConfigDict) -> str:
    """Return a supported book extension, including for hand-edited config."""
    extension = config.get('book_extension', DEFAULT_CONFIG['book_extension'])
    if isinstance(extension, str) and extension in BOOK_OUTPUT_EXTS:
        return extension
    return DEFAULT_CONFIG['book_extension']


def _same_directory(first: str, second: str) -> bool:
    """Compare directories by identity, with a path fallback before mounts exist.

    samefile is needed for Docker bind mounts: two different container paths can
    point at the same host directory even though their realpath strings differ.
    """
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return os.path.realpath(first) == os.path.realpath(second)


def _path_is_within(path: str, parent: str) -> bool:
    """Return whether path is parent itself or one of its descendants."""
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _decode_mount_field(value: str) -> str:
    """Decode the octal escapes Linux uses in /proc/self/mountinfo paths."""
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def _mount_location(path: str) -> tuple[str, str] | None:
    """Map a container path to its filesystem device and mount-root location.

    Docker can mount a host folder and one of its children at unrelated paths
    such as /Books_in and /Books_out. Their container paths do not overlap, but
    mountinfo retains the underlying relationship needed to detect the loop.
    """
    resolved = os.path.realpath(path)
    best: tuple[str, str, str] | None = None
    try:
        with open('/proc/self/mountinfo') as mountinfo:
            for line in mountinfo:
                fields = line.partition(' - ')[0].split()
                if len(fields) < 5:
                    continue
                device = fields[2]
                root = _decode_mount_field(fields[3])
                mount_point = _decode_mount_field(fields[4])
                if not _path_is_within(resolved, mount_point):
                    continue
                if best is None or len(mount_point) > len(best[2]):
                    best = (device, root, mount_point)
    except OSError:
        return None

    if best is None:
        return None
    device, root, mount_point = best
    relative = os.path.relpath(resolved, mount_point)
    location = root if relative == '.' else os.path.normpath(os.path.join(root, relative))
    return device, location


def _output_reenters_input(books_in: str, books_out: str) -> bool:
    """Return whether either book folder contains the other."""
    if _same_directory(books_in, books_out):
        return True

    input_path = os.path.realpath(books_in)
    output_path = os.path.realpath(books_out)
    if (_path_is_within(output_path, input_path)
            or _path_is_within(input_path, output_path)):
        return True

    input_mount = _mount_location(input_path)
    output_mount = _mount_location(output_path)
    return bool(
        input_mount and output_mount
        and input_mount[0] == output_mount[0]
        and (_path_is_within(output_mount[1], input_mount[1])
             or _path_is_within(input_mount[1], output_mount[1]))
    )


def book_output_error(config: ConfigDict, books_in: str | None = None,
                      books_out: str | None = None) -> str | None:
    """Explain an unsafe book output loop, or return None when conversion is safe."""
    extension = _book_extension(config)
    if extension not in BOOK_RESCANNED_OUTPUT_EXTS:
        return None
    if not _output_reenters_input(books_in or BOOKS_IN, books_out or BOOKS_OUT):
        return None
    return (f"Book conversion is paused because .{extension} output could be picked up "
            "again: Books_in and Books_out overlap. Choose .kepub or use two separate "
            "folders where neither one contains the other.")


def retry_file(job_id: str) -> bool:
    """Rename the .failed file back to its original name and re-dispatch it.

    Returns True if the retry was successfully queued.
    """
    with job_registry_lock:
        job = JOB_REGISTRY.get(job_id)
    if not job or job['state'] != 'failed':
        return False
    if job['type'] == 'book' and book_output_error(load_config()):
        return False
    original    = job['filepath']
    failed_path = job.get('failed_path') or (original + '.failed')
    if not os.path.exists(failed_path):
        return False
    if os.path.exists(original):
        # Something new was dropped under the original name — don't clobber it.
        return False
    try:
        os.rename(failed_path, original)
    except OSError:
        return False
    _update_job(job_id, state='queued', error=None, started=None, finished=None, failed_path=None)
    c_type = job['type']
    with lock_mutex:
        if original not in PROCESSING_LOCKS:
            PROCESSING_LOCKS.add(original)
            if os.path.isdir(original):
                threading.Thread(target=process_folder, args=(original, job_id), daemon=True).start()
            else:
                threading.Thread(target=process_file, args=(original, c_type, job_id), daemon=True).start()
    return True


def wait_for_file_ready(filepath: str, timeout: int = 60) -> bool:
    """Poll until the file size stabilises, indicating the transfer is complete.

    Polls every 2s for up to `timeout` seconds. Requires STABLE_NEEDED
    consecutive identical non-zero size readings before declaring the file
    ready. A single 2-second stable window is not enough — copy tools like
    FileBrowser pause briefly between write chunks, which fools a one-shot
    stability check into processing a still-incomplete file.

    Returns False on timeout; the caller logs SKIP and leaves the source
    untouched so it retries next scan. Only definitive failures rename to
    .failed.
    """
    STABLE_NEEDED = 3  # require ~6 s of stable size before processing
    last_size    = -1
    stable_count =  0
    for _ in range(max(1, (timeout + 1) // 2)):
        try:
            if not os.path.exists(filepath):
                return False
            size = os.path.getsize(filepath)
            if size > 0 and size == last_size:
                stable_count += 1
                if stable_count >= STABLE_NEEDED:
                    return True
            else:
                stable_count = 0
                last_size = size
        except OSError:
            stable_count = 0
        time.sleep(2)
    return False


def _profile_for_path(path: str, config: ConfigDict) -> str | None:
    """Name of the device profile a Comics_in path falls under, or None.

    A path is inside a profile when its first component below Comics_in
    exactly matches a profile the user created. Everything else — including
    all of Books_in — uses the main settings.
    """
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(COMICS_IN))
    if rel == '.' or rel.startswith('..'):
        return None
    top = rel.split(os.sep)[0]
    profiles = config.get('profiles') or {}
    return top if top in profiles else None


def _config_for_path(path: str) -> ConfigDict:
    """Load settings with the right device profile applied for this path."""
    config = load_config()
    name = _profile_for_path(path, config)
    return profile_overrides(config, name) if name else config


def _folder_quiet_secs(config: ConfigDict) -> int:
    """Quiet window a folder must sit unmodified before converting.

    Follows the user's File Stability Timeout so slow downloaders can extend
    it, but never drops below the FOLDER_STABILITY_SECS floor — a folder fills
    file-by-file, so a short single-file timeout is not a safe folder window.
    """
    try:
        timeout = int(config.get('file_wait_timeout', 60))
    except (TypeError, ValueError):
        timeout = 60
    return max(FOLDER_STABILITY_SECS, timeout)


def _is_dir_stable(dirpath: str, quiet_secs: int = FOLDER_STABILITY_SECS) -> bool:
    """Return True if dirpath is non-empty and no file inside was modified recently.

    Walks the directory recursively and checks that the newest mtime is at least
    quiet_secs seconds in the past. An empty directory returns False — it may
    still be populated.
    """
    newest    = 0.0
    found_any = False
    for root, _dirs, files in os.walk(dirpath):
        for fname in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, fname))
                found_any = True
                if mtime > newest:
                    newest = mtime
            except OSError:
                pass
    if not found_any:
        return False
    return (time.time() - newest) >= quiet_secs


def get_output_files(directory: str) -> list[str]:
    """Return all files in directory, sorted oldest to newest."""
    files = [
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f))
    ]
    return sorted(files, key=os.path.getmtime)


def prune_empty_dirs(file_path: str, stop_at: str) -> None:
    """Walk upward from file_path's directory, removing empty dirs until stop_at."""
    d = os.path.dirname(os.path.abspath(file_path))
    stop_at = os.path.abspath(stop_at)
    while d != stop_at and d.startswith(stop_at + os.sep):
        try:
            os.rmdir(d)
            d = os.path.dirname(d)
        except OSError:
            break


def _collision_free(dest: str) -> str:
    """Return dest, or dest with a _2/_3/... suffix if something already lives there."""
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    counter = 2
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _rename_failed(path: str) -> str | None:
    """Rename a failed source to <path>.failed without clobbering earlier failures.

    Collisions become <path>_2.failed etc. so the name always ends in .failed
    and stays invisible to the scanners. Returns the new path, or None if the
    rename itself failed (source vanished, permissions).
    """
    candidate = path + '.failed'
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{path}_{counter}.failed"
        counter += 1
    try:
        os.rename(path, candidate)
        return candidate
    except OSError:
        return None


_output_move_lock = threading.Lock()


def move_output_file(produced_file: str, target_dir: str,
                     book_ext: str | None = None) -> str:
    """Move a single conversion output to target_dir, applying any needed
    renaming. Returns the final destination path.

    book_ext is the extension the Books settings asked for ('kepub',
    'kepub.epub' or 'epub'). None means comic output, which keeps KCC's
    .kepub.epub normalised down to .kepub.
    """
    filename = os.path.basename(produced_file)
    if book_ext:
        # Longest suffix first: .kepub.epub also ends with .epub.
        for suffix in ('.kepub.epub', '.kepub', '.epub'):
            if filename.endswith(suffix):
                filename = filename[:-len(suffix)]
                break
        filename += '.' + book_ext
    elif filename.endswith('.kepub.epub'):
        filename = filename[:-len('.kepub.epub')] + '.kepub'
    os.makedirs(target_dir, exist_ok=True)
    # Books convert in parallel; the lock keeps two same-named outputs from
    # both picking the same collision-free name and overwriting each other.
    with _output_move_lock:
        dest = _collision_free(os.path.join(target_dir, filename))
        shutil.move(produced_file, dest)
    return dest


class ConversionError(Exception):
    """Raised when a converter process exits with a non-zero return code."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _run_conversion(cmd: list[str], short: str) -> None:
    """Run cmd, streaming output to the log. Raises ConversionError on non-zero exit."""
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in process.stdout:
        log(f"[{short}] {line.rstrip()}")
    process.wait()
    if process.returncode != 0:
        raise ConversionError(process.returncode)


def _build_kcc_cmd(config: ConfigDict, filepath: str, temp_out: str) -> list[str]:
    """Build and return the kcc-c2e argument list from the current config."""
    # MOBI needs kindlegen and KFX needs a Calibre plugin — neither exists in
    # this container, so configs that predate their removal fall back to EPUB.
    fmt = config['kcc_format'] if config['kcc_format'] in ('EPUB', 'CBZ') else 'EPUB'

    # The UI takes cropping minimum as a percentage; kcc-c2e wants a 0-1 ratio.
    try:
        crop_min = float(config['kcc_croppingminimum']) / 100
    except (TypeError, ValueError):
        crop_min = 0.0

    splitter = config.get('kcc_splitter', DEFAULT_CONFIG['kcc_splitter'])
    if splitter in ('3', '4'):
        splitter = '2'
    elif splitter not in ('0', '1', '2'):
        splitter = DEFAULT_CONFIG['kcc_splitter']

    cmd = [
        'kcc-c2e',
        '--profile',         config['kcc_profile'],
        '--format',          fmt,
        '--splitter',        splitter,
        '--cropping',        config['kcc_cropping'],
        '--croppingpower',   config['kcc_croppingpower'],
        '--croppingminimum', str(crop_min),
        '--batchsplit',      config['kcc_batchsplit'],
        '--output',          temp_out,
    ]

    gamma = config.get('kcc_gamma', '0')
    if gamma and gamma != '0':
        cmd.extend(['--gamma', gamma])

    if config['kcc_manga_style']:       cmd.append('--manga-style')
    if config['kcc_hq']:                cmd.append('--hq')
    if config['kcc_two_panel']:         cmd.append('--two-panel')
    if config['kcc_webtoon']:           cmd.append('--webtoon')
    if config.get('kcc_borders') == 'black': cmd.append('--blackborders')
    if config.get('kcc_borders') == 'white': cmd.append('--whiteborders')
    if config['kcc_forcecolor']:        cmd.append('--forcecolor')
    if config['kcc_colorautocontrast']: cmd.append('--colorautocontrast')
    if config.get('kcc_eraserainbow'): cmd.append('--eraserainbow')
    if config.get('kcc_mozjpeg'):       cmd.append('--mozjpeg')
    if config['kcc_stretch']:           cmd.append('--stretch')
    if config['kcc_upscale']:           cmd.append('--upscale')
    if config.get('kcc_norotate'):      cmd.append('--norotate')
    if config.get('kcc_rotateright'):   cmd.append('--rotateright')
    if config.get('kcc_rotatefirst'):   cmd.append('--rotatefirst')
    if config['kcc_nokepub']:           cmd.append('--nokepub')

    # = form, so filenames/authors starting with a dash don't read as options
    if config.get('kcc_comicinfo'):
        # Pull the title and series from an embedded ComicInfo.xml, falling back
        # to the filename when the archive has none.
        cmd += ['--metadatatitle', '1']
    elif config['kcc_metadatatitle']:
        title = os.path.splitext(os.path.basename(filepath))[0]
        cmd.append('--title=' + title)

    if config.get('kcc_author', '').strip():
        cmd.append('--author=' + config['kcc_author'].strip())

    if config['kcc_profile'] == 'OTHER':
        if config.get('kcc_customwidth', '').strip():
            cmd.extend(['--customwidth', config['kcc_customwidth'].strip()])
        if config.get('kcc_customheight', '').strip():
            cmd.extend(['--customheight', config['kcc_customheight'].strip()])

    cmd.append(filepath)
    return cmd


def _strip_leading_dash(filepath: str, job_id: str) -> str:
    """Rename a dash-leading source file so KCC's 7z call doesn't eat it.

    KCC extracts archives by running 7z with the bare basename (cwd-relative),
    and 7z parses a leading dash as a switch — every such file would fail. The
    rename is logged and the job's filepath updated so Retry follows it.
    """
    base = os.path.basename(filepath)
    if not base.startswith('-'):
        return filepath
    stripped = base.lstrip('- ')
    if not stripped or stripped.startswith('.'):
        stripped = 'file' + os.path.splitext(base)[1]
    safe = _collision_free(os.path.join(os.path.dirname(filepath), stripped))
    try:
        os.rename(filepath, safe)
    except OSError:
        return filepath
    log(f">>> RENAMED (leading dash breaks extraction): {base} -> {os.path.basename(safe)}")
    _update_job(job_id, filepath=safe, filename=os.path.basename(safe))
    return safe


def _build_kepubify_cmd(config: ConfigDict, filepath: str, temp_out: str) -> list[str]:
    """Build the kepubify argument list from the Books settings.

    kepubify's own output extension is .kepub.epub, and --calibre switches it
    to .kepub. A plain .epub is not something kepubify can emit, so it is asked
    for .kepub here and renamed on the way out by move_output_file.
    """
    cmd = ['kepubify', '--inplace', '--output', temp_out]

    # settings.json bypasses _validate_post, so re-clamp here too (mirrors
    # kcc_format's fallback in _build_kcc_cmd).
    book_ext = _book_extension(config)
    if book_ext in ('kepub', 'epub'):
        cmd.append('--calibre')

    if config.get('book_smarten_punctuation'): cmd.append('--smarten-punctuation')
    if config.get('book_fullscreen_fixes'):    cmd.append('--fullscreen-reading-fixes')

    hyphenate = config.get('book_hyphenate', 'auto')
    if hyphenate == 'on':  cmd.append('--hyphenate')
    if hyphenate == 'off': cmd.append('--no-hyphenate')

    titlepage = config.get('book_dummy_titlepage', 'auto')
    if titlepage == 'on':  cmd.append('--add-dummy-titlepage')
    if titlepage == 'off': cmd.append('--no-add-dummy-titlepage')

    # = form, so values starting with a dash don't read as options. `or ''`
    # instead of a get(..., '') default: a hand-edited settings.json can carry
    # an explicit JSON null, and str(None) is the literal string 'None'.
    css = (config.get('book_css') or '').strip()
    if css:
        cmd.append('--css=' + css)
    for line in (config.get('book_replace') or '').splitlines():
        line = line.strip()
        if '|' in line:
            cmd.append('--replace=' + line)
    charset = (config.get('book_charset') or '').strip()
    if charset:
        cmd.append('--charset=' + charset)

    cmd.append(filepath)
    return cmd


def _format_cmd_for_log(cmd: list[str]) -> str:
    """Keep converter logs useful without dumping multiline book customisations."""
    shown = []
    for arg in cmd:
        if arg.startswith('--css='):
            arg = '--css=<set>'
        elif arg.startswith('--replace='):
            arg = '--replace=<set>'
        shown.append(arg.replace('\r', r'\r').replace('\n', r'\n'))
    return ' '.join(shown)


def process_file(filepath: str, c_type: str, job_id: str | None = None) -> None:
    """Convert a single file, tracking state in the job registry."""
    short    = os.path.basename(filepath)[:40]
    in_base  = BOOKS_IN if c_type == 'book' else COMICS_IN
    temp_out = os.path.join('/tmp', uuid.uuid4().hex + '_out')
    lock_key = filepath

    try:
        # Register inside try so PROCESSING_LOCKS.discard always runs in finally.
        if job_id is None:
            job_id = _register_job(filepath, c_type)

        config = _config_for_path(filepath)
        if c_type == 'book':
            error = book_output_error(config)
            if error:
                log(f">>> SKIP: {short} — {error}")
                with job_registry_lock:
                    JOB_REGISTRY.pop(job_id, None)
                    _save_job_registry()
                return
        if not wait_for_file_ready(filepath, int(config.get('file_wait_timeout', 60))):
            log(f">>> SKIP (not ready): {short}")
            # Remove job so the next scan creates a fresh one
            with job_registry_lock:
                JOB_REGISTRY.pop(job_id, None)
                _save_job_registry()
            return

        if c_type == 'comic':
            filepath = _strip_leading_dash(filepath, job_id)
            short    = os.path.basename(filepath)[:40]

        src_bytes = _tree_bytes(filepath)
        _update_job(job_id, state='processing', started=_now(),
                    src_bytes=src_bytes,
                    profile=_profile_for_path(filepath, config))

        rel_dir = os.path.relpath(os.path.dirname(filepath), in_base)
        if rel_dir == '.':
            rel_dir = ''
        out_base   = BOOKS_OUT if c_type == 'book' else COMICS_OUT
        target_dir = os.path.join(out_base, rel_dir) if rel_dir else out_base
        os.makedirs(temp_out, exist_ok=True)

        if c_type == 'book':
            log(f">>> STARTING: kepubify on {short}")
            cmd = _build_kepubify_cmd(config, filepath, temp_out)
            log(f">>> CMD: {_format_cmd_for_log(cmd)}")
            _run_conversion(cmd, short)

        else:
            cmd = _build_kcc_cmd(config, filepath, temp_out)
            log(f">>> QUEUED: {short}")
            with kcc_semaphore:
                log(f">>> STARTING: kcc-c2e on {short}")
                log(f">>> CMD: {_format_cmd_for_log(cmd)}")
                _run_conversion(cmd, short)

        produced = get_output_files(temp_out)
        if produced:
            out_bytes = sum(_tree_bytes(f) for f in produced)
            # Books never keep or archive their source; only comics do.
            mode = config.get('originals', 'delete') if c_type == 'comic' else 'delete'
            if mode == 'keep':
                _discard_previous_outputs(filepath)
            book_ext = None
            if c_type == 'book':
                book_ext = _book_extension(config)
            dests = [move_output_file(f, target_dir, book_ext) for f in produced]
            if os.path.exists(filepath):
                if mode == 'keep':
                    _mark_converted(filepath, dests)
                elif mode == 'archive':
                    _dest = os.path.join(COMICS_ARCHIVE, os.path.relpath(filepath, COMICS_IN))
                    os.makedirs(os.path.dirname(_dest), exist_ok=True)
                    shutil.move(filepath, _collision_free(_dest))
                    prune_empty_dirs(filepath, in_base)
                else:
                    os.remove(filepath)
                    prune_empty_dirs(filepath, in_base)
            count  = len(produced)
            suffix = 's' if count > 1 else ''
            log(f">>> SUCCESS ({count} file{suffix}): {short}")
            _update_job(job_id, state='success', finished=_now(), out_bytes=out_bytes)
            _bump_stats(converted=1, saved=src_bytes - out_bytes)
            _notify('success', os.path.basename(filepath))
        else:
            log(f">>> FAILED (no output file found): {short}")
            failed_path = _rename_failed(filepath) if os.path.exists(filepath) else None
            _update_job(job_id, state='failed', finished=_now(),
                        error='no output produced', failed_path=failed_path)
            _notify('failure', os.path.basename(filepath), 'no output produced')

    except ConversionError as e:
        msg = f'exit {e.returncode}'
        log(f">>> FAILED ({msg}): {short}")
        failed_path = _rename_failed(filepath) if os.path.exists(filepath) else None
        _update_job(job_id, state='failed', finished=_now(), error=msg, failed_path=failed_path)
        _notify('failure', os.path.basename(filepath), msg)
    except Exception as e:
        msg = str(e)
        log(f">>> ERROR: {short} — {msg}")
        failed_path = _rename_failed(filepath) if os.path.exists(filepath) else None
        _update_job(job_id, state='failed', finished=_now(), error=msg, failed_path=failed_path)
        _notify('failure', os.path.basename(filepath), msg)
    finally:
        shutil.rmtree(temp_out, ignore_errors=True)
        with lock_mutex:
            PROCESSING_LOCKS.discard(lock_key)


def process_folder(folderpath: str, job_id: str | None = None) -> None:
    """Convert a folder of comic files as a single bundled KCC volume.

    KCC accepts a directory as its input argument and treats the contents as
    chapters of one volume. The folder is removed (or archived) on success,
    or renamed to <name>.failed on error.
    """
    short      = os.path.basename(folderpath)[:40]
    temp_out   = os.path.join('/tmp', uuid.uuid4().hex + '_out')
    bundle_tmp = None

    try:
        if job_id is None:
            job_id = _register_job(folderpath, 'comic')

        config = _config_for_path(folderpath)
        if not _is_dir_stable(folderpath, _folder_quiet_secs(config)):
            log(f">>> SKIP (not ready): {short}/")
            with job_registry_lock:
                JOB_REGISTRY.pop(job_id, None)
                _save_job_registry()
            return

        src_bytes = _tree_bytes(folderpath)
        _update_job(job_id, state='processing', started=_now(),
                    src_bytes=src_bytes,
                    profile=_profile_for_path(folderpath, config))

        kcc_input = folderpath
        chapters  = 0
        for _r, _cdirs, fs in os.walk(folderpath):
            _cdirs[:] = [d for d in _cdirs if not d.startswith('.')]
            chapters += sum(1 for f in fs
                            if os.path.splitext(f)[1].lower() in BUNDLE_EXTS)
        if chapters:
            log(f">>> BUNDLING: extracting {chapters} chapter archives from {short}/")
            bundle_tmp, kcc_input = _extract_chapter_folder(folderpath)

        os.makedirs(temp_out, exist_ok=True)
        cmd = _build_kcc_cmd(config, kcc_input, temp_out)

        log(f">>> QUEUED (folder): {short}/")
        with kcc_semaphore:
            log(f">>> STARTING: kcc-c2e on {short}/")
            log(f">>> CMD: {' '.join(cmd)}")
            _run_conversion(cmd, short)

        rel_dir = os.path.relpath(os.path.dirname(folderpath), COMICS_IN)
        out_dir = COMICS_OUT if rel_dir in ('.', '') else os.path.join(COMICS_OUT, rel_dir)

        produced = get_output_files(temp_out)
        if produced:
            out_bytes = sum(_tree_bytes(f) for f in produced)
            mode = config.get('originals', 'delete')
            if mode == 'keep':
                _discard_previous_outputs(folderpath)
            dests = [move_output_file(f, out_dir) for f in produced]
            if os.path.exists(folderpath):
                if mode == 'keep':
                    _mark_converted(folderpath, dests)
                elif mode == 'archive':
                    _dest = os.path.join(COMICS_ARCHIVE, os.path.relpath(folderpath, COMICS_IN))
                    os.makedirs(os.path.dirname(_dest), exist_ok=True)
                    shutil.move(folderpath, _collision_free(_dest))
                else:
                    shutil.rmtree(folderpath)
            count  = len(produced)
            suffix = 's' if count > 1 else ''
            log(f">>> SUCCESS ({count} file{suffix}): {short}/")
            _update_job(job_id, state='success', finished=_now(), out_bytes=out_bytes)
            _bump_stats(converted=1, saved=src_bytes - out_bytes)
            _notify('success', short + '/')
        else:
            log(f">>> FAILED (no output file found): {short}/")
            failed_path = _rename_failed(folderpath) if os.path.exists(folderpath) else None
            _update_job(job_id, state='failed', finished=_now(),
                        error='no output produced', failed_path=failed_path)
            _notify('failure', short + '/', 'no output produced')

    except ConversionError as e:
        msg = f'exit {e.returncode}'
        log(f">>> FAILED ({msg}): {short}/")
        failed_path = _rename_failed(folderpath) if os.path.exists(folderpath) else None
        _update_job(job_id, state='failed', finished=_now(), error=msg, failed_path=failed_path)
        _notify('failure', short + '/', msg)
    except Exception as e:
        msg = str(e)
        log(f">>> ERROR: {short}/ — {msg}")
        failed_path = _rename_failed(folderpath) if os.path.exists(folderpath) else None
        _update_job(job_id, state='failed', finished=_now(), error=msg, failed_path=failed_path)
        _notify('failure', short + '/', msg)
    finally:
        shutil.rmtree(temp_out, ignore_errors=True)
        if bundle_tmp:
            shutil.rmtree(bundle_tmp, ignore_errors=True)
        with lock_mutex:
            PROCESSING_LOCKS.discard(folderpath)


def _is_bundle_folder(dirpath: str, config: ConfigDict | None = None) -> bool:
    """Decide whether a top-level Comics_in folder converts as one bundled volume.

    Folders holding images always do — KCC takes the directory as-is.
    Folders of chapter archives bundle only when Bundle Chapter Folders is
    enabled, and only if everything comic-typed inside is an extractable
    archive: PDFs and loose images alongside archives keep the per-file path.
    Folders with nothing comic-typed at all (a library's epub-only book
    folders, stray sidecar files) are never jobs — KCC would fail on them and
    the folder would get renamed .failed.
    """
    saw_archive = saw_pdf = saw_image = saw_file = False
    for _root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            saw_file = True
            ext = os.path.splitext(f)[1].lower()
            if ext in BUNDLE_EXTS:
                saw_archive = True
            elif ext == '.pdf':
                saw_pdf = True
            elif ext in IMAGE_EXTS:
                saw_image = True
    if not saw_file:
        # Nothing inside yet (a deleted profile's leftover folder, or a copy
        # that hasn't landed) — not a job; the next scan rechecks.
        return False
    if not saw_archive and not saw_pdf:
        return saw_image
    if saw_pdf or saw_image:
        return False
    if config is None:
        config = load_config()
    return bool(config.get('bundle_chapter_folders', False))


def _natural_key(s: str) -> list:
    """Sort key that orders embedded numbers numerically: ch2 before ch10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def _extract_chapter_folder(folderpath: str) -> tuple[str, str]:
    """Extract every chapter archive under folderpath into a directory KCC can
    bundle: one numbered subfolder per archive, in natural filename order (the
    prefix matters — KCC sorts chapter dirs lexicographically).

    Returns (temp_parent, kcc_input_dir); the caller removes temp_parent when
    done. Raises ValueError, with its own temp cleaned up, if extraction fails.
    """
    archives: list[str] = []
    for root, dirs, files in os.walk(folderpath):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if os.path.splitext(f)[1].lower() in BUNDLE_EXTS:
                archives.append(os.path.join(root, f))
    archives.sort(key=lambda p: _natural_key(os.path.relpath(p, folderpath)))

    temp_parent = os.path.join('/tmp', uuid.uuid4().hex + '_bundle')
    kcc_input   = os.path.join(temp_parent, os.path.basename(folderpath))
    try:
        os.makedirs(kcc_input)
        for i, arc in enumerate(archives, 1):
            stem     = os.path.splitext(os.path.basename(arc))[0]
            chap_dir = os.path.join(kcc_input, f'{i:03d} - {stem}')
            os.makedirs(chap_dir)
            r = subprocess.run(['7z', 'x', '-y', '-o' + chap_dir, arc],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise ValueError(f'could not extract {os.path.basename(arc)}')
            # Hoist single-directory wrappers (a cbz that is one folder of
            # pages) so the chapter dir itself holds the images.
            while True:
                entries = os.listdir(chap_dir)
                if len(entries) != 1:
                    break
                inner = os.path.join(chap_dir, entries[0])
                if not os.path.isdir(inner):
                    break
                for e in os.listdir(inner):
                    shutil.move(os.path.join(inner, e), os.path.join(chap_dir, '.' + e + '.hoist'))
                os.rmdir(inner)
                for e in os.listdir(chap_dir):
                    if e.startswith('.') and e.endswith('.hoist'):
                        os.rename(os.path.join(chap_dir, e), os.path.join(chap_dir, e[1:-6]))
    except Exception:
        shutil.rmtree(temp_parent, ignore_errors=True)
        raise
    return temp_parent, kcc_input


def scan_directories() -> None:
    config = load_config()
    if not book_output_error(config):
        for root, dirs, files in os.walk(BOOKS_IN):
            dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.failed')]
            for f in files:
                if os.path.splitext(f)[1].lower() in BOOK_EXTS and not f.endswith('.failed'):
                    path = os.path.join(root, f)
                    with lock_mutex:
                        if path not in PROCESSING_LOCKS:
                            PROCESSING_LOCKS.add(path)
                            threading.Thread(target=process_file,
                                             args=(path, 'book'), daemon=True).start()

    # Comics_in and each device-profile folder inside it are scanned as
    # separate roots: a profile folder is a drop target with its own settings,
    # never a conversion job itself. Within every root, top-level folders that
    # qualify as bundled KCC jobs dispatch whole; everything else is left to
    # the per-file walk. KCC rejects nested archives ("No images detected"),
    # so archive folders only bundle via the extraction pre-pass, and only
    # when the user enabled it.
    profile_names = set(config.get('profiles') or {})
    comic_roots   = [COMICS_IN] + [os.path.join(COMICS_IN, n) for n in sorted(profile_names)
                                   if os.path.isdir(os.path.join(COMICS_IN, n))]
    # In Keep-in-place mode a converted source stays put, so skip anything the
    # ledger already knows. Other modes remove the source, so no check is needed.
    keep_mode     = config.get('originals') == 'keep'

    for base in comic_roots:
        is_main = (base == COMICS_IN)
        folder_job_names: set[str] = set()
        try:
            top_entries = os.listdir(base)
        except OSError:
            top_entries = []
        for entry in top_entries:
            if entry.startswith('.') or entry.endswith('.failed'):
                continue
            if is_main and entry in profile_names:
                continue
            full = os.path.join(base, entry)
            if not os.path.isdir(full) or not _is_bundle_folder(full, config):
                continue
            folder_job_names.add(entry)
            if keep_mode and _already_converted(full):
                continue
            with lock_mutex:
                if full not in PROCESSING_LOCKS:
                    PROCESSING_LOCKS.add(full)
                    threading.Thread(target=process_folder, args=(full,), daemon=True).start()

        for root, dirs, files in os.walk(base):
            if root == base:
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.failed')
                           and d not in folder_job_names
                           and not (is_main and d in profile_names)]
            else:
                dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.failed')]
            for f in files:
                if os.path.splitext(f)[1].lower() in COMIC_EXTS and not f.endswith('.failed'):
                    path = os.path.join(root, f)
                    if keep_mode and _already_converted(path):
                        continue
                    with lock_mutex:
                        if path not in PROCESSING_LOCKS:
                            PROCESSING_LOCKS.add(path)
                            threading.Thread(target=process_file,
                                             args=(path, 'comic'), daemon=True).start()


def watch_loop() -> None:
    while True:
        try:
            scan_directories()
        except Exception as e:
            log(f">>> SCAN ERROR: {e}")
        time.sleep(10)


def inotify_watch_loop() -> None:
    """Inotify-based watcher for Books_in and Comics_in.

    Uses watchdog's Observer (inotify on Linux). Dispatches process_file on
    FileCreatedEvent and FileMovedEvent, so both direct writes and
    temp-file-then-rename patterns (e.g. WinSCP) are handled correctly.
    wait_for_file_ready still runs inside process_file, so partial writes
    from direct-write clients are tolerated.

    inotify does not fire for NFS/SMB mounts, and folder jobs are usually not
    stable yet when their events arrive, so a slow backstop scan runs every
    BACKSTOP_SCAN_SECS to catch anything the events missed.
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def __init__(self, c_type: str) -> None:
            self.c_type = c_type
            self.exts   = BOOK_EXTS if c_type == 'book' else COMIC_EXTS

        def _maybe_dispatch(self, path: str) -> None:
            if os.path.splitext(path)[1].lower() not in self.exts:
                return
            if any(part.startswith('.') or part.endswith('.failed')
                   for part in path.split(os.sep) if part):
                return
            if self.c_type == 'book' and book_output_error(load_config()):
                return
            if self.c_type == 'comic':
                config = load_config()
                base   = COMICS_IN
                rel    = os.path.relpath(os.path.abspath(path), os.path.abspath(COMICS_IN))
                parts  = rel.split(os.sep)
                if len(parts) > 1 and parts[0] in (config.get('profiles') or {}):
                    # Inside a device profile folder — treat it as its own root.
                    base  = os.path.join(COMICS_IN, parts[0])
                    parts = parts[1:]
                if len(parts) > 1:
                    top = os.path.join(base, parts[0])
                    if _is_bundle_folder(top, config):
                        # Bundle folder — it's one volume, never converted
                        # piecemeal. Poke the folder job instead.
                        self._maybe_dispatch_dir(top)
                        return
                    # Everything else converts per-file; fall through.
                if config.get('originals') == 'keep' and _already_converted(path):
                    return
            with lock_mutex:
                if path not in PROCESSING_LOCKS:
                    PROCESSING_LOCKS.add(path)
                    threading.Thread(
                        target=process_file,
                        args=(path, self.c_type),
                        daemon=True,
                    ).start()

        def _maybe_dispatch_dir(self, path: str) -> None:
            # Folder jobs live directly inside Comics_in or a profile folder.
            # Deeper directories are ignored here, and a profile folder is a
            # drop target, never a job itself.
            config   = load_config()
            profiles = config.get('profiles') or {}
            parent   = os.path.dirname(os.path.abspath(path))
            main     = os.path.abspath(COMICS_IN)
            roots    = [main] + [os.path.join(main, n) for n in profiles]
            if parent not in roots:
                return
            base = os.path.basename(path)
            if base.startswith('.') or base.endswith('.failed'):
                return
            if parent == main and base in profiles:
                return
            if not _is_bundle_folder(path, config):
                # Its files convert per-file instead.
                return
            if config.get('originals') == 'keep' and _already_converted(path):
                return
            with lock_mutex:
                if path not in PROCESSING_LOCKS:
                    PROCESSING_LOCKS.add(path)
                    threading.Thread(target=process_folder, args=(path,), daemon=True).start()

        def on_created(self, event) -> None:  # type: ignore[override]
            # on_created fires as soon as the file appears, before data is
            # written. Still handle it so wait_for_file_ready can do its
            # stability check, but on_closed is the more reliable signal.
            if event.is_directory:
                if self.c_type == 'comic':
                    self._maybe_dispatch_dir(event.src_path)
            else:
                self._maybe_dispatch(event.src_path)

        def on_closed(self, event) -> None:  # type: ignore[override]
            # Fires on IN_CLOSE_WRITE — the write handle was closed, meaning
            # the transfer is complete. This is the definitive signal for
            # direct-write clients like FileBrowser. PROCESSING_LOCKS prevents
            # double-dispatch if on_created already queued a thread.
            if not event.is_directory:
                self._maybe_dispatch(event.src_path)

        def on_moved(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                if self.c_type == 'comic':
                    self._maybe_dispatch_dir(event.dest_path)
            else:
                self._maybe_dispatch(event.dest_path)

    scan_directories()
    observer = Observer()
    observer.schedule(_Handler('book'),  BOOKS_IN,  recursive=True)
    observer.schedule(_Handler('comic'), COMICS_IN, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(BACKSTOP_SCAN_SECS)
            try:
                scan_directories()
            except Exception as e:
                log(f">>> SCAN ERROR: {e}")
    finally:
        observer.stop()
        observer.join()
