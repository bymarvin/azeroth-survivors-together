"""Build the game file assets of one release for the gamefiles repository (boot-log format).

  make_game_patches.py --target <staged package> --from <old package folder> [--from ...] --out <folder>
                       [--full-max-mb 256] [--generator azpatch-gen.exe]

For every game file (AzerothSurvivors.exe, Data/*) of the target:
- one patch per old folder whose copy differs (old SHA -> new SHA), made with
  azpatch-gen (bsdiff) and packed by game_patch.pack (lzma streams);
- one full asset (xz -9, parallel 64 MiB blocks via xz.exe; same bytes for any thread count) only for files at least one --from version lacks (new
  files such as patch-3/4.MPQ for 0.92/0.93.1) up to --full-max-mb. Supported versions are
  exactly the --from folders: any other version (e.g. 0.84.3) has no patch for
  its exe and gets the full package link (--full-for-all: a full asset for
  every file up to --full-max-mb instead, so any version can update).
Each patch is verified by applying it in a separate process (time and peak
RAM in the log). gamefiles.json in --out lists everything for make_release.
Patches run in parallel (threads from the process affinity, at most 4 at once:
each generator holds about 7x the file size in memory).
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import game_patch  # noqa: E402

GENERATOR = Path(r'D:\Azeroth Survivors Together Releases\stage\patchgen-cargo\release\azpatch-gen.exe')


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def threads():
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t))
    a, b = ctypes.c_size_t(), ctypes.c_size_t()
    return max(1, min(16, bin(a.value).count('1'))) if k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(a), ctypes.byref(b)) else 4


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while piece := stream.read(1 << 22):
            digest.update(piece)
    return digest.hexdigest()


def game_file(path):
    return path == 'AzerothSurvivors.exe' or path.startswith('Data/')


def asset_name(path, suffix):
    return path.replace('/', '-').replace(' ', '-') + suffix


VERIFY = r'''
import ctypes, sys, time
from ctypes import wintypes
sys.path.insert(0, sys.argv[1])
import game_patch
t = time.time()
digest = game_patch.apply(sys.argv[2], sys.argv[3], sys.argv[4]) if sys.argv[5] == 'patch' else \
    game_patch.unpack_full(sys.argv[3], sys.argv[4], int(sys.argv[6]))
seconds = time.time() - t
class Counters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD), ('peak', ctypes.c_size_t)] + \
               [(f'x{i}', ctypes.c_size_t) for i in range(7)]
counters = Counters(); counters.cb = ctypes.sizeof(counters)
kernel, psapi = ctypes.WinDLL('kernel32'), ctypes.WinDLL('psapi')
kernel.GetCurrentProcess.restype = wintypes.HANDLE
psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
print(digest, f'{seconds:.2f}', counters.peak)
'''


def verify(kind, old, asset, size, digest, work):
    """Apply in a fresh process (like a player's updater) and check SHA, time and peak RAM."""
    out = work / (asset.name + '.check')
    result = subprocess.run([sys.executable, '-I', '-c', VERIFY, str(HERE), str(old or ''), str(asset), str(out), kind, str(size)],
                            capture_output=True, text=True, check=True)
    got, seconds, peak = result.stdout.split()
    out.unlink()
    if got != digest:
        raise RuntimeError(f'{asset.name} does not reproduce the target SHA-256')
    return float(seconds), int(peak)


def make_patch(target_file, old_file, old_sha, new_sha, out, work, generator):
    started = time.time()
    asset = out / asset_name(target_file.relative_to(TARGET).as_posix(), f'-{old_sha[:12]}-{new_sha[:12]}.azp')
    prefix = work / asset.stem
    run = subprocess.run([str(generator), str(old_file), str(target_file), str(prefix)], capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(f'azpatch-gen failed: {run.stdout[-400:]}{run.stderr[-400:]}')
    game_patch.pack(prefix, target_file.stat().st_size, asset)
    seconds, peak = verify('patch', old_file, asset, target_file.stat().st_size, new_sha, work)
    return dict(from_sha256=old_sha, asset=asset.name, sha256=sha256(asset), size=asset.stat().st_size), \
        time.time() - started, seconds, peak


XZ_CANDIDATES = (Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'mingw64' / 'bin' / 'xz.exe',)
XZ_BLOCK = 64 << 20  # Fixed block size: the output is then the same for any thread count.


def find_xz():
    found = shutil.which('xz')
    return found or next((str(p) for p in XZ_CANDIDATES if p.is_file()), None)


def peak_ram(process):
    """Peak working set of a finished child (its handle is still open while the Popen object lives)."""
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD), ('peak', ctypes.c_size_t)] + \
                   [(f'x{i}', ctypes.c_size_t) for i in range(7)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL('psapi')
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    return counters.peak if psapi.GetProcessMemoryInfo(int(process._handle), ctypes.byref(counters), counters.cb) else 0


def pack_full_mt(source, target, label, workers, xz=None):
    """xz -9 in parallel blocks (one ordinary xz stream; the updater's lzma reads it like a single-block one).

    Returns (threads, peak RAM of xz). Logs progress at least every 10 s. Without xz: single-threaded lzma.
    """
    xz = xz or find_xz()
    if not xz:
        log(' WARN ', f'{label}: xz.exe nicht gefunden, packe einthreadig mit lzma (langsam)')
        game_patch.pack_full(source, target)
        return 1, 0
    size = Path(source).stat().st_size
    started = last = time.time()
    part = Path(str(target) + '.part')
    with open(part, 'wb') as out:
        # --threads=+N: the multithreaded encoder even for 1 thread (the single-threaded one writes other block
        # headers), so the bytes never depend on the thread count.
        process = subprocess.Popen([xz, '-9', f'--threads=+{workers}', f'--block-size={XZ_BLOCK}', '--check=crc64',
                                    '-c', '-'],
                                   stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE)
        fed = 0
        try:
            with open(source, 'rb') as inp:
                while piece := inp.read(8 << 20):
                    process.stdin.write(piece)
                    fed += len(piece)
                    if time.time() - last >= 10:
                        last = time.time()
                        rate = fed / max(last - started, .01)
                        log(f'{100 * fed // max(size, 1):3d} %', f'xz {label}: {fed / 2**20:.0f}/{size / 2**20:.0f} MiB, '
                            f'{rate / 2**20:.0f} MiB/s, {workers} Threads, noch ~{(size - fed) / max(rate, 1):.0f} s')
        finally:
            process.stdin.close()
        error = process.stderr.read()
        code = process.wait()
        peak = peak_ram(process)
    if code:
        part.unlink()
        raise RuntimeError(f'xz failed ({code}): {error.decode(errors="replace")[-300:]}')
    part.replace(target)
    return workers, peak


def shrinks(path, sample=32 << 20):
    """True if xz saves at least 5 % on a sample (MPQ archives are already compressed)."""
    import lzma
    with open(path, 'rb') as stream:
        data = stream.read(sample)
    return len(lzma.compress(data, preset=1)) < .95 * len(data)


def make_full(target_file, new_sha, out, work, xz_threads=1):
    started = time.time()
    rel = target_file.relative_to(TARGET).as_posix()
    if not shrinks(target_file):
        # Raw: the file itself (updater 4), no pointless xz pass over e.g. 1.3 GB of MPQ data.
        asset = out / asset_name(rel, f'-{new_sha[:12]}.raw')
        if not asset.exists():
            shutil.copyfile(target_file, asset)
        if sha256(asset) != new_sha:
            raise RuntimeError(f'{asset.name} does not reproduce the target SHA-256')
        return dict(asset=asset.name, sha256=new_sha, size=asset.stat().st_size, encoding='raw'), \
            time.time() - started, 0.0, 0
    asset = out / asset_name(rel, f'-{new_sha[:12]}.xz')
    packed_with, xz_peak = pack_full_mt(target_file, asset, rel, xz_threads)
    size = target_file.stat().st_size
    log(' INFO ', f'xz {rel}: {time.time() - started:.1f} s, {packed_with} Threads, '
                  f'{size / 2**20 / max(time.time() - started, .01):.0f} MiB/s, Spitze {xz_peak / 2**20:.0f} MiB RAM')
    seconds, peak = verify('full', None, asset, size, new_sha, work)
    return dict(asset=asset.name, sha256=sha256(asset), size=asset.stat().st_size), time.time() - started, seconds, peak


def main():
    global TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=Path, required=True)
    ap.add_argument('--from', dest='sources', type=Path, action='append', default=[])
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--full-max-mb', type=float, default=256)
    ap.add_argument('--full-for-all', action='store_true',
                    help='full assets for every game file (fresh installs by the web installer; large ones raw)')
    ap.add_argument('--generator', type=Path, default=GENERATOR)
    args = ap.parse_args()
    TARGET = args.target
    t0 = time.time()
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.out / '.work'
    work.mkdir(exist_ok=True)

    log(' .... ', 'Spieldateien lesen')
    target = json.loads((args.target / 'package-manifest.json').read_text(encoding='utf-8'))
    game = sorted(p for p in target if game_file(p))
    olds = []
    for folder in args.sources:
        manifest = json.loads((folder / 'package-manifest.json').read_text(encoding='utf-8'))
        version = (folder / 'COOP-VERSION.txt').read_text(encoding='utf-8').strip()
        olds.append((folder, version, manifest))
    jobs = []
    for p in game:
        size = (args.target / p).stat().st_size
        seen = set()
        for folder, version, manifest in olds:
            old_sha = manifest.get(p)
            if old_sha and old_sha != target[p] and old_sha not in seen and (folder / p).is_file():
                seen.add(old_sha)
                jobs.append(('patch', p, folder / p, old_sha, version))
        missing = any(not (manifest.get(p) or (folder / p).is_file()) for folder, _, manifest in olds)
        if size <= args.full_max_mb * 2**20 and (missing or args.full_for_all):
            jobs.append(('full', p, None, None, None))
    log('  OK  ', f'Spieldateien lesen ({time.time() - t0:.1f} s, {len(game)} Spieldateien, {len(jobs)} Assets zu bauen)')

    workers = min(threads(), 4)
    xz_threads = max(1, threads() - workers + 1)  # One job's xz plus the other workers stay within the affinity.
    log(' INFO ', f'{len(jobs)} Assets mit {workers} Threads (Generator je Patch etwa 7x Dateigröße RAM), '
                  f'Vollassets: xz mit {xz_threads} Threads ({find_xz() or "kein xz.exe: lzma einthreadig"})')
    result = {p: dict(sha256=target[p], size=(args.target / p).stat().st_size, patches=[]) for p in game}
    started = time.time()
    with ThreadPoolExecutor(workers) as pool:
        futures = {}
        for kind, p, old_file, old_sha, version in jobs:
            log(' .... ', f'{kind} {p}' + (f' von {version} ({old_sha[:12]})' if version else ''))
            if kind == 'patch':
                futures[pool.submit(make_patch, args.target / p, old_file, old_sha, target[p], args.out, work,
                                    args.generator)] = (kind, p, version)
            else:
                futures[pool.submit(make_full, args.target / p, target[p], args.out, work, xz_threads)] = (kind, p, version)
        for done, future in enumerate(as_completed(futures), 1):
            kind, p, version = futures[future]
            entry, seconds, apply_seconds, peak = future.result()
            size = result[p]['size']
            if kind == 'patch':
                result[p]['patches'].append(entry)
            else:
                result[p]['full'] = entry
            log('  OK  ', f'{kind} {p}' + (f' von {version}' if version else '') +
                f' ({seconds:.1f} s, {entry["size"] / 2**20:.2f} MiB statt {size / 2**20:.1f} MiB = '
                f'{100 * entry["size"] / max(size, 1):.1f} %; Anwenden {apply_seconds:.1f} s, '
                f'Spitze {peak / 2**20:.0f} MiB RAM)')
            log(f'{100 * done // len(futures):3d} %', f'{done}/{len(futures)} Assets, '
                f'{sum(result[q]["size"] for q in result) / 2**20 / max(time.time() - started, .01):.0f} MiB/s Spieldaten')
    for entry in result.values():
        entry['patches'].sort(key=lambda e: e['from_sha256'])
    (args.out / 'gamefiles.json').write_text(json.dumps(result, indent=1, sort_keys=True), encoding='utf-8')
    for leftover in work.iterdir():
        leftover.unlink()
    work.rmdir()
    assets = sorted(p.name for p in args.out.iterdir() if p.suffix in ('.azp', '.xz', '.raw'))
    total = sum((args.out / a).stat().st_size for a in assets)
    log(' INFO ', f'Zusammenfassung: {len(assets)} Assets, {total / 2**20:.1f} MiB in {args.out} ({time.time() - t0:.0f} s)')
    return 0


TARGET = None

if __name__ == '__main__':
    sys.exit(main())
