"""Write coop-lab/azsurvivors.ico: the launcher icon of the installer and updater exe (deterministic, no game art).

  make_icon.py [--out coop-lab/azsurvivors.ico]

A crimson shield with a gold rim and a silver sword, drawn per pixel with 4x4 supersampling, as PNG images of
16, 32, 48 and 256 px in one ICO (Windows Vista and newer read PNG entries). Same bytes on every run.
"""
import argparse
import struct
import zlib
from pathlib import Path

SIZES = (16, 32, 48, 256)
CRIMSON, DARK, GOLD, SILVER, HILT = (150, 28, 34), (88, 14, 20), (222, 176, 56), (226, 230, 236), (120, 78, 38)


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


def shield(x, y):
    """Signed distance-like test in unit space (0..1): inside the shield outline?"""
    cx = abs(x - .5)
    if y < .1 or y > .94:
        return False
    if y < .58:
        return cx <= .4 - (.02 if y < .14 else 0)
    # Lower part narrows to the tip along a curve.
    t = (y - .58) / .36
    return cx <= .4 * (1 - t * t) ** .5 * (1 - .15 * t)


def colour(x, y):
    if not shield(x, y):
        return None
    inner = shield(.5 + (x - .5) * 1.13, .1 + (y - .14) * 1.09)  # The rim: inside the outline, outside a smaller one.
    if not inner:
        return GOLD
    # Sword: blade, guard, grip, pommel.
    cx = abs(x - .5)
    if .2 <= y <= .66 and cx <= .035 * (1 if y > .26 else (y - .17) / .09):
        return SILVER
    if .66 < y <= .7 and cx <= .19:
        return GOLD
    if .7 < y <= .82 and cx <= .03:
        return HILT
    if .82 < y <= .87 and cx <= .055:
        return GOLD
    return DARK if y > .5 + .35 * abs(x - .5) else CRIMSON  # A darker lower half gives the shield depth.


def image(size):
    rows = []
    samples = 4
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0.
        for px in range(size):
            acc, hits = [0, 0, 0], 0
            for sy in range(samples):
                for sx in range(samples):
                    c = colour((px + (sx + .5) / samples) / size, (py + (sy + .5) / samples) / size)
                    if c:
                        hits += 1
                        acc = [a + b for a, b in zip(acc, c)]
            alpha = round(255 * hits / samples ** 2)
            row += bytes([round(a / hits) for a in acc] if hits else [0, 0, 0]) + bytes([alpha])
        rows.append(bytes(row))
    raw = b''.join(rows)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=Path(__file__).resolve().parent / 'azsurvivors.ico')
    args = ap.parse_args()
    log(' .... ', f'Icon {args.out.name}')
    images = [image(size) for size in SIZES]
    header = struct.pack('<HHH', 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b'', b''
    for size, png in zip(SIZES, images):
        entries += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    args.out.write_bytes(header + entries + data)
    log('  OK  ', f'Icon {args.out.name} ({len(header + entries + data)} B, {", ".join(map(str, SIZES))} px)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
