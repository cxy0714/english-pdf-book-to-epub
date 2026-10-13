#!/usr/bin/env python3
"""把逐块中英对照排成 Markdown。

产物（都在 books/<slug>/out/ 下，不入库）：

    book.bilingual.md   完整双语对照稿：正文英一段、中一段，公式/表格/图片共用
    book.zh.md          纯中文稿
    book.en.md          纯英文稿（校对用）
    <slug>.bilingual.md 分篇对照稿

约定
----
- 一个块一行，块之间空行，因此**块级对齐**在 Markdown 里也是可见的。
- 标题：中文标题 + 紧随其后的英文标题（斜体），层级用 `#`。
- 公式、表格行、图片行中英共用，不重复。
- 脚注在篇末统一排：`[^n]: 中文译文`，下面缩进一段 `原文：…`。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import pairing  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402


def render_pair(pair: pairing.Pair, mode: str) -> list[str]:
    """把一个块渲染成 Markdown 行（可能多行）。"""
    kind = pair.type
    zh, en = pair.zh_text, pair.en_text

    if pair.en.get("absorbed") or (not zh and not en and not pair.image):
        # 被公式裁图吸收掉的碎片块：内容已经在图的 alt 里，正文不再重复印
        return []

    if kind in ("part", "heading"):
        heading = zh if mode != "en" else en
        lines = [f"{'#' * min(6, max(1, pair.level))} {heading}"]
        if mode == "bilingual":
            other = en if zh else ""
            if other and other != heading:
                lines.append("")
                lines.append(f"*{other}*")
        return lines
    if kind == "list":
        if mode == "zh":
            return [f"- {zh}"]
        if mode == "en":
            return [f"- {en}"]
        return [f"- {zh}", "", f"- *{en}*"]
    if kind in ("caption", "source"):
        if mode == "zh":
            return [f"*{zh}*"]
        if mode == "en":
            return [f"*{en}*"]
        return [f"*{zh}*", "", f"*{en}*"]
    if kind == "figure":
        return [f"![{pair.en.get('meta', {}).get('alt', 'figure')}]({pair.image})"]
    if kind == "equation":
        if pair.image:
            alt = " ".join(en.split())[:120] or "formula"
            return [f"![{alt}]({pair.image})"]
        return [f"$$ {en} $$"]
    if kind == "row":
        return [en]
    if kind == "note":
        return []  # 脚注在篇末统一排
    if mode == "zh":
        return [zh]
    if mode == "en":
        return [en]
    return [zh, "", f"<small>{en}</small>"]


def render_notes(part: pairing.Part, mode: str) -> list[str]:
    if not part.notes:
        return []
    title = {"zh": "注释", "en": "Notes", "bilingual": "注释 / Notes"}[mode]
    lines = [f"## {title}"]
    for pair in part.notes:
        lines.append("")
        if mode == "en":
            lines.append(f"[^{pair.number}]: {pair.en_text}")
            continue
        lines.append(f"[^{pair.number}]: {pair.zh_text}")
        if mode == "bilingual" and pair.en_text:
            lines.append("")
            lines.append(f"    原文：{pair.en_text}")
    return lines


def render_part(part: pairing.Part, mode: str, with_title: bool = True) -> list[str]:
    lines: list[str] = []
    pairs = part.pairs if with_title else part.body
    for pair in pairs:
        rendered = render_pair(pair, mode)
        if not rendered:
            continue
        if lines:
            lines.append("")
        lines.extend(rendered)
    notes = render_notes(part, mode)
    if notes:
        lines.append("")
        lines.extend(notes)
    return lines


def render_book(book, parts: list[pairing.Part], mode: str) -> str:
    lines: list[str] = [f"# {book.title_zh if mode != 'en' else book.title_en}"]
    if mode == "bilingual":
        lines += ["", f"*{book.title_en}*"]
    lines += ["", f"{book.author}", ""]
    for part in parts:
        lines.append("")
        lines.extend(render_part(part, mode))
    text = "\n".join(lines)
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    book = resolve_book(args.book)
    parts = pairing.load_parts(book)
    problems = pairing.alignment_report(parts)
    if problems:
        print("对齐检查未通过：")
        for problem in problems:
            print(f"  - {problem}")
        if any("块数不足" in problem for problem in problems):
            print("\n请先运行：python tools/translate_mt.py")

    written: list[pathlib.Path] = []
    for mode, name in (("bilingual", "book.bilingual.md"), ("zh", "book.zh.md"), ("en", "book.en.md")):
        text = render_book(book, parts, mode)
        path = book.out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
        if not args.quiet:
            print(f"{path}  {len(text):,} 字符")

    for part in parts:
        text = render_book(book, [part], "bilingual")
        path = book.out_dir / f"{part.slug}.bilingual.md"
        path.write_text(text, encoding="utf-8")
        written.append(path)

    if not args.quiet:
        print(f"共写出 {len(written)} 个文件 → {book.out_dir}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())