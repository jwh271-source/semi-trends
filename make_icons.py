"""make_icons.py — PWA 플레이스홀더 아이콘 생성 (Pillow 불필요, 표준라이브러리만).
나중에 실제 아이콘으로 교체할 때 같은 파일명(site/icons/icon-*.png)으로 덮어쓰면 됨."""
import struct, zlib
from pathlib import Path

BG = (11, 92, 173)    # CSS --accent #0b5cad
FG = (255, 255, 255)

def _chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))

def make_png(size, dot_ratio=0.2):
    cx = cy = (size - 1) / 2
    r = size * dot_ratio  # 반지름 20% → maskable 안전영역(40%) 이내
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG 필터: None
        for x in range(size):
            px = FG if (x-cx)**2 + (y-cy)**2 <= r*r else BG
            rows.extend(px)
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)  # 8bit RGB
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr)
            + _chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + _chunk(b'IEND', b''))

out = Path(__file__).resolve().parent / 'site' / 'icons'
out.mkdir(exist_ok=True)
for name, size in [('icon-192.png', 192), ('icon-512.png', 512),
                   ('icon-maskable-512.png', 512), ('icon-180.png', 180)]:
    (out / name).write_bytes(make_png(size))
    print(f'[icons] {out / name}')
