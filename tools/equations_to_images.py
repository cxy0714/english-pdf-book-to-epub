"""把展示公式从 PDF 裁成图片（EPUB/Markdown 里直接插图，还原原书排版）。

为什么必须裁图：这本书的公式文本层是残缺的——积分号在 MTEX 字体里是私有码位
（``\\x02``），上下限和分式各占一行，抽成文字必然是「半条公式一行、积分号丢失」。
EPUB 阅读器又不跑 JavaScript（MathJax/KaTeX 用不了）、MathML 支持参差，纯文本方案
救不回来。裁图则与原书逐像素一致。

做法：
1. 同一页上**连续**的公式块（包括被拆出来的 ``(`` 这类碎片）合并成一条公式；
2. 按合并外框从 PDF 裁图（灰度、3 倍、自动裁掉四周空白），写成
   ``out/images/eq-<篇>-p<页>-<序号>.png``；
3. 组内**第一块**带上 ``image``（原文本留作 alt 可搜索），其余块文本清空并加
   ``absorbed`` 标记——**不删块、不合并下标**，所以已有译文与对齐关系完全不受影响；
4. ``blocks.json`` 与 ``zh-blocks.json`` 同步改，保证共享块中英文本一致。

用法：
    python tools/equations_to_images.py --part ch03            # 试点一篇（先看报告）
    python tools/equations_to_images.py --part ch03 --apply
    python tools/equations_to_images.py --apply               # 全书
    python tools/equations_to_images.py --apply --scale 2.5 --pad 2
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import fitz
import numpy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import textnorm  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402

# 一条公式内部的纵向间隙上限（pt）：超过就当成两条公式分开裁
GROUP_GAP = 6.0
# 一组最多多高（pt）：防止把满页公式连成一张图
GROUP_MAX_HEIGHT = 150.0
# 外框四周留白（pt），裁完再把空白裁掉
DEFAULT_PAD = 2.0


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: pathlib.Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def union(boxes: list[list[float]]) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def has_signal(text: str) -> bool:
    return any(signal in text for signal in textnorm.MATH_SIGNALS)


def is_candidate(block: dict, box: list[float] | None) -> bool:
    """这一块能不能算公式的一部分。"""
    if not box:
        return False
    text = (block.get("text") or "").strip()
    if block.get("type") == "equation":
        # 一段真句子混进来的（早期规则误标）不算公式
        return len(textnorm.prose_words(text)) < 3
    return textnorm.looks_like_debris(text)


def is_seed(block: dict) -> bool:
    """够格当一条公式的开头：带数学符号，或本身够长。"""
    text = (block.get("text") or "").strip()
    if block.get("type") != "equation":
        return False
    return has_signal(text) or len(text) >= 12


def worth_cropping(text: str, rect: list[float]) -> bool:
    """太碎的「公式」不值得裁图（``∫`` 单独一个符号、``x∗ +`` 这种断片）。

    这些多半是抽取时大运算符单独占了一行、公式主体被并进邻段的结果：裁成一张几像素
    宽的图反而更糟，不如让它照原样以文字出现。
    """
    width = rect[2] - rect[0]
    if width < 30:
        return False
    if "=" not in text and len(text.strip()) < 8 and width < 70:
        return False
    return True


def collect_groups(blocks: list[dict], columns: list) -> list[list[int]]:
    groups: list[list[int]] = []
    current: list[int] = []
    for index, block in enumerate(blocks):
        box = columns[index] if index < len(columns) else None
        if not is_candidate(block, box):
            if current:
                groups.append(current)
                current = []
            continue
        if current:
            previous = columns[current[-1]]
            merged = union([columns[i] for i in current] + [box])
            gap = box[1] - max(columns[i][3] for i in current)
            if block.get("page") != blocks[current[-1]].get("page") or gap > GROUP_GAP or merged[3] - merged[1] > GROUP_MAX_HEIGHT:
                groups.append(current)
                current = []
        current.append(index)
    if current:
        groups.append(current)
    return [group for group in groups if any(is_seed(blocks[i]) for i in group)]


def crop(doc: fitz.Document, page_no: int, box: list[float], target: pathlib.Path, scale: float, pad: float) -> tuple[int, int]:
    """按外框裁图，并把四周空白裁掉（先渲染一次量墨迹，再按换算出的紧贴矩形渲染一次）。

    不直接裁 Pixmap：PyMuPDF 各版本这个构造签名的行为不一致，重新渲染更稳。
    """
    page = doc[page_no - 1]
    rect = fitz.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    pixmap = page.get_pixmap(clip=rect, matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
    array = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(pixmap.height, pixmap.width)
    ink = array < 250  # 有字的地方
    rows = numpy.where(ink.any(axis=1))[0]
    cols = numpy.where(ink.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        raise ValueError("裁出来是空白")
    margin = 1.5  # pt
    tight = fitz.Rect(
        rect.x0 + cols[0] / scale - margin,
        rect.y0 + rows[0] / scale - margin,
        rect.x0 + (cols[-1] + 1) / scale + margin,
        rect.y0 + (rows[-1] + 1) / scale + margin,
    )
    pixmap = page.get_pixmap(clip=tight, matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
    pixmap.save(target)
    return pixmap.width, pixmap.height


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--apply", action="store_true", help="真的裁图并写回；默认只报告")
    parser.add_argument("--part", action="append", default=[], help="只处理这些篇章（可重复）")
    parser.add_argument("--scale", type=float, default=3.0, help="裁图放大倍数（默认 3.0）")
    parser.add_argument("--pad", type=float, default=DEFAULT_PAD, help="外框留白 pt（默认 2）")
    parser.add_argument("--show", type=int, default=5, help="每篇最多打印几条公式的尺寸")
    args = parser.parse_args()

    book = resolve_book(args.book)
    blocks_path = book.build_dir / "blocks.json"
    boxes_path = book.build_dir / "block-bboxes.json"
    if not blocks_path.exists() or not boxes_path.exists():
        raise SystemExit(
            f"缺少 {blocks_path.name} 或 {boxes_path.name}：\n"
            "  先跑 python tools/block_bboxes.py（抽取并记录每块在 PDF 上的外框）"
        )
    payload = load(blocks_path)
    boxes = load(boxes_path)["parts"]

    zh_path = book.build_dir / "zh-blocks.json"
    zh_payload = load(zh_path) if zh_path.exists() else None
    zh_parts = {part["slug"]: part["blocks"] for part in (zh_payload or {}).get("parts", [])}

    pdf_path = book.pdf_path if hasattr(book, "pdf_path") else next(book.source_dir.glob("*.pdf"))
    doc = fitz.open(pdf_path)

    images_dir = book.out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    total_groups = total_images = total_bytes = total_absorbed = skipped = 0
    failures: list[str] = []
    for part in payload["parts"]:
        slug = part["slug"]
        if args.part and slug not in args.part:
            continue
        columns = boxes.get(slug) or []
        groups = collect_groups(part["blocks"], columns)
        if args.apply:
            for stale in images_dir.glob(f"eq-{slug}-p*.png"):
                stale.unlink()
        mirror = zh_parts.get(slug)
        written: list[tuple[list[int], pathlib.Path, list[float], int, int]] = []
        for sequence, group in enumerate(groups, start=1):
            page_no = part["blocks"][group[0]].get("page") or 0
            name = f"eq-{slug}-p{page_no:03d}-{sequence}.png"
            target = images_dir / name
            rect = union([columns[i] for i in group])
            joined = " ".join((part["blocks"][i].get("text") or "").strip() for i in group)
            if not worth_cropping(joined, rect):
                skipped += 1
                continue
            try:
                if args.apply:
                    width, height = crop(doc, page_no, rect, target, args.scale, args.pad)
                else:
                    width = height = 0
            except Exception as error:  # noqa: BLE001 - 单条公式失败不该拖垮整本
                failures.append(f"{slug}[{group[0]}] {error}")
                continue
            written.append((group, target, rect, width, height))

        if written and args.show:
            print(f"{slug}：{len(written)} 张")
            for group, target, rect, width, height in written[: args.show]:
                text = " ".join((part["blocks"][i].get("text") or "").strip() for i in group)
                print(f"    p{part['blocks'][group[0]].get('page')} {width}×{height}px "
                      f"{len(group)} 块 ← {' '.join(text.split())[:60]}")

        if not args.apply:
            total_groups += len(written)
            continue

        image_blocks = {group[0]: target.name for group, target, _, _, _ in written}
        absorbed: set[int] = set()
        group_text: dict[int, str] = {}
        for group, _, _, _, _ in written:
            absorbed.update(group[1:])
            group_text[group[0]] = " ".join(
                (part["blocks"][i].get("text") or "").strip() for i in group
            ).strip()
        for index, block in enumerate(part["blocks"]):
            if index in image_blocks:
                block["type"] = "equation"
                block["no_translate"] = True
                block["image"] = f"images/{image_blocks[index]}"
                block.pop("absorbed", None)
                # 整组文本留在第一块上：alt 可搜索，重跑时也还能据此判断要不要裁图
                block["text"] = group_text[index]
            elif index in absorbed:
                block["type"] = "equation"
                block["no_translate"] = True
                block["text"] = ""
                block["image"] = ""
                block["absorbed"] = True
            elif block.get("type") == "equation" and block.get("image"):
                # 重跑时被跳过（够碎）的组：把上一轮的图摘掉，别留一张没用的旧图。
                # 只碰公式块——插图（figure）的 image 不归这里管，清掉会让书里的图消失。
                block.pop("image", None)
                block.pop("absorbed", None)
        if mirror is not None and len(mirror) == len(part["blocks"]):
            for index, block in enumerate(mirror):
                if index in image_blocks:
                    block["type"] = "equation"
                    block["no_translate"] = True
                    block["image"] = f"images/{image_blocks[index]}"
                    block.pop("absorbed", None)
                    block["text"] = group_text[index]
                elif index in absorbed:
                    block["type"] = "equation"
                    block["no_translate"] = True
                    block["text"] = ""
                    block["image"] = ""
                    block["absorbed"] = True
                elif block.get("type") == "equation" and block.get("image"):
                    block.pop("image", None)
                    block.pop("absorbed", None)

        total_groups += len(written)
        total_images += len(written)
        total_absorbed += len(absorbed)
        total_bytes += sum(target.stat().st_size for _, target, _, _, _ in written if target.exists())

    if args.apply:
        save(blocks_path, payload)
        if zh_payload is not None:
            save(zh_path, zh_payload)
        print(f"公式裁图 {total_images} 张，吸收碎片块 {total_absorbed} 块，"
              f"跳过碎片 {skipped} 处，合计 {total_bytes / 1024 / 1024:.1f} MB")
        print(f"已写回 {blocks_path.name} 与 {zh_path.name}，图片目录 {images_dir}")
    else:
        print(f"（未落盘）将裁 {total_groups} 张公式图；确认后加 --apply")
    if failures:
        print(f"有 {len(failures)} 条公式裁图失败：")
        for line in failures[:8]:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())