#!/usr/bin/env python3
"""调用机器翻译接口，把英文底本逐块译成中文。

块级对齐是**结构保证**的：请求里每条待译内容编号 ``[B1] [B2] …``，模型按同样的
编号逐条返回；译文按编号回填到原块位置，非翻译块（公式、表格行、图片、参考文献、
索引）原样镜像。所以 ``sources/zh/<slug>.zh.md`` 与 ``sources/en/<slug>.en.md``
的行数与顺序天然一致，后续排版只需按行配对。

用法：
    python tools/translate_mt.py --probe                      # 只验证 key / base_url / 模型
    python tools/translate_mt.py --part front --dry-run       # 看批切分方案，不联网
    python tools/translate_mt.py --part front                 # 真跑
    python tools/translate_mt.py                              # 全书
    python tools/translate_mt.py --part ch01 --limit 12       # 只译前 12 条（冒烟）
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


def load_blocks(book) -> dict[str, list[dict]]:
    path = book.build_dir / "blocks.json"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，请先运行：python tools/extract_pdf.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {part["slug"]: part["blocks"] for part in payload["parts"]}


def markdown_for(blocks: list[dict], translations: dict[int, str], limit: int | None) -> list[str]:
    """把译文回填成 Markdown 行；非翻译块原样镜像。"""
    lines: list[str] = []
    translatable_seen = 0
    for index, block in enumerate(blocks):
        text = block.get("text") or ""
        if mt.is_translatable(block):
            translatable_seen += 1
            if index in translations:
                text = translations[index]
            elif limit is not None and translatable_seen > limit:
                text = f"（待译）{text}"
        lines.append(_render(block, text))
    return lines


def zh_blocks_for(blocks: list[dict], translations: dict[int, str]) -> list[dict]:
    """生成结构化的译文块，供排版阶段直接按序号配原文，不必反解 Markdown。"""
    result: list[dict] = []
    for index, block in enumerate(blocks):
        item = {
            "type": block["type"],
            "text": translations.get(index, block.get("text") or ""),
            "page": block.get("page"),
        }
        for key in ("level", "image", "number", "meta", "no_translate"):
            if block.get(key):
                item[key] = block[key]
        result.append(item)
    return result


def _render(block: dict, text: str) -> str:
    kind = block["type"]
    if kind in ("heading", "part"):
        level = max(1, min(6, block.get("level") or 2))
        return f"{'#' * level} {text}"
    if kind == "list":
        return f"- {text}"
    if kind in ("caption", "source"):
        return f"*{text}*"
    if kind == "figure":
        return f"![{block.get('meta', {}).get('alt', 'figure')}]({block.get('image', '')})"
    if kind == "equation":
        return f"$$ {text} $$"
    if kind == "note":
        return f"[^{block.get('number', 0)}]: {text}"
    return text


def probe(cfg: dict) -> int:
    print(f"接口：{cfg['base_url']}")
    print(f"模型：{cfg['model']}")
    if not cfg.get("api_key"):
        print("api_key 为空。请在 config/mt.local.json 里填写，或设置环境变量 MOYU_API_KEY / MT_API_KEY。")
        return 2
    print(f"key：{cfg['api_key'][:6]}…（长度 {len(cfg['api_key'])}）")
    messages = [
        {"role": "system", "content": "你是测试助手，只回答被问到的内容。"},
        {"role": "user", "content": "请用一行中文回答：同步升价拍卖的英文通常缩写为什么？只回答缩写。"},
    ]
    started = time.time()
    try:
        content, usage = mt.chat(cfg, messages)
    except mt.MTError as error:
        print(f"失败：{error}")
        return 1
    print(f"耗时 {time.time() - started:.1f}s")
    print(f"回复：{content.strip()[:200]}")
    print(f"usage：{usage}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--part", action="append", default=[], help="只处理指定篇章，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只打印批切分方案，不联网")
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="排版预演：不联网，直接把英文照抄成 zh-blocks.json，用来先验证排版产物",
    )
    parser.add_argument("--probe", action="store_true", help="只发一个测试请求，验证配置")
    parser.add_argument("--limit", type=int, help="每篇只译前 N 条（冒烟测试）")
    parser.add_argument("--force", action="store_true", help="忽略已有译文与缓存，重译")
    parser.add_argument("--model", help="临时覆盖模型名")
    parser.add_argument("--base-url", help="临时覆盖接口地址（自检用，如 http://127.0.0.1:8765/v1）")
    parser.add_argument("--concurrency", type=int, help="临时覆盖并发数")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    book = resolve_book(args.book)
    cfg = book.mt_config(args.model)
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.concurrency:
        cfg["concurrency"] = args.concurrency

    if args.probe:
        return probe(cfg)

    blocks_by_part = load_blocks(book)
    wanted = args.part or [part["slug"] for part in book.content_parts()]
    rows = ["篇章\t块数\t待译\t译文估算字数\t批数\t最大批\t耗时s\tprompt\tcompletion\t状态"]
    total_started = time.time()
    exit_code = 0
    zh_payload: dict[str, list[dict]] = {}
    zh_blocks_path = book.build_dir / "zh-blocks.json"
    if zh_blocks_path.exists() and not args.force:
        existing = json.loads(zh_blocks_path.read_text(encoding="utf-8"))
        zh_payload.update({part["slug"]: part["blocks"] for part in existing.get("parts", [])})

    for slug in wanted:
        part = book.part(slug)
        blocks = blocks_by_part.get(slug) or []
        translatable = [i for i, block in enumerate(blocks) if mt.is_translatable(block)]
        if args.limit:
            translatable = translatable[: args.limit]
        estimated = sum(mt.estimate_zh(blocks[i]["text"]) for i in translatable)
        plan = mt.plan_batches(blocks, cfg, limit=args.limit)

        if not args.quiet:
            print(f"\n=== {slug}（{part.get('title_en') or ''}）p{part.get('page_start')}-{part.get('page_end')} ===")
            print(f"  块 {len(blocks)}，待译 {len(translatable)}，译文估算 {estimated} 字，批 {len(plan)}")
            for number, batch in enumerate(plan, start=1):
                pages = [blocks[item.block].get("page") for item in batch.items]
                print(
                    f"   批{number}: {len(batch.items)} 条，约 {batch.est_chars} 字，"
                    f"块 #{batch.items[0].block + 1}–#{batch.items[-1].block + 1}，p{pages[0]}–p{pages[-1]}"
                )

        if args.dry_run:
            prompt = (
                mt.build_prompt(book, part, blocks, plan[0], book.glossary(), cfg, book.style_guide())
                if plan
                else None
            )
            if prompt:
                print(f"  首批请求：system {len(prompt.system)} 字符 / user {len(prompt.user)} 字符")
                print("  ---- user 前 600 字 ----")
                print("  " + prompt.user[:600].replace("\n", "\n  "))
            rows.append(f"{slug}\t{len(blocks)}\t{len(translatable)}\t{estimated}\t{len(plan)}\t-\t-\t-\t-\tdry-run")
            continue

        if args.mirror:
            zh_payload[slug] = zh_blocks_for(blocks, {})
            zh_path = book.zh_md(slug)
            zh_path.write_text(
                "\n\n".join(markdown_for(blocks, {}, None)) + "\n",
                encoding="utf-8",
            )
            rows.append(f"{slug}\t{len(blocks)}\t{len(translatable)}\t{estimated}\t{len(plan)}\t-\t-\t-\t-\tmirror")
            continue

        zh_path = book.zh_md(slug)
        if zh_path.exists() and not args.force:
            existing = zh_path.read_text(encoding="utf-8").count("\n\n")
            if existing + 1 >= len(blocks):
                if not args.quiet:
                    print(f"  已有译文（{existing + 1} 块），跳过；要重译用 --force")
                rows.append(f"{slug}\t{len(blocks)}\t{len(translatable)}\t{estimated}\t{len(plan)}\t-\t-\t-\t-\t已存在")
                continue

        translator = mt.Translator(book, cfg, logger=(lambda *a: None) if args.quiet else print)
        started = time.time()
        translations, results = translator.translate_part(part, blocks, limit=args.limit)
        elapsed = time.time() - started

        lines = markdown_for(blocks, translations, args.limit)
        zh_path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        zh_payload[slug] = zh_blocks_for(blocks, translations)

        failed = [result for result in results if result.error]
        if failed:
            exit_code = 1
        status = f"失败{len(failed)}批" if failed else "完成"
        if args.limit and len(translatable) < len(
            [i for i, block in enumerate(blocks) if mt.is_translatable(block)]
        ):
            status += "（限译）"
        biggest = max((len(batch.items) for batch in plan), default=0)
        rows.append(
            f"{slug}\t{len(blocks)}\t{len(translatable)}\t{estimated}\t{len(plan)}\t{biggest}\t"
            f"{elapsed:.1f}\t{translator.stats['prompt_tokens']}\t{translator.stats['completion_tokens']}\t{status}"
        )
        if not args.quiet:
            print(f"  → {zh_path}（{len(lines)} 块，{elapsed:.1f}s，缓存命中 {translator.stats['cached']}）")

    report = book.qa_dir / "mt-report.txt"
    report.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if zh_payload:
        ordered = [part["slug"] for part in book.content_parts() if part["slug"] in zh_payload]
        zh_blocks_path.write_text(
            json.dumps(
                {
                    "_comment": "由 tools/translate_mt.py 生成：逐块译文，与 build/blocks.json 的下标一一对应。",
                    "slug": book.slug,
                    "parts": [{"slug": slug, "blocks": zh_payload[slug]} for slug in ordered],
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"逐块译文：{zh_blocks_path}")
    if not args.quiet:
        print(f"\n总耗时 {time.time() - total_started:.1f}s；报告：{report}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())