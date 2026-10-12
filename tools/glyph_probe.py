#!/usr/bin/env python3
"""找出 PDF 里「文字层没有 Unicode 映射」的字形，并把它们渲染成一张对照图。

老式 PDF（Textures/Distiller）常用自有编码的数学字体，PyMuPDF 取到的是控制字符
（`\\x03`、`\\x0e` …）而不是 `∑`、`∫`。这些字形在全书里是**同码同形**的，
所以只要人工认一次，把结果写进 data/glyphmap.json，全书（以及以后同字体的书）
都能自动还原。

产物：
    books/<slug>/qa/glyph-sheet-1.png …   放大后的字形对照图（每格左上角是编号）
    books/<slug>/qa/glyph-candidates.tsv  编号 / 字体 / 码位 / 出现次数 / 上下文

用法：
    python tools/glyph_probe.py                    # 自动选唯一一本书
    python tools/glyph_probe.py --per-sheet 48
"""

from __future__ import annotations

import argparse
import io
import pathlib
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fitz  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from bookkit import pdfextract  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CELL = 190
PAD = 44


def is_missing(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or code == 0x7F or 0xE000 <= code <= 0xF8FF or 0x2400 <= code <= 0x2426


def collect(doc, math_prefixes) -> dict[tuple[str, str], dict]:
    found: dict[tuple[str, str], dict] = {}
    counts: Counter = Counter()
    for page_no in range(doc.page_count):
        page = doc[page_no]
        raw = page.get_text("rawdict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = "".join(
                    "".join(ch["c"] for ch in span["chars"]) for span in line["spans"]
                )
                for span in line["spans"]:
                    font = span.get("font", "")
                    for char in span["chars"]:
                        if not is_missing(char["c"]):
                            continue
                        key = (font, char["c"])
                        counts[key] += 1
                        record = found.get(key)
                        size = float(span.get("size", 0))
                        if record is None or size > record["size"]:
                            bbox = char["bbox"]
                            context = line_text.strip()
                            index = context.find(char["c"])
                            found[key] = {
                                "font": font,
                                "char": char["c"],
                                "code": f"U+{ord(char['c']):04X}",
                                "count": counts[key],
                                "size": size,
                                "page": page_no + 1,
                                "bbox": [round(v, 2) for v in bbox],
                                "context": context[:110],
                                "index": index,
                                "math": any(font.startswith(prefix) for prefix in math_prefixes),
                            }
    for key, record in found.items():
        record["count"] = counts[key]
    return found


def render_cell(doc, record: dict) -> Image.Image:
    page = doc[record["page"] - 1]
    x0, y0, x1, y1 = record["bbox"]
    # 公式字形的包围盒可能为 0 宽（组合符号），给一个最小框
    width = max(x1 - x0, record["size"] * 0.45)
    height = max(y1 - y0, record["size"] * 0.75)
    rect = fitz.Rect(x0 - 1.5, y0 - 1.5, x0 + width + 1.5, y0 + height + 1.5)
    zoom = max(1.0, 72.0 / max(record["size"], 1.0))
    pixmap = page.get_pixmap(clip=rect, matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")


def build_sheet(records: list[dict], doc, path: pathlib.Path, columns: int = 8) -> None:
    rows = (len(records) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * CELL, rows * (CELL + PAD)), "white")
    draw = ImageDraw.Draw(sheet)
    for position, record in enumerate(records):
        column, row = position % columns, position // columns
        left = column * CELL
        top = row * (CELL + PAD)
        draw.rectangle([left, top, left + CELL - 1, top + CELL + PAD - 1], outline="#cccccc")
        draw.text((left + 4, top + 3), f"#{position}  {record['code']}", fill="#000000")
        draw.text((left + 4, top + 15), f"{record['font'][:22]}", fill="#666666")
        draw.text((left + 4, top + 27), f"x{record['count']}", fill="#888888")
        try:
            image = render_cell(doc, record)
        except Exception as error:  # noqa: BLE001 - 渲染失败不该中断整张图
            draw.text((left + 4, top + 50), f"render error: {error}"[:28], fill="#aa0000")
            continue
        scale = min((CELL - 8) / max(image.width, 1), (CELL - 8) / max(image.height, 1), 1.0)
        if scale < 1.0:
            image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        sheet.paste(image, (left + (CELL - image.width) // 2, top + PAD // 2 + (CELL - image.height) // 2 + 20))
    sheet.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug")
    parser.add_argument("--per-sheet", type=int, default=48, help="每张对照图的字形数")
    args = parser.parse_args()

    book = resolve_book(args.book)
    doc = fitz.open(book.pdf_path)
    found = collect(doc, book.fonts["math_prefixes"])
    if not found:
        print("没有发现未映射字形。")
        return 0

    records = sorted(found.values(), key=lambda r: (not r["math"], r["font"], ord(r["char"])))
    rows = ["编号\t字体\t码位\t次数\t字号\t页\t字形所在行的上下文"]
    for position, record in enumerate(records):
        rows.append(
            f"{position}\t{record['font']}\t{record['code']}\t{record['count']}\t"
            f"{record['size']:.1f}\t{record['page']}\t{record['context']}"
        )
    (book.qa_dir / "glyph-candidates.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    for sheet_index in range(0, len(records), args.per_sheet):
        chunk = records[sheet_index:sheet_index + args.per_sheet]
        path = book.qa_dir / f"glyph-sheet-{sheet_index // args.per_sheet + 1}.png"
        build_sheet(chunk, doc, path)
        print(f"写入 {path}（#{sheet_index}–#{sheet_index + len(chunk) - 1}）")

    print(f"\n共 {len(records)} 个不同字形，{sum(r['count'] for r in records)} 次出现。")
    print(f"候选表：{book.qa_dir / 'glyph-candidates.tsv'}")
    print("\n请把识别结果写进 data/glyphmap.json，形如：")
    print('  { "MTEX": { "\\u0003": "∑" }, "*": { "\\u0007": "→" } }')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())