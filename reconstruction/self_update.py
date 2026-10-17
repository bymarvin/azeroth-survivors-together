"""Azeroth Survivors Together co-op self-updater (boot-log format).

One updater for every later version. It reads the signed release description
from GitHub, downloads only the files that differ from this game folder and
installs them like the offline updater: backup, verification, rollback.

Release files (one GitHub release, fixed names via releases/latest/download/):
  latest.json        signed: version, sequence, signing time, minimum updater,
                     updater exe, manifest and archive assets with SHA-256/size,
                     optional key rotation (keys.add / keys.retire)
  latest.json.sig    Ed25519 signature (64 bytes) of the exact latest.json bytes
  manifest-<v>.json  every package file: path, SHA-256, size and source, either
                     {"archive": "mod"|"runtime"} (an asset zip of mod files) or
                     {"url": "https://..."} (game files hosted elsewhere)
Integrity: latest.json is signed, every other file is pinned by SHA-256 in it.

Security rules: HTTPS only, also after redirects (at most 5, only to GitHub or
the hosts of signed game file addresses), size limits and timeouts on every
request, the signature must come from a trusted key (built in, or added by an
earlier signed release), never an older version than installed and never an
older signed release than the last one accepted here (replay). Only signed
manifest paths are written (no drives, ADS, '..', reserved names or links out
of the folder; archive members are copied one by one with their signed size);
player data (saves, profiles, addons, WTF, logs) never. External game file
addresses that answer with a web page (download page, captcha) stop the update
before anything is installed and point to the full package instead.
A release that changes .coop-runtime exits with RUNTIME_BUSY after copying the
runtime; the updater exe runs this script again from the copy (--runtime-free).
Signatures use the audited `cryptography` package shipped in .coop-runtime.
"""
import argparse
import base64
import ctypes
import hashlib
import http.client
import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

UPDATER_VERSION = 1
PRODUCT = 'azeroth-survivors-together'
RELEASE_URL = 'https://github.com/bymarvin/azeroth-survivors-together/releases/latest/download/'
# Release signing keys (Ed25519, raw 32 bytes, base64). The private key exists
# only on the release machine (C:/Users/<you>/.azcoop-signing/).
TRUSTED_KEYS = ('KT0uZ26Iiq4BoCNZPNuYDOWFCMJJenW83dALTZFyMjc=',)
UPDATER_ASSET = 'Azeroth-Survivors-Together-Updater.exe'
EXE = 'AzerothSurvivors.exe'
STATE, TRUST = 'coop-update-state.json', 'coop-update-trust.json'
LATEST_LIMIT, MANIFEST_LIMIT = 256 * 1024, 8 * 1024 * 1024
SELF_UPDATE_READY = 3  # Exit code: the new updater exe waits next to the running one.
RUNTIME_BUSY = 5  # Exit code: restart from coop-update/<RUNTIME_COPY> with --runtime-free.
RUNTIME_COPY = 'runtime'
FULL_PACKAGE = 'Download the full Azeroth Survivors Together package instead and keep your saves folder.'
# Never written or deleted, even if a release manifest ever listed them: player
# data and settings (addons, WTF, saves, profiles, logs, room logs, backups).
USER_AREAS = ('benilla-config/', 'wtf/', 'interface/addons/', 'addons/', 'saves/', 'save/', 'profiles/', 'profile/',
              'logs/', 'collected-logs/', 'screenshots/', '.coop-rooms/', '.coop-pairing/', 'coop-update-backup-',
              'coop-update/')
USER_FILES = ('config.wtf', STATE, TRUST)
LOG = None
IGNORED_PIDS = set()  # An updater exe that replaced itself and still waits.


class Refused(Exception):
    """A check failed before anything was changed."""


class Offline(Refused):
    """The release server could not be reached."""


def log(tag, text):
    line = f'[{tag:^6}] {text}'
    print(line, flush=True)
    if LOG is not None:
        LOG.write(line + '\n')
        LOG.flush()


def step(name):
    log(' .... ', name)
    return time.time()


def done(name, started, info=''):
    log('  OK  ', f'{name} ({time.time() - started:.1f} s{", " + info if info else ""})')


def threads():
    try:
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                                  ctypes.POINTER(ctypes.c_size_t))
        mask, system = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(mask), ctypes.byref(system)):
            return max(1, min(16, bin(mask.value).count('1')))
    except (AttributeError, OSError):
        pass
    return 4


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def hash_all(paths, label):
    """Parallel streaming hashes with a progress line at least every ~10 s."""
    paths = list(paths)
    results, last = {}, time.time()
    with ThreadPoolExecutor(threads()) as pool:
        futures = {pool.submit(lambda p: sha256(p) if p.is_file() else None, p): p for p in paths}
        for count, future in enumerate(futures, 1):
            results[futures[future]] = future.result()
            if time.time() - last >= 10:
                last = time.time()
                log(f'{count * 100 // len(paths):>3} %', f'{label} {count}/{len(paths)} files')
    return results


def user_area(path):
    lower = path.lower()
    name = lower.rsplit('/', 1)[-1]
    return (any(lower.startswith(a) or f'/{a}' in lower for a in USER_AREAS) or name in USER_FILES
            or (lower.startswith('analysis/room-player-') and lower.endswith('.jsonl')) or name.endswith('.log'))


RESERVED = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
            *(f'{d}{i}' for d in ('com', 'lpt') for i in [*'0123456789', '¹', '²', '³'])}


def safe_path(path):
    """A relative release path that Windows stores as written: no drive, ADS, '..', reserved or odd names."""
    if not isinstance(path, str) or not path or len(path) > 240 or path.startswith('/') or '\\' in path:
        return False
    if any(ord(c) < 32 or c in ':*?"<>|' for c in path):
        return False
    for part in path.split('/'):
        if part in ('', '.', '..') or part != part.rstrip(' .') or part.split('.')[0].lower() in RESERVED:
            return False
    return True


def parse_version(text):
    """'0.0.11-community+game-7264d50b' -> (0, 0, 11)."""
    core = text.strip().split('-', 1)[0].split('+', 1)[0]
    parts = core.split('.')
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f'Invalid version {text!r}')
    return tuple(int(p) for p in parts)


def find_game(start):
    # The updater lives in <game>/coop-update or in <game>/reconstruction; started
    # from a subfolder of the game folder also works.
    for folder in (start, start.parent, start.parent.parent):
        if (folder / 'package-manifest.json').is_file() and (folder / EXE).is_file():
            return folder
    raise Refused('No Azeroth Survivors Together co-op folder found next to this updater. '
                  'Put the updater into your game folder (where Start-Coop.cmd is) and run it there.')


def processes_in(root):
    """Programs started from inside the game folder (game, co-op launcher), except this updater and its starter."""
    try:
        psapi, kernel = ctypes.WinDLL('psapi'), ctypes.WinDLL('kernel32')
    except OSError:
        return []
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    pids = (wintypes.DWORD * 8192)()
    size = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size)):
        return []
    prefix = os.path.normcase(str(root)).rstrip(os.sep) + os.sep
    found = []
    for pid in pids[:size.value // ctypes.sizeof(wintypes.DWORD)]:
        if pid in (0, os.getpid(), os.getppid()) or pid in IGNORED_PIDS:  # This updater and its starters.
            continue
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                if os.path.normcase(buffer.value).startswith(prefix):
                    found.append(f'{Path(buffer.value).name} (process {pid})')
        finally:
            kernel.CloseHandle(handle)
    return found


def writable(path):
    """A running exe, a read-only file or one another program holds open refuses writing."""
    try:
        with open(path, 'r+b'):
            return True
    except OSError:
        return False


def ensure_closed(root):
    running = processes_in(root)
    if running or not writable(root / EXE):
        raise Refused(f'Still running from this folder: {", ".join(running) or EXE + " is in use"}. '
                      'Close the game and the co-op launcher, then run the updater again. Nothing was changed.')


# ---------------------------------------------------------------- network

GITHUB_HOSTS = ('github.com', '.githubusercontent.com')  # Release assets redirect to *.githubusercontent.com.
MAX_REDIRECTS = 5


class NotDirect(Refused):
    """An external game file address is a web page (download page, captcha), not the file itself."""


class Net:
    """HTTPS to allowed hosts only (redirects included, at most 5), bounded sizes, timeouts, resumable downloads."""

    def __init__(self, base_url, timeout=10.0, allow_local_http=False):
        self.base, self.timeout, self.allow_local_http = base_url, timeout, allow_local_http
        self.hosts = set()
        self.check_scheme(base_url)
        self.allow(base_url)
        net = self

        class Redirect(urllib.request.HTTPRedirectHandler):
            max_redirections = MAX_REDIRECTS

            def redirect_request(self, req, fp, code, msg, headers, newurl):
                net.check_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(Redirect, urllib.request.HTTPSHandler(context=context))
        self.opener.addheaders = [('User-Agent', f'{PRODUCT}-updater/{UPDATER_VERSION}')]

    @staticmethod
    def host(url):
        return (urllib.parse.urlsplit(url).hostname or '').lower()

    def allow(self, url):
        """Allow the host of a signed address (release base, external game file sources)."""
        self.hosts.add(self.host(url))

    def check_scheme(self, url):
        if url.startswith('https://'):
            return
        if self.allow_local_http and url.startswith(('http://127.0.0.1:', 'http://localhost:')):
            return
        raise Refused(f'Refusing a non-HTTPS address: {url.split("?")[0]}')

    def check_url(self, url):
        self.check_scheme(url)
        host = self.host(url)
        if host in self.hosts or any(host == h or (h.startswith('.') and host.endswith(h)) for h in GITHUB_HOSTS):
            return
        raise Refused(f'Refusing an address on a host that is not allowed: {host}')

    def url(self, asset):
        return self.base + asset

    def open(self, request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise Refused(f'Too many redirects (more than {MAX_REDIRECTS}) for '
                              f'{request.full_url.rsplit("/", 1)[-1]}') from error
            raise

    def fetch(self, url, limit):
        self.check_url(url)
        try:
            with self.open(urllib.request.Request(url)) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            raise Refused(f'Release server answered {error.code} for {url.rsplit("/", 1)[-1]}') from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise Offline(f'Release server not reachable ({getattr(error, "reason", error)})') from error
        if len(data) > limit:
            raise Refused(f'{url.rsplit("/", 1)[-1]} is larger than allowed ({limit} bytes)')
        return data

    def download(self, url, target, size, digest, external=False):
        """Resume a partial download (HTTP Range) and verify size and SHA-256.

        external: a game file on another host; a web page instead of the file
        (download page, captcha) raises NotDirect instead of a hash failure.
        """
        self.check_url(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + '.part')
        if target.is_file() and target.stat().st_size == size and sha256(target) == digest:
            return 'cached'
        for attempt in range(3):
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            request = urllib.request.Request(url, headers={'Range': f'bytes={have}-'} if have else {})
            try:
                with self.open(request) as response:
                    kind = (response.headers.get('Content-Type') or '').lower()
                    if external and kind.startswith(('text/html', 'application/xhtml')):
                        raise NotDirect(f'{target.name}: {self.host(response.geturl())} answers with a web page, '
                                        'not the file (a download page or captcha)')
                    if have and response.status != 206:
                        have = 0  # The server ignored the range: start again.
                    mode = 'ab' if have else 'wb'
                    with open(part, mode) as stream:
                        written = have
                        while True:
                            chunk = response.read(1 << 20)
                            if not chunk:
                                break
                            if written == 0 and external and chunk.lstrip()[:15].lower().startswith(
                                    (b'<!doctype html', b'<html')):
                                raise NotDirect(f'{target.name}: the address returns a web page, not the file')
                            written += len(chunk)
                            if written > size:
                                if external:
                                    raise NotDirect(f'{target.name}: the address returns something else than the '
                                                    'signed file (larger)')
                                raise Refused(f'{target.name}: server sent more than the signed size')
                            stream.write(chunk)
            except Refused as error:
                if external and not isinstance(error, NotDirect):  # Redirect to plain HTTP or another host.
                    raise NotDirect(f'{target.name}: {error}') from error
                raise
            except urllib.error.HTTPError as error:
                if error.code == 416 and have == size:
                    pass  # Already complete.
                elif external and error.code in (401, 403, 404, 410, 429):
                    raise NotDirect(f'{target.name}: the download address answered {error.code}') from error
                else:
                    raise Refused(f'Download {target.name}: server answered {error.code}') from error
            except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
                if attempt == 2:
                    raise Offline(f'Download {target.name} interrupted ({getattr(error, "reason", error)}); '
                                  'run the updater again to continue') from error
                log(' WARN ', f'Download {target.name} interrupted, continuing ({attempt + 1}/3)')
                continue
            if part.stat().st_size == size:
                break
        if not part.exists() or part.stat().st_size != size:
            raise Offline(f'Download {target.name} incomplete; run the updater again to continue')
        if sha256(part) != digest:
            part.unlink()
            raise Refused(f'Download {target.name} does not match its signed SHA-256 (removed). '
                          'Run the updater again; if it persists, report it.')
        os.replace(part, target)
        return 'downloaded'


# ---------------------------------------------------------------- trust

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def trusted_keys(root, builtin=TRUSTED_KEYS):
    store = load_json(root / TRUST, {})
    keys = [k for k in (*builtin, *store.get('added', [])) if k not in store.get('retired', [])]
    return list(dict.fromkeys(keys)) or list(builtin)


def verify(data, signature, keys):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if len(signature) != 64:
        return None
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(signature, data)
            return key
        except (InvalidSignature, ValueError):
            continue
    return None


def validate_latest(latest):
    if not isinstance(latest, dict) or latest.get('format') != 1 or latest.get('product') != PRODUCT:
        raise Refused('latest.json is not an Azeroth Survivors Together release description')
    for field in ('version', 'signed_at'):
        if not isinstance(latest.get(field), str):
            raise Refused(f'latest.json has no {field}')
    parse_version(latest['version'])
    if type(latest.get('sequence')) is not int or type(latest.get('min_updater')) is not int:
        raise Refused('latest.json has no sequence or minimum updater version')
    for entry in [latest.get('manifest'), latest.get('updater'), *(latest.get('archives') or {}).values()]:
        if (not isinstance(entry, dict) or not isinstance(entry.get('asset'), str) or not safe_path(entry['asset'])
                or '/' in entry['asset'] or type(entry.get('size')) is not int or len(entry.get('sha256', '')) != 64):
            raise Refused('latest.json lists an invalid asset')
    full = latest.get('full_package')
    if full is not None and not (isinstance(full, dict) and isinstance(full.get('url'), str)
                                 and full['url'].startswith('https://')):
        raise Refused('latest.json has an invalid full package address')
    keys = latest.get('keys') or {}
    if not isinstance(keys, dict) or not all(isinstance(k, str) and valid_key(k) for k in
                                             [*keys.get('add', []), *keys.get('retire', [])]):
        raise Refused('latest.json announces an invalid key')


def valid_key(text):
    try:
        return len(base64.b64decode(text, validate=True)) == 32
    except ValueError:
        return False


def read_latest(net, root, builtin=TRUSTED_KEYS):
    """Fetch and verify latest.json; returns (latest, raw bytes, signing key)."""
    data = net.fetch(net.url('latest.json'), LATEST_LIMIT)
    signature = net.fetch(net.url('latest.json.sig'), 1024)
    key = verify(data, signature, trusted_keys(root, builtin))
    if key is None:
        raise Refused('The release description is not signed by a trusted key. Nothing was changed.')
    try:
        latest = json.loads(data)
    except ValueError as error:
        raise Refused('latest.json is not valid JSON') from error
    validate_latest(latest)
    return latest, data, key


def installed_version(root):
    try:
        return (root / 'COOP-VERSION.txt').read_text(encoding='utf-8').strip()
    except OSError:
        return '0.0.0'


def compare(root, latest):
    """'newer' / 'same'; refuse downgrade and replay of an older signed release."""
    installed = installed_version(root)
    state = load_json(root / STATE, {})
    if latest['sequence'] < state.get('sequence', 0) or latest['signed_at'] < state.get('signed_at', ''):
        raise Refused(f'The server offers an older signed release (sequence {latest["sequence"]}, signed '
                      f'{latest["signed_at"]}) than the one already accepted here ({state.get("sequence")}). '
                      'Nothing was changed.')
    if parse_version(latest['version']) < parse_version(installed):
        raise Refused(f'The server offers co-op {latest["version"]}, older than the installed {installed}. '
                      'Nothing was changed.')
    return 'newer' if parse_version(latest['version']) > parse_version(installed) else 'same'


def check(root, base_url=RELEASE_URL, timeout=3.0, allow_local_http=False, builtin=TRUSTED_KEYS):
    """For the launcher: (status, version) where status is newer/same/offline/invalid; never raises."""
    try:
        net = Net(base_url, timeout=timeout, allow_local_http=allow_local_http)
        latest, _, _ = read_latest(net, root, builtin)
        return compare(root, latest), latest['version']
    except Offline:
        return 'offline', None
    except (Refused, ValueError):
        return 'invalid', None


# ---------------------------------------------------------------- install

def install(root, staged, target, delete, label):
    """Write staged files {path: source file} over root with backup and full rollback."""
    changed = sorted(staged)
    blocked = [p for p in changed + delete + ['package-manifest.json'] if (root / p).exists() and not writable(root / p)]
    if blocked:
        raise Refused(f'{len(blocked)} files are read-only or locked (for example {blocked[0]}). '
                      'Close the game, remove the write protection and run the updater again. Nothing was changed.')
    backup_bytes = sum((root / p).stat().st_size for p in changed + delete if (root / p).exists())
    largest = max([Path(staged[p]).stat().st_size for p in changed] or [0])
    need = backup_bytes + largest + 64 * 2**20
    free = shutil.disk_usage(root).free
    if free < need:
        raise Refused(f'Not enough free disk space: {free / 2**20:.0f} MiB free, {need / 2**20:.0f} MiB needed. '
                      'Free some space and run the updater again. Nothing was changed.')
    backup = root / f'coop-update-backup-{time.strftime("%Y%m%d-%H%M%S")}'
    replaced, added, deleted, manifest_written = [], [], [], False
    try:
        started = step('Back up replaced files')
        for p in changed + delete + ['package-manifest.json']:
            if (root / p).exists():
                (backup / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / p, backup / p)
        done('Back up replaced files', started, backup.name)

        started = step('Install new files')
        for p in changed:
            destination = root / p
            existed = destination.exists()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + '.coop-new')
            try:
                shutil.copyfile(staged[p], temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            (replaced if existed else added).append(p)
        for p in delete:
            (root / p).unlink()
            deleted.append(p)
        manifest_written = True
        (root / 'package-manifest.json').write_text(json.dumps(target, indent=1, sort_keys=True), encoding='utf-8')
        done('Install new files', started, f'{len(replaced)} replaced, {len(added)} added, {len(deleted)} deleted')

        started = step('Verify written files')
        result = hash_all([root / p for p in changed], 'verify')
        wrong = [p for p in changed if result[root / p] != target[p]]
        if wrong:
            raise RuntimeError(f'Written file does not match the release: {wrong[0]}')
        done('Verify written files', started, f'{len(changed)} files match the release')
    except BaseException as error:
        log('FAILED', f'Update: {type(error).__name__}: {error}')
        started = step('Restore previous version')
        problems = []
        for p in added:
            try:
                (root / p).unlink(missing_ok=True)
            except OSError as problem:
                problems.append(f'{p}: {problem}')
        for p in replaced + deleted + (['package-manifest.json'] if manifest_written else []):
            try:
                temporary = (root / p).with_name(Path(p).name + '.coop-restore')
                shutil.copy2(backup / p, temporary)
                os.replace(temporary, root / p)
            except OSError as problem:
                problems.append(f'{p}: {problem}')
        if problems:
            log('FAILED', f'Restore previous version: {len(problems)} files could not be restored, first {problems[0]}; '
                          f'copy them back from {backup.name} by hand')
        else:
            done('Restore previous version', started, f'{len(replaced) + len(deleted)} restored, {len(added)} removed; '
                                                     'the folder is as before')
        raise
    return backup


class RuntimeBusy(Refused):
    """The release changes .coop-runtime, which this Python runs from: restart from a copy."""


class NeedFullPackage(Refused):
    """New game files exist but cannot be downloaded directly: the player needs the full package."""


def plan(root, target, files, runtime_free=False):
    """Files to fetch (hash differs or missing) and old release files to delete (only if unchanged)."""
    installed = load_json(root / 'package-manifest.json', {})
    paths = [p for p in target if not user_area(p)]
    for p in target:
        if user_area(p):
            log(' WARN ', f'Player data or settings, never touched by the updater: {p}')
    local = hash_all([root / p for p in paths], 'scan')
    needed = sorted(p for p in paths if local[root / p] != target[p])
    removed = sorted(p for p in installed if p not in target and not user_area(p) and safe_path(p))
    current = hash_all([root / p for p in removed], 'scan')
    delete = [p for p in removed if current[root / p] == installed[p]]
    for p in removed:
        if current[root / p] not in (None, installed[p]):
            log(' WARN ', f'Not deleted (changed since the old release, kept as it is): {p}')
    runtime = [p for p in needed + delete if p.startswith('.coop-runtime/')]
    if runtime:
        running = Path(sys.executable).resolve()
        inside = (root / '.coop-runtime').resolve()
        if not runtime_free or running == inside or inside in running.parents:
            raise RuntimeBusy(f'This update changes the co-op Python runtime ({len(runtime)} files, for example '
                              f'{runtime[0]}), which this updater runs from')
    escaped = [p for p in needed + delete if not inside_root(root, p)]
    if escaped:
        raise Refused(f'{escaped[0]} would be written outside the game folder (a link in the folder?). '
                      'Nothing was changed.')
    return needed, delete


def inside_root(root, path):
    """The real location (links resolved) stays inside the game folder."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(root / path)
    return os.path.normcase(real).startswith(os.path.normcase(real_root).rstrip(os.sep) + os.sep)


def extract_member(archive, name, size, digest, target):
    """Copy exactly one signed member; refuse links, wrong sizes and decompression bombs."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise Refused(f'{name} is missing from the archive. Nothing was changed.') from None
    mode = (info.external_attr >> 16) & 0o170000
    if info.is_dir() or mode in (0o120000, 0o040000) or info.file_size != size or info.flag_bits & 0x1:
        raise Refused(f'{name} in the archive is not the signed plain file. Nothing was changed.')
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with archive.open(info) as member, open(target, 'wb') as stream:
            while True:
                chunk = member.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > size:  # Never trust the archive's own size fields.
                    raise Refused(f'{name} unpacks larger than signed. Nothing was changed.')
                stream.write(chunk)
    except (zipfile.BadZipFile, EOFError, zlib.error) as error:  # zipfile stops at the header size, then CRC.
        raise Refused(f'{name} in the archive is damaged ({error}). Nothing was changed.') from error
    if written != size or sha256(target) != digest:
        raise Refused(f'{name} does not match its signed SHA-256. Nothing was changed.')


def fetch_files(net, latest, files, needed, work):
    """Download everything for the needed paths before anything is installed; returns {path: staged file}.

    Game files on external hosts first: if they cannot be downloaded directly, nothing
    else is fetched and nothing is installed (NeedFullPackage).
    """
    staged, archives, external = {}, {}, []
    for p in needed:
        source = files[p]['source']
        if 'archive' in source:
            archives.setdefault(source['archive'], []).append(p)
        elif 'url' in source:
            external.append(p)
        else:
            raise Refused(f'manifest has no source for {p}')
    if any(a not in latest['archives'] for a in archives):
        raise Refused('latest.json does not list an archive the manifest needs. Nothing was changed.')
    total = sum(latest['archives'][a]['size'] for a in archives) + sum(files[p]['size'] for p in external)
    free = shutil.disk_usage(work.parent).free
    if free < total * 2 + 64 * 2**20:
        raise Refused(f'Not enough free disk space for the download: {free / 2**20:.0f} MiB free, '
                      f'{(total * 2 + 64 * 2**20) / 2**20:.0f} MiB needed. Nothing was changed.')
    for p in external:
        entry = files[p]
        started = step(f'Download {p}')
        target = work / 'files' / p
        try:
            how = net.download(entry['source']['url'], target, entry['size'], entry['sha256'], external=True)
        except NotDirect as error:
            full = (latest.get('full_package') or {}).get('url')
            raise NeedFullPackage(f'This update needs new game files that cannot be downloaded automatically '
                                  f'({error}). Nothing was changed. Download the full package'
                                  + (f' here: {full}' if full else ' from the release page')) from error
        done(f'Download {p}', started, f'{entry["size"] / 2**20:.1f} MiB {how}')
        staged[p] = target
    for name, paths in sorted(archives.items()):
        entry = latest['archives'][name]
        started = step(f'Download {entry["asset"]}')
        archive = work / 'download' / entry['asset']
        how = net.download(net.url(entry['asset']), archive, entry['size'], entry['sha256'])
        done(f'Download {entry["asset"]}', started, f'{entry["size"] / 2**20:.1f} MiB {how}')
        with zipfile.ZipFile(archive) as z:
            for p in paths:  # Only signed members; everything else in the archive is ignored.
                extract_member(z, p, files[p]['size'], files[p]['sha256'], work / 'files' / p)
                staged[p] = work / 'files' / p
    return staged


def self_update(net, latest, exe):
    """Fetch the newer updater exe next to the running one; the exe swaps itself."""
    entry = latest['updater']
    if exe is None:
        raise Refused(f'This update needs updater {latest["min_updater"]} or newer. Download {entry["asset"]} '
                      'and run it in your game folder. Nothing was changed.')
    started = step(f'Update the updater to version {entry.get("version")}')
    target = Path(exe).with_name(Path(exe).name + '.new')
    net.download(net.url(entry['asset']), target, entry['size'], entry['sha256'])
    done(f'Update the updater to version {entry.get("version")}', started, 'restarting with the new updater')
    return SELF_UPDATE_READY


def main(argv=None, builtin=TRUSTED_KEYS):
    global LOG
    ap = argparse.ArgumentParser(description='Azeroth Survivors Together co-op updater')
    ap.add_argument('--release-url', default=RELEASE_URL)
    ap.add_argument('--updater-version', type=int, default=UPDATER_VERSION)
    ap.add_argument('--updater-exe', help='path of the running updater exe (self-update)')
    ap.add_argument('--game', type=Path, help='game folder (default: next to this file)')
    ap.add_argument('--timeout', type=float, default=10.0)
    ap.add_argument('--allow-local-http', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--ignore-pid', type=int, action='append', default=[], help=argparse.SUPPRESS)
    # Set by the updater exe when it restarted this script from a copy of .coop-runtime.
    ap.add_argument('--runtime-free', action='store_true', help=argparse.SUPPRESS)
    # Only test builds of the updater exe pass this (compiled-in test key).
    ap.add_argument('--extra-trusted-key', action='append', default=[], help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    IGNORED_PIDS.update(args.ignore_pid)
    builtin = (*builtin, *args.extra_trusted_key)
    try:
        sys.stdout.reconfigure(errors='replace')
    except AttributeError:
        pass
    t0 = time.time()
    root = args.game.resolve() if args.game else find_game(Path(__file__).resolve().parent)
    work = root / 'coop-update'
    work.mkdir(exist_ok=True)
    LOG = (work / 'update.log').open('a', encoding='utf-8')
    log(' INFO ', f'Updater {args.updater_version}, game folder {root}, {threads()} threads')

    started = step('Game and co-op launcher are closed')
    ensure_closed(root)
    done('Game and co-op launcher are closed', started)

    started = step('Read signed release description')
    net = Net(args.release_url, timeout=args.timeout, allow_local_http=args.allow_local_http)
    latest, raw, key = read_latest(net, root, builtin)
    status = compare(root, latest)
    done('Read signed release description', started,
         f'co-op {latest["version"]} (sequence {latest["sequence"]}, signed {latest["signed_at"]}), '
         f'installed {installed_version(root)}')
    if latest['min_updater'] > args.updater_version:
        return self_update(net, latest, args.updater_exe)

    started = step('Read release file list')
    entry = latest['manifest']
    data = net.fetch(net.url(entry['asset']), min(entry['size'], MANIFEST_LIMIT))
    if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise Refused('manifest does not match its signed SHA-256. Nothing was changed.')
    files = validate_files(json.loads(data), net)
    target = {p: e['sha256'] for p, e in files.items()}
    done('Read release file list', started, f'{len(files)} files')

    started = step('Compare with this folder')
    try:
        needed, delete = plan(root, target, files, args.runtime_free)
    except RuntimeBusy as error:
        if args.runtime_free or not args.updater_exe:
            raise Refused(f'{error}. Run {UPDATER_ASSET} in the game folder; it restarts from a copy of the '
                          'runtime. Nothing was changed.') from error
        log(' INFO ', f'{error}: restarting from a temporary copy of it')
        copy_runtime(root, work / RUNTIME_COPY)
        return RUNTIME_BUSY
    done('Compare with this folder', started, f'{len(needed)} files to download, {len(delete)} to delete')
    if not needed and not delete:
        accept(root, latest, key)
        if load_json(root / 'package-manifest.json', {}) != target:
            (root / 'package-manifest.json').write_text(json.dumps(target, indent=1, sort_keys=True), encoding='utf-8')
        log('  OK  ', f'Nothing to do: this folder is co-op {installed_version(root)}'
                      + (' (newer files already present)' if status == 'newer' else ''))
        return 0

    staging = work / f'release-{latest["version"]}'
    staged = fetch_files(net, latest, files, needed, staging)
    started = step('Game and co-op launcher are closed')
    ensure_closed(root)  # The downloads may have taken a while.
    done('Game and co-op launcher are closed', started)
    backup = install(root, staged, target, delete, latest['version'])
    accept(root, latest, key)
    shutil.rmtree(staging / 'files', ignore_errors=True)
    log(' INFO ', f'Summary: co-op {installed_version(root)} in {time.time() - t0:.1f} s; backup in {backup.name} '
                  '(delete it once the game runs)')
    log('  OK  ', f'Installed co-op version: {installed_version(root)}. Start the game with Start-Coop.cmd.')
    return 0


def validate_files(manifest, net):
    """The signed file list: safe paths, hashes, sizes and HTTPS sources; external hosts become allowed."""
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        raise Refused('manifest has no file list. Nothing was changed.')
    for p, entry in files.items():
        if not safe_path(p):
            raise Refused(f'manifest lists an unsafe path ({p!r}). Nothing was changed.')
        source = entry.get('source') if isinstance(entry, dict) else None
        if (not isinstance(source, dict) or type(entry.get('size')) is not int or entry['size'] < 0
                or not isinstance(entry.get('sha256'), str) or len(entry['sha256']) != 64):
            raise Refused(f'manifest has an invalid entry for {p}. Nothing was changed.')
        if 'url' in source:
            if not isinstance(source['url'], str):
                raise Refused(f'manifest has an invalid source for {p}. Nothing was changed.')
            net.check_scheme(source['url'])
            net.allow(source['url'])
    return files


def copy_runtime(root, target):
    """Copy .coop-runtime so the updater can run from the copy while it replaces the original."""
    started = step('Copy the co-op Python runtime')
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(root / '.coop-runtime', target)
    count = sum(1 for p in target.rglob('*') if p.is_file())
    size = sum(p.stat().st_size for p in target.rglob('*') if p.is_file())
    done('Copy the co-op Python runtime', started, f'{count} files, {size / 2**20:.1f} MiB')


def accept(root, latest, key):
    """Remember the accepted release (replay protection) and announced key changes."""
    (root / STATE).write_text(json.dumps(dict(sequence=latest['sequence'], signed_at=latest['signed_at'],
                                              version=latest['version'], key=key), indent=1), encoding='utf-8')
    keys = latest.get('keys') or {}
    if keys.get('add') or keys.get('retire'):
        store = load_json(root / TRUST, {'added': [], 'retired': []})
        for k in keys.get('add', []):
            if k not in store['added']:
                store['added'].append(k)
        for k in keys.get('retire', []):
            if k not in store['retired'] and k != key:  # Never retire the key that signed this release.
                store['retired'].append(k)
        (root / TRUST).write_text(json.dumps(store, indent=1), encoding='utf-8')
        log(' INFO ', f'Release keys updated: {len(store["added"])} added, {len(store["retired"])} retired')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Offline as error:
        log('FAILED', f'{error}. Check your internet connection and try again. Nothing was changed.')
        sys.exit(4)
    except Refused as error:
        log('FAILED', str(error))
        sys.exit(2)
    except Exception as error:
        log('FAILED', f'{type(error).__name__}: {error}')
        sys.exit(1)
