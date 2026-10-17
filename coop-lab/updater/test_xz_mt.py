"""Multithreaded xz for full game file assets (make_game_patches.pack_full_mt), boot-log format.

  test_xz_mt.py [--real <big file>]

1 thread and 16 threads give the same bytes, the updater's own reader (game_patch.unpack_full) restores the
file, damage is refused, without xz.exe it falls back to single-threaded lzma. --real also measures one big
file (time, size, peak RAM of xz) and unpacks it again.
"""
import hashlib
import random
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import game_patch  # noqa: E402
import make_game_patches as mg  # noqa: E402

RESULTS = []


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def test(name, fn):
    log(' .... ', name)
    t = time.time()
    try:
        info = fn()
        log('  OK  ', f'{name} ({time.time() - t:.2f} s{", " + info if info else ""})')
        RESULTS.append(True)
    except Exception as error:
        log('FAILED', f'{name}: {type(error).__name__}: {error}')
        for line in traceback.format_exc().splitlines()[-4:]:
            log(' INFO ', line)
        RESULTS.append(False)


def sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while piece := stream.read(1 << 22):
            digest.update(piece)
    return digest.hexdigest()


def main():
    real = Path(sys.argv[sys.argv.index('--real') + 1]) if '--real' in sys.argv else None
    work = Path(tempfile.mkdtemp(prefix='xz mt '))
    source = work / 'game file.bin'
    rng = random.Random(7264)  # Fixed seed: half noise, half repeated text, like an MPQ with tables.
    with open(source, 'wb') as out:
        for i in range(24):
            out.write(rng.randbytes(512 << 10) + (f'block {i} '.encode() * 70000)[:512 << 10])
    mg.XZ_BLOCK = 4 << 20  # 24 MiB in 6 blocks: several blocks even in a small test.
    log(' INFO ', f'xz: {mg.find_xz()}, Testdatei {source.stat().st_size / 2**20:.0f} MiB')

    def threads_equal():
        one, many = work / 'one.xz', work / 'many.xz'
        mg.pack_full_mt(source, one, 'test', 1)
        mg.pack_full_mt(source, many, 'test', 16)
        assert one.read_bytes() == many.read_bytes(), 'output depends on the thread count'
        return f'1 and 16 threads byte-equal ({one.stat().st_size / 2**20:.1f} MiB, sha {sha(one)[:12]})'
    test('Same bytes for 1 and 16 threads', threads_equal)

    def updater_reads_it():
        out = work / 'restored.bin'
        got = game_patch.unpack_full(work / 'many.xz', out, source.stat().st_size)
        assert got == sha(source) and out.read_bytes() == source.read_bytes()
        return 'game_patch.unpack_full (updater 4) restores it byte-equal'
    test('Updater unpacks the multi-block stream', updater_reads_it)

    def damaged():
        data = bytearray((work / 'many.xz').read_bytes())
        data[len(data) // 2] ^= 0x40
        (work / 'bad.xz').write_bytes(bytes(data))
        try:
            game_patch.unpack_full(work / 'bad.xz', work / 'bad.bin', source.stat().st_size)
        except ValueError as error:
            return f'refused: {error}'
        raise AssertionError('damaged asset accepted')
    test('Damaged asset refused', damaged)

    def fallback():
        original = mg.find_xz
        mg.find_xz = lambda: None
        try:
            used, _ = mg.pack_full_mt(source, work / 'lzma.xz', 'test', 16)
        finally:
            mg.find_xz = original
        assert used == 1
        assert game_patch.unpack_full(work / 'lzma.xz', work / 'lzma.bin', source.stat().st_size) == sha(source)
        return 'without xz.exe: lzma, single-threaded, still readable'
    test('Fallback without xz.exe', fallback)

    if real:
        def measure():
            mg.XZ_BLOCK = 64 << 20
            target = work / 'real.xz'
            started = time.time()
            used, peak = mg.pack_full_mt(real, target, real.name, max(1, mg.threads() - 3))
            seconds = time.time() - started
            got = game_patch.unpack_full(target, work / 'real.bin', real.stat().st_size)
            assert got == sha(real), 'real file not restored'
            (work / 'real.bin').unlink()
            return (f'{real.name}: {seconds:.0f} s with {used} threads, {real.stat().st_size / 2**20 / seconds:.0f} MiB/s, '
                    f'{target.stat().st_size / 2**20:.1f} MiB, xz peak {peak / 2**20:.0f} MiB RAM, unpacked byte-equal')
        test('Real file', measure)

    for p in sorted(work.iterdir()):
        p.unlink()
    work.rmdir()
    log(' INFO ', f'Zusammenfassung: {sum(RESULTS)}/{len(RESULTS)} bestanden')
    return 0 if all(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
