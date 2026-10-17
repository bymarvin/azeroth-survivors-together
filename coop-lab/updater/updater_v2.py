"""Azeroth Survivors Together co-op updater 2: game file patches (boot-log format).

Embedded only in Azeroth-Survivors-Together-Updater.exe (version 2) next to
reconstruction/self_update.py, which stays unchanged in the game folder (it is
pinned by the binding and keeps the launcher check). This module extends it:

- A game file in the signed manifest may have the source
    {"patches": [{"from_sha256", "asset", "sha256", "size"}...], "full": {"asset", "sha256", "size"}}
  with assets in the release named by the manifest's "gamefiles": {"url": "https://.../download/<tag>/"}.
- The updater picks the patch for the local file's SHA-256, else the full
  asset (xz), else it asks for the full package. The local SHA is checked
  before patching and the result against the target SHA; then the usual
  backup / install / verify / rollback of self_update runs.
- Releases that use patches set min_updater 2, so updater 1 fetches this one first.
- Version 3: the package ships this updater exe (from 0.0.13). If the running
  exe is one of the changed files, it is not replaced while it runs: after a
  successful install the new one waits as <exe>.new and the run ends with 3;
  the exe swaps itself and runs once more (nothing left to do). Releases that
  ship the exe set min_updater 3.
- Version 4: "gamefiles": {"urls": [...]} is an ordered, signed list of sources
  (e.g. GitHub, our server); every asset comes from the first source that
  delivers it (a page/404/410/429, an interrupted download or a wrong SHA-256
  moves on to the next one, a partial download continues there). Full assets
  may be "encoding": "raw" (MPQ archives xz cannot shrink). A folder without
  the game (the web installer's fresh install) is installed completely.
  Releases using this set min_updater 4.
- Repair (0.0.20, still updater 4): a folder that already has the newest version is compared
  file by file with the signed release as well; every package file that differs (another
  AzerothSurvivors.exe, a changed or missing mod or runtime file) is restored from its signed
  source (archives, game file patch or full asset) with the usual backup and rollback, and the run
  ends with "Game files repaired". Start-Coop.cmd starts this when its check finds such a file;
  --repair runs it on purpose (player data is never touched).
"""
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script folder out.
import self_update as su  # noqa: E402
import game_patch  # noqa: E402

UPDATER_VERSION = 4
su.UPDATER_VERSION = UPDATER_VERSION
RUNNING = {}  # 'exe': the running updater exe, 'pending': its new version staged during this run
_validate_files, _fetch_files, _ensure_closed = su.validate_files, su.fetch_files, su.ensure_closed
GAMEFILES = {}  # 'urls': the signed, ordered game file sources of this run; 'override': diagnostics/tests


def valid_asset(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('asset'), str) and su.safe_path(entry['asset'])
            and '/' not in entry['asset'] and type(entry.get('size')) is int and entry['size'] >= 0
            and isinstance(entry.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
            and entry.get('encoding', 'xz') in ('xz', 'raw'))


def ensure_closed(root):
    """Like self_update, but a folder without the game yet (fresh install) is not 'in use'."""
    if (root / su.EXE).exists():
        return _ensure_closed(root)
    running = su.processes_in(root)
    if running:
        raise su.Refused(f'Still running from this folder: {", ".join(running)}. Close it and run again. '
                         'Nothing was changed.')


def validate_files(manifest, net):
    files = _validate_files(manifest, net)
    patched = [p for p, e in files.items() if 'patches' in e['source'] or 'full' in e['source']]
    if not patched:
        return files
    gamefiles = manifest.get('gamefiles') if isinstance(manifest.get('gamefiles'), dict) else {}
    urls = gamefiles.get('urls', [gamefiles['url']] if 'url' in gamefiles else None)
    if (not isinstance(urls, list) or not 1 <= len(urls) <= 8
            or not all(isinstance(u, str) and u.endswith('/') for u in urls)):
        raise su.Refused('manifest has game file patches but no valid game file sources. Nothing was changed.')
    if GAMEFILES.get('override'):  # Diagnostics/tests, like --release-url: assets stay SHA-pinned.
        urls = [GAMEFILES['override']]
    for url in urls:
        net.check_scheme(url)
        net.allow(url)
    GAMEFILES['urls'] = urls
    for p in patched:
        source = files[p]['source']
        patches, full = source.get('patches', []), source.get('full')
        if (not isinstance(patches, list) or len(patches) > 64 or not all(
                valid_asset(e) and isinstance(e.get('from_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', e['from_sha256'])
                for e in patches) or full is not None and not valid_asset(full)):
            raise su.Refused(f'manifest has an invalid game file source for {p}. Nothing was changed.')
    return files


def need_full_package(latest, reason):
    full = ((latest or {}).get('full_package') or {}).get('url')
    return su.NeedFullPackage(f'This update needs new game files that cannot be updated automatically ({reason}). '
                              'Nothing was changed. Download the full package'
                              + (f' here: {full}' if full else ' from the release page'))


def download_asset(net, latest, entry, target):
    """Fetch one asset from the first signed source that delivers it: (source host, how)."""
    problems = []
    for base in GAMEFILES['urls']:
        host = su.Net.host(base)
        try:
            # external: a missing or taken-down release (404/410/a page) is "not here", not an error.
            return host, net.download(base + urllib.parse.quote(entry['asset']), target, entry['size'],
                                      entry['sha256'], external=True)
        except su.NotDirect as error:
            problems.append(('missing', error))
        except su.Offline as error:
            problems.append(('offline', error))  # The partial download continues at the next source.
        except su.Refused as error:
            problems.append(('refused', error))  # E.g. a wrong SHA-256: the damaged copy is removed.
        if len(problems) < len(GAMEFILES['urls']):
            su.log(' WARN ', f'{entry["asset"]} from {host}: {problems[-1][1]}; trying the next source')
    kinds = {kind for kind, _ in problems}
    if kinds == {'missing'}:
        raise need_full_package(latest, str(problems[-1][1]))
    if 'offline' in kinds and 'refused' not in kinds:
        raise problems[-1][1]
    raise next(error for kind, error in reversed(problems) if kind == 'refused')


def choose(path, local, entry):
    """(kind, asset entry) for turning the local file into the target, or None."""
    source = entry['source']
    for patch in source.get('patches', []):
        if local is not None and patch['from_sha256'] == local:
            return 'patch', patch
    if source.get('full'):
        return 'full', source['full']
    return None


def fetch_files(net, latest, files, needed, work):
    root = work.parent.parent  # <game>/coop-update/release-<version>
    patched = [p for p in needed if 'patches' in files[p]['source'] or 'full' in files[p]['source']]
    plans = {}
    for p in patched:
        local = su.sha256(root / p) if (root / p).is_file() else None
        chosen = choose(p, local, files[p])
        if chosen is None:
            raise need_full_package(latest, f'no patch from your {p}')
        plans[p] = (local, *chosen)
    total = sum(entry['size'] for _, _, entry in plans.values()) + sum(files[p]['size'] for p in plans)
    free = shutil.disk_usage(work.parent).free
    if free < total * 2 + 64 * 2**20:
        raise su.Refused(f'Not enough free disk space for the game file update: {free / 2**20:.0f} MiB free, '
                         f'{(total * 2 + 64 * 2**20) / 2**20:.0f} MiB needed. Nothing was changed.')
    staged = {}
    for p, (local, kind, entry) in sorted(plans.items()):
        started = su.step(f'Download {kind} for {p}')
        asset = work / 'gamefiles' / entry['asset']
        host, how = download_asset(net, latest, entry, asset)
        su.done(f'Download {kind} for {p}', started, f'{entry["size"] / 2**20:.2f} MiB {how} from {host} '
                                                    f'(file {files[p]["size"] / 2**20:.1f} MiB)')
        started = su.step(f'Build {p}')
        target = work / 'files' / p
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.building')
        if kind == 'full' and entry.get('encoding') == 'raw':
            if entry['sha256'] != files[p]['sha256'] or entry['size'] != files[p]['size']:
                raise su.Refused(f'manifest: the raw asset of {p} is not the file itself. Nothing was changed.')
            os.replace(asset, target)  # Verified by the download (SHA-256 of the file itself).
            su.done(f'Build {p}', started, 'raw, SHA-256 matches the release')
            staged[p] = target
            continue
        try:
            if kind == 'patch':
                if su.sha256(root / p) != local:  # Unchanged since the comparison.
                    raise su.Refused(f'{p} changed during the update. Nothing was changed.')
                digest = game_patch.apply(root / p, asset, temporary)
            else:
                digest = game_patch.unpack_full(asset, temporary, files[p]['size'])
        except ValueError as error:
            temporary.unlink(missing_ok=True)
            asset.unlink(missing_ok=True)  # Damaged: the next run downloads it again.
            raise su.Refused(f'{entry["asset"]} could not be applied ({error}). Run the updater again; '
                             'if it persists, report it. Nothing was changed.') from error
        if digest != files[p]['sha256'] or os.path.getsize(temporary) != files[p]['size']:
            temporary.unlink(missing_ok=True)
            asset.unlink(missing_ok=True)
            raise su.Refused(f'{p} built from {entry["asset"]} does not match its signed SHA-256. Run the updater '
                             'again; if it persists, report it. Nothing was changed.')
        os.replace(temporary, target)
        su.done(f'Build {p}', started, f'{kind}, SHA-256 matches the release')
        staged[p] = target
    rest = [p for p in needed if p not in plans and p != su.UPDATER_ASSET]
    staged.update(_fetch_files(net, latest, files, rest, work) if rest else {})
    if su.UPDATER_ASSET in needed and su.UPDATER_ASSET not in plans:
        staged.update(fetch_updater_exe(net, latest, files, work))
    running = RUNNING.get('exe')
    if su.UPDATER_ASSET in staged and running is not None and running == (root / su.UPDATER_ASSET).resolve():
        # A running exe cannot be replaced (install would refuse it as locked): it follows after the install.
        pending = work.parent / (su.UPDATER_ASSET + '.pending')
        shutil.copyfile(staged.pop(su.UPDATER_ASSET), pending)
        RUNNING['pending'] = pending
        su.log(' INFO ', f'{su.UPDATER_ASSET} is running: its new version is put in place after this update')
    return staged


def security_blocked(error):
    """Windows security refused to write an exe: ERROR_VIRUS_INFECTED/_DELETED (225/226), which the C runtime
    reports as [Errno 22] Invalid argument without a WinError, or access denied right after writing."""
    return (getattr(error, 'winerror', None) in (225, 226) or isinstance(error, PermissionError)
            or getattr(error, 'errno', None) == 22)


def fetch_updater_exe(net, latest, files, work):
    """The updater exe (in every package since 0.0.13) is the one file the co-op does not need to run.

    Seen on a player's PC: Windows security blocked writing the unsigned exe while the installer unpacked
    it (OSError [Errno 22] Invalid argument) and the whole installation stopped, again on every retry. It
    is fetched last and on its own; if Windows blocks it, everything else is installed and the player is
    told how to allow it. Start-Coop.cmd and the next update fetch it again once it is allowed.
    """
    try:
        return _fetch_files(net, latest, files, [su.UPDATER_ASSET], work)
    except OSError as error:
        if not security_blocked(error):
            raise
        try:
            (work / 'files' / su.UPDATER_ASSET).unlink(missing_ok=True)  # A partly written copy, if any.
        except OSError:
            pass
        su.log(' WARN ', f'Windows security blocked {su.UPDATER_ASSET} ({error.__class__.__name__}: {error}). '
                         'The co-op is installed without it and works. To get automatic updates: Windows Security > '
                         'Virus & threat protection > Protection history > the blocked item > Allow on device, '
                         'then run the installer (or Start-Coop.cmd) again.')
        return {}


su.validate_files, su.fetch_files, su.ensure_closed = validate_files, fetch_files, ensure_closed

REPAIR = {}  # This run: release status (same/newer), files written and their backup (for the repair report).
_compare, _install = su.compare, su.install


def compare(root, latest):
    REPAIR['status'] = _compare(root, latest)
    return REPAIR['status']


def install(root, staged, target, delete, label):
    REPAIR['changed'], REPAIR['deleted'] = sorted(staged), list(delete)
    backup = _install(root, staged, target, delete, label)
    REPAIR['backup'] = backup.name
    return backup


su.compare, su.install = compare, install


def repair_report(repair):
    """After a successful run: the files a same-version run restored ("Game files repaired")."""
    if REPAIR.get('status') != 'same':
        return
    changed = REPAIR.get('changed') or []
    if changed:
        su.log('  OK  ', f'Game files repaired: {len(changed)} files restored from the signed release '
                         f'({", ".join(changed[:3])}{", ..." if len(changed) > 3 else ""}); backup in {REPAIR["backup"]}')
    elif repair:
        su.log('  OK  ', 'Repair: every game file matches the signed release, nothing to repair')


def parent_pid():
    """os.getppid() without PssCaptureSnapshot: Python 3.12 calls it on Windows, Wine 10 does not implement it
    and aborts the process (Linux players). The toolhelp snapshot works on Windows and Wine alike."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return 0  # Unknown: 0 is skipped anyway; this updater's starter then simply counts as a process.
    try:
        entry = Entry(dwSize=ctypes.sizeof(Entry))
        me, ok = os.getpid(), kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == me:
                return entry.th32ParentProcessID
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return 0
    finally:
        kernel.CloseHandle(snapshot)


if os.name == 'nt':
    os.getppid = parent_pid  # self_update.processes_in skips its starter by os.getppid().


def main(argv=None, builtin=su.TRUSTED_KEYS):
    import argparse
    ap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)  # --game is not --gamefiles-url.
    ap.add_argument('--gamefiles-url')  # Hidden: another address for the signed game file assets.
    ap.add_argument('--repair', action='store_true')  # Compare every file with the signed release on purpose.
    extra, rest = ap.parse_known_args(sys.argv[1:] if argv is None else argv)
    REPAIR.clear()
    if extra.repair:
        su.log(' INFO ', 'Repair: every game file is compared with the signed release (backup and rollback as usual)')
    GAMEFILES.clear()
    # Also from the environment: it survives the exe's self-update and older updaters ignore it.
    override = extra.gamefiles_url or os.environ.get('COOP_GAMEFILES_URL')
    if override:
        GAMEFILES['override'] = override
    RUNNING.clear()
    exe = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    exe.add_argument('--updater-exe')
    running = exe.parse_known_args(rest)[0].updater_exe
    if running:
        RUNNING['exe'] = Path(running).resolve()
    try:
        code = su.main(rest, builtin)
        if code == 0:
            repair_report(extra.repair)
        if RUNNING.get('pending') is not None and code == 0:
            os.replace(RUNNING.pop('pending'), RUNNING['exe'].with_name(RUNNING['exe'].name + '.new'))
            su.log(' INFO ', 'Restarting with the new updater to finish (it replaces itself now)')
            return su.SELF_UPDATE_READY
        return code
    finally:
        leftover = RUNNING.pop('pending', None)
        if leftover is not None:
            leftover.unlink(missing_ok=True)  # Install failed: the running exe stays as it is.


if __name__ == '__main__':
    try:
        sys.exit(main())
    except su.Offline as error:
        su.log('FAILED', f'{error}. Check your internet connection and try again. Nothing was changed.')
        sys.exit(4)
    except su.Refused as error:
        su.log('FAILED', str(error))
        sys.exit(2)
    except Exception as error:
        su.log('FAILED', f'{type(error).__name__}: {error}')
        sys.exit(1)
