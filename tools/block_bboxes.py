"""把每个块在 PDF 上的外框（bbox）抽出来，存成 build/block-bboxes.json。

块本身就带 bbox，但块序列是**抽取时**定下来的：事后重跑抽取可能因为规则调整而与已有
``blocks.json``（以及已译好的 ``zh-blocks.json``）错位，那会把译文对错块。所以这里不覆盖
blocks.json，只做「重跑一遍 → 逐块按 (页码, 文本) 与原文件对齐 → 对齐成功才写出侧车文件」，
对不上就报错退出，绝不猜。

用法：
    python tools/block_bboxes.py            # 抽取并对齐，写出 build/block-bboxes.json
    python tools/block_bboxes.py --check    # 只校验能否对齐
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import pdfextract  # noqa: E402
from bookkit.config import REPO_ROOT, resolve_book  # noqa: E402


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: pathlib.Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--restore", action="store_true", help="顺带把丢掉的 image（插图路径）补回 blocks.json/zh-blocks.json")
    parser.add_argument("--limit", type=int, default=12, help="错位时最多打印几例")
    args = parser.parse_args()

    book = resolve_book(args.book)
    blocks_path = book.build_dir / "blocks.json"
    if not blocks_path.exists():
        raise SystemExit(f"缺少 {blocks_path}，请先运行：python tools/extract_pdf.py")
    payload = load(blocks_path)
    existing = {part["slug"]: part["blocks"] for part in payload["parts"]}

    glyphmap = pdfextract.load_glyphmap(REPO_ROOT)
    extractor = pdfextract.Extractor(book, glyphmap)
    extractor.prepare()
    result = extractor.run()

    boxes: dict[str, list] = {}
    images: dict[str, list] = {}
    mismatches: list[str] = []
    for part in book.content_parts():
        slug = part["slug"]
        fresh = result.blocks.get(slug, [])
        old = existing.get(slug, [])
        if len(fresh) != len(old):
            mismatches.append(f"{slug}: 块数 {len(fresh)}（新抽取） ≠ {len(old)}（blocks.json）")
            continue
        columns = []
        pictures = []
        for index, (new_block, old_block) in enumerate(zip(fresh, old)):
            if (new_block.page, new_block.text.strip()) != (old_block.get("page"), (old_block.get("text") or "").strip()):
                mismatches.append(
                    f"{slug}[{index}]: 新抽取 {new_block.text[:40]!r} ≠ 已有 {(old_block.get('text') or '')[:40]!r}"
                )
            columns.append([round(value, 1) for value in new_block.bbox] or None)
            pictures.append(new_block.image or None)
        boxes[slug] = columns
        images[slug] = pictures

    if mismatches and not args.restore:
        print(f"对不上：{len(mismatches)} 处（不写文件）")
        for line in mismatches[: args.limit]:
            print("  " + line)
        print("提示：公式裁图（tools/equations_to_images.py）会改写块文本，跑完之后对本文件来说就是「对不上」了；")
        print("      补图片引用请用 --restore。")
        return 1
    if mismatches:
        print(f"（块文本已被公式裁图改写，跳过整体对齐检查：{len(mismatches)} 处）")

    total = sum(1 for columns in boxes.values() for box in columns if box)
    if not mismatches:
        print(f"对齐通过：{len(boxes)} 篇，{sum(len(c) for c in boxes.values())} 块，其中有外框 {total} 块")

    if args.restore:
        # 补回丢掉的 image 字段（插图块的图片路径）。公式裁图之后块文本已经变了、整体对齐
        # 不再成立，但插图块是唯一的类型，按**出现顺序**一一对应即可（两边数量必须相等）。
        zh_path = book.build_dir / "zh-blocks.json"
        zh_payload = load(zh_path) if zh_path.exists() else None
        zh_parts = {part["slug"]: part["blocks"] for part in (zh_payload or {}).get("parts", [])}
        fixed = 0
        for part in payload["parts"]:
            slug = part["slug"]
            fresh_pictures = [block.image for block in result.blocks.get(slug, []) if block.type == "figure" and block.image]
            targets = [
                block
                for block in part["blocks"]
                if block.get("type") == "figure" and not block.get("image")
            ]
            if len(fresh_pictures) != len(targets):
                print(f"  {slug}: 插图块 {len(targets)} 个 vs 新抽取 {len(fresh_pictures)} 个，跳过")
                continue
            mirror = zh_parts.get(slug) or []
            for block, picture in zip(targets, fresh_pictures):
                block["image"] = picture
                fixed += 1
                print(f"  补回 {slug} 第 {part['blocks'].index(block)} 块 → {picture}")
            for index, block in enumerate(part["blocks"]):
                if block.get("type") == "figure" and block.get("image") and index < len(mirror) and not mirror[index].get("image"):
                    mirror[index]["image"] = block["image"]
        if fixed:
            save(blocks_path, payload)
            if zh_payload is not None:
                save(zh_path, zh_payload)
        print(f"补回 {fixed} 个图片引用")
        return 0

    if args.check:
        return 0
    target = book.build_dir / "block-bboxes.json"
    target.write_text(
        json.dumps({"book": payload.get("book", {}), "parts": boxes}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"已写出 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())