"""Azeroth Survivors Together co-op updater (boot-log format).

Run by Update-Coop.cmd with the game folder's own .coop-runtime Python. It
brings a known earlier co-op package to the version in target-manifest.json:

1. find the game folder; refuse while the game or the co-op launcher still
   runs from it (process list and file locks), before anything changes,
2. recognise the installed release by its package-manifest.json AND the hash
   of its AzerothSurvivors.exe; an unknown or modified install is refused,
3. verify every file of this update and the free disk space,
4. back up every file it replaces or deletes into coop-update-backup-<time>,
5. write only the files of this update; delete a file the new release no
   longer has only if it is still exactly the old release's file,
6. verify every written file; on any failure (also a full disk) restore the
   backup completely.

Only files listed in the release manifests are ever written or deleted:
saves, profiles, addons and settings (WTF), logs and other user files are
never touched.
"""
import ctypes
import hashlib
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = None
EXE = 'AzerothSurvivors.exe'
FULL_PACKAGE = 'Download the full Azeroth Survivors Together package instead and keep your saves folder.'
# Never written or deleted, even if a release manifest ever listed them: player
# data and settings (addons, WTF, saves, profiles, logs, room logs, backups).
USER_AREAS = ('benilla-config/', 'wtf/', 'interface/addons/', 'addons/', 'saves/', 'save/', 'profiles/', 'profile/',
              'logs/', 'collected-logs/', 'screenshots/', '.coop-rooms/', '.coop-pairing/', 'coop-update-backup-')
USER_FILES = ('config.wtf',)


def user_area(path):
    lower = path.lower()
    name = lower.rsplit('/', 1)[-1]
    return (any(lower.startswith(a) or f'/{a}' in lower for a in USER_AREAS) or name in USER_FILES
            or (lower.startswith('analysis/room-player-') and lower.endswith('.jsonl')) or name.endswith('.log'))


class Refused(Exception):
    """A check failed before anything was changed."""


def log(tag, text):
    line = f'[{tag:^6}] {text}'
    print(line, flush=True)
    if LOG is not None:
        LOG.write(line + '\n')
        LOG.flush()


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


def step(name):
    log(' .... ', name)
    return time.time()


def done(name, started, info=''):
    log('  OK  ', f'{name} ({time.time() - started:.1f} s{", " + info if info else ""})')


def find_game(start):
    # Extracted into the game folder, or into a subfolder of it.
    for folder in (start, start.parent):
        if (folder / 'package-manifest.json').is_file() and (folder / EXE).is_file():
            return folder
    raise Refused('No Azeroth Survivors Together co-op folder found next to this updater. '
                  'Extract the update into your game folder (where Start-Coop.cmd is) and run Update-Coop.cmd there.')


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
        if pid in (0, os.getpid(), os.getppid()):  # This updater and the exe or cmd that started it.
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


def recognise(root, installed, sources, target):
    """(label, manifest) of the installed release; refuse unknown or modified installs."""
    exe = sha256(root / EXE)
    for label, entry in sources.get('too_old', {}).items():
        if exe == entry:
            raise Refused(f'This folder is {label}. It is too old for this update. {FULL_PACKAGE}')
    if installed == target:
        if exe != target[EXE]:
            raise Refused(f'{EXE} was changed (sha256 {exe[:8]}), it is not the release file. {FULL_PACKAGE}')
        return 'current', target
    for label, manifest in sources['releases'].items():
        if manifest == installed:
            if exe != manifest[EXE]:
                raise Refused(f'This looks like {label}, but {EXE} was changed (sha256 {exe[:8]}, expected '
                              f'{manifest[EXE][:8]}). Nothing was changed. {FULL_PACKAGE}')
            return label, manifest
    raise Refused(f'The installed package-manifest.json matches no release this update knows (game {exe[:8]}). '
                  f'Nothing was changed. {FULL_PACKAGE}')


def main():
    global LOG
    t0 = time.time()
    try:
        sys.stdout.reconfigure(errors='replace')
    except AttributeError:
        pass
    target = json.loads((HERE / 'target-manifest.json').read_text(encoding='utf-8'))
    sources = json.loads((HERE / 'sources.json').read_text(encoding='utf-8'))
    version = (HERE / 'VERSION').read_text(encoding='utf-8').strip()
    root = find_game(HERE.parent)
    LOG = (HERE / 'update.log').open('a', encoding='utf-8')
    log(' INFO ', f'Update to co-op {version}, game folder {root}, {threads()} threads')

    started = step('Game and co-op launcher are closed')
    running = processes_in(root)
    if running or not writable(root / EXE):
        raise Refused(f'Still running from this folder: {", ".join(running) or EXE + " is in use"}. '
                      'Close the game and the co-op launcher, then run Update-Coop.cmd again. Nothing was changed.')
    done('Game and co-op launcher are closed', started)

    started = step('Recognise installed version')
    installed = json.loads((root / 'package-manifest.json').read_text(encoding='utf-8'))
    label, old = recognise(root, installed, sources, target)
    if label == 'current':
        done('Recognise installed version', started, f'already co-op {version}')
        log('  OK  ', f'Nothing to do: this folder is already co-op {version}')
        return 0
    changed = sorted(p for p in target if old.get(p) != target[p])
    removed = sorted(p for p in old if p not in target)
    for p in [p for p in changed + removed if user_area(p)]:
        log(' WARN ', f'Player data or settings, never touched by the updater: {p}')
    changed = [p for p in changed if not user_area(p)]
    removed = [p for p in removed if not user_area(p)]
    runtime = [p for p in changed + removed if p.startswith('.coop-runtime/')]
    if runtime:
        # This updater runs from that Python; it cannot replace itself safely.
        raise Refused(f'This update would change the co-op Python runtime ({runtime[0]}). Nothing was changed. {FULL_PACKAGE}')
    done('Recognise installed version', started, f'{label}: {len(changed)} files to update, {len(removed)} no longer used')

    started = step('Check update files')
    missing = [p for p in changed if not (HERE / 'files' / p).is_file()]
    if missing:
        raise Refused(f'This update does not contain {len(missing)} needed files (for example {missing[0]}). '
                      f'Extract the whole update zip again. Nothing was changed.')
    payload = hash_all([HERE / 'files' / p for p in changed], 'check')
    bad = [p for p in changed if payload[HERE / 'files' / p] != target[p]]
    if bad:
        raise Refused(f'Update file damaged: {bad[0]}. Download the update again. Nothing was changed.')
    done('Check update files', started, f'{len(changed)} files verified')

    started = step('Check files, write access and disk space')
    current = hash_all([root / p for p in changed + removed], 'scan')
    keep = [p for p in removed if current[root / p] not in (None, old[p])]
    delete = [p for p in removed if current[root / p] == old[p]]
    for p in keep:
        log(' WARN ', f'Not deleted (changed since the old release, kept as it is): {p}')
    edited = [p for p in changed if current[root / p] not in (None, old.get(p), target[p])]
    for p in edited[:5]:
        log(' WARN ', f'Changed since the old release, replaced (old copy in the backup): {p}')
    blocked = [p for p in changed + delete + ['package-manifest.json'] if (root / p).exists() and not writable(root / p)]
    if blocked:
        raise Refused(f'{len(blocked)} files are read-only or locked (for example {blocked[0]}). '
                      'Close the game, remove the write protection and run Update-Coop.cmd again. Nothing was changed.')
    backup_bytes = sum((root / p).stat().st_size for p in changed + delete if (root / p).exists())
    largest = max([(HERE / 'files' / p).stat().st_size for p in changed] or [0])
    need = backup_bytes + largest + 64 * 2**20
    free = shutil.disk_usage(root).free
    if free < need:
        raise Refused(f'Not enough free disk space: {free / 2**20:.0f} MiB free, {need / 2**20:.0f} MiB needed. '
                      'Free some space and run Update-Coop.cmd again. Nothing was changed.')
    done('Check files, write access and disk space', started,
         f'{free / 2**30:.1f} GiB free, {need / 2**20:.0f} MiB needed')

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
                shutil.copyfile(HERE / 'files' / p, temporary)
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
    installed_version = (root / 'COOP-VERSION.txt').read_text(encoding='utf-8').strip()
    log(' INFO ', f'Summary: {label} -> co-op {installed_version} in {time.time() - t0:.1f} s; '
                  f'backup in {backup.name} (delete it once the game runs)')
    log('  OK  ', f'Installed co-op version: {installed_version}. Start the game with Start-Coop.cmd.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Refused as error:
        log('FAILED', str(error))
        sys.exit(2)
    except Exception as error:
        log('FAILED', str(error))
        sys.exit(1)
