#!/usr/bin/env python
"""让模型审校已有译文：只挑必须改的问题，不重译。

机械质检（`tools/lint_zh.py`）只能抓标点、术语、未译这类可判定的东西；漏译、误译、
数字不符、中文生硬这些得靠读。这一遍把「读」外包给模型，输出一份逐块问题清单，
供用户逐章校订时对照——**不改译文**，改不改由人决定。

用法：
    python tools/review_zh.py --part front              # 审校一篇，报告写到 qa/review-front.txt
    python tools/review_zh.py --part ch03 --json out.json
    python tools/review_zh.py                           # 全书（逐篇）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import mt  # noqa: E402
from bookkit.config import resolve_book  # noqa: E402


def load_zh(book) -> dict[str, list[dict]]:
    path = book.build_dir / "zh-blocks.json"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，请先跑 python tools/translate_mt.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {part["slug"]: part["blocks"] for part in payload["parts"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", help="书目 slug（默认取 books/ 下唯一一本）")
    parser.add_argument("--part", action="append", help="只审校指定篇章，可重复")
    parser.add_argument("--limit", type=int, help="每篇只审前 N 条（冒烟）")
    parser.add_argument("--json", type=pathlib.Path, help="把问题清单写成 JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    book = resolve_book(args.book)
    cfg = book.mt_config(None)
    if not cfg.get("api_key"):
        print("api_key 为空。请在 config/mt.local.json 里填写，或设置环境变量 MOYU_API_KEY / MT_API_KEY。")
        return 2
    blocks_payload = json.loads((book.build_dir / "blocks.json").read_text(encoding="utf-8"))
    blocks_by_slug = {part["slug"]: part["blocks"] for part in blocks_payload["parts"]}
    zh_by_slug = load_zh(book)

    wanted = args.part or [part["slug"] for part in blocks_payload["parts"]]
    translator = mt.Translator(book, cfg, logger=(lambda *a: None) if args.quiet else print)
    collected: dict[str, dict[str, list[str]]] = {}
    report_lines = ["篇章\t块下标\t页码\t问题", ]
    reviewed_total = 0
    issue_total = 0

    for slug in wanted:
        blocks = blocks_by_slug.get(slug)
        if blocks is None:
            print(f"跳过 {slug}：blocks.json 里没有")
            continue
        zh_blocks = zh_by_slug.get(slug, [])
        zh_texts = {
            index: (item.get("text") or "") for index, item in enumerate(zh_blocks) if index < len(blocks)
        }
        if not args.quiet:
            print(f"\n=== 审校 {slug} ===")
        started = time.time()
        issues, reviewed = translator.review_part(part={"slug": slug}, blocks=blocks, zh_texts=zh_texts, limit=args.limit)
        if reviewed == 0:
            print(f"  {slug}：没有可审的块（还没有中文译文）")
            continue
        reviewed_total += reviewed
        issue_total += sum(len(items) for items in issues.values())
        collected[slug] = {str(index): items for index, items in sorted(issues.items())}
        for index, items in sorted(issues.items()):
            page = blocks[index].get("page")
            for item in items:
                report_lines.append(f"{slug}\t{index}\t{page}\t{item}")
        if not args.quiet:
            print(
                f"  {slug}：审 {reviewed} 块，{len(issues)} 块有问题（{sum(len(v) for v in issues.values())} 条），"
                f"{time.time() - started:.1f}s"
            )
            for index, items in list(sorted(issues.items()))[:5]:
                print(f"    块{index}：{items[0][:100]}")

    report = book.qa_dir / f"review-{'-'.join(wanted) if len(wanted) < 3 else 'all'}.txt"
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(collected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"\n审校 {reviewed_total} 块，{issue_total} 条问题；报告：{report}"
        + (f"；JSON：{args.json}" if args.json else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())