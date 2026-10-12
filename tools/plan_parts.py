#!/usr/bin/env python3
"""由 PDF 书签生成/刷新 books/<slug>/parts.json（篇章切分）。

parts.json 是人工可编辑的配置：生成后请打开确认一遍，之后的抽取都按它执行，
不再靠字号猜结构。`kind` 决定是否翻译：

    front      前言/序言等前置正文        —— 翻译
    part       分部引言                   —— 翻译
    chapter    章                         —— 翻译（其中的 REFERENCES 小节不译）
    library    Bibliography / References  —— 不译
    index      Author Index / Subject Index —— 不译
    skip       半书名页/版权页/目录       —— 不抽取

用法：
    python tools/plan_parts.py                     # 自动选唯一一本书
    python tools/plan_parts.py putting-auction-theory-to-work
    python tools/plan_parts.py --print             # 只打印，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fitz  # noqa: E402

from bookkit import planning  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（books/ 下的目录名）")
    parser.add_argument("--print", action="store_true", dest="dry_run", help="只打印，不写文件")
    parser.add_argument("--quiet", action="store_true", help="不打印清单")
    args = parser.parse_args()

    book = resolve_book(args.book)
    doc = fitz.open(book.pdf_path)
    parts = planning.finish_parts(planning.build_parts(doc, doc.page_count), doc.page_count)

    lines = [f"{book.slug}: {doc.page_count} 页，{len(doc.get_toc())} 条书签 → {len(parts)} 个篇章", ""]
    for part in parts:
        lines.append(
            f"  {part['slug']:<16} {part['kind']:<8} p{part['page_start']:>4}-{part['page_end']:<4} "
            f"{part['title_en']}"
        )
        for entry in part["entries"][1:6]:
            lines.append(f"        L{entry['level']} p{entry['page']}  {entry['title']}")
        if len(part["entries"]) > 6:
            lines.append(f"        … 另有 {len(part['entries']) - 6} 条")
    if not args.quiet:
        print("\n".join(lines))

    if args.dry_run:
        return 0
    target = book.dir / "parts.json"
    payload = {
        "_comment": "由 tools/plan_parts.py 生成；可手工调整 kind / page_start / page_end / title_en。",
        "slug": book.slug,
        "total_pages": doc.page_count,
        "parts": parts,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())