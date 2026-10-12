#!/usr/bin/env python3
"""把「不该翻译的段落块」就地标好：公式、参考文献、标点碎片。

抽取阶段偶尔会把展示公式的一行判成正文段落（判据是逐行的，公式被拆行后容易漏），
章末参考文献也常常以段落身份进来。这类块的后果：当成待译条目白发给模型（数学和
书目只能原样抄回来），且对照稿里同一行印两遍（正文一次 + 英文对照一次）。

本工具对已抽取的书做事后补救，只改 ``type``/``no_translate``（并把共享块的 zh 文本
归位为英文底本），**不重切块**，所以块下标、已有译文映射都不受影响。

用法：
    python tools/retype_math.py                 # 只看会改哪些块（dry-run）
    python tools/retype_math.py --apply         # 落盘（同时改 blocks.json 与 zh-blocks.json）
    python tools/retype_math.py --apply --part ch03
    python tools/retype_math.py --refs-only     # 只处理参考文献（先确认判定是否合适）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import textnorm  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402

LABELS = {
    "equation": "段落 → 公式",
    "reference": "段落 → 参考文献（不译）",
    "debris": "段落 → 零碎片段（不译）",
    "running-head": "段落 → 页眉重复行（不译）",
}


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: pathlib.Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--apply", action="store_true", help="真的写回；默认只报告")
    parser.add_argument("--part", action="append", default=[], help="只处理这些篇章（可重复）")
    parser.add_argument("--show", type=int, default=6, help="每个篇章最多打印几例")
    parser.add_argument("--refs-only", action="store_true", help="只处理参考文献（不碰公式/碎片）")
    args = parser.parse_args()

    book = resolve_book(args.book)
    blocks_path = book.build_dir / "blocks.json"
    if not blocks_path.exists():
        raise SystemExit(f"缺少 {blocks_path}，请先运行：python tools/extract_pdf.py")
    payload = load(blocks_path)

    zh_path = book.build_dir / "zh-blocks.json"
    zh_payload = load(zh_path) if zh_path.exists() else None
    zh_parts = {part["slug"]: part["blocks"] for part in (zh_payload or {}).get("parts", [])}

    total = 0
    fixed_total = 0
    for part in payload["parts"]:
        slug = part["slug"]
        if args.part and slug not in args.part:
            continue
        changed = textnorm.retype_nonprose_blocks(part["blocks"])
        if args.refs_only:
            changed = {"reference": changed["reference"]}
        total += sum(len(indices) for indices in changed.values())
        mirror = zh_parts.get(slug)
        fixed = 0
        if mirror and len(mirror) == len(part["blocks"]):
            # 共享块（公式/表行/图）的 zh 文本必须与英文底本逐字相同：补判成公式的块，
            # 之前可能存着模型抄回来的旧文本（空格/连字符未必一致），这里一并归位，
            # 否则对齐校验会一直报「共享块中英文本不一致」。
            for source, target in zip(part["blocks"], mirror):
                if not source.get("no_translate"):
                    continue
                target["type"] = source["type"]
                target["no_translate"] = True
                if target.get("text") != source.get("text"):
                    target["text"] = source.get("text")
                    fixed += 1
        for kind, indices in changed.items():
            if not indices:
                continue
            print(f"{slug}：{len(indices)} 块 {LABELS.get(kind, kind)}")
            for index in indices[: args.show]:
                block = part["blocks"][index]
                print(f"    [{index}] p{block.get('page')}: {(block.get('text') or '')[:70]}")
        if fixed:
            print(f"{slug}：{fixed} 个共享块的 zh 文本归位为英文底本")
            fixed_total += fixed

    print(f"合计 {total} 块；共享块归位 {fixed_total} 块")
    if not total and not fixed_total:
        return 0
    if not args.apply:
        print("（未写回；确认无误后加 --apply）")
        return 0
    save(blocks_path, payload)
    print(f"已写回 {blocks_path}")
    if zh_payload:
        save(zh_path, zh_payload)
        print(f"已写回 {zh_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())