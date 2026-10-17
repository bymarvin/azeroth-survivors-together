"""Updater tests on disposable copies of every earlier release (boot-log format, isolated per test).

  test_update.py [--update <update zip>] [--work <scratch folder>]

Each test copies a release folder, adds a fake save, extracts the update zip
into it and runs coop-update/update.py with the folder's own Python, exactly
as Update-Coop.cmd does. Locks use exclusive Win32 handles like a running game.
"""
import argparse
import ctypes
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from ctypes import wintypes
from pathlib import Path
from unittest import mock

RELEASES = Path(r'D:\Azeroth Survivors Together Releases')
TARGET = RELEASES / 'stage/Azeroth Survivors Together 0.95.0'
SOURCES = {'0.92.0': RELEASES / 'stage/Azeroth Survivors Together 0.92.0',
           '0.93.1': RELEASES / 'stage/Azeroth Survivors Together 0.93.1',
           '0.95.0-before': Path(r'D:\Azeroth-Survivors-Together-0.95.0.zip')}
SEVEN_ZIP = Path(r'C:\Program Files\7-Zip\7z.exe')
RESULTS = []


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(source):
    if source.suffix == '.zip':
        with zipfile.ZipFile(source) as archive:
            name = next(n for n in archive.namelist() if n.endswith('/package-manifest.json') and n.count('/') == 1)
            return json.loads(archive.read(name))
    return json.loads((source / 'package-manifest.json').read_text(encoding='utf-8'))


def exclusive(path):
    """Open like a running image: no other process may write, rename or delete it."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)  # GENERIC_READ, FILE_SHARE_READ
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return lambda: kernel.CloseHandle(handle)


class Folder:
    def __init__(self, work, name, source, update, extract=True):
        self.root = work / name
        if self.root.exists():
            shutil.rmtree(self.root)
        if source.suffix == '.zip':
            subprocess.run([str(SEVEN_ZIP), 'x', '-y', '-bd', f'-o{work / (name + "-x")}', str(source)],
                           check=True, capture_output=True)
            next((work / (name + '-x')).iterdir()).rename(self.root)
            (work / (name + '-x')).rmdir()
        else:
            shutil.copytree(source, self.root)
        (self.root / 'profiles').mkdir(exist_ok=True)
        (self.root / 'profiles/save.txt').write_text(f'save of {name}', encoding='utf-8')
        if extract:
            subprocess.run([str(SEVEN_ZIP), 'x', '-y', '-bd', f'-o{self.root}', str(update)], check=True,
                           capture_output=True)
        self.before = manifest(self.root)

    def update(self):
        return subprocess.run([str(self.root / '.coop-runtime/python.exe'), '-I', '-B',
                               str(self.root / 'coop-update/update.py')],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)

    def matches(self, expected):
        wrong = [p for p, digest in expected.items()
                 if not (self.root / p).exists() or sha256(self.root / p) != digest]
        assert not wrong, f'{len(wrong)} files differ, first {wrong[0]}'
        assert manifest(self.root) == expected, 'package-manifest.json differs'
        extra = [p for p in self.mod_files() if p not in expected]
        assert not extra, f'leftover files {extra[:3]}'
        assert (self.root / 'profiles/save.txt').read_text(encoding='utf-8').startswith('save of'), 'save changed'

    def mod_files(self):
        skip = ('coop-update', 'profiles', 'coop-update-backup-', 'Update-Coop.cmd', 'README-UPDATE.txt',
                'package-manifest.json', 'Azeroth-Survivors-Together-', 'Downloads/')
        for path in self.root.rglob('*'):
            rel = path.relative_to(self.root).as_posix()
            if path.is_file() and not rel.startswith(skip) and '__pycache__' not in rel:
                yield rel


def test(name, fn):
    log(' .... ', name)
    t = time.time()
    try:
        info = fn()
        log('  OK  ', f'{name} ({time.time() - t:.1f} s{", " + info if info else ""})')
        RESULTS.append((name, True))
    except Exception as error:
        log('FAILED', f'{name}: {type(error).__name__}: {error}')
        for line in traceback.format_exc().splitlines()[-4:]:
            log(' INFO ', line)
        RESULTS.append((name, False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--update', type=Path, default=RELEASES / 'Azeroth-Survivors-Together-0.95.0-update.zip')
    ap.add_argument('--work', type=Path, default=Path(r'D:\Azeroth Survivors Together Update-Test'))
    args = ap.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    target = manifest(TARGET)
    t0 = time.time()

    for label, source in SOURCES.items():
        def run(label=label, source=source):
            folder = Folder(args.work, label, source, args.update)
            result = folder.update()
            assert result.returncode == 0, result.stdout[-400:]
            assert f'Recognise installed version' in result.stdout and label.split('-')[0] in result.stdout, result.stdout[-400:]
            folder.matches(target)
            again = folder.update()
            assert again.returncode == 0 and 'Nothing to do' in again.stdout, again.stdout[-300:]
            backup = next(folder.root.glob('coop-update-backup-*'))
            kept = [p for p in target if p in folder.before and folder.before[p] != target[p]]
            assert all(sha256(backup / p) == folder.before[p] for p in kept), 'backup differs from the old files'
            return f'{len([p for p in target if folder.before.get(p) != target[p]])} files, backup {len(kept)}, second run no-op'
        test(f'Update from {label}', run)

    q = chr(34)

    def refused(folder, result, text):
        assert result.returncode == 2 and text in result.stdout, result.stdout[-400:]
        folder.matches(folder.before)
        assert not list(folder.root.glob('coop-update-backup-*')), 'backup made although refused'

    def running_game():
        folder = Folder(args.work, 'running', SOURCES['0.93.1'], args.update)
        close = exclusive(folder.root / 'AzerothSurvivors.exe')
        try:
            refused(folder, folder.update(), 'is in use')
        finally:
            close()
        return 'refused, nothing changed'
    test('Running game (locked exe) is refused', running_game)

    def running_launcher():
        folder = Folder(args.work, 'launcher', SOURCES['0.93.1'], args.update)
        launcher = subprocess.Popen([str(folder.root / '.coop-runtime/python.exe'), '-c', 'import time; time.sleep(60)'])
        try:
            time.sleep(1)
            refused(folder, folder.update(), 'Still running from this folder: python.exe')
        finally:
            launcher.kill()
            launcher.wait()
        return 'co-op launcher process found, nothing changed'
    test('Running co-op launcher is refused', running_launcher)

    def locked_file():
        folder = Folder(args.work, 'locked', SOURCES['0.93.1'], args.update)
        close = exclusive(folder.root / 'reconstruction/world_packets.py')
        try:
            refused(folder, folder.update(), 'read-only or locked')
        finally:
            close()
        return 'refused before any change'
    test('Locked file is refused', locked_file)

    def unknown_manifest():
        folder = Folder(args.work, 'unknown', SOURCES['0.93.1'], args.update)
        changed = dict(folder.before, **{'reconstruction/lobby.py': '0' * 64})
        (folder.root / 'package-manifest.json').write_text(json.dumps(changed), encoding='utf-8')
        folder.before = changed
        result = folder.update()
        assert result.returncode == 2 and 'matches no release' in result.stdout, result.stdout[-400:]
        assert not list(folder.root.glob('coop-update-backup-*'))
        return 'unknown release refused'
    test('Unknown installation is refused', unknown_manifest)

    def modified_exe():
        folder = Folder(args.work, 'modified', SOURCES['0.92.0'], args.update)
        with open(folder.root / 'AzerothSurvivors.exe', 'ab') as stream:
            stream.write(b'\0')
        result = folder.update()
        assert result.returncode == 2 and 'was changed' in result.stdout, result.stdout[-400:]
        assert not list(folder.root.glob('coop-update-backup-*'))
        return 'modified game refused'
    test('Modified game exe is refused', modified_exe)

    def too_old():
        old = Path('D:/Azeroth Survivors Together 010')
        source = args.work / 'too-old-source'
        if source.exists():
            shutil.rmtree(source)
        source.mkdir()
        for name in ('AzerothSurvivors.exe', 'package-manifest.json', 'COOP-VERSION.txt'):
            shutil.copy2(old / name, source / name)
        shutil.copytree(old / '.coop-runtime', source / '.coop-runtime')
        folder = Folder(args.work, 'too-old', source, args.update)
        result = folder.update()
        assert result.returncode == 2 and 'too old' in result.stdout, result.stdout[-400:]
        assert not list(folder.root.glob('coop-update-backup-*'))
        return 'co-op 0.0.10 / game 0.84 sent to the full package'
    test('Co-op 0.0.10 (game 0.84) is sent to the full package', too_old)

    def injected(folder, target_name, nth, error):
        import shutil as module
        real, calls = getattr(module, target_name), []
        def failing(*a, **k):
            calls.append(a)
            if len(calls) == nth:
                raise error
            return real(*a, **k)
        with mock.patch(f'shutil.{target_name}', failing), mock.patch.object(sys, 'argv', ['update.py']):
            try:
                runpy.run_path(str(folder.root / 'coop-update/update.py'), run_name='__main__')
            except SystemExit as exit:
                assert exit.code == 1, exit.code
            else:
                raise AssertionError('update did not fail')
        folder.matches(folder.before)
        leftovers = [p for pattern in ('*.coop-new', '*.coop-restore') for p in folder.root.rglob(pattern)]
        assert not leftovers, leftovers[:3]

    def disk_full_installing():
        import errno
        folder = Folder(args.work, 'full-install', SOURCES['0.92.0'], args.update)
        injected(folder, 'copyfile', 12, OSError(errno.ENOSPC, 'No space left on device (injected)'))
        return 'disk full at file 12, folder restored to 0.92.0'
    test('Disk full while installing restores the previous version', disk_full_installing)

    def disk_full_backup():
        import errno
        folder = Folder(args.work, 'full-backup', SOURCES['0.93.1'], args.update)
        injected(folder, 'copy2', 5, OSError(errno.ENOSPC, 'No space left on device (injected)'))
        return 'disk full while backing up, nothing changed'
    test('Disk full while backing up changes nothing', disk_full_backup)

    def mini_release(where, name, runtime, listed_user_files=False):
        """A tiny fake release pair (removed and user-edited files) built with make_update."""
        base = args.work / 'mini'
        source, target_dir = base / 'release-1', base / 'release-2'
        if base.exists():
            shutil.rmtree(base)
        files1 = {'AzerothSurvivors.exe': b'exe 1', 'COOP-VERSION.txt': b'mini-1', 'reconstruction/a.py': b'a = 1',
                  'Data/x.MPQ': b'mpq 1', 'gone.py': b'old', 'gone_edited.py': b'old edited'}
        files2 = {'AzerothSurvivors.exe': b'exe 2', 'COOP-VERSION.txt': b'mini-2', 'reconstruction/a.py': b'a = 2',
                  'Data/x.MPQ': b'mpq 2', 'reconstruction/new.py': b'new'}
        if listed_user_files:  # A manifest that (wrongly) lists player settings, addons and logs.
            files1.update({'WTF/Config.wtf': b'SET old', 'Interface/AddOns/MyMeter/MyMeter.lua': b'addon 1',
                           'logs/old.log': b'log'})
            files2.update({'WTF/Config.wtf': b'SET new', 'Interface/AddOns/MyMeter/MyMeter.lua': b'addon 2'})
        for folder, files in ((source, files1), (target_dir, files2)):
            for rel, data in files.items():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_bytes(data)
            (folder / 'package-manifest.json').write_text(json.dumps(
                {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}), encoding='utf-8')
        built = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'port-7264/make_update.py'),
                                '--target', str(target_dir), '--source', f'Mini 1={source}', '--out', str(base),
                                '--name', 'mini-update', '--no-exe'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        assert built.returncode == 0, built.stdout[-400:]
        game = where / name
        if game.exists():
            shutil.rmtree(game)
        shutil.copytree(source, game)
        (game / 'gone_edited.py').write_bytes(b'my own change')
        (game / 'WTF').mkdir(exist_ok=True)
        (game / 'WTF/Config.wtf').write_text('SET gxResolution "1920x1080"', encoding='utf-8')
        if listed_user_files:
            (game / 'Interface/AddOns/MyMeter/MyMeter.lua').write_bytes(b'my addon tweaks')
        if runtime:
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(game / '.coop-runtime'),
                            str(Path(sys.executable).parent)], check=True, capture_output=True)
        subprocess.run([str(SEVEN_ZIP), 'x', '-y', '-bd', f'-o{game}', str(base / 'mini-update.zip')],
                       check=True, capture_output=True)
        return game, files2

    def explorer(game):
        # The command line a double-click in Explorer uses, plus --no-pause for the test.
        return 'cmd /c ' + q + q + str(game / 'Update-Coop.cmd') + q + ' --no-pause' + q

    def check_mini(game, files2, result):
        assert result.returncode == 0, result.stdout[-500:]
        for rel, data in files2.items():
            assert (game / rel).read_bytes() == data, rel
        assert not (game / 'gone.py').exists(), 'unchanged old file was not deleted'
        assert (game / 'gone_edited.py').read_bytes() == b'my own change', 'edited old file was deleted'
        assert 'Not deleted (changed since the old release' in result.stdout, result.stdout[-500:]
        assert (game / 'WTF/Config.wtf').read_text(encoding='utf-8').startswith('SET gx'), 'WTF settings touched'
        assert 'Installed co-op version: mini-2' in result.stdout, result.stdout[-300:]

    def removal_rule():
        game, files2 = mini_release(args.work, 'mini-game', True)
        result = subprocess.run(explorer(game), capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120)
        check_mini(game, files2, result)
        return 'old file deleted, edited old file kept, WTF untouched, via Update-Coop.cmd'
    test('Removal rule and user files (Update-Coop.cmd)', removal_rule)

    def user_areas_listed_in_manifests():
        """Even if a release manifest listed player settings, the updater never writes or deletes them."""
        game, files2 = mini_release(args.work, 'mini-user', True, listed_user_files=True)
        addon = game / 'Interface/AddOns/MyMeter/MyMeter.lua'
        before = {p: (game / p).read_bytes() for p in ('WTF/Config.wtf', 'Interface/AddOns/MyMeter/MyMeter.lua',
                                                        'logs/old.log')}
        result = subprocess.run(explorer(game), capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120)
        assert result.returncode == 0, result.stdout[-500:]
        after = {p: (game / p).read_bytes() for p in before}
        assert after == before, 'player data changed'
        assert result.stdout.count('never touched by the updater') == 3, result.stdout[-600:]
        assert addon.exists()
        return 'WTF, addon and log byte-identical although listed'
    test('Player data listed in a manifest stays byte-identical', user_areas_listed_in_manifests)

    def runtime_change_refused():
        game, files2 = mini_release(args.work, 'mini-runtime', False)
        tool = game / 'coop-update'
        target = json.loads((tool / 'target-manifest.json').read_text(encoding='utf-8'))
        target['.coop-runtime/Lib/site.py'] = '1' * 64  # As if a release changed the running Python.
        (tool / 'target-manifest.json').write_text(json.dumps(target), encoding='utf-8')
        before = {p.relative_to(game).as_posix(): p.read_bytes() for p in game.rglob('*')
                  if p.is_file() and not p.relative_to(game).as_posix().startswith('coop-update')}
        result = subprocess.run([sys.executable, '-I', '-B', str(tool / 'update.py')], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120)
        assert result.returncode == 2 and 'co-op Python runtime' in result.stdout, result.stdout[-400:]
        after = {p.relative_to(game).as_posix(): p.read_bytes() for p in game.rglob('*')
                 if p.is_file() and not p.relative_to(game).as_posix().startswith('coop-update')}
        assert after == before, 'files changed although refused'
        return 'refused, sent to the full package'
    test('A change to the running Python runtime is refused', runtime_change_refused)

    umlauts = 'Spiel Ordner ' + chr(228) + chr(246) + chr(252) + ' ' + chr(223)
    places = [(args.work / umlauts, 'Azeroth Survivors Together (Test) & Co', True),
              (Path.home() / 'Desktop' / ('Spiele ' + chr(196)), 'Azeroth Survivors Together', True),
              (Path.home() / 'OneDrive' / ('AST Update-Test ' + chr(228) + chr(246) + chr(252)), 'Azeroth Survivors Together', False)]
    for where, name, runtime in places:
        def place(where=where, name=name, runtime=runtime):
            where.mkdir(parents=True, exist_ok=True)
            try:
                game, files2 = mini_release(where, name, runtime)
                if runtime:
                    command = explorer(game)
                else:  # No 100 MB runtime into OneDrive: the same updater with this Python.
                    command = [sys.executable, '-I', '-B', str(game / 'coop-update/update.py')]
                result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                        timeout=120)
                check_mini(game, files2, result)
                return str(game)
            finally:
                shutil.rmtree(where, ignore_errors=True)
        test(f'Path {where.parent.name}/{where.name}/{name}', place)

    exe = args.update.with_suffix('.exe')

    def run_exe(path, cwd):
        return subprocess.run([str(path), '--no-pause'], cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300)

    def exe_update():
        folder = Folder(args.work, 'exe', SOURCES['0.93.1'], args.update, extract=False)
        shutil.copy2(exe, folder.root / exe.name)
        result = run_exe(folder.root / exe.name, folder.root)
        assert result.returncode == 0 and 'Installed co-op version: 0.0.11' in result.stdout, result.stdout[-500:]
        folder.matches(target)
        assert not list(folder.root.glob('coop-update-*.zip')), 'embedded zip left behind'
        again = run_exe(folder.root / exe.name, folder.root)
        assert again.returncode == 0 and 'Nothing to do' in again.stdout, again.stdout[-300:]
        return f'{exe.stat().st_size / 2**20:.0f} MiB exe in the game folder, second run no-op'
    test('Update with the one-file exe (0.93.1)', exe_update)

    def exe_in_subfolder():
        folder = Folder(args.work, 'exe-sub', SOURCES['0.95.0-before'], args.update, extract=False)
        (folder.root / 'Downloads').mkdir()
        shutil.copy2(exe, folder.root / 'Downloads' / exe.name)
        result = run_exe(folder.root / 'Downloads' / exe.name, folder.root / 'Downloads')
        assert result.returncode == 0 and 'Installed co-op version' in result.stdout, result.stdout[-500:]
        folder.matches(target)
        return 'found the game folder one level up'
    test('Exe started from a subfolder of the game folder', exe_in_subfolder)

    def exe_elsewhere():
        place = args.work / 'not-a-game'
        place.mkdir(exist_ok=True)
        shutil.copy2(exe, place / exe.name)
        result = run_exe(place / exe.name, place)
        assert result.returncode == 2 and 'not an Azeroth Survivors Together co-op folder' in result.stdout, result.stdout
        assert sorted(p.name for p in place.iterdir()) == [exe.name], 'wrote files outside a game folder'
        return 'refused, wrote nothing'
    test('Exe outside a game folder refuses', exe_elsewhere)

    passed = sum(ok for _, ok in RESULTS)
    log(' INFO ', f'Zusammenfassung: {passed}/{len(RESULTS)} bestanden ({time.time() - t0:.0f} s)')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
