"""End-to-end test of a GitHub release folder against copies of old game folders (boot-log).

  e2e_release.py <release folder> <0.0.12 staging> <work> <name>=<old folder>[::<player exe>] ...
                 [--gamefiles <game file asset folder>]

Names starting with expect-update must end byte-equal to the staging, all
others with the full package message and nothing changed. A player exe (for
example updater 1) replaces the release's updater exe in that case.

A local HTTP server serves exactly the release folder. Each old folder is
copied (big game files as hard links: the updater only ever replaces files),
the release's updater exe is put into it and run like a player would.
"""
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

ARGS = list(sys.argv[1:])
GAMEFILES = Path(ARGS.pop(ARGS.index('--gamefiles') + 1)) if '--gamefiles' in ARGS else None
if GAMEFILES:
    ARGS.remove('--gamefiles')
RELEASE, STAGING, WORK = Path(ARGS[0]), Path(ARGS[1]), Path(ARGS[2])
OLD = dict(arg.split('=', 1) for arg in ARGS[3:])
SKIP = ('coop-update',)  # The updater exe is a package file from 0.0.13; its .old waits for the next start.


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def tree(root):
    result = {}
    for path in root.rglob('*'):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith(SKIP) and not rel.endswith('.exe.old'):
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def copy(source, target):
    if target.exists():
        raise SystemExit(f'{target} exists; remove it first')
    linked = 0
    for path in source.rglob('*'):
        out = target / path.relative_to(source)
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif path.stat().st_size > 8 << 20 and path.suffix.lower() in ('.exe', '.mpq'):
            out.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, out)  # Game files: the updater replaces, never edits in place.
            linked += 1
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
    return linked


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(folder):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}/'


def main():
    server, url = serve(RELEASE)
    log(' INFO ', f'Release-Ordner {RELEASE} unter {url}')
    player_env = {}
    if GAMEFILES:
        game_server, game_url = serve(GAMEFILES)
        player_env = {'COOP_GAMEFILES_URL': game_url}  # Hidden updater 2 override, like --release-url.
        log(' INFO ', f'Spieldatei-Assets {GAMEFILES} unter {game_url}')
    staged = tree(STAGING)
    ok = True
    for name, spec in OLD.items():
        old, _, player_exe = spec.partition('::')
        target = WORK / name
        started = time.time()
        log(' .... ', f'{name}: Kopie von {old}')
        linked = copy(Path(old), target)
        shutil.copy2(Path(player_exe) if player_exe else RELEASE / 'Azeroth-Survivors-Together-Updater.exe',
                     target / 'Azeroth-Survivors-Together-Updater.exe')
        before = tree(target)  # With the player's exe: from 0.0.13 it is a package file and compared too.
        log('  OK  ', f'{name}: Kopie ({time.time() - started:.1f} s, {len(before)} Dateien, {linked} Spieldateien verlinkt)')
        started = time.time()
        log(' .... ', f'{name}: Updater-exe wie ein Spieler starten')
        run = subprocess.run([str(target / 'Azeroth-Survivors-Together-Updater.exe'), '--no-pause', '--release-url', url,
                              '--allow-local-http'], env=dict(os.environ, **player_env), capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=1800, cwd=target)
        for line in run.stdout.splitlines()[-14:]:
            log(' INFO ', f'  {line}')
        after = tree(target)
        if name.startswith('expect-update'):
            extra = {p for p in after if p not in staged and not p.startswith(('coop-update-backup-', 'coop-update-'))}
            missing = {p for p in staged if p not in after}
            differ = {p for p in staged if p in after and after[p] != staged[p]}
            # The updater writes package-manifest.json itself (same content, its own JSON layout).
            manifest = target / 'package-manifest.json'
            if 'package-manifest.json' in differ and json.loads(manifest.read_bytes()) == json.loads(
                    (STAGING / 'package-manifest.json').read_bytes()):
                differ.discard('package-manifest.json')
                log(' INFO ', f'{name}: package-manifest.json inhaltlich gleich (nur JSON-Einrückung anders)')
            good = run.returncode == 0 and not missing and not differ
            log('  OK  ' if good else 'FAILED', f'{name}: Exit {run.returncode}, {len(staged)} Paketdateien, '
                f'{len(missing)} fehlen, {len(differ)} anders, zusätzlich: {sorted(extra)[:6]} '
                f'({time.time() - started:.1f} s)')
            for p in sorted(missing | differ)[:10]:
                log(' INFO ', f'  {p}')
        else:
            changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
            message = 'Download the full package' in run.stdout
            good = run.returncode == 2 and not changed and message
            log('  OK  ' if good else 'FAILED', f'{name}: Exit {run.returncode}, Vollpaket-Meldung {message}, '
                f'{len(changed)} Dateien geändert ({time.time() - started:.1f} s)')
            for p in sorted(changed)[:10]:
                log(' INFO ', f'  {p}')
        ok &= good
    server.shutdown()
    if GAMEFILES:
        game_server.shutdown()
    log(' INFO ', f'Zusammenfassung: {"alle bestanden" if ok else "FEHLER"}')
    return 0 if ok else 1


sys.exit(main())
