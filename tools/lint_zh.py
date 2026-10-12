#!/usr/bin/env python
"""译文质检：把能机械判定的一致性问题找出来，供人工逐章校订。

用法：
    python tools/lint_zh.py                  # 出报告 qa/zh-lint.txt
    python tools/lint_zh.py --part ch01      # 只查某篇
    python tools/lint_zh.py --max 30         # 每类最多列 30 条样本
    python tools/lint_zh.py --strict         # 有「问题」时退出码非 0（接 CI）

只查不改。规则见 bookkit/zh.py 的 lint()。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import pairing, zh  # noqa: E402
from bookkit.config import ConfigError, resolve_book  # noqa: E402

# 这些是「提示」，其余算「问题」
ADVISORY = ("术语未译", "用了避免用词", "超长汉字串")


def main() -> int:
    parser = argparse.ArgumentParser(description="译文质检")
    parser.add_argument("--book", help="书目录名；只有一本书时可以省略")
    parser.add_argument("--part", action="append", help="只查这些篇（可重复）")
    parser.add_argument("--max", type=int, default=20, help="每类最多列多少条样本")
    parser.add_argument("--strict", action="store_true", help="有问题时返回非 0")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        book = resolve_book(args.book)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    glossary = book.glossary()
    parts = pairing.load_parts(book)
    wanted = set(args.part or [])

    problems: Counter = Counter()
    advisories: Counter = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    checked = 0
    untranslated_parts: list[str] = []

    for part in parts:
        if wanted and part.slug not in wanted:
            continue
        part_issues = 0
        for index, pair in enumerate(part.pairs):
            if pair.shared or pair.zh is None:
                continue
            text = pair.zh_text
            if not text:
                continue
            checked += 1
            issues = zh.lint(text, pair.en_text, glossary)
            for issue in issues:
                key = issue.split("（")[0].split("：")[0]
                bucket = advisories if issue.startswith(ADVISORY) else problems
                bucket[key] += 1
                part_issues += 1
                if len(samples[key]) < args.max:
                    samples[key].append(f"{part.slug} 块{index}：{issue} — {text[:50]}")
        if part_issues == 0 and any(not pair.shared for pair in part.pairs):
            untranslated_parts.append(part.slug)

    lines = [f"# 译文质检报告 —— {book.slug}", "", f"检查 {checked} 个待译块。", ""]
    if problems:
        lines.append("## 问题")
        for key, count in problems.most_common():
            lines.append(f"- {key}：{count} 处")
            lines.extend(f"    · {sample}" for sample in samples[key])
    else:
        lines.append("## 问题\n- 无")
    if advisories:
        lines.append("")
        lines.append("## 提示")
        for key, count in advisories.most_common():
            lines.append(f"- {key}：{count} 处")
            lines.extend(f"    · {sample}" for sample in samples[key])
    lines.append("")
    lines.append("## 整篇未见问题")
    lines.append("、".join(untranslated_parts) if untranslated_parts else "（无）")

    report_path = book.qa_dir / "zh-lint.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not args.quiet:
        print(f"检查 {checked} 块；问题 {sum(problems.values())} 处，提示 {sum(advisories.values())} 处")
        for key, count in problems.most_common(8):
            print(f"  问题  {key}：{count}")
        for key, count in advisories.most_common(5):
            print(f"  提示  {key}：{count}")
        print(f"报告：{report_path}")
    if args.strict and problems:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())