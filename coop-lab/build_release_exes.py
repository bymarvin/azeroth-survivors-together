"""Release build of the installer and updater exe: reproducible, version info + icon, PDB separate (boot-log).

  build_release_exes.py --out <folder>

For each exe: two release builds in two fresh target folders with --remap-path-prefix (cargo registry and this
checkout -> fixed names, so no local path ends up in the exe), byte comparison of both, a check that the exe
contains no local path, then the exe and its PDB (debug information, never embedded) go to --out under their
asset names, each exe is scanned by Windows Defender and its SHA-256 listed. No test keys (COOP_* variables are
removed), no packer.
"""
import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXES = (
    ('installer', ROOT / 'coop-lab/installer', [], 'azsurvivors-installer', 'Azeroth-Survivors-Together-Installer'),
    ('updater', ROOT / 'coop-lab/updater/exe', ['--bin', 'coop-updater'], 'coop-updater', 'Azeroth-Survivors-Together-Updater'),
)
DEFENDER = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Windows Defender' / 'MpCmdRun.exe'


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def threads():
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t))
    a, b = ctypes.c_size_t(), ctypes.c_size_t()
    return max(1, min(16, bin(a.value).count('1'))) if k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(a), ctypes.byref(b)) else 4


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(crate, extra, target):
    cargo_home = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
    env = {k: v for k, v in os.environ.items() if not k.startswith('COOP_') and k not in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS')}
    env['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join([f'--remap-path-prefix={cargo_home}=/cargo',
                                                  f'--remap-path-prefix={ROOT}=/src'])
    run = subprocess.run(['cargo', 'build', '--release', '--locked', '-j', str(threads()), '--target-dir', str(target), *extra],
                         cwd=crate, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if run.returncode:
        raise RuntimeError(run.stderr[-1500:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--', 'coop-lab', 'reconstruction', 'COOP-VERSION.txt'],
                           capture_output=True, text=True).stdout.strip()
    log(' INFO ', f'Stand {commit}{" (mit lokalen Änderungen)" if dirty else ""}, '
                  f'COOP-VERSION {(ROOT / "COOP-VERSION.txt").read_text().strip()}, {threads()} Threads')
    work = Path(tempfile.mkdtemp(prefix='release-exes-'))
    local = [str(ROOT).encode(), str(Path.home()).encode(), str(Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))).encode(),
             str(work).encode()]
    results = []
    for name, crate, extra, binary, asset in EXES:
        started = time.time()
        log(' .... ', f'{name}: zwei Release-Builds')
        try:
            for run in ('a', 'b'):
                build(crate, extra, work / f'{name}-{run}')
        except RuntimeError as error:
            log('FAILED', f'{name}: cargo build: {str(error).strip().splitlines()[-1] if str(error).strip() else error}')
            return 1
        a, b = (work / f'{name}-{run}' / 'release' / f'{binary}.exe' for run in ('a', 'b'))
        if a.read_bytes() != b.read_bytes():
            log('FAILED', f'{name}: die beiden Builds unterscheiden sich ({sha256(a)[:12]} / {sha256(b)[:12]})')
            return 1
        data = a.read_bytes()
        leaked = [p.decode(errors='replace') for p in local if p and p.lower() in data.lower()]
        if leaked:
            log('FAILED', f'{name}: lokale Pfade in der exe: {leaked}')
            return 1
        exe, pdb = args.out / f'{asset}.exe', args.out / f'{asset}.pdb'
        shutil.copyfile(a, exe)
        shutil.copyfile(a.with_name(binary.replace('-', '_') + '.pdb'), pdb)
        log('  OK  ', f'{name}: reproduzierbar ({time.time() - started:.1f} s, {len(data)} B, sha256 {sha256(exe)}, '
                      f'PDB {pdb.stat().st_size // 1024} KiB getrennt)')
        results.append(exe)
    for exe in results:
        started = time.time()
        log(' .... ', f'Defender {exe.name}')
        scan = subprocess.run([str(DEFENDER), '-Scan', '-ScanType', '3', '-File', str(exe), '-DisableRemediation'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        last = (scan.stdout.strip().splitlines() or [''])[-1]
        if scan.returncode or 'found no threats' not in last:
            log('FAILED', f'Defender {exe.name}: {last}')
            return 1
        log('  OK  ', f'Defender {exe.name} ({time.time() - started:.1f} s, keine Bedrohung)')
    shutil.rmtree(work, ignore_errors=True)
    log(' INFO ', f'Zusammenfassung: {len(results)} exe in {args.out} ({time.time() - t0:.0f} s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
