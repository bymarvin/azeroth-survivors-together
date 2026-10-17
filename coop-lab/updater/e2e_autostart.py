"""End-to-end: automatic update through Start-Coop.cmd, 0.0.12 -> 0.0.13 -> 0.0.14 (boot-log format).

  e2e_autostart.py --staging <0.0.12 staging> --gamefiles <game file asset folder> --work <folder>

Builds, all in --work and signed with a throwaway test key (never the release key):
- an updater exe 2 that trusts the test key (COOP_UPDATER_TEST_KEY),
- staging 0.0.13 = 0.0.12 + this worktree's Start-Coop.cmd, coop_launcher.py, coop_autoupdate.py, COOP-VERSION.txt,
- staging 0.0.14 = 0.0.13 with another version and a longer Start-Coop.cmd (it replaces itself while it runs),
- one release folder for each, served by a local server; game files via COOP_GAMEFILES_URL.
Both packages ship the updater exe (0.0.13: test build version 3, 0.0.14: version 4, so the running
exe must replace itself). Cases: a 0.0.12 folder (old Start-Coop.cmd) -> 0.0.13 with the updater exe
(new Start-Coop.cmd, coop_autoupdate.py and the exe in the folder), then THAT folder -> 0.0.14 through
its new Start-Coop.cmd; a 0.0.13 folder without the exe (downloaded); a damaged release (old state
kept); offline; switched off. The launcher always runs with --help (prints and ends: no room, no game).
"""
import argparse
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / 'reconstruction')]
import sign_release  # noqa: E402

PYTHON = Path(sys.executable)
RESULTS = []
SKIP = ('coop-update',)
EXE = 'Azeroth-Survivors-Together-Updater.exe'


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*')
            if p.is_file() and not p.relative_to(root).as_posix().startswith(SKIP)
            and '__pycache__' not in p.parts and p.name != EXE + '.old'}  # .old: removed on the next exe start


def copy(source, target):
    for path in source.rglob('*'):
        out = target / path.relative_to(source)
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif path.stat().st_size > 8 << 20 and path.suffix.lower() in ('.exe', '.mpq'):
            out.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, out)  # Game files: the updater replaces, never edits in place.
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)


def manifest(stage):
    files = sorted(p for p in stage.rglob('*') if p.is_file() and p.name != 'package-manifest.json')
    data = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (stage / 'package-manifest.json').write_text(json.dumps(data, indent=1, sort_keys=True), encoding='utf-8')


class Release(http.server.SimpleHTTPRequestHandler):
    root, tamper = None, {}

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(Release.root), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        name = self.path.lstrip('/')
        if name in Release.tamper:
            body = Release.tamper[name]
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve(handler):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}/'


def case(name, fn):
    log(' .... ', name)
    started = time.time()
    try:
        info = fn()
        log('  OK  ', f'{name} ({time.time() - started:.1f} s, {info})')
        RESULTS.append(True)
    except Exception as error:
        log('FAILED', f'{name}: {type(error).__name__}: {error}')
        RESULTS.append(False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--staging', type=Path, required=True)
    ap.add_argument('--gamefiles', type=Path, required=True)
    ap.add_argument('--work', type=Path, required=True)
    args = ap.parse_args()
    work = args.work
    if work.exists():
        raise SystemExit(f'{work} exists; use a new folder')
    work.mkdir(parents=True)
    t0 = time.time()

    log(' .... ', 'Testschlüssel und Updater-exe (Test-Build)')
    started = time.time()
    sign_release.create(work / 'key', sign_release.CURRENT)
    pub = sign_release.public(sign_release.load(work / 'key'))
    exes = {}
    for version in ('3', '4'):
        built = subprocess.run(['cargo', 'build', '--release', '--quiet', '--bin', 'coop-updater'], cwd=HERE / 'exe',
                               capture_output=True, text=True,
                               env=dict(os.environ, CARGO_TARGET_DIR=str(work / f'cargo-{version}'),
                                        COOP_UPDATER_VERSION=version, COOP_UPDATER_TEST_KEY=pub))
        if built.returncode:
            raise SystemExit(built.stderr[-800:])
        exes[version] = work / f'cargo-{version}/release/coop-updater.exe'
    exe = exes['3']
    log('  OK  ', f'Testschlüssel und Updater-exe ({time.time() - started:.1f} s)')

    log(' .... ', 'Staging 0.0.13 und 0.0.14')
    started = time.time()
    stage13, stage14 = work / 'stage-0.0.13', work / 'stage-0.0.14'
    copy(args.staging, stage13)
    for name in ('Start-Coop.cmd', 'coop_launcher.py', 'coop_autoupdate.py', 'COOP-VERSION.txt'):
        shutil.copy2(ROOT / name, stage13 / name)
    shutil.copy2(exes['3'], stage13 / EXE)  # From 0.0.13 the package ships the updater.
    manifest(stage13)
    copy(stage13, stage14)
    (stage14 / 'COOP-VERSION.txt').write_text('0.0.14-community+game-7264d50b', encoding='utf-8')
    with open(stage14 / 'Start-Coop.cmd', 'ab') as stream:
        stream.write(b'rem 0.0.14 test build: this file is longer and replaced while it runs.\r\n' * 3)
    with open(stage14 / 'coop_launcher.py', 'a', encoding='utf-8') as stream:
        stream.write('\n# 0.0.14 test build\n')
    shutil.copy2(exes['4'], stage14 / EXE)  # A newer updater: the running one replaces itself.
    manifest(stage14)
    log('  OK  ', f'Staging 0.0.13 und 0.0.14 ({time.time() - started:.1f} s)')

    log(' .... ', 'Release-Ordner (Testschlüssel)')
    started = time.time()
    releases = {}
    for version, stage, sequence, updater in (('0.0.13', stage13, 1000, exes['3']), ('0.0.14', stage14, 2000, exes['4'])):
        out = work / f'release-{version}'
        run = subprocess.run([str(PYTHON), str(ROOT / 'coop-lab/port-7264/make_release.py'), '--target', str(stage),
                              '--updater-exe', str(updater), '--updater-version', '3', '--min-updater', '3',
                              '--gamefiles', str(args.gamefiles), '--gamefiles-url',
                              'https://github.com/bymarvin/azeroth-survivors-gamefiles/releases/download/game-7264d50b/',
                              '--full-package-url', 'https://github.com/bymarvin/azeroth-survivors-together#full-package',
                              '--sequence', str(sequence), '--key-dir', str(work / 'key'), '--out', str(out)],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
        if run.returncode:
            raise SystemExit(run.stdout[-800:])
        releases[version] = out
    log('  OK  ', f'Release-Ordner ({time.time() - started:.1f} s)')

    release_server, release_url = serve(Release)

    class Game(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(args.gamefiles), **k)

        def log_message(self, *a):
            pass
    game_server, game_url = serve(Game)
    env = dict(os.environ, COOP_GAMEFILES_URL=game_url,
               COOP_UPDATE_ARGS=f'--release-url {release_url} --allow-local-http --extra-trusted-key {pub}')
    env.pop('COOP_AUTO_UPDATE', None)

    def start_coop(folder, extra_env=None):
        run = subprocess.run(f'cmd /c ""{folder / "Start-Coop.cmd"}" --help"', capture_output=True, text=True,
                             encoding='utf-8', errors='replace', env=dict(env, **(extra_env or {})), timeout=900)
        if 'usage: coop_launcher.py' not in run.stdout:
            raise AssertionError(f'launcher did not start: {run.stdout[-600:]}{run.stderr[-300:]}')
        return run

    def same(folder, stage):
        got, want = tree(folder), tree(stage)
        differ = sorted(p for p in want if got.get(p) != want[p])
        extra = sorted(p for p in got if p not in want and not p.startswith('coop-update-backup-'))
        if differ or extra:
            raise AssertionError(f'differs from {stage.name}: {differ[:5]} extra {extra[:5]}')

    def updater_exe():
        Release.root = releases['0.0.13']
        folder = work / 'a-0.0.12'
        copy(args.staging, folder)
        if (folder / 'coop_autoupdate.py').exists() or b'coop_autoupdate' in (folder / 'Start-Coop.cmd').read_bytes():
            raise AssertionError('the 0.0.12 staging already has the automatic update')
        shutil.copy2(exe, folder / EXE)  # The player puts the updater from the release into the folder.
        run = subprocess.run([str(folder / 'Azeroth-Survivors-Together-Updater.exe'), '--no-pause', '--release-url',
                              release_url, '--allow-local-http'], capture_output=True, text=True, encoding='utf-8',
                             errors='replace', env=env, timeout=900)
        if run.returncode:
            raise AssertionError(run.stdout[-600:])
        same(folder, stage13)
        return 'byte-equal to 0.0.13 incl. new Start-Coop.cmd, coop_autoupdate.py, updater exe, package-manifest.json'
    case('0.0.12 (old Start-Coop) -> 0.0.13 with the updater exe', updater_exe)

    def chain():
        Release.root = releases['0.0.14']
        folder = work / 'a-0.0.12'  # The folder the previous case updated to 0.0.13.
        run = start_coop(folder)
        if 'Update 0.0.13-community+game-7264d50b -> 0.0.14' not in run.stdout or 'Starting the new updater' not in run.stdout:
            raise AssertionError(run.stdout[-800:])
        same(folder, stage14)
        return 'same folder via its new Start-Coop.cmd: byte-equal to 0.0.14, running updater replaced itself (.new)'
    case('Same folder 0.0.13 -> 0.0.14 through the new Start-Coop.cmd', chain)

    def auto(name, with_exe):
        def run_case():
            Release.root = releases['0.0.14']
            folder = work / name
            copy(stage13, folder)
            if not with_exe:
                (folder / EXE).unlink()
            run = start_coop(folder)
            if 'Update 0.0.13-community+game-7264d50b -> 0.0.14' not in run.stdout:
                raise AssertionError(run.stdout[-600:])
            same(folder, stage14)
            note = 'exe downloaded, ' if not with_exe and 'Azeroth-Survivors-Together-Updater.exe downloaded' in run.stdout else ''
            return f'{note}byte-equal to 0.0.14, Start-Coop.cmd replaced itself, launcher started'
        return run_case
    case('Start-Coop 0.0.13 -> 0.0.14, no updater exe in the folder', auto('c-auto-noexe', False))

    def damaged():
        Release.root = releases['0.0.14']
        latest = json.loads((releases['0.0.14'] / 'latest.json').read_text())
        mod = latest['archives']['mod']['asset']
        data = bytearray((releases['0.0.14'] / mod).read_bytes())
        data[len(data) // 2] ^= 1
        Release.tamper = {mod: bytes(data)}
        try:
            folder = work / 'd-damaged'
            copy(stage13, folder)
            before = tree(folder)
            run = start_coop(folder)
        finally:
            Release.tamper = {}
        if tree(folder) != before or 'does not match its signed SHA-256' not in run.stdout:
            raise AssertionError(run.stdout[-600:])
        return 'update refused, 0.0.13 unchanged, launcher started'
    case('Start-Coop with a damaged release', damaged)

    def offline():
        folder = work / 'e-offline'
        copy(stage13, folder)
        before = tree(folder)
        started = time.time()
        start_coop(folder, dict(COOP_UPDATE_ARGS=f'--release-url http://127.0.0.1:9/ --allow-local-http --extra-trusted-key {pub}'))
        seconds = time.time() - started
        if tree(folder) != before:
            raise AssertionError('folder changed')
        return f'unchanged, launcher after {seconds:.1f} s'
    case('Start-Coop offline', offline)

    def switched_off():
        Release.root = releases['0.0.14']
        folder = work / 'f-off'
        copy(stage13, folder)
        before = tree(folder)
        run = start_coop(folder, dict(COOP_AUTO_UPDATE='0'))
        if tree(folder) != before or 'switched off' not in run.stdout:
            raise AssertionError(run.stdout[-600:])
        return 'COOP_AUTO_UPDATE=0: unchanged, launcher started'
    case('Start-Coop with automatic update switched off', switched_off)

    release_server.shutdown()
    game_server.shutdown()
    log(' INFO ', f'Zusammenfassung: {sum(RESULTS)}/{len(RESULTS)} bestanden ({time.time() - t0:.0f} s)')
    return 0 if all(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
