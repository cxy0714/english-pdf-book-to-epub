#!/usr/bin/env python
"""用本地假接口把翻译客户端整条路径跑一遍——不消耗真实 token。

翻译这一段代码（提示词组装、占位符屏蔽与还原、缓存、重试、缺条目拆批）此前
从未被任何服务器响应验证过。等真 key 到位才发现问题，代价是钱和时间；本脚本先用
一个本地 HTTP 假接口把这些路径全走一遍。

用法：
    python tools/mt_selftest.py
退出码非 0 表示失败。
"""

from __future__ import annotations

import http.server
import json
import pathlib
import re
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit import mt  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else f' —— {detail}'}")


class MockState:
    """假接口的状态：模式、收到的请求、命中次数。"""

    def __init__(self) -> None:
        self.mode = "normal"
        self.requests: list[dict] = []

    def reset(self, mode: str = "normal") -> None:
        self.mode = mode
        self.requests = []


STATE = MockState()
ITEM_RE = re.compile(r"^\[B(\d+)\]\s?(.*)$", re.MULTILINE)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        STATE.requests.append(payload)

        if STATE.mode == "fail_once" and len(STATE.requests) == 1:
            self._send(500, {"error": {"message": "模拟服务端错误", "type": "mock_error"}})
            return

        user = payload["messages"][-1]["content"]
        review = "【待审条目】" in user
        # 模拟网关超时（HTTP 524）：条目多于 4 条的请求一律超时。真实接口就是这样把
        # 「整章一次翻完」的大请求掐死的（9,399 字的整章请求重试四次、白等 15 分钟）。
        if STATE.mode == "gateway" and len(ITEM_RE.findall(user)) > 4:
            self._send(524, {"error": {"message": "openai_error", "type": "bad_response_status_code"}})
            return
        lines: list[str] = []
        first_request = len(STATE.requests) == 1
        for number, text in ITEM_RE.findall(user):
            # 模拟「响应被截断」：只在第一次请求里漏答部分条目，
            # 用来验证客户端「缺条目 → 拆批重试」能自愈
            if STATE.mode == "missing" and first_request and int(number) % 3 == 0:
                continue
            if review:
                # 模拟审校：每 3 条报一个问题；其余条目不报（= 没问题）
                if int(number) % 3 == 0:
                    lines.append(f"[B{number}] 术语不一致：auction 这里没译成「拍卖」")
                continue
            # 回显源文（保留占位符），便于断言「译文确实回填到了正确的块」
            # 且「占位符能原样还原」
            lines.append(f"[B{number}] 【译】{text.strip()[:200]}。")
        if review and not lines:
            lines = ["NOISSUES"]
        content = "\n".join(lines) if lines else "[B1] 【译】空。"
        self._send(
            200,
            {
                "id": "mock",
                "model": payload.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
            },
        )

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class StubBook:
    """够 Translator / build_prompt 用的最小书目对象。"""

    slug = "mt-selftest"
    title_en = "Mock Book"
    title_zh = "假书"
    author = "Mock Author"

    def __init__(self, root: pathlib.Path):
        self.build_dir = root
        self._glossary = {
            "terms": {"auction": "拍卖", "envelope theorem": "包络定理"},
            "names": {"Milgrom": "米尔格罗姆", "Vickrey": "维克里"},
            "keep_english": ["VCG"],
            "avoid": {"mechanism": "机制（不要译成“机理”）"},
        }

    def style_guide(self) -> str:
        return "保持学术书面语。"

    def glossary(self) -> dict:
        return self._glossary


def block(kind: str, text: str, number: int = 0) -> dict:
    return {"type": kind, "text": text, "page": 1, "level": 2, "number": number, "no_translate": False}


def sample(limit: int | None = None) -> tuple[dict, list[dict]]:
    blocks = [
        block("part", "1 Getting to Work"),
        block("paragraph", "Suppose that the payoff is v(x) and it holds[^1]."),
        block("paragraph", "The auction is efficient."),
        block("equation", "v j (x) ∈ arg max"),
        block("paragraph", "See Milgrom (2004) for the R&D evidence."),
        block("note", "See chapter 2.", number=1),
    ]
    part = {"slug": "mock01", "title_en": "Chapter 1", "title_zh": "第1章", "page_start": 1, "page_end": 2}
    return part, blocks


def make_cfg(base_url: str) -> dict:
    return {
        "base_url": base_url,
        "model": "mock-model",
        "api_key": "mock-key",
        "temperature": 0.15,
        "max_tokens": 2048,
        "timeout": 20,
        "retries": 3,
        "concurrency": 1,
        "target_output_chars": 400,
        "context_prev_blocks": 2,
        "context_next_blocks": 1,
        "context_prev_zh": True,
        "glossary_in_prompt": True,
        "chapter_brief": True,
    }


def end_to_end(base_url: str) -> None:
    """真跑一次 translate_mt.py（走 CLI），验证「请求 → 译文 → zh-blocks.json」的回填。

    这一步专门冲着块下标去的：CLI 按块下标取译文，而接口返回的是批内序号，
    两者一旦错位，整本书的译文会静默错行。测试会改动真实的 build/zh-blocks.json，
    所以在 finally 里恢复原样——失败时也会恢复，并明确警告。
    """
    import importlib
    import shutil

    from bookkit.config import resolve_book

    translate_cli = importlib.import_module("translate_mt")
    book = resolve_book(None)
    zh_blocks = book.build_dir / "zh-blocks.json"
    front_md = book.zh_md("front")
    report = book.qa_dir / "mt-report.txt"
    targets = [zh_blocks, front_md, report]
    backups = {path: (path.read_bytes() if path.exists() else None) for path in targets}
    blocks_payload = json.loads((book.build_dir / "blocks.json").read_text(encoding="utf-8"))
    front = next(part for part in blocks_payload["parts"] if part["slug"] == "front")
    before = {}
    if zh_blocks.exists():
        before = {
            part["slug"] for part in json.loads(zh_blocks.read_text(encoding="utf-8")).get("parts", [])
        }
    try:
        saved_argv = sys.argv
        sys.argv = [
            "translate_mt.py", "--part", "front", "--base-url", base_url, "--force", "--quiet",
        ]
        try:
            code = translate_cli.main()
        finally:
            sys.argv = saved_argv
        check("CLI 退出码为 0", code == 0, f"exit={code}")

        payload = json.loads(zh_blocks.read_text(encoding="utf-8"))
        written = next((part for part in payload["parts"] if part["slug"] == "front"), None)
        check("CLI 写出了 front 的逐块译文", written is not None)
        # --force 只表示「点名的篇章重译」，不能把没点名的篇章从 zh-blocks.json 里删掉。
        # 真踩过：--force --part front 跑完，其余十三篇的译文全没了。
        missing = before - {part["slug"] for part in payload["parts"]}
        check(
            "--force --part 不会删掉其他篇章",
            not missing,
            f"丢了 {sorted(missing)}",
        )
        if written is None:
            return
        blocks = written["blocks"]
        check("块数与英文底本一致", len(blocks) == len(front["blocks"]), f"{len(blocks)} vs {len(front['blocks'])}")
        cjk_blocks = [
            index
            for index, item in enumerate(blocks)
            if any("\u4e00" <= char <= "\u9fff" for char in (item.get("text") or ""))
        ]
        check("确实写进了中文", len(cjk_blocks) > 5, f"{len(cjk_blocks)} 块含汉字")
        # 关键：逐块比对「译文是不是落在该落的位置」
        mismatched = 0
        for index, (en_block, zh_block) in enumerate(zip(front["blocks"], blocks)):
            if en_block["type"] in ("equation", "row", "figure") or en_block.get("no_translate"):
                if (zh_block.get("text") or "") != (en_block.get("text") or ""):
                    mismatched += 1
                continue
            if en_block["type"] not in mt.TRANSLATABLE:
                continue
            source_head = (en_block.get("text") or "").strip()[:20]
            if source_head and source_head not in (zh_block.get("text") or ""):
                mismatched += 1
        check("每块译文都落在对应块上（无错行）", mismatched <= 1, f"{mismatched} 块对不上")
        front_md_exists = front_md.exists() and "。" in front_md.read_text(encoding="utf-8")
        check("中文 Markdown 已写出", front_md_exists)
    finally:
        restored = True
        for path, data in backups.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            except OSError:
                restored = False
        if not restored:
            print("  !! 未能恢复原始 zh-blocks.json / front.zh.md，请检查后手动重跑 --mirror")


def long_sample() -> tuple[dict, list[dict]]:
    """足够长的一篇：会被切成多批，用来验证「后一批看得见前一批的中文」。"""
    blocks = [block("part", "2 Multi-batch Chapter")]
    for index in range(1, 13):
        blocks.append(
            block(
                "paragraph",
                f"Paragraph {index}. The auction is efficient and the bidder profits, "
                f"so the seller's revenue rises when competition is strong. " * 6,
            )
        )
    part = {"slug": "mock02", "title_en": "Chapter 2", "title_zh": "第2章", "page_start": 3, "page_end": 9}
    return part, blocks


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    print(f"假接口：{base_url}")

    root = pathlib.Path(tempfile.mkdtemp(prefix="bookkit-mt-selftest-"))
    book = StubBook(root)
    cfg = make_cfg(base_url)

    try:
        print("请求格式与占位符保护")
        STATE.reset()
        translator = mt.Translator(book, cfg, logger=lambda *a: None)
        translator.cache_dir = root / "cache"
        translator.cache_dir.mkdir(parents=True, exist_ok=True)
        part, blocks = sample()
        translations, results = translator.translate_part(part, blocks)
        payload = STATE.requests[0]
        check("只发了 1 个请求", len(STATE.requests) == 1, f"{len(STATE.requests)} 个")
        check("请求带 model", payload.get("model") == "mock-model")
        check("请求带 temperature", payload.get("temperature") == 0.15)
        check("请求带 max_tokens", payload.get("max_tokens") == 2048)
        check("两条消息（system+user）", [m["role"] for m in payload["messages"]] == ["system", "user"])
        user = payload["messages"][-1]["content"]
        check("系统提示含风格约定", "学术书面语" in payload["messages"][0]["content"])
        check("提示含术语表", "auction" in user and "拍卖" in user)
        check("提示含人名表", "Milgrom" in user and "米尔格罗姆" in user)
        check(
            "无关术语被过滤掉（省重复开销）",
            "包络定理" not in user and "维克里" not in user,
            "envelope theorem / Vickrey 不该出现在这一批的提示里",
        )
        check("脚注被屏蔽为占位符", "[[fn1]]" in user and "[^1]" not in user, user[-200:])
        check("公式块不进待译条目", "arg max" not in user)
        check("脚注条目要翻译", "See chapter 2." in user)

        print("译文回填")
        check("译文按块下标回填", set(translations) == {0, 1, 2, 4, 5}, str(sorted(translations)))
        check("公式块未被翻译", 3 not in translations)
        check("占位符还原回脚注", "[^1]" in translations.get(1, ""), translations.get(1, ""))
        check("译文对应正确的块", "Suppose" in translations.get(1, "") and "auction" in translations.get(2, ""))
        check("篇名译文在第一块", "Getting to Work" in translations.get(0, ""), translations.get(0, ""))
        check("规范化已生效（全角句号）", translations.get(1, "").endswith("。"), translations.get(1, ""))

        print("缓存")
        STATE.reset()
        cached_translations, cached_results = translator.translate_part(part, blocks)
        check("命中缓存不再发请求", len(STATE.requests) == 0, f"{len(STATE.requests)} 个")
        check("缓存返回同样的译文", cached_translations == translations)
        check("缓存命中计数", translator.stats["cached"] >= 1)

        print("缺条目 → 拆批重试")
        STATE.reset("missing")
        translator2 = mt.Translator(book, cfg, logger=lambda *a: None)
        translator2.cache_dir = root / "cache2"
        translator2.cache_dir.mkdir(parents=True, exist_ok=True)
        translations2, results2 = translator2.translate_part(part, blocks)
        check("拆批后仍补齐全部译文", set(translations2) == {0, 1, 2, 4, 5}, str(sorted(translations2)))
        check("确实发了多次请求", len(STATE.requests) > 1, f"{len(STATE.requests)} 个")
        check("没有遗留失败批", not [r for r in results2 if r.error], str([r.error for r in results2]))

        print("服务端错误 → 重试")
        STATE.reset("fail_once")
        translator3 = mt.Translator(book, cfg, logger=lambda *a: None)
        translator3.cache_dir = root / "cache3"
        translator3.cache_dir.mkdir(parents=True, exist_ok=True)
        translations3, results3 = translator3.translate_part(part, blocks)
        check("500 后重试成功", not [r for r in results3 if r.error], str([r.error for r in results3]))
        check("重试后译文完整", set(translations3) == {0, 1, 2, 4, 5}, str(sorted(translations3)))
        check("重试次数正确", len(STATE.requests) == 2, f"{len(STATE.requests)} 个")

        print("冒烟限译与批切分")
        plan_all = mt.plan_batches(blocks, cfg)
        plan_limit = mt.plan_batches(blocks, cfg, limit=2)
        check("整章一次装下", len(plan_all) == 1 and len(plan_all[0].items) == 5, str(len(plan_all)))
        check("--limit 真的砍到切批之前", sum(len(b.items) for b in plan_limit) == 2, str(plan_limit))

        print("响应解析")
        parsed, missing = mt.parse_response("[B1] 第一段\n[ B2 ] 带空格也认\n[B3] 换行\n继续", [1, 2, 3])
        check("认 [Bn] 标记", parsed.get(1) == "第一段" and parsed.get(2) == "带空格也认", str(parsed))
        check("折行会拼接", parsed.get(3, "").startswith("换行"), str(parsed))
        check("没有缺失编号", missing == [], str(missing))
        _, missing2 = mt.parse_response("[B1] 只有一条", [1, 2, 3])
        check("缺失编号能报出", missing2 == [2, 3], str(missing2))

        print("前文带中文语境（跨批一致性）")
        STATE.reset()
        cfg_multi = dict(cfg)
        cfg_multi["concurrency"] = 3  # 开了前文中文语境就该被压到 1
        logs: list[str] = []
        translator4 = mt.Translator(book, cfg_multi, logger=logs.append)
        translator4.cache_dir = root / "cache4"
        translator4.cache_dir.mkdir(parents=True, exist_ok=True)
        part_long, blocks_long = long_sample()
        plan_long = mt.plan_batches(blocks_long, cfg_multi)
        check("长章确实切成多批", len(plan_long) > 1, f"{len(plan_long)} 批")
        long_translations, _ = translator4.translate_part(part_long, blocks_long)
        check("多批译文齐全", len(long_translations) == len(blocks_long), str(len(long_translations)))
        prompts = [payload["messages"][-1]["content"] for payload in STATE.requests]
        check("请求数等于批数", len(prompts) == len(plan_long), f"{len(prompts)} vs {len(plan_long)}")
        check("第一批没有前文中文语境", "【前文（已译" not in prompts[0], prompts[0][:200])
        check(
            "第二批起带上了已译中文",
            "【前文（已译" in prompts[1] and "中：" in prompts[1] and "原：" in prompts[1],
            prompts[1][:400],
        )
        # 前一批的译文必须真的出现在后一批的语境里（而不是空壳标题）
        last_item = plan_long[0].items[-1]
        first_batch_zh = long_translations[last_item.block]
        check(
            "语境里是前一批的真实译文",
            first_batch_zh[:12] in prompts[1],
            f"没找到 {first_batch_zh[:20]!r}",
        )
        check(
            "并发被压到 1（顺序译）",
            any("并发由 3 降为 1" in line for line in logs),
            f"日志里没有降并发提示：{logs[:3]}",
        )

        print("审校（只挑错、不重译）")
        parsed_review = mt.parse_review(
            "[B2] 漏译了半句\n[B3] 数字不符：原文 1994，译文 1984\n续行补充\n[ B9 ] 超范围要忽略\n",
            [1, 2, 3],
        )
        check("审校按编号归位", set(parsed_review) == {2, 3}, str(parsed_review))
        check("审校续行并进上一条", "续行补充" in parsed_review.get(3, [""])[0], str(parsed_review))
        check("审校忽略越界编号", 9 not in parsed_review)
        check("NOISSUES 表示没问题", mt.parse_review("NOISSUES", [1, 2]) == {})
        check("空响应不报错", mt.parse_review("", [1]) == {})

        review_items = [
            (0, "a " * 40, "甲" * 40),
            (1, "b " * 40, "乙" * 40),
            (2, "c " * 40, "丙" * 40),
            (3, "d " * 40, "丁" * 40),
        ]
        check(
            "审校按长度切批",
            len(mt.review_batches(review_items, {"review_batch_chars": 200})) > 1,
            str(len(mt.review_batches(review_items, {"review_batch_chars": 200}))),
        )

        STATE.reset()
        translator5 = mt.Translator(book, cfg, logger=lambda *a: None)
        translator5.cache_dir = root / "cache5"
        translator5.cache_dir.mkdir(parents=True, exist_ok=True)
        review_part, review_blocks = sample()
        # 只给前两个可译块中文，第三块留空：空的应被跳过
        zh_texts = {0: "第零块中文", 1: "第一块中文", 4: "第四块中文"}
        issues, reviewed = translator5.review_part(review_part, review_blocks, zh_texts)
        review_prompt = STATE.requests[0]["messages"][-1]["content"]
        check("只审有中文的块", reviewed == 3, f"审了 {reviewed} 块")
        check("没有中文的块不进审校请求", "阈值" not in review_prompt and "efficient" not in review_prompt)
        check("审校请求带术语表", "米尔格罗姆" in review_prompt, "被审的块里有 Milgrom，人名表该带上")
        check("审校请求含原文与译文对照", "原：" in review_prompt and "译：" in review_prompt)
        check("问题按块下标归位", set(issues) <= {0, 1, 4}, str(sorted(issues)))
        check("模拟的第 3 条问题落在第 4 块", 4 in issues, str(sorted(issues)))
        check("问题内容取到了", any("术语不一致" in item for items in issues.values() for item in items), str(issues))

        STATE.reset()
        cached_issues, cached_reviewed = translator5.review_part(review_part, review_blocks, zh_texts)
        check("审校命中缓存", len(STATE.requests) == 0, f"{len(STATE.requests)} 个请求")
        check("缓存返回同样结果", cached_issues == issues)

        print("网关超时应拆批而不是原地重试")
        STATE.reset()
        STATE.mode = "gateway"
        cfg_gw = dict(cfg)
        cfg_gw["retries"] = 2
        cfg_gw["max_tokens"] = 8192  # 抬高天花板，让样本先被排成一个大批
        cfg_gw["target_output_chars"] = 5000
        logs_gw: list[str] = []
        translator6 = mt.Translator(book, cfg_gw, logger=logs_gw.append)
        translator6.cache_dir = root / "cache6"
        translator6.cache_dir.mkdir(parents=True, exist_ok=True)
        part_gw, blocks_gw = long_sample()  # 13 块，切成 1 批 → 网关必超时
        plan_gw = mt.plan_batches(blocks_gw, cfg_gw)
        check("样本先被排成一个大批", len(plan_gw) == 1, f"{len(plan_gw)} 批")
        gw_translations, gw_results = translator6.translate_part(part_gw, blocks_gw)
        check("拆批后译文齐全", len(gw_translations) == len(blocks_gw), f"{len(gw_translations)} / {len(blocks_gw)}")
        check("拆批后没有失败批次", not [r for r in gw_results if r.error], str([r.error for r in gw_results if r.error]))
        sizes = [len(ITEM_RE.findall(r["messages"][-1]["content"])) for r in STATE.requests]
        check("没有第二次发同样大的请求", sizes.count(sizes[0]) == 1, f"请求条数序列 {sizes}")
        check(
            "超时后走的是拆批提示",
            any("请求过大" in line or "拆批" in line for line in logs_gw),
            str(logs_gw[:4]),
        )
        STATE.mode = "ok"

        print("单请求译文长度硬上限")
        many = [{"type": "paragraph", "text": "The auction is efficient and the bidder profits. " * 12} for _ in range(20)]
        cfg_cap = dict(cfg)
        cfg_cap["target_output_chars"] = 400
        cfg_cap["max_batch_output_chars"] = 500
        capped = mt.plan_batches(many, cfg_cap)
        check("整章放行也受硬上限约束", len(capped) > 1, f"{len(capped)} 批")
        check(
            "每批不超过硬上限（允许单块超限）",
            all(b.est_chars <= 500 or len(b.items) == 1 for b in capped),
            str([b.est_chars for b in capped]),
        )

        print("端到端（真跑一次 CLI，用假接口）")
        end_to_end(base_url)
    finally:
        server.shutdown()
        server.server_close()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"翻译自检不通过：{len(failed)}/{len(RESULTS)} 项失败")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"翻译自检通过：{len(RESULTS)}/{len(RESULTS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())