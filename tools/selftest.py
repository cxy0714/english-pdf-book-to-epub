#!/usr/bin/env python
"""不依赖书数据的自检：中文渲染路径 + 质检器的「反例」验证。

为什么需要它：整条流水线此前只在 `--mirror` 排练产物上跑过，那时 EPUB 里
一个汉字都没有——中文渲染、XML 转义、汉字占比统计这些路径从未被真正走过。
本脚本用一份手工构造的中文样张，把「块 → Markdown → EPUB → 校验」整条链跑通，
并且**故意造一个坏 EPUB 证明校验器确实会报错**（否则"通过"可能是假的）。

用法：
    python tools/selftest.py
退出码非 0 表示自检不通过。
"""

from __future__ import annotations

import base64
import pathlib
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import check_epub  # noqa: E402
from bookkit import epub, mt, pairing, zh  # noqa: E402
from build_book_md import render_book  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

UNKNOWN_GLYPH = check_epub.UNKNOWN_GLYPH


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else f' —— {detail}'}")


class StubBook:
    """够 epub.build / title_page 用的最小书目对象。"""

    slug = "selftest"
    title_zh = "自检样张"
    title_en = "Self Test Sample"
    author = "Test Author"
    data = {"publisher": "Test Press"}

    def __init__(self, root: pathlib.Path):
        self.out_dir = root


def block(kind: str, text: str, **extra) -> dict:
    payload = {"type": kind, "text": text, "page": 1, "level": 2, "number": 0, "no_translate": False}
    payload.update(extra)
    return payload


def sample_parts() -> list[pairing.Part]:
    """一份中文样张，覆盖所有块类型与几个容易出事的字符。"""
    pairs: list[pairing.Pair] = []

    def add(kind: str, en: str, cn: str | None = None, **extra) -> None:
        # 共享块（公式/表行/图）与不翻译块的中英文本必须完全一致，这是流水线的约定
        if kind in pairing.SHARED_TYPES and cn is None:
            cn = en
        en_block = block(kind, en, **extra)
        zh_block = None if cn is None else block(kind, cn, **extra)
        pairs.append(pairing.Pair(en=en_block, zh=zh_block))

    add("part", "1 Getting to Work", "1 让拍卖理论付诸实践")
    add("heading", "1.1 Introduction", "1.1 引言")
    add(
        "paragraph",
        "Suppose that x is the type and the payoff is v(x) & it holds.",
        "假设 x 是类型，收益为 v(x) 且成立[^1]。条件是 x ∈ (0,1) 与 R&D 投入。",
    )
    add("list", "first-price auction", "第一价格拍卖")
    add("row", "| Lot | Bidder | High Bid |")
    add("row", "| --- | --- | --- |")
    add("row", "| A | Sky Network TV | 2,371,000 |")
    add("equation", "v j (xˆ(X, N,t⃗), t j) ∈ arg max")
    add("figure", "Figure 1.1", image="images/fig1.png", meta={"alt": "Figure 1.1"})
    add("caption", "Figure 1.1: The auction.", "图1.1：拍卖过程。")
    add("source", "Source: FCC.", "来源：FCC。")
    add("note", "See Milgrom (2004).", "见 Milgrom（2004）。", number=1)
    add("paragraph", "Revenue rose 12.5% in 1994.", "1994 年收益增长12.5%。")
    return [
        pairing.Part(
            slug="selftest01",
            title_en="Chapter 1",
            title_zh="第1章",
            kind="chapter",
            page_start=1,
            page_end=2,
            pairs=pairs,
        )
    ]


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def tamper(source: pathlib.Path, target: pathlib.Path, injection: str) -> None:
    """把好 EPUB 改坏：往每个正文 XHTML 的 <body> 后塞一段坏内容。"""
    with zipfile.ZipFile(source) as archive:
        items = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for index, (info, data) in enumerate(items):
            if info.filename.startswith("OEBPS/text/") and info.filename.endswith(".xhtml"):
                text = data.decode("utf-8").replace("<body>", f"<body>{injection}", 1)
                data = text.encode("utf-8")
            # mimetype 必须首个且不压缩
            compression = zipfile.ZIP_STORED if index == 0 else zipfile.ZIP_DEFLATED
            out.writestr(info.filename, data, compress_type=compression)


def main() -> int:
    print("中文规范化")
    cases = [
        ("这是中文,后面是英文text.", "这是中文，后面是英文 text."),
        ("第3.2节和第2.5%的增长率", "第3.2节和第2.5%的增长率"),
        ("拍卖(auction)机制", "拍卖（auction）机制"),
        ("设 y=f(x)中文", "设 y=f(x) 中文"),
        ("中文[^1]后面", "中文[^1]后面"),
        ("Pure English, unchanged (ok).", "Pure English, unchanged (ok)."),
    ]
    for source, expect in cases:
        got = zh.normalize(source)
        check(f"normalize {source!r}", got == expect, f"得到 {got!r}，期望 {expect!r}")
    idempotent = all(zh.normalize(zh.normalize(text)) == zh.normalize(text) for text, _ in cases)
    check("normalize 幂等", idempotent)

    print("译文质检")
    check("检出未全角标点", zh.lint("这是中文,没全角。") == ["ASCII 标点紧贴汉字（应全角）"])
    check("检出整块未翻译", "整块未见汉字" in " ".join(zh.lint("This is a whole English paragraph with words.")))
    check("检出术语未译", any("术语未译" in item for item in zh.lint("收益 auction 上升。", "", {"terms": {"auction": "拍卖"}})))
    check("干净译文不报错", zh.lint("设 x 是类型，收益为 v(x)。") == [])

    print("部分校订合并")
    existing = {0: "第零块", 1: "第一块", 2: "第二块"}
    patched = mt.merge_partial(existing, {1: "改过的第一块"})
    check("只覆盖改动的块", patched == {0: "第零块", 1: "改过的第一块", 2: "第二块"}, str(patched))
    check("patch 不修改原字典", existing[1] == "第一块", str(existing))
    check(
        "空 patch 不会清空译文",
        mt.merge_partial(existing, {}) == existing,
        "改一段不该让整章退回英文",
    )

    print("类型标记不该混进译文")
    parsed, _ = mt.parse_response("[B1]（标题）第1章 开始工作\n[B2]（脚注）见前注。\n", [1, 2])
    check("剥掉行首类型标记", parsed[1] == "第1章 开始工作", parsed[1])
    check("脚注标记也剥掉", parsed[2] == "见前注。", parsed[2])
    check(
        "正文里的括号不受影响",
        mt.parse_response("[B1] 拍卖（英文 auction）很有效。", [1])[0][1] == "拍卖（英文 auction）很有效。",
    )

    print("术语命中区分大小写")
    glossary = {"terms": {"US": "美国", "auction": "拍卖"}, "keep_english": ["FCC", "Sky"]}
    hits = mt.glossary_hits(glossary, "Let us consider the US auction market, not fcc either.")
    check("全大写条目只在原样出现时命中", hits.get("US") == 1, str(hits))
    check("小写 us 不会误命中 US", "us" not in {k.lower() for k in hits} - {"us"}, str(sorted(hits)))
    check("小写单词条目仍然不区分大小写", hits.get("auction") == 1, str(hits))
    check("keep_english 也不误命中", "fcc" not in hits, str(sorted(hits)))

    root = pathlib.Path(tempfile.mkdtemp(prefix="bookkit-selftest-"))
    try:
        book = StubBook(root)
        parts = sample_parts()
        # 造一张 1×1 的 PNG，顺便验证图片确实被打进包里
        (root / "images").mkdir(parents=True, exist_ok=True)
        (root / "images" / "fig1.png").write_bytes(TINY_PNG)

        print("Markdown 渲染")
        markdown = render_book(book, parts, "bilingual")
        check("Markdown 含中文标题", "让拍卖理论付诸实践" in markdown)
        check("Markdown 含双语标记", "<small>" in markdown)
        check("Markdown 表格成形", markdown.count("| Sky Network TV |") == 1)
        check("Markdown 脚注成形", "[^1]" in markdown and "原文：" in markdown)

        print("对齐校验")
        check("共享块一致时不报警", pairing.alignment_report(parts) == [], str(pairing.alignment_report(parts)))
        broken = sample_parts()
        for pair in broken[0].pairs:
            if pair.type == "equation":
                pair.zh = block("equation", "被改过的公式")
        check("共享块被改时会报警", any("共享块" in item for item in pairing.alignment_report(broken)))

        print("EPUB 打包与校验")
        good = root / "good.bilingual.epub"
        stats = epub.build(book, parts, good, mode="bilingual")
        problems, notes, report = check_epub.check(good)
        check("合规 EPUB 校验通过", not problems, str(problems[:3]))
        check("EPUB 记录图片", report.get("images") == 1, str(report.get("images")))
        ratio = report.get("cjk_ratio") or 0
        check(f"双语版汉字占比合理（{ratio}）", ratio > 0.4, str(report.get("cjk_ratio")))
        check("脚注落入篇末", any(pair.type == "note" for pair in parts[0].pairs))

        zh_epub = root / "good.zh.epub"
        epub.build(book, parts, zh_epub, mode="zh")
        zh_problems, _, zh_report = check_epub.check(zh_epub)
        zh_ratio = zh_report.get("cjk_ratio") or 0
        check("纯中文版校验通过", not zh_problems, str(zh_problems[:3]))
        check(f"纯中文版汉字占比高（{zh_ratio}）", zh_ratio > 0.8, str(zh_report.get("cjk_ratio")))

        print("语言标记（阅读器读音 / 断行 / 字体）")
        with zipfile.ZipFile(good) as archive:
            bilingual_xhtml = "\n".join(
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.startswith("OEBPS/text/")
            )
        check(
            "双语版英文段落标了 lang=en",
            'class="en" lang="en"' in bilingual_xhtml,
            "英文对照没有 lang 标记，阅读器会按中文念",
        )
        check(
            "双语版中文段落不标 lang=en",
            'class="zh" lang="en"' not in bilingual_xhtml,
            "中文段落被误标成英文",
        )
        check("英文对照有拉丁字体栈", "Georgia" in epub.STYLE_CSS)

        print("校验器反例（证明「通过」不是假的）")
        for label, injection, keyword in [
            ("未转义 &", "<p>坏 A & B</p>", "合法 XML"),
            ("残留占位符", "<p>坏 [[fn1]] 占位符</p>", "占位符"),
            ("控制字符", "<p>坏 \x01 控制字符</p>", "控制字符"),
            ("未映射字形", f"<p>坏 {UNKNOWN_GLYPH} 字形</p>", "未映射字形"),
        ]:
            bad = root / f"bad-{abs(hash(label))}.epub"
            tamper(good, bad, injection)
            bad_problems, _, _ = check_epub.check(bad)
            joined = " | ".join(bad_problems)
            check(f"查出{label}", keyword in joined, joined[:200])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"自检不通过：{len(failed)}/{len(RESULTS)} 项失败")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"自检通过：{len(RESULTS)}/{len(RESULTS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())