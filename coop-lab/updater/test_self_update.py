"""Self-updater tests with local fake release servers (boot-log format, isolated per test).

  test_self_update.py [--work <scratch folder>] [--skip-exe]

Two local HTTP servers stand in for GitHub (signed latest.json, manifest,
archives, updater exe) and the external host of the game files. They support
HTTP Range and can cut connections, tamper with files or redirect. Test
releases are signed with throwaway test keys, never with the release key.
"""
import argparse
import base64
import contextlib
import errno
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'reconstruction'), str(Path(__file__).resolve().parent)]
import self_update  # noqa: E402
import sign_release  # noqa: E402
import game_patch  # noqa: E402
import updater_v2  # noqa: E402  (extends self_update; supersets for v1 manifests)
GENERATOR = Path(__file__).resolve().parents[3] / 'Azeroth Survivors Together Releases/stage/patchgen-cargo/release/azpatch-gen.exe'

RESULTS = []
OLD = '0.0.11-community+game-7264d50b'
NEW = '0.0.12-community+game-7264d50b'
FULL = 'https://example.invalid/Azeroth-Survivors-Together-0.0.12.zip'
RUNTIME = Path(sys.executable).parent
PADDED = RUNTIME.joinpath('python.exe').read_bytes() + bytes(512)  # Still a valid exe, other SHA-256.


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def test(name, fn):
    log(' .... ', name)
    t = time.time()
    try:
        info = fn()
        log('  OK  ', f'{name} ({time.time() - t:.1f} s{", " + info if info else ""})')
        RESULTS.append((name, True))
    except Exception as error:
        log('FAILED', f'{name}: {type(error).__name__}: {error}')
        for line in traceback.format_exc().splitlines()[-5:]:
            log(' INFO ', line)
        RESULTS.append((name, False))


class Server:
    """Static files with HTTP Range, plus fault rules per file name."""

    def __init__(self, root):
        self.root, self.cut, self.tamper, self.redirect, self.ranges = Path(root), {}, {}, {}, []
        self.status, self.types, self.hits = {}, {}, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.lstrip('/')
                server.hits.append(name)
                if name in server.status:
                    self.send_error(server.status[name])
                    return
                if name in server.redirect:
                    self.send_response(302)
                    self.send_header('Location', server.redirect[name])
                    self.end_headers()
                    return
                path = server.root / name
                if not path.is_file():
                    self.send_error(404)
                    return
                data = server.tamper.get(name, path.read_bytes())
                start = 0
                header = self.headers.get('Range')
                if header:
                    server.ranges.append((name, header))
                    start = int(header.split('=')[1].split('-')[0])
                    if start >= len(data):
                        self.send_response(416)
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
                else:
                    self.send_response(200)
                body = data[start:]
                if name in server.types:
                    self.send_header('Content-Type', server.types[name])
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                rule = server.cut.get(name)
                if rule and rule['times'] > 0:
                    rule['times'] -= 1
                    self.wfile.write(body[:rule['after']])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                with contextlib.suppress(OSError):  # make_release probes only read the first KiB.
                    self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}/'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def write_package(folder, files):
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    (folder / 'package-manifest.json').write_text(
        json.dumps({rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}), encoding='utf-8')


def packages():
    big1, big2 = os.urandom(3 << 20), os.urandom(3 << 20)
    v1 = {'AzerothSurvivors.exe': b'game exe 1', 'Data/big.MPQ': big1, 'Data/same.MPQ': b'same mpq',
          'COOP-VERSION.txt': OLD.encode(), 'reconstruction/a.py': b'a = 1', 'reconstruction/old.py': b'old',
          'relay/client.py': b'relay', '.coop-runtime/marker.txt': b'runtime'}
    v2 = {'AzerothSurvivors.exe': b'game exe 2', 'Data/big.MPQ': big2, 'Data/same.MPQ': b'same mpq',
          'COOP-VERSION.txt': NEW.encode(), 'reconstruction/a.py': b'a = 2', 'reconstruction/b.py': b'b = 2',
          'relay/client.py': b'relay', '.coop-runtime/marker.txt': b'runtime'}
    return v1, v2


class Lab:
    """A key, a v2 release on two fake servers and a v1 game folder."""

    def __init__(self, work, name, key='key-1', version_files=None, min_updater=1, updater=b'updater exe v2',
                 sequence=100, announce=(), retire=(), hosted=True):
        self.hosted = hosted
        self.work = work / name
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True)
        self.v1, self.v2 = version_files or packages()
        self.keys = work / 'keys'
        for k in ('key-1', 'key-2', 'other'):
            if not (self.keys / k / sign_release.CURRENT).exists():
                sign_release.create(self.keys / k, sign_release.CURRENT)
        self.pub = {k: sign_release.public(sign_release.load(self.keys / k)) for k in ('key-1', 'key-2', 'other')}
        self.github, self.external = Server(self.work / 'github'), Server(self.work / 'external')
        self.release(key, min_updater, updater, sequence, announce, retire)
        self.game = self.work / 'game'
        write_package(self.game, self.v1)
        (self.game / 'WTF').mkdir()
        (self.game / 'WTF/Config.wtf').write_bytes(b'SET gxResolution "1920x1080"')
        (self.game / 'saves').mkdir()
        (self.game / 'saves/hero.sav').write_bytes(b'my hero')

    def release(self, key='key-1', min_updater=1, updater=b'updater exe v2', sequence=100, announce=(), retire=(),
                files=None, extra=(), expect=0, gamefiles=None):
        target = self.work / 'package'
        if target.exists():
            shutil.rmtree(target)
        write_package(target, files or self.v2)
        sources = {}
        for p in (files or self.v2):
            if self.hosted and (self_update.EXE == p or p.startswith('Data/')):
                (self.work / 'external' / p).parent.mkdir(parents=True, exist_ok=True)
                (self.work / 'external' / p).write_bytes((files or self.v2)[p])
                sources[p] = self.external.url + p
        (self.work / 'game-sources.json').write_text(json.dumps(sources), encoding='utf-8')
        exe = self.work / 'updater.exe'
        exe.write_bytes(updater)
        out = self.work / 'github'
        shutil.rmtree(out, ignore_errors=True)
        command = [sys.executable, str(ROOT / 'coop-lab/port-7264/make_release.py'), '--target', str(target),
                   '--game-sources', str(self.work / 'game-sources.json'), '--updater-exe', str(exe),
                   '--min-updater', str(min_updater), '--sequence', str(sequence), '--key-dir', str(self.keys / key),
                   '--out', str(out), '--allow-local-http', '--updater-version', str(max(min_updater, 1))]
        for k in announce:
            command += ['--announce-key', self.pub[k]]
        for k in retire:
            command += ['--retire-key', self.pub[k]]
        command += ['--full-package-url', FULL, *extra]
        if gamefiles:
            served = self.work / 'external' / 'gf'
            served.mkdir(parents=True, exist_ok=True)
            for asset in Path(gamefiles).iterdir():
                shutil.copy2(asset, served / asset.name)
            command += ['--gamefiles', str(gamefiles), '--gamefiles-url', self.external.url + 'gf/']
        if not self.hosted:
            (self.work / 'external').mkdir(parents=True, exist_ok=True)
            (self.work / 'external' / 'releases.html').write_bytes(b'<!DOCTYPE html><html>Release page</html>')
            self.external.types['releases.html'] = 'text/html; charset=utf-8'
            command += ['--unhosted-source', self.external.url + 'releases.html']
        built = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
        assert built.returncode == expect, built.stdout[-500:]
        return built.stdout

    def run(self, *extra, trusted=('key-1',), updater_version=1, exe=None, url=None):
        argv = ['--game', str(self.game), '--release-url', url or self.github.url, '--allow-local-http',
                '--updater-version', str(updater_version), '--timeout', '3', *extra]
        if exe:
            argv += ['--updater-exe', str(exe)]
        self_update.LOG = None
        self_update.IGNORED_PIDS.clear()
        main = updater_v2.main if updater_version >= 2 else self_update.main
        return main(argv, builtin=tuple(self.pub[k] for k in trusted))

    def snapshot(self):
        return {p.relative_to(self.game).as_posix(): p.read_bytes() for p in self.game.rglob('*')
                if p.is_file() and not p.relative_to(self.game).as_posix().startswith('coop-update')}

    def is_version(self, files):
        for rel, data in files.items():
            assert (self.game / rel).read_bytes() == data, f'{rel} differs'
        assert (self.game / 'WTF/Config.wtf').read_bytes() == b'SET gxResolution "1920x1080"', 'WTF touched'
        assert (self.game / 'saves/hero.sav').read_bytes() == b'my hero', 'save touched'

    def close(self):
        self.github.close()
        self.external.close()


def refused(lab, kind, text, **run):
    before = lab.snapshot()
    try:
        lab.run(**run)
    except kind as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError(f'not refused ({text})')
    assert lab.snapshot() == before, 'folder changed although refused'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', type=Path, default=Path(r'D:\Azeroth Survivors Together Update-Test\self-update'))
    ap.add_argument('--skip-exe', action='store_true')
    args = ap.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    def valid_update():
        lab = Lab(args.work, 'valid')
        try:
            assert lab.run() == 0
            lab.is_version(lab.v2)
            assert not (lab.game / 'reconstruction/old.py').exists(), 'unchanged old file not deleted'
            state = json.loads((lab.game / self_update.STATE).read_text(encoding='utf-8'))
            assert state['sequence'] == 100 and state['version'] == NEW, state
            assert json.loads((lab.game / 'package-manifest.json').read_text()) == \
                {p: hashlib.sha256(d).hexdigest() for p, d in lab.v2.items()}
            downloaded = [n for n, _ in lab.github.ranges]
            assert lab.run() == 0  # Second run: nothing to download.
            return 'mod archive + external game files, old file deleted, WTF/saves untouched, second run no-op'
        finally:
            lab.close()
    test('Signed release updates the folder', valid_update)

    def bad_signature():
        lab = Lab(args.work, 'badsig')
        try:
            sig = lab.work / 'github/latest.json.sig'
            data = bytearray(sig.read_bytes())
            data[0] ^= 1
            sig.write_bytes(bytes(data))
            refused(lab, self_update.Refused, 'not signed by a trusted key')
            return 'one flipped bit refused'
        finally:
            lab.close()
    test('Invalid signature is refused', bad_signature)

    def untrusted_key():
        lab = Lab(args.work, 'untrusted', key='other')
        try:
            refused(lab, self_update.Refused, 'not signed by a trusted key')
            return 'release signed with an unknown key refused'
        finally:
            lab.close()
    test('Untrusted signing key is refused', untrusted_key)

    def tampered_file():
        lab = Lab(args.work, 'tampered')
        try:
            latest = json.loads((lab.work / 'github/latest.json').read_text())
            name = latest['archives']['mod']['asset']
            lab.github.tamper[name] = (lab.work / 'github' / name).read_bytes()[:-1] + b'X'
            refused(lab, self_update.Refused, 'does not match its signed SHA-256')
            lab.github.tamper.clear()
            for staged in lab.game.glob('coop-update/release-*'):  # Verified files from the first run stay cached.
                shutil.rmtree(staged)
            lab.external.tamper['Data/big.MPQ'] = os.urandom(3 << 20)
            refused(lab, self_update.Refused, 'does not match its signed SHA-256')
            return 'tampered mod archive and tampered game file refused before install'
        finally:
            lab.close()
    test('Tampered download is refused', tampered_file)

    def resume():
        lab = Lab(args.work, 'resume')
        try:
            lab.external.cut['Data/big.MPQ'] = dict(after=1 << 20, times=1)
            assert lab.run() == 0
            lab.is_version(lab.v2)
            assert ('Data/big.MPQ', 'bytes=1048576-') in lab.external.ranges, lab.external.ranges
            return 'cut at 1 MiB, continued with Range'
        finally:
            lab.close()
    test('Interrupted download continues (HTTP Range)', resume)

    def resume_next_run():
        lab = Lab(args.work, 'resume-later')
        try:
            lab.external.cut['Data/big.MPQ'] = dict(after=512 << 10, times=3)  # Three attempts, 1.5 of 3 MiB.
            refused(lab, self_update.Offline, 'run the updater again')
            part = next(lab.game.glob('coop-update/release-*/files/Data/big.MPQ.part'))
            assert part.stat().st_size == 3 * (512 << 10), part.stat().st_size
            lab.external.cut.clear()
            lab.external.ranges.clear()
            assert lab.run() == 0
            assert lab.external.ranges == [('Data/big.MPQ', f'bytes={3 * (512 << 10)}-')], lab.external.ranges
            lab.is_version(lab.v2)
            return '3 cuts -> offline, nothing changed; next run resumed at 1.5 MiB'
        finally:
            lab.close()
    test('Download continues in the next run', resume_next_run)

    def downgrade():
        lab = Lab(args.work, 'downgrade')
        try:
            (lab.game / 'COOP-VERSION.txt').write_text('0.0.13-community+game-7264d50b', encoding='utf-8')
            files = dict(lab.v1, **{'COOP-VERSION.txt': b'0.0.13-community+game-7264d50b'})
            write_package(lab.game, files)
            refused(lab, self_update.Refused, 'older than the installed')
            return 'offered 0.0.12 over installed 0.0.13 refused'
        finally:
            lab.close()
    test('Downgrade is refused', downgrade)

    def replay():
        lab = Lab(args.work, 'replay')
        try:
            (lab.game / self_update.STATE).write_text(json.dumps(dict(sequence=200, signed_at='2030-01-01T00:00:00Z')))
            refused(lab, self_update.Refused, 'older signed release')
            return 'an older signed latest.json (sequence 100 < 200) refused'
        finally:
            lab.close()
    test('Replay of an older signed release is refused', replay)

    def offline():
        lab = Lab(args.work, 'offline')
        try:
            url = lab.github.url
            lab.close()
            refused(lab, self_update.Offline, 'not reachable', url=url)
            started = time.time()
            status = self_update.check(lab.game, url, timeout=3, allow_local_http=True,
                                       builtin=(lab.pub['key-1'],))
            assert status == ('offline', None) and time.time() - started < 3.5, status
            return 'updater: nothing changed; launcher check: offline in under 3 s'
        finally:
            with contextlib.suppress(Exception):
                lab.close()
    test('Offline: nothing changes, the launcher goes on', offline)

    def https_only():
        net = self_update.Net('https://example.invalid/')
        for url in ('http://example.invalid/x', 'file:///C:/Windows/win.ini', 'ftp://example.invalid/x'):
            try:
                net.check_url(url)
            except self_update.Refused:
                continue
            raise AssertionError(f'accepted {url}')
        lab = Lab(args.work, 'redirect')
        try:
            name = 'Data/big.MPQ'
            lab.external.redirect[name] = 'http://example.invalid/big.MPQ'  # Downgrade to plain HTTP.
            refused(lab, self_update.Refused, 'non-HTTPS')
            return 'http/file/ftp and a redirect to plain HTTP refused'
        finally:
            lab.close()
    test('Only HTTPS, also after redirects', https_only)

    def self_update_python():
        lab = Lab(args.work, 'self-python', min_updater=2, updater=b'new updater exe')
        try:
            exe = lab.game / self_update.UPDATER_ASSET
            exe.write_bytes(b'running updater exe')
            before = lab.snapshot()
            assert lab.run(exe=exe) == self_update.SELF_UPDATE_READY
            assert exe.with_name(exe.name + '.new').read_bytes() == b'new updater exe'
            after = lab.snapshot()
            after.pop(exe.name + '.new')
            assert after == before, 'files changed before the updater was replaced'
            refused(lab, self_update.Refused, 'needs updater 2')  # Without an exe: tell the player.
            return 'verified new exe saved as .new, exit 3, game folder unchanged'
        finally:
            lab.close()
    test('Release needing a newer updater fetches it first', self_update_python)

    def rollback():
        lab = Lab(args.work, 'rollback')
        try:
            before = lab.snapshot()
            real, calls = shutil.copyfile, []
            def failing(*a, **k):
                if str(a[1]).endswith('.coop-new'):
                    calls.append(a)
                    if len(calls) == 3:
                        raise OSError(errno.ENOSPC, 'No space left on device (injected)')
                return real(*a, **k)
            with mock.patch('shutil.copyfile', failing):
                try:
                    lab.run()
                except OSError:
                    pass
                else:
                    raise AssertionError('update did not fail')
            after = lab.snapshot()
            assert {k: v for k, v in after.items() if not k.startswith('coop-update-backup-')} == before
            return 'disk full at the 3rd file, folder restored'
        finally:
            lab.close()
    test('Failure while installing restores the previous version', rollback)

    def key_rotation():
        lab = Lab(args.work, 'rotation', key='key-1', announce=('key-2',), sequence=100)
        try:
            assert lab.run() == 0
            trust = json.loads((lab.game / self_update.TRUST).read_text())
            assert trust['added'] == [lab.pub['key-2']], trust
            files3 = dict(lab.v2, **{'COOP-VERSION.txt': b'0.0.13-community+game-7264d50b', 'reconstruction/a.py': b'a = 3'})
            lab.release(key='key-2', sequence=200, retire=('key-1',), files=files3)
            assert lab.run() == 0  # Only key-1 is built in; key-2 came signed by key-1.
            lab.is_version(files3)
            files4 = dict(files3, **{'COOP-VERSION.txt': b'0.0.14-community+game-7264d50b'})
            lab.release(key='key-1', sequence=300, files=files4)
            refused(lab, self_update.Refused, 'not signed by a trusted key')
            return 'key-2 announced by key-1 accepted; key-1 retired by key-2 then refused'
        finally:
            lab.close()
    test('Key rotation: announce, switch, retire', key_rotation)

    def launcher_check():
        lab = Lab(args.work, 'check')
        try:
            pub = (lab.pub['key-1'],)
            status = self_update.check(lab.game, lab.github.url, allow_local_http=True, builtin=pub)
            assert status == ('newer', NEW), status
            assert lab.run() == 0
            status = self_update.check(lab.game, lab.github.url, allow_local_http=True, builtin=pub)
            assert status == ('same', NEW), status
            status = self_update.check(lab.game, lab.github.url, allow_local_http=True, builtin=(lab.pub['other'],))
            assert status == ('invalid', None), status
            launcher = (ROOT / 'coop_launcher.py').read_text(encoding='utf-8')
            assert 'check_for_update(' in launcher and 'worker.join(wait)' in launcher
            return 'newer / same / invalid; the launcher waits at most 3 s'
        finally:
            lab.close()
    test('Launcher start check', launcher_check)

    def player_data_in_manifest():
        lab = Lab(args.work, 'userdata')
        try:
            target = {p: hashlib.sha256(d).hexdigest() for p, d in lab.v2.items()}
            target['WTF/Config.wtf'] = '0' * 64
            target['saves/hero.sav'] = '1' * 64
            needed, delete = self_update.plan(lab.game, target, {})
            assert not any(p.startswith(('WTF/', 'saves/')) for p in needed + delete), needed
            return 'WTF and saves listed in a manifest are never planned'
        finally:
            lab.close()
    test('Player data in a manifest is never touched', player_data_in_manifest)

    def unsafe_paths():
        net = self_update.Net('https://example.invalid/')
        entry = dict(sha256='0' * 64, size=1, source=dict(archive='mod'))
        for bad in ('../evil.txt', 'Data/../../evil.txt', 'C:/Windows/evil.dll', 'C:evil.txt', '/abs.txt',
                    'Data/x.MPQ:hidden', 'CON', 'Data/nul.txt', 'com1', 'LPT\u00b9.log', 'a//b', 'dir/x.', 'dir/x ',
                    'a' + chr(92) + 'b', 'x' + chr(1), 'a/*.MPQ', 'x' * 241):
            try:
                self_update.validate_files({'files': {bad: entry}}, net)
            except self_update.Refused:
                continue
            raise AssertionError(f'accepted {bad!r}')
        work = args.work / 'zip'
        shutil.rmtree(work, ignore_errors=True)
        (work / 'out').mkdir(parents=True)
        archive = work / 'evil.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('ok.txt', b'fine')
            z.writestr('../evil.txt', b'evil')
            link = zipfile.ZipInfo('link.txt')
            link.external_attr = 0o120777 << 16  # Unix symlink.
            z.writestr(link, b'../../outside')
            z.writestr('big.txt', b'A' * (1 << 20))
            z.writestr('bomb.txt', b'B' * (1 << 20))
        data = bytearray(archive.read_bytes())
        at = 0
        while (at := data.find(b'PK\x01\x02', at)) >= 0:  # Lie about the size of bomb.txt in both headers.
            length = int.from_bytes(data[at + 28:at + 30], 'little')
            if data[at + 46:at + 46 + length] == b'bomb.txt':
                local = int.from_bytes(data[at + 42:at + 46], 'little')
                data[at + 24:at + 28] = data[local + 22:local + 26] = (10).to_bytes(4, 'little')
            at += 4
        archive.write_bytes(bytes(data))
        digest = lambda b: hashlib.sha256(b).hexdigest()
        with zipfile.ZipFile(archive) as z:
            self_update.extract_member(z, 'ok.txt', 4, digest(b'fine'), work / 'out/ok.txt')
            for name, size, content in (('link.txt', 13, b'../../outside'), ('big.txt', 10, b'A' * 10),
                                        ('bomb.txt', 10, b'B' * 10), ('missing.txt', 1, b'x')):
                try:
                    self_update.extract_member(z, name, size, digest(content), work / 'out' / name)
                except self_update.Refused:
                    continue
                raise AssertionError(f'extracted {name}')
        assert not (work / 'evil.txt').exists() and not (args.work / 'evil.txt').exists()
        lab = Lab(args.work, 'junction')
        try:
            outside = lab.work / 'outside'
            outside.mkdir()
            shutil.rmtree(lab.game / 'reconstruction')
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(lab.game / 'reconstruction'), str(outside)],
                           check=True, capture_output=True)
            refused(lab, self_update.Refused, 'outside the game folder')
            assert not any(outside.iterdir()), 'wrote through the junction'
        finally:
            lab.close()
        return '17 unsafe paths, link/oversized/lying/missing members and a junction out of the folder refused'
    test('Unsafe paths and archive members are refused', unsafe_paths)

    def redirects():
        lab = Lab(args.work, 'redirects')
        try:
            url = lab.github.url
            (lab.work / 'github/hop6').write_bytes(b'end')
            lab.github.redirect.update({f'hop{i}': f'{url}hop{i + 1}' for i in range(6)})
            net = self_update.Net(url, timeout=3, allow_local_http=True)
            assert net.fetch(url + 'hop1', 100) == b'end'  # 5 redirects.
            try:
                net.fetch(url + 'hop0', 100)  # 6 redirects.
            except self_update.Refused as error:
                assert 'Too many redirects' in str(error), error
            else:
                raise AssertionError('6 redirects accepted')
            latest = json.loads((lab.work / 'github/latest.json').read_text())
            name = latest['archives']['mod']['asset']
            lab.github.redirect[name] = url.replace('127.0.0.1', 'localhost') + name  # Another host name.
            refused(lab, self_update.Refused, 'not allowed')
            lab.github.redirect.clear()
            for staged in lab.game.glob('coop-update/release-*'):  # Verified files from the last run stay cached.
                shutil.rmtree(staged)
            lab.external.redirect['Data/big.MPQ'] = 'https://cdn.example.invalid/big.MPQ'
            refused(lab, self_update.NeedFullPackage, 'not allowed')
            return '5 redirects followed, 6 refused; redirects to unlisted hosts refused (GitHub asset, game file)'
        finally:
            lab.close()
    test('Redirects only to allowed hosts, at most 5', redirects)

    def captcha():
        lab = Lab(args.work, 'captcha')
        try:
            page = b'<!DOCTYPE html><html><body>Please solve the captcha</body></html>'
            cases = (('a download or captcha page', lambda: lab.external.tamper.update({'Data/big.MPQ': page})),
                     ('403', lambda: lab.external.status.update({'Data/big.MPQ': 403})),
                     ('Content-Type text/html', lambda: lab.external.types.update({'Data/big.MPQ': 'text/html'})))
            for _, arrange in cases:
                for rules in (lab.external.tamper, lab.external.status, lab.external.types):
                    rules.clear()
                arrange()
                lab.github.hits.clear()
                refused(lab, self_update.NeedFullPackage, FULL)
                assert not any(h.startswith(('mod-', 'runtime-')) for h in lab.github.hits), lab.github.hits
            lab.external.status.clear()
            lab.external.types.clear()
            lab.external.tamper['Data/big.MPQ'] = page
            out = lab.release(expect=1)
            assert 'web page' in out, out[-400:]
            lab.release(extra=['--no-check-urls'])
            return 'page/403/text-html: full package address shown, nothing installed or downloaded from GitHub; ' \
                   'make_release refuses such an address'
        finally:
            lab.close()
    test('Captcha or download page: full package hint, nothing installed', captcha)

    def runtime_change():
        v1, v2 = packages()
        v1['.coop-runtime/python.exe'] = RUNTIME.joinpath('python.exe').read_bytes()
        v2['.coop-runtime/python.exe'] = PADDED
        lab = Lab(args.work, 'runtime', version_files=(v1, v2))
        try:
            started = time.time()
            shutil.copytree(RUNTIME, lab.game / '.coop-runtime', dirs_exist_ok=True)
            log(' INFO ', f'real runtime copied into the test folder ({time.time() - started:.1f} s)')
            game_python = lab.game / '.coop-runtime/python.exe'
            trial = game_python.with_name('python.exe.try')
            trial.write_bytes(PADDED)
            running = subprocess.Popen([str(game_python), '-I', '-c', 'import time; time.sleep(30)'])
            try:
                time.sleep(0.5)
                try:
                    os.replace(trial, game_python)
                except PermissionError as error:
                    log(' INFO ', f'running python.exe cannot be replaced: {error.strerror}')
                else:
                    raise AssertionError('a running python.exe was replaced')
            finally:
                running.kill()
                running.wait()
                trial.unlink(missing_ok=True)
            refused(lab, self_update.Refused, 'restarts from a copy')  # Without the updater exe.
            common = [str(ROOT / 'reconstruction/self_update.py'), '--game', str(lab.game), '--release-url',
                      lab.github.url, '--allow-local-http', '--timeout', '3', '--updater-exe',
                      str(lab.game / self_update.UPDATER_ASSET), '--extra-trusted-key', lab.pub['key-1']]
            run = lambda python, *extra: subprocess.run([str(python), '-I', '-B', *common, *extra], timeout=300,
                                                        capture_output=True, text=True, encoding='utf-8',
                                                        errors='replace')
            before = lab.snapshot()
            first = run(game_python)
            assert first.returncode == self_update.RUNTIME_BUSY, first.stdout[-600:]
            copy = lab.game / 'coop-update' / self_update.RUNTIME_COPY
            assert (copy / 'python.exe').read_bytes() == v1['.coop-runtime/python.exe']
            assert lab.snapshot() == before, 'folder changed before the restart'
            forced = run(game_python, '--runtime-free')
            assert forced.returncode == 2 and 'restarts from a copy' in forced.stdout, forced.stdout[-600:]
            second = run(copy / 'python.exe', '--runtime-free')
            assert second.returncode == 0, second.stdout[-600:]
            lab.is_version(v2)
            assert subprocess.run([str(game_python), '-I', '-c', 'import ssl'], timeout=60).returncode == 0
            return 'running python.exe is locked; exit 5 + copy, folder unchanged; from the copy the new ' \
                   'python.exe is installed and runs'
        finally:
            lab.close()
    test('Runtime change: restart from a copy of .coop-runtime', runtime_change)

    def full_package_only():
        """No direct host for game files (0.0.12): same game version updates, another one is sent to the full package."""
        v1, v2 = packages()
        v2.update({p: v1[p] for p in v1 if p == self_update.EXE or p.startswith('Data/')})  # Same game files.
        lab = Lab(args.work, 'nohost', version_files=(v1, v2), hosted=False)
        try:
            manifest = json.loads(next((lab.work / 'github').glob('manifest-*.json')).read_text())
            assert manifest['files'][self_update.EXE]['source'] == dict(url=lab.external.url + 'releases.html')
            assert lab.run() == 0
            lab.is_version(v2)
            assert not lab.external.hits, lab.external.hits  # No game file requested at all.
        finally:
            lab.close()
        old = dict(v1, **{self_update.EXE: b'game exe of 0.93.1'})
        lab = Lab(args.work, 'nohost-old', version_files=(old, v2), hosted=False)
        try:
            lab.github.hits.clear()
            refused(lab, self_update.NeedFullPackage, FULL)
            assert not any(h.startswith(('mod-', 'runtime-')) for h in lab.github.hits), lab.github.hits
            assert lab.external.hits == ['releases.html'], lab.external.hits  # One page request, no download.
            assert not (lab.game / 'coop-update' / self_update.RUNTIME_COPY).exists()
            return 'same game files: mod archive only, no game download; other game exe: full package link, nothing changed'
        finally:
            lab.close()
    test('Game files without a host: full package only for other game versions', full_package_only)

    def patch_lab(name, full_max_mb='256', changed_local=False):
        """v1 -> v2 with a changed exe (patch), the same big MPQ and a new MPQ (full asset)."""
        v1, v2 = packages()
        exe1 = os.urandom(3 << 20)
        v1[self_update.EXE] = exe1
        v2[self_update.EXE] = exe1[:1 << 20] + b'0.95 code' + exe1[(1 << 20) + 9:] + os.urandom(4096)
        v2['Data/big.MPQ'] = v1['Data/big.MPQ']
        v2['Data/new.MPQ'] = os.urandom(300 << 10)
        lab = Lab(args.work, name, version_files=(v1, v2), hosted=False)
        old = lab.work / 'old'
        write_package(old, v1)
        gamefiles = lab.work / 'gamefiles'
        built = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve().parent / 'make_game_patches.py'),
                                '--target', str(lab.work / 'package'), '--from', str(old), '--out', str(gamefiles),
                                '--full-max-mb', full_max_mb, '--full-for-all', '--generator', str(GENERATOR)],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
        assert built.returncode == 0, built.stdout[-600:] + built.stderr[-600:]
        lab.release(min_updater=4, gamefiles=gamefiles)
        if changed_local:
            (lab.game / self_update.EXE).write_bytes(b'x' + exe1[1:])  # A version without a patch.
        lab.external.hits.clear()
        return lab, json.loads((gamefiles / 'gamefiles.json').read_text())

    def game_patches():
        if not GENERATOR.is_file():
            built = subprocess.run(['cargo', 'build', '--release'], cwd=Path(__file__).resolve().parent / 'patchgen',
                                   capture_output=True, text=True,
                                   env=dict(os.environ, CARGO_TARGET_DIR=str(GENERATOR.parents[1])))
            assert built.returncode == 0, built.stderr[-500:]
        lab, assets = patch_lab('patch')
        try:
            refused(lab, self_update.Refused, 'needs updater 4')  # Updater 1 without an exe to replace.
            assert lab.run(updater_version=4) == 0
            lab.is_version(lab.v2)
            patch = assets[self_update.EXE]['patches'][0]['asset']
            fetched = sorted(h for h in lab.external.hits if h.startswith('gf/'))
            assert fetched == sorted(['gf/' + patch, 'gf/' + assets['Data/new.MPQ']['full']['asset']]), fetched
            size = assets[self_update.EXE]['patches'][0]['size']
            info = f'patch {size} B for a {assets[self_update.EXE]["size"]} B exe'
        finally:
            lab.close()
        lab, assets = patch_lab('patch-other', changed_local=True)
        try:
            assert lab.run(updater_version=4) == 0  # No patch for this exe: the full asset.
            lab.is_version(lab.v2)
            assert 'gf/' + assets[self_update.EXE]['full']['asset'] in lab.external.hits, lab.external.hits
        finally:
            lab.close()
        lab, assets = patch_lab('patch-none', full_max_mb='0', changed_local=True)
        try:
            refused(lab, self_update.NeedFullPackage, FULL, updater_version=4)  # Neither patch nor full asset.
        finally:
            lab.close()
        return info + '; other exe -> full asset; no patch and no full asset -> full package link'
    test('Game files: binary patch, full asset fallback, full package', game_patches)

    def game_patch_damage():
        lab, assets = patch_lab('patch-damage')
        try:
            name = assets[self_update.EXE]['patches'][0]['asset']
            data = bytearray((lab.work / 'external/gf' / name).read_bytes())
            data[len(data) // 2] ^= 0x40
            lab.external.tamper['gf/' + name] = bytes(data)
            refused(lab, self_update.Refused, 'does not match its signed SHA-256', updater_version=4)
            lab.external.tamper.clear()
            for staged in lab.game.glob('coop-update/release-*'):
                shutil.rmtree(staged)
            lab.external.cut['gf/' + name] = dict(after=len(data) // 4, times=3)  # 3 x 1/4: never complete
            refused(lab, self_update.Offline, 'run the updater again', updater_version=4)
            lab.external.cut.clear()
            lab.external.ranges.clear()
            assert lab.run(updater_version=4) == 0  # Continues the cut download.
            assert any(n == 'gf/' + name for n, _ in lab.external.ranges), lab.external.ranges
            lab.is_version(lab.v2)
        finally:
            lab.close()
        # A patch that passed the download check but is broken inside never yields a wrong file.
        work = args.work / 'patch-unit'
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        old, new = work / 'old.bin', work / 'new.bin'
        old.write_bytes(os.urandom(1 << 20))
        new.write_bytes(old.read_bytes()[:500000] + b'new' + old.read_bytes()[500003:])
        subprocess.run([str(GENERATOR), str(old), str(new), str(work / 'p')], check=True, capture_output=True)
        game_patch.pack(work / 'p', new.stat().st_size, work / 'p.azp')
        assert game_patch.apply(old, work / 'p.azp', work / 'out.bin') == hashlib.sha256(new.read_bytes()).hexdigest()
        data = bytearray((work / 'p.azp').read_bytes())
        for at in (60, len(data) // 2, len(data) - 30):
            broken = bytearray(data)
            broken[at] ^= 1
            (work / 'broken.azp').write_bytes(bytes(broken))
            try:
                digest = game_patch.apply(old, work / 'broken.azp', work / 'out.bin')
            except ValueError:
                continue
            assert digest != hashlib.sha256(new.read_bytes()).hexdigest(), at
        wrong = work / 'wrong.bin'
        wrong.write_bytes(os.urandom(1 << 20))
        assert game_patch.apply(wrong, work / 'p.azp', work / 'out.bin') != hashlib.sha256(new.read_bytes()).hexdigest()
        return 'tampered asset refused, cut download continued, broken patch or wrong source never matches the target'
    test('Game file patches: damage, wrong source, interrupted download', game_patch_damage)

    def game_sources():
        """Updater 4: the signed source list (first one gone / wrong -> next one), fresh install into an empty folder."""
        lab, assets = patch_lab('sources')
        try:
            patch = assets[self_update.EXE]['patches'][0]['asset']
            mirror = lab.work / 'github' / 'mirror'
            mirror.mkdir()
            for asset in (lab.work / 'gamefiles').iterdir():
                shutil.copy2(asset, mirror / asset.name)
            lab.release(min_updater=4, gamefiles=lab.work / 'gamefiles',
                        extra=['--gamefiles-url', lab.github.url + 'gone/', '--gamefiles-url', lab.github.url + 'mirror/'])
            manifest = json.loads(next((lab.work / 'github').glob('manifest-*.json')).read_text())
            assert len(manifest['gamefiles']['urls']) == 3, manifest['gamefiles']
            lab.github.tamper['mirror/' + patch] = b'not the patch'
            lab.github.hits.clear()
            lab.external.hits.clear()
            assert lab.run(updater_version=4) == 0
            lab.is_version(lab.v2)
            assert 'gone/' + patch in lab.github.hits and 'mirror/' + patch in lab.github.hits, lab.github.hits
            assert 'gf/' + patch in lab.external.hits, lab.external.hits  # Third source after 404 and wrong bytes.
            raw = [a for a in assets.values() if (a.get('full') or {}).get('encoding') == 'raw']
            assert raw, 'no raw asset (random MPQ data does not shrink)'
            fresh = lab.work / 'fresh'
            for rel, data in lab.v2.items():  # The installer unpacks the signed runtime archive first.
                if rel.startswith('.coop-runtime/'):
                    (fresh / rel).parent.mkdir(parents=True, exist_ok=True)
                    (fresh / rel).write_bytes(data)
            updater_v2.su.LOG = None
            updater_v2.su.IGNORED_PIDS.clear()
            code = updater_v2.main(['--game', str(fresh), '--release-url', lab.github.url, '--allow-local-http',
                                    '--updater-version', '4', '--timeout', '3'], builtin=(lab.pub['key-1'],))
            assert code == 0, code
            for rel, data in lab.v2.items():
                assert (fresh / rel).read_bytes() == data, rel
            return f'404 -> wrong bytes -> third source; fresh install of {len(lab.v2)} files incl. {len(raw)} raw asset(s)'
        finally:
            lab.close()
    test('Updater 4: signed source list and fresh install', game_sources)

    def captured(fn):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = fn()
        return code, buffer.getvalue()

    def repair_same_version():
        """0.0.20: a folder on the newest version with another exe, a changed and a missing mod file is repaired."""
        lab, assets = patch_lab('repair')
        try:
            assert lab.run(updater_version=4) == 0
            lab.is_version(lab.v2)
            foreign = os.urandom(2 << 20)  # Another AzerothSurvivors.exe: no patch for its hash, room says "Game Unknown".
            (lab.game / self_update.EXE).write_bytes(foreign)
            (lab.game / 'reconstruction/a.py').write_bytes(b'changed locally')
            (lab.game / 'reconstruction/b.py').unlink()
            lab.external.hits.clear()
            code, text = captured(lambda: lab.run(updater_version=4))
            assert code == 0, text[-800:]
            lab.is_version(lab.v2)
            assert 'Game files repaired: 3 files' in text, text[-800:]
            full = assets[self_update.EXE]['full']['asset']
            assert 'gf/' + full in lab.external.hits, lab.external.hits  # The target exe's full asset.
            backup = max(lab.game.glob('coop-update-backup-*'))
            assert (backup / self_update.EXE).read_bytes() == foreign, 'replaced exe not in the backup'
            code, text = captured(lambda: lab.run('--repair', updater_version=4))
            assert code == 0 and 'nothing to repair' in text, text[-600:]
            return 'unknown exe -> full asset of the target exe, changed + missing mod file restored, backup, --repair'
        finally:
            lab.close()
    test('Repair: same version, unknown game exe, changed and missing files', repair_same_version)

    def repair_check():
        """Start-Coop: a current folder is checked against the signed list (hash cache); a difference runs the updater."""
        sys.path.insert(0, str(ROOT))
        import coop_autoupdate as auto
        lab = Lab(args.work, 'repair-check')
        try:
            assert lab.run() == 0
            argv = ['--game', str(lab.game), '--release-url', lab.github.url, '--allow-local-http', '--countdown', '0']
            keys = (lab.pub['key-1'],)
            code, text = captured(lambda: auto.main(argv, keys))
            assert code == 0 and '0 differ' in text, text[-600:]
            assert (lab.game / auto.CACHE).is_file()
            reads = []
            original = auto.su.sha256
            with mock.patch.object(auto.su, 'sha256', lambda p: reads.append(p) or original(p)):
                code, text = captured(lambda: auto.main(argv, keys))
                assert code == 0 and not reads, (code, reads)  # Size and time unchanged: nothing read again.
                (lab.game / 'Data/big.MPQ').write_bytes(b'damaged')
                code, text = captured(lambda: auto.main(argv, keys))
            assert code == auto.INSTALL, text[-600:]
            package_reads = [r for r in reads if r.name != auto.su.UPDATER_ASSET + '.download.part']  # Not the exe download.
            assert package_reads == [lab.game / 'Data/big.MPQ'] and 'Data/big.MPQ' in text, (reads, text[-600:])
            (lab.game / 'WTF/Config.wtf').write_bytes(b'player setting')  # Player data never counts.
            assert lab.run() == 0
            code, text = captured(lambda: auto.main(argv, keys))
            assert code == 0, text[-600:]
            return 'current folder: cached check reads nothing; one damaged file -> updater; player data ignored'
        finally:
            lab.close()
    test('Repair: Start-Coop check with hash cache', repair_check)

    def blocked_updater_exe():
        """Player report 0.0.16: Windows security blocked writing the unsigned updater exe while the installer
        unpacked it ([Errno 22] Invalid argument, again on every retry). The rest is installed, the exe later."""
        import contextlib
        import io
        v1, v2 = packages()
        v2[self_update.UPDATER_ASSET] = b'updater exe 0.0.16'
        lab = Lab(args.work, 'blocked-exe', version_files=(v1, v2), min_updater=4, updater=b'updater exe 0.0.16')
        try:
            fresh = lab.work / 'fresh'
            for rel, data in v2.items():  # The installer unpacks the signed runtime archive first.
                if rel.startswith('.coop-runtime/'):
                    (fresh / rel).parent.mkdir(parents=True, exist_ok=True)
                    (fresh / rel).write_bytes(data)
            original = self_update.extract_member

            def blocked(errno_value):
                def extract(archive, name, size, digest, target):
                    if Path(target).name == self_update.UPDATER_ASSET:
                        Path(target).write_bytes(b'partial')
                        raise OSError(errno_value, os.strerror(errno_value), str(target))
                    return original(archive, name, size, digest, target)
                return extract

            def install(extract):
                updater_v2.su.LOG = None
                updater_v2.su.IGNORED_PIDS.clear()
                output = io.StringIO()
                with mock.patch.object(self_update, 'extract_member', extract), contextlib.redirect_stdout(output):
                    try:
                        code = updater_v2.main(['--game', str(fresh), '--release-url', lab.github.url, '--allow-local-http',
                                                '--updater-version', '4', '--timeout', '3'], builtin=(lab.pub['key-1'],))
                    except OSError as error:
                        code = error
                return code, output.getvalue()
            code, output = install(blocked(28))  # Disk full is a real failure: nothing hides it.
            assert code != 0, code
            code, output = install(blocked(22))
            assert code == 0, (code, output[-800:])
            assert 'Windows security blocked Azeroth-Survivors-Together-Updater.exe' in output, output[-800:]
            assert 'Allow on device' in output
            assert not (fresh / self_update.UPDATER_ASSET).exists()
            for rel, data in v2.items():
                if rel != self_update.UPDATER_ASSET:
                    assert (fresh / rel).read_bytes() == data, rel
            code, output = install(original)  # Allowed again: the next run brings the exe.
            assert code == 0, output[-800:]
            assert (fresh / self_update.UPDATER_ASSET).read_bytes() == b'updater exe 0.0.16'
            return 'Errno 22 on the updater exe: everything else installed + WARN; later run adds it; disk full still fails'
        finally:
            lab.close()
    test('Blocked updater exe does not stop the installation', blocked_updater_exe)

    def parent_without_pss():
        """Wine 10 aborts on os.getppid (PssCaptureSnapshot); updater 4 reads the parent from a toolhelp snapshot."""
        import nt
        assert os.getppid is updater_v2.parent_pid, 'os.getppid not replaced'
        got, want = updater_v2.parent_pid(), nt.getppid()
        assert got == want and got > 0, (got, want)
        return f'toolhelp parent {got} == nt.getppid(), no PssCaptureSnapshot'
    if os.name == 'nt':
        test('Parent process without PssCaptureSnapshot (Wine)', parent_without_pss)

    def auto_update_check():
        """Start-Coop.cmd step: coop_autoupdate decides (0 = launcher, 10 = run the updater exe first)."""
        sys.path.insert(0, str(ROOT))
        import coop_autoupdate as auto
        lab = Lab(args.work, 'auto')
        try:
            argv = ['--game', str(lab.game), '--release-url', lab.github.url, '--allow-local-http', '--countdown', '0']
            keys = tuple(lab.pub[k] for k in ('key-1',))
            with mock.patch.dict(os.environ, COOP_AUTO_UPDATE='0'):
                assert auto.main(argv, keys) == 0
            (lab.game / 'coop-update').mkdir(exist_ok=True)
            (lab.game / auto.OFF_FILE).write_text('')
            assert auto.main(argv, keys) == 0
            (lab.game / auto.OFF_FILE).unlink()
            exe = lab.game / self_update.UPDATER_ASSET
            with mock.patch.object(auto, 'enter_pressed', lambda: True):
                assert auto.main(argv[:-1] + ['1'], keys) == 0  # Enter during the countdown: skipped.
            assert not exe.exists(), 'downloaded although skipped'
            before = lab.snapshot()
            assert auto.main(argv, keys) == auto.INSTALL
            assert exe.read_bytes() == b'updater exe v2', 'signed updater not downloaded'
            assert {k: v for k, v in lab.snapshot().items() if k != exe.name} == before, 'auto check changed the folder'
            assert auto.main(argv, keys) == auto.INSTALL and exe.read_bytes() == b'updater exe v2'  # Present: kept.
            assert lab.run() == 0
            assert auto.main(argv, keys) == 0  # Current now.
            assert auto.main(argv, (lab.pub['other'],)) == 0  # Untrusted release: start as usual.
            url = lab.github.url
        finally:
            lab.close()
        started = time.time()
        assert auto.main(['--game', str(lab.game), '--release-url', url, '--allow-local-http', '--countdown', '0'],
                         keys) == 0  # Offline.
        assert time.time() - started < 3.5, time.time() - started
        return 'off (variable/file), skip, update -> signed exe downloaded and exit 10, current, untrusted, offline < 3.5 s'
    test('Start-Coop auto update check', auto_update_check)

    def shipped_updater():
        """From 0.0.13 the package ships the updater exe: the running one is replaced after the install."""
        import ctypes
        from ctypes import wintypes
        assert not any(self_update.user_area(p) for p in ('Start-Coop.cmd', 'coop_autoupdate.py', 'coop_launcher.py',
                                                          self_update.UPDATER_ASSET)), 'shipped files are user data'
        v1, v2 = packages()
        v1[self_update.UPDATER_ASSET] = b'updater exe 0.0.12'
        v2[self_update.UPDATER_ASSET] = b'updater exe 0.0.13'
        lab = Lab(args.work, 'shipped', version_files=(v1, v2), min_updater=3, updater=b'updater exe 0.0.13')
        try:
            exe = lab.game / self_update.UPDATER_ASSET
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
            # Like a running exe: readable, but nobody may open it for writing.
            handle = kernel.CreateFileW(str(exe), 0x80000000, 0x1, None, 3, 0, None)
            assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
            try:
                assert not self_update.writable(exe)
                assert lab.run(updater_version=3, exe=exe) == self_update.SELF_UPDATE_READY
            finally:
                kernel.CloseHandle(handle)
            assert exe.read_bytes() == b'updater exe 0.0.12', 'running exe was touched'
            assert exe.with_name(exe.name + '.new').read_bytes() == b'updater exe 0.0.13'
            assert not list(lab.game.glob('coop-update/*.pending')), 'pending copy left'
            lab.is_version({p: d for p, d in v2.items() if p != self_update.UPDATER_ASSET})
            manifest = json.loads((lab.game / 'package-manifest.json').read_text())
            assert manifest[self_update.UPDATER_ASSET] == hashlib.sha256(b'updater exe 0.0.13').hexdigest()
            # What the exe does on exit 3: swap and run once more -> nothing left to do.
            os.replace(exe.with_name(exe.name + '.new'), exe)
            assert lab.run(updater_version=3, exe=exe) == 0
            lab.is_version(v2)
        finally:
            lab.close()
        lab = Lab(args.work, 'shipped-direct', version_files=(v1, v2), min_updater=3, updater=b'updater exe 0.0.13')
        try:
            assert lab.run(updater_version=3) == 0  # Python run without the exe: replaced like any file.
            lab.is_version(v2)
            refused(lab, self_update.Refused, 'needs updater 3', updater_version=2)  # v2 must fetch v3 first.
        finally:
            lab.close()
        return 'locked running exe: install + exe as .new + exit 3, then nothing left; without exe: normal; v2 -> needs v3'
    test('Shipped updater exe replaces itself after the install', shipped_updater)

    if not args.skip_exe:
        def exe_self_update():
            """Real exes: v1 finds a release that needs updater 2, swaps itself and installs."""
            v1, v2 = packages()
            for files in (v1, v2):
                files.pop('.coop-runtime/marker.txt')
            v1['.coop-runtime/python.exe'] = RUNTIME.joinpath('python.exe').read_bytes()
            v2['.coop-runtime/python.exe'] = PADDED  # The release changes the runtime too.
            lab = Lab(args.work, 'exe', version_files=(v1, v2))
            try:
                exes = {}
                for version in (1, 2):
                    target = lab.work / f'cargo-{version}'
                    built = subprocess.run(['cargo', 'build', '--release', '--quiet', '--bin', 'coop-updater'],
                                           cwd=ROOT / 'coop-lab/updater/exe', capture_output=True, text=True,
                                           env=dict(os.environ, CARGO_TARGET_DIR=str(target),
                                                    COOP_UPDATER_VERSION=str(version),
                                                    COOP_UPDATER_TEST_KEY=lab.pub['key-1']))
                    assert built.returncode == 0, built.stderr[-500:]
                    exes[version] = (target / 'release/coop-updater.exe').read_bytes()
                lab.release(min_updater=2, updater=exes[2])
                shutil.copytree(RUNTIME, lab.game / '.coop-runtime', dirs_exist_ok=True)  # A real copy.
                exe = lab.game / self_update.UPDATER_ASSET
                exe.write_bytes(exes[1])
                result = subprocess.run([str(exe), '--no-pause', '--release-url', lab.github.url, '--allow-local-http'],
                                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                assert result.returncode == 0, result.stdout[-800:]
                assert 'Starting the new updater' in result.stdout and 'Updater 2' in result.stdout, result.stdout[-800:]
                assert 'from a copy of the co-op Python runtime' in result.stdout, result.stdout[-800:]
                assert exe.read_bytes() == exes[2], 'exe not replaced'
                lab.is_version(lab.v2)
                assert not (lab.game / 'coop-update' / self_update.RUNTIME_COPY).exists(), 'runtime copy left'
                again = subprocess.run([str(exe), '--no-pause', '--release-url', lab.github.url, '--allow-local-http'],
                                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                assert again.returncode == 0 and not exe.with_name(exe.name + '.old').exists(), again.stdout[-400:]
                return 'v1 exe -> downloaded v2, swapped, runtime copy, installed incl. python.exe; .old removed'
            finally:
                lab.close()
        test('Updater exe replaces itself and installs', exe_self_update)

    passed = sum(ok for _, ok in RESULTS)
    log(' INFO ', f'Zusammenfassung: {passed}/{len(RESULTS)} bestanden ({time.time() - t0:.0f} s)')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
