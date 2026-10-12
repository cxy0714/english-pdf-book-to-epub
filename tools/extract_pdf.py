#!/usr/bin/env python3
"""从 PDF 抽取结构化英文底本：sources/en/<slug>.en.md + build/blocks.json。

用法：
    python tools/extract_pdf.py                       # 自动选唯一一本书，全书抽取
    python tools/extract_pdf.py putting-auction-theory-to-work
    python tools/extract_pdf.py --part ch01           # 只抽一章（调试用）
    python tools/extract_pdf.py --no-figures          # 不渲染插图
    python tools/extract_pdf.py --plan                # 顺带刷新 parts.json

产物（均不入库）：
    books/<slug>/sources/en/<slug>.en.md   英文底本，一行一个 block
    books/<slug>/build/blocks.json         结构化 block（含页码、类型、图、注）
    books/<slug>/build/text.txt            逐页纯文本（供词表与人工抽查）
    books/<slug>/out/images/*.png          从 PDF 矢量图渲染的插图
    books/<slug>/qa/extract-report.txt     抽取质量报告
    books/<slug>/qa/dehyphenation.tsv      断词判定中「词表未覆盖」的个案
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fitz  # noqa: E402

from bookkit import pdfextract, planning, textnorm  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug")
    parser.add_argument("--part", action="append", help="只处理指定篇章（可重复）")
    parser.add_argument("--no-figures", action="store_true", help="跳过插图渲染")
    parser.add_argument("--plan", action="store_true", help="抽取前刷新 parts.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    book = resolve_book(args.book)
    if not book.pdf_path.exists():
        print(f"找不到源 PDF：{book.pdf_path}", file=sys.stderr)
        print("请把原书 PDF 放到该路径（该目录已在 .gitignore 中，不会被提交）。", file=sys.stderr)
        return 2

    doc = fitz.open(book.pdf_path)
    parts_path = book.dir / "parts.json"
    if args.plan or not parts_path.exists():
        parts = planning.finish_parts(planning.build_parts(doc, doc.page_count), doc.page_count)
        parts_path.write_text(
            json.dumps(
                {
                    "_comment": "由 tools/plan_parts.py 生成；可手工调整。",
                    "slug": book.slug,
                    "total_pages": doc.page_count,
                    "parts": parts,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"已生成 {parts_path}（{len(parts)} 个篇章）")
    book = resolve_book(args.book)  # 重新载入 parts

    glyphmap = pdfextract.load_glyphmap(REPO_ROOT)
    extractor = pdfextract.Extractor(book, glyphmap)
    extractor.prepare()

    wanted = set(args.part or [])
    if wanted:
        book.parts = [p for p in book.parts if p["slug"] in wanted]
        if not book.parts:
            print(f"parts.json 里没有这些篇章：{', '.join(sorted(wanted))}", file=sys.stderr)
            return 2

    result = extractor.run()

    # --- 写出 ---
    blocks_payload = {
        "book": {
            "slug": book.slug,
            "title_en": book.title_en,
            "title_zh": book.title_zh,
            "author": book.author,
            "publisher": book.data.get("publisher", ""),
            "year": book.data.get("year", ""),
        },
        "parts": [
            {
                "slug": part["slug"],
                "kind": part.get("kind", "chapter"),
                "title_en": part["title_en"],
                "page_start": part["page_start"],
                "page_end": part["page_end"],
                "blocks": [block.to_dict() for block in result.blocks.get(part["slug"], [])],
            }
            for part in book.content_parts()
        ],
        "figures": result.figures,
        "stats": result.stats,
    }
    (book.build_dir / "blocks.json").write_text(
        json.dumps(blocks_payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    for part in book.content_parts():
        blocks = result.blocks.get(part["slug"], [])
        if not blocks:
            continue
        lines = [block.to_md() for block in blocks]
        book.en_md(part["slug"]).write_text("\n\n".join(lines) + "\n", encoding="utf-8")

    if not args.no_figures and extractor.figures:
        written = extractor.render_figures(book.images_dir)
        print(f"渲染插图 {len(written)} 张 → {book.images_dir}")

    # --- 报告 ---
    report = build_report(book, result)
    (book.qa_dir / "extract-report.txt").write_text(report, encoding="utf-8")
    unresolved = extractor.dehyphenator.unresolved
    if unresolved:
        rows = ["head\ttail\tjoined"] + [f"{a}\t{b}\t{c}" for a, b, c in unresolved]
        (book.qa_dir / "dehyphenation.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    if not args.quiet:
        print(report)
    return 0


def build_report(book, result: pdfextract.ExtractResult) -> str:
    lines = [f"# 抽取报告：{book.title_en}（{book.slug}）", ""]
    lines += result.qa_lines
    lines.append("")
    lines.append("## 各篇章统计")
    lines.append(f"{'篇章':<18}{'页范围':<14}{'块数':>7}{'段':>7}{'公式':>7}{'表行':>7}{'图':>5}{'注':>6}{'标题':>6}")
    for part in book.content_parts():
        blocks = result.blocks.get(part["slug"], [])
        if not blocks:
            continue
        counts = Counter(block.type for block in blocks)
        lines.append(
            f"{part['slug']:<18}"
            f"p{part['page_start']}-{part['page_end']:<9}"
            f"{len(blocks):>7}"
            f"{counts['paragraph']:>7}"
            f"{counts['equation']:>7}"
            f"{counts['row']:>7}"
            f"{counts['figure']:>5}"
            f"{counts['note']:>6}"
            f"{counts['heading'] + counts['part']:>6}"
        )
    lines.append("")
    lines.append("## 质检线索")
    lines.append(f"- 断词判定未覆盖词表（需人工过一遍 qa/dehyphenation.tsv）：{result.stats.get('unresolved_dehyphenation', 0)} 处")
    lines.append(f"- 页眉/页码删除行数：{result.stats.get('furniture_lines_removed', 0)}")

    # 残留可疑字符
    issues = Counter()
    samples: dict[str, list[str]] = {}
    math_chars = set("∈∀∃∑∫∏√≤≥≠≈∞∂∇⊗⊙→←↔")
    for part in book.content_parts():
        blocks = result.blocks.get(part["slug"], [])
        for index, block in enumerate(blocks):
            text = block.text or ""
            if textnorm.UNKNOWN_GLYPH in text:
                label = f"未映射字形 {textnorm.UNKNOWN_GLYPH}"
                issues[label] += 1
                samples.setdefault(label, []).append(f"p{block.page} {text[:70]}")
            if block.type == "paragraph" and len(text) > 6000:
                issues["超长段落（可能漏切）"] += 1
                samples.setdefault("超长段落（可能漏切）", []).append(f"p{block.page} {len(text)} 字符")
            if not (block.type == "paragraph" and text and not text[0].isupper()
                    and text[0].isalpha() and len(text) > 20):
                continue
            # 公式行前后的正文续行天然以小写开头（公式独立成块），属于预期情况，
            # 与「真的被切断」分开统计，否则这条质检线索会被淹没。
            previous = blocks[index - 1].type if index else ""
            head = text[:30]
            mathy = (
                previous in ("equation", "row")
                or any(char in math_chars for char in head)
                or textnorm.UNKNOWN_GLYPH in head
                # 形如 `v j (xˆ…`、`x ∈ …`：小写字母开头紧跟下标/运算符
                or bool(re.match(r"^[a-z]{1,3}\s*[(=∈≤≥+\-−′ˆ]", text))
            )
            if mathy:
                issues["段落紧接着公式块（预期，公式独立成块）"] += 1
            else:
                issues["段落以小写字母开头（可能被切断）"] += 1
                samples.setdefault("段落以小写字母开头（可能被切断）", []).append(
                    f"p{block.page} {text[:70]}"
                )
    if issues:
        for key, count in issues.most_common():
            lines.append(f"- {key}：{count} 处")
            for sample in samples.get(key, [])[:5]:
                lines.append(f"    · {sample}")
    else:
        lines.append("- 未发现上述问题")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())