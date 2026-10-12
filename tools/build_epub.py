#!/usr/bin/env python3
"""把逐块对照稿打包成 EPUB3（中英双语 + 纯中文两种）。

产物（books/<slug>/out/，不入库）：
    <slug>.bilingual.epub   中英双语对照
    <slug>.zh.epub          纯中文

用法：
    python tools/build_epub.py
    python tools/build_epub.py --mode zh
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import epub, pairing  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--mode", choices=["bilingual", "zh"], action="append", help="只打包指定版本")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    book = resolve_book(args.book)
    parts = pairing.load_parts(book)
    if not parts:
        raise SystemExit("没有可排版的篇章，请先运行 tools/extract_pdf.py")
    problems = pairing.alignment_report(parts)
    modes = args.mode or ["bilingual", "zh"]

    exit_code = 0
    if problems and "bilingual" in modes and not args.quiet:
        print("对齐提示：")
        for problem in problems:
            print(f"  - {problem}")
        exit_code = 1

    for mode in modes:
        suffix = "bilingual" if mode == "bilingual" else "zh"
        out_path = book.out_dir / f"{book.slug}.{suffix}.epub"
        info = epub.build(book, parts, out_path, mode=mode)
        if not args.quiet:
            print(
                f"{out_path}  {info['bytes'] / 1024:.0f} KB，"
                f"{info['parts']} 篇，脚注 {info['notes']} 条，图 {info['images']} 张"
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())