"""把六页界面快照拼成一张 3×2 概览图，放进 README 让人一眼看到效果。

    python tools/make_contact_sheet.py                     # 读 docs/shots，写 docs/shots/overview.png
    python tools/make_contact_sheet.py --out docs/overview.png

只依赖标准库：PNG 用 zlib + struct 手写（与 tools/make_samples.py 同一套做法），
不引入 Pillow。拼图本身是"把若干同尺寸 PNG 的像素块搬到画布上再加边距"，
对 800×480 这种尺寸用纯 Python 完全够快。
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import sys
import zlib

PAGE_ORDER = ("workbench", "scan", "environment", "delivery", "update", "status")
PAGE_LABELS = {
    "workbench": "任务工作台",
    "scan": "检测作业（首屏）",
    "environment": "环境与自检",
    "delivery": "数据交付",
    "update": "更新管理",
    "status": "设备状态",
}

#: 画布与排版（不放文字：手写点阵字模只覆盖 ASCII，中文标签写在 README 正文里）
BG = (216, 218, 220)        # 与界面外层背景同色
BORDER = (183, 189, 193)    # 分隔线色
GAP = 10
MARGIN = 12


# --------------------------------------------------------------------------- #
# 最小 PNG 读写
# --------------------------------------------------------------------------- #

def read_png(path: pathlib.Path):
    """读 8 位 RGB/RGBA 的 PNG，返回 (width, height, rgba bytes)。

    只支持本仓库自己生成的那几种格式（8 位、无隔行、RGB 或 RGBA），
    遇到不支持的格式直接报错，不用一套通用解码器把问题藏起来。
    """
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG")

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        chunk_type = raw[pos + 4 : pos + 8]
        data = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length  # 长度 + 类型 + 数据 + CRC
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
            if bit_depth != 8 or interlace != 0:
                raise ValueError(f"{path} 不是 8 位非隔行 PNG（bit_depth={bit_depth} interlace={interlace}）")
        elif chunk_type == b"IDAT":
            idat.extend(data)
        elif chunk_type == b"IEND":
            break

    channels = {2: 3, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"{path} 的颜色类型 {color_type} 不支持（只支持 RGB/RGBA）")

    decompressed = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 4)
    previous = bytearray(stride)
    cursor = 0
    for row in range(height):
        filter_type = decompressed[cursor]
        cursor += 1
        line = bytearray(decompressed[cursor : cursor + stride])
        cursor += stride
        _unfilter(filter_type, line, previous, channels)
        for column in range(width):
            source = column * channels
            target = (row * width + column) * 4
            out[target] = line[source]
            out[target + 1] = line[source + 1]
            out[target + 2] = line[source + 2]
            out[target + 3] = line[source + 3] if channels == 4 else 255
        previous = line
    return width, height, bytes(out)


def _unfilter(filter_type: int, line: bytearray, previous: bytearray, bpp: int) -> None:
    if filter_type == 0:
        return
    if filter_type == 1:
        for index in range(bpp, len(line)):
            line[index] = (line[index] + line[index - bpp]) & 0xFF
    elif filter_type == 2:
        for index in range(len(line)):
            line[index] = (line[index] + previous[index]) & 0xFF
    elif filter_type == 3:
        for index in range(len(line)):
            left = line[index - bpp] if index >= bpp else 0
            line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
    elif filter_type == 4:
        for index in range(len(line)):
            left = line[index - bpp] if index >= bpp else 0
            up = previous[index]
            upper_left = previous[index - bpp] if index >= bpp else 0
            estimate = left + up - upper_left
            pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - upper_left)
            predictor = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upper_left)
            line[index] = (line[index] + predictor) & 0xFF
    else:
        raise ValueError(f"未知的 PNG 行过滤器 {filter_type}")


def write_png(path: pathlib.Path, width: int, height: int, rgb: bytes) -> None:
    """写 8 位 RGB PNG（每行用 filter 0，实现最简单）。"""
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(rgb[row * stride : (row + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# 拼图
# --------------------------------------------------------------------------- #

def make_sheet(shots_dir: pathlib.Path, output: pathlib.Path, columns: int = 3, scale: float = 0.5) -> int:
    pages = []
    for key in PAGE_ORDER:
        path = shots_dir / f"{key}.png"
        if not path.is_file():
            print(f"缺少快照 {path}（先运行 python tools/ui_shots.py --out {shots_dir}）", file=sys.stderr)
            return 1
        pages.append((key, path))

    # 以第一页的尺寸为准，其余尺寸不同就报错 —— 静默缩放会让拼图看起来"差不多"但实际不对
    first_w, first_h, _ = read_png(pages[0][1])
    for key, path in pages[1:]:
        width, height, _ = read_png(path)
        if (width, height) != (first_w, first_h):
            print(f"{path} 尺寸 {width}×{height} 与 {pages[0][1]} 的 {first_w}×{first_h} 不一致", file=sys.stderr)
            return 1

    tile_w = max(1, int(first_w * scale))
    tile_h = max(1, int(first_h * scale))
    rows = (len(pages) + columns - 1) // columns
    canvas_w = MARGIN * 2 + tile_w * columns + GAP * (columns - 1)
    canvas_h = MARGIN * 2 + tile_h * rows + GAP * (rows - 1)

    canvas = bytearray(canvas_w * canvas_h * 3)
    for index in range(canvas_w * canvas_h):
        canvas[index * 3 : index * 3 + 3] = bytes(BG)

    def put_pixel(x: int, y: int, rgb_value) -> None:
        if 0 <= x < canvas_w and 0 <= y < canvas_h:
            offset = (y * canvas_w + x) * 3
            canvas[offset : offset + 3] = bytes(rgb_value)

    for index, (key, path) in enumerate(pages):
        source_w, source_h, rgba = read_png(path)
        origin_x = MARGIN + (index % columns) * (tile_w + GAP)
        origin_y = MARGIN + (index // columns) * (tile_h + GAP)

        # 边框
        for x in range(origin_x - 1, origin_x + tile_w + 1):
            put_pixel(x, origin_y - 1, BORDER)
            put_pixel(x, origin_y + tile_h, BORDER)
        for y in range(origin_y - 1, origin_y + tile_h + 1):
            put_pixel(origin_x - 1, y, BORDER)
            put_pixel(origin_x + tile_w, y, BORDER)

        # 最近邻缩放：界面截图里全是 UI 元素与文字，最近邻比双线性更能保住笔画
        for y in range(tile_h):
            source_y = min(source_h - 1, int(y / scale))
            for x in range(tile_w):
                source_x = min(source_w - 1, int(x / scale))
                offset = (source_y * source_w + source_x) * 4
                put_pixel(origin_x + x, origin_y + y, rgba[offset : offset + 3])
        print(f"  拼入 {key:12s} {PAGE_LABELS.get(key, '')}")

    output.parent.mkdir(parents=True, exist_ok=True)
    write_png(output, canvas_w, canvas_h, bytes(canvas))
    print(f"已生成概览图：{output}（{canvas_w}×{canvas_h}，{len(pages)} 页）")
    return 0


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="把界面快照拼成一张概览图")
    parser.add_argument("--shots", default=str(root / "docs" / "shots"), help="快照目录")
    parser.add_argument("--out", default=str(root / "docs" / "shots" / "overview.png"), help="输出 PNG")
    parser.add_argument("--columns", type=int, default=3, help="每行几页")
    parser.add_argument("--scale", type=float, default=0.5, help="缩放比例（0.5 表示原图一半）")
    args = parser.parse_args()
    return make_sheet(pathlib.Path(args.shots), pathlib.Path(args.out), args.columns, args.scale)


if __name__ == "__main__":
    raise SystemExit(main())
