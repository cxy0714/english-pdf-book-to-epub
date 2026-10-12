#!/usr/bin/env python
"""规范化已有译文的中文标点与空格（不联网、不改动词句）。

用法：
    python tools/normalize_zh.py                 # 处理全部篇章
    python tools/normalize_zh.py --part ch01     # 只处理某篇
    python tools/normalize_zh.py --dry-run       # 只看会改多少，不落盘
    python tools/normalize_zh.py --all           # 连 sources/zh/*.md 一起重排

规则见 bookkit/zh.py；改规则后直接重跑本工具即可，不必重新调接口。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import pairing, zh  # noqa: E402
from bookkit.config import ConfigError, resolve_book  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="译文标点与空格规范化")
    parser.add_argument("--book", help="书目录名；只有一本书时可以省略")
    parser.add_argument("--part", action="append", help="只处理这些篇（可重复）")
    parser.add_argument("--dry-run", action="store_true", help="不写文件，只报统计")
    args = parser.parse_args()

    try:
        book = resolve_book(args.book)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    payload_path = book.build_dir / "zh-blocks.json"
    if not payload_path.exists():
        print(f"缺少 {payload_path}，请先运行：python tools/translate_mt.py")
        return 2
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    wanted = set(args.part or [])
    changed_blocks = 0
    total_blocks = 0
    skipped_shared = 0
    report: list[str] = [f"# 标点规范化报告 —— {book.slug}", ""]
    for part in payload.get("parts", []):
        slug = part["slug"]
        if wanted and slug not in wanted:
            continue
        changed = 0
        samples: list[str] = []
        for block in part.get("blocks", []):
            text = block.get("text") or ""
            if not text:
                continue
            # 公式、表行、图片与「不翻译」块必须与英文逐字一致，谁也不许动
            if block.get("type") in pairing.SHARED_TYPES or block.get("no_translate"):
                skipped_shared += 1
                continue
            total_blocks += 1
            fixed = zh.normalize(text)
            if fixed != text:
                changed += 1
                if len(samples) < 5:
                    samples.append(f"    改前：{text[:60]}\n    改后：{fixed[:60]}")
                block["text"] = fixed
        changed_blocks += changed
        report.append(f"- {slug}: {changed} 块被规范化")
        report.extend(samples)

    if not args.dry_run:
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        (book.qa_dir / "normalize-zh-report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"{'（试运行）' if args.dry_run else ''}检查 {total_blocks} 块，规范化 {changed_blocks} 块"
          f"（跳过 {skipped_shared} 个公式/表行/不翻译块）")
    if changed_blocks and not args.dry_run:
        print("下一步：python tools/build_book_md.py && python tools/build_epub.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())