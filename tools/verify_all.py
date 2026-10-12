#!/usr/bin/env python
"""一条命令验收整本书：结构对齐 + 文本质量 + 交付物完整性。

用法：
    python tools/verify_all.py                 # 全部检查
    python tools/verify_all.py --book <slug>
    python tools/verify_all.py --expect-zh     # 要求译文确实是中文（默认允许 --mirror 产物）

检查项：
  1. parts.json 与 build/blocks.json 的篇章集合一致；
  2. 每篇英文底本行数与块数一致；
  3. 中英块一一对应（见 bookkit.pairing.alignment_report）；
  4. 译文里没有控制字符、未映射字形 `▯`、残留占位符；
  5. 每篇译文汉字占比（低于 0.5 说明大段没翻）；
  6. 交付物齐备：双语/纯中文 Markdown、EPUB、以及 EPUB 校验结论。
退出码非 0 表示有不通过项。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import pairing, textnorm, zh  # noqa: E402
from bookkit.config import ConfigError, resolve_book  # noqa: E402

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def md_lines(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="整本书验收")
    parser.add_argument("--book", help="书目录名；只有一本书时可以省略")
    parser.add_argument(
        "--expect-zh",
        nargs="?",
        const="*",
        default=None,
        help="要求译文确实是中文（不允许多为 --mirror 排练产物）。可给篇章列表："
        "--expect-zh front,ch01；不给值则要求全书。逐章校订时用列表只卡已译篇章",
    )
    args = parser.parse_args()

    if args.expect_zh is None:
        expect_parts: set[str] = set()
        expect_all = False
    elif args.expect_zh.strip() in ("", "*"):
        expect_parts = set()
        expect_all = True
    else:
        expect_parts = {item.strip() for item in args.expect_zh.split(",") if item.strip()}
        expect_all = False

    def expects(slug: str) -> bool:
        return expect_all or slug in expect_parts

    if expect_parts or expect_all:
        scope = "全书" if expect_all else "、".join(sorted(expect_parts))
        print(f"要求中文化的篇章：{scope}")

    try:
        book = resolve_book(args.book)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    glossary = book.glossary()
    problems: list[str] = []
    notes: list[str] = []
    print(f"书：{book.slug}（{book.title_en}）")

    # 1. 篇章集合
    parts_meta = {part["slug"] for part in (book.parts or [])}
    parts = pairing.load_parts(book)
    blocks_slugs = {part.slug for part in parts}
    if parts_meta and parts_meta != blocks_slugs:
        only_meta = sorted(parts_meta - blocks_slugs)
        only_blocks = sorted(blocks_slugs - parts_meta)
        problems.append(f"parts.json 与 blocks.json 篇章不一致：仅配置有 {only_meta}，仅抽取有 {only_blocks}")
    print(f"  篇章 {len(parts)} 篇，块 {sum(len(p.pairs) for p in parts)} 个")

    # 2. 英文底本行数
    for part in parts:
        lines = md_lines(book.en_md(part.slug))
        if lines and len(lines) != len(part.pairs):
            problems.append(
                f"{part.slug}: 英文底本 {len(lines)} 行 ≠ 块数 {len(part.pairs)}"
                "（底本过时，请重跑 extract_pdf.py）"
            )

    # 3. 中英对齐
    problems.extend(pairing.alignment_report(parts))

    # 4. 译文文本质量
    lint_counts: Counter = Counter()
    untranslated_by_part: Counter = Counter()
    for part in parts:
        bad_control = bad_glyph = bad_placeholder = 0
        zh_chars = latin_words = 0
        for pair in part.pairs:
            if pair.shared:
                continue
            text = pair.zh_text
            if not text:
                continue
            if CONTROL_RE.search(text):
                bad_control += 1
            if textnorm.UNKNOWN_GLYPH in text:
                bad_glyph += 1
            if pairing.PLACEHOLDER_RE.search(text):
                bad_placeholder += 1
            for issue in zh.lint(text, pair.en_text, glossary):
                key = issue.split("（")[0].split("：")[0]
                lint_counts[key] += 1
                if key == "整块未见汉字":
                    untranslated_by_part[part.slug] += 1
            zh_chars += len(CJK_RE.findall(text))
            latin_words += len(LATIN_WORD_RE.findall(text))
        if bad_control:
            problems.append(f"{part.slug}: {bad_control} 块译文含控制字符")
        if bad_glyph:
            problems.append(f"{part.slug}: {bad_glyph} 块译文含未映射字形 {textnorm.UNKNOWN_GLYPH}")
        if bad_placeholder:
            problems.append(f"{part.slug}: {bad_placeholder} 块译文残留占位符")
        total = zh_chars + latin_words
        ratio = zh_chars / total if total else 0.0
        zh_md = md_lines(book.zh_md(part.slug))
        tag = "zh"
        if not zh_md:
            notes.append(f"{part.slug}: 还没有译文（先跑 translate_mt.py）")
        elif ratio < 0.5 and part.kind != "library":
            message = f"{part.slug}: 汉字占比仅 {ratio:.2f}（汉字 {zh_chars} / 英文词 {latin_words}）"
            if expects(part.slug):
                problems.append(message + "，译文可能没生成")
            else:
                notes.append(message + "，疑似 --mirror 排练产物")
        else:
            tag = f"zh {ratio:.2f}"
        print(f"  {part.slug:<18} {len(part.pairs):>5} 块  {tag}")

    # 4b. 译文质检汇总
    if lint_counts:
        untranslated = lint_counts.get("整块未见汉字", 0)
        other = sum(count for key, count in lint_counts.items() if key != "整块未见汉字")
        expected_untranslated = sum(
            count for slug, count in untranslated_by_part.items() if expects(slug)
        )
        if expected_untranslated:
            worst = "、".join(
                f"{slug} {count}"
                for slug, count in untranslated_by_part.most_common()
                if expects(slug)
            )
            problems.append(f"{expected_untranslated} 块未见汉字（译文可能没生成：{worst}）")
        print(f"  译文质检：{len(lint_counts)} 类问题，共 {sum(lint_counts.values())} 处"
              f"（整块未见汉字 {untranslated}，其余 {other}）")
        if expect_parts or expect_all:
            print(f"    其中「要求中文化」的篇章里未见汉字 {expected_untranslated} 块")
        for key, count in lint_counts.most_common(5):
            print(f"    {key}：{count}")
        notes.append("译文质检明细见 qa/zh-lint.txt（python tools/lint_zh.py）")

    # 5. 交付物
    out = book.out_dir
    expected = [
        out / "book.bilingual.md",
        out / "book.zh.md",
        out / "book.en.md",
        out / f"{book.slug}.bilingual.epub",
        out / f"{book.slug}.zh.epub",
    ]
    missing = [path.name for path in expected if not path.exists()]
    if missing:
        notes.append(f"交付物尚未生成：{', '.join(missing)}（先跑 build_book_md.py / build_epub.py）")

    epub_report = book.qa_dir / "epub-report.txt"
    if epub_report.exists():
        report = epub_report.read_text(encoding="utf-8")
        failed = [line for line in report.splitlines() if "不通过" in line or "错误" in line]
        if failed:
            problems.extend(failed)
        print(f"  EPUB 校验：{'有问题' if failed else '通过'}")
    else:
        notes.append("还没有 qa/epub-report.txt（先跑 check_epub.py）")

    print()
    if notes:
        print("提示：")
        for note in notes:
            print(f"  - {note}")
    if problems:
        print("不通过：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())