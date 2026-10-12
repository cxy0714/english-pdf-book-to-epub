#!/usr/bin/env python
"""把人工译文/校订译文写回 ``build/zh-blocks.json``，或导出现有译文供校订。

机器翻译出第一遍后，用户是逐章校订的。直接改 ``sources/zh/*.md`` 或
``build/zh-blocks.json`` 都不合适：前者会被下一次翻译覆盖，后者要手改 JSON 结构。
这个工具提供往返闭环：

    # 1) 导出一章的译文（键是块下标），拿到可编辑的 JSON
    python tools/apply_zh.py --part ch03 --export _scratch/ch03.zh.json

    # 2) 改完之后写回（会先做中文规范化，再顺手质检）
    python tools/apply_zh.py --part ch03 --from _scratch/ch03.zh.json

只改指定篇章，其余篇章原样保留；写回后需重跑
``build_book_md.py`` / ``build_epub.py`` 才能反映到成品。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import mt, zh  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402
from translate_mt import markdown_for, zh_blocks_for  # noqa: E402

HELP = """JSON 格式：{"块下标": "译文", ...}，块下标与 build/blocks.json 一致。
未给出的块保持原样（未译的块仍是英文）。"""


def load_zh(path: pathlib.Path) -> dict:
    if not path.exists():
        raise SystemExit(f"没有 {path}，请先跑 python tools/translate_mt.py --part <篇>")
    return json.loads(path.read_text(encoding="utf-8"))


def export(book, slug: str, target: pathlib.Path) -> int:
    blocks_payload = json.loads((book.build_dir / "blocks.json").read_text(encoding="utf-8"))
    part = next((p for p in blocks_payload["parts"] if p["slug"] == slug), None)
    if part is None:
        raise SystemExit(f"没有篇章 {slug}")
    payload = load_zh(book.build_dir / "zh-blocks.json")
    zh_part = next((p for p in payload["parts"] if p["slug"] == slug), None)
    if zh_part is None:
        raise SystemExit(f"{slug} 还没有译文，请先翻译该篇")

    data = {}
    for index, (en_block, zh_block) in enumerate(zip(part["blocks"], zh_part["blocks"])):
        if not mt.is_translatable(en_block):
            continue
        data[str(index)] = zh_block.get("text") or ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    english = sum(1 for text in data.values() if not zh.has_cjk(text or ""))
    print(f"导出 {slug}：{len(data)} 条待译块 → {target}")
    print(f"其中 {english} 条还没有汉字（未译）")
    return 0


def apply(book, slug: str, source: pathlib.Path, quiet: bool = False) -> int:
    translations_in = json.loads(source.read_text(encoding="utf-8"))
    translations = {int(key): value for key, value in translations_in.items()}

    blocks_payload = json.loads((book.build_dir / "blocks.json").read_text(encoding="utf-8"))
    part = next((p for p in blocks_payload["parts"] if p["slug"] == slug), None)
    if part is None:
        raise SystemExit(f"没有篇章 {slug}")
    blocks = part["blocks"]

    adjusted = 0
    for index, text in translations.items():
        if index >= len(blocks):
            raise SystemExit(f"块下标 {index} 越界（{slug} 共 {len(blocks)} 块）")
        if not mt.is_translatable(blocks[index]) and zh.has_cjk(text or ""):
            if not quiet:
                print(f"  块{index} 是不翻译块（{blocks[index]['type']}），跳过")
            translations[index] = blocks[index].get("text") or ""
            continue
        normalized = zh.normalize(text)
        if normalized != text:
            adjusted += 1
            if not quiet:
                print(f"  块{index} 规范化：{text[:32]}… → {normalized[:32]}…")
        translations[index] = normalized

    zh_path = book.build_dir / "zh-blocks.json"
    payload = load_zh(zh_path)
    target = next((p for p in payload["parts"] if p["slug"] == slug), None)
    if target is None:
        raise SystemExit(f"{slug} 不在 zh-blocks.json 里，请先跑一次翻译或 --mirror")

    # 只覆盖 patch 里出现的块，其余块沿用现有译文（否则「改三段」会把整章退回英文）
    existing = {
        index: (zh_block.get("text") or "")
        for index, zh_block in enumerate(target["blocks"])
        if index < len(blocks) and mt.is_translatable(blocks[index])
    }
    merged = mt.merge_partial(existing, translations)
    target["blocks"] = zh_blocks_for(blocks, merged)
    zh_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    book.zh_md(slug).write_text(
        "\n\n".join(markdown_for(blocks, merged, None)) + "\n", encoding="utf-8"
    )

    filled = sum(1 for b in target["blocks"] if zh.has_cjk(b.get("text") or ""))
    translatable = sum(1 for b in blocks if mt.is_translatable(b))
    print(f"{slug}：本次改动 {len(translations)} 块（规范化调整 {adjusted} 块），"
          f"现含汉字 {filled}/{len(blocks)} 块（可译 {translatable} 块）")

    glossary = book.glossary()
    issues: list[str] = []
    for index, block in enumerate(target["blocks"]):
        if not mt.is_translatable(blocks[index]):
            continue
        for item in zh.lint(block.get("text") or "", blocks[index].get("text") or "", glossary):
            issues.append(f"块{index} {item}")
    if issues:
        print(f"质检：{len(issues)} 处问题（详见 python tools/lint_zh.py --part {slug}）")
        for item in issues[:10]:
            print(f"  - {item}")
        if len(issues) > 10:
            print(f"  … 另有 {len(issues) - 10} 处")
    else:
        print("质检：无问题")
    print(f"提示：重跑 python tools/build_book_md.py && python tools/build_epub.py 才会反映到成品")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", help="书目 slug（默认取 books/ 下唯一一本）")
    parser.add_argument("--part", required=True, help="篇章 slug，如 ch03")
    parser.add_argument("--from", dest="source", type=pathlib.Path, help="把该 JSON 里的译文写回")
    parser.add_argument("--export", type=pathlib.Path, help="把该篇现有译文导出成可编辑 JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if bool(args.source) == bool(args.export):
        parser.error("--from 与 --export 需要且只能给一个")

    book = resolve_book(args.book)
    if args.export:
        return export(book, args.part, args.export)
    return apply(book, args.part, args.source, args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())