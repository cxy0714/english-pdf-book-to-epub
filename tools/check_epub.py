#!/usr/bin/env python3
"""校验 EPUB 是否合规、内容是否完整。

检查项
------
1. `mimetype` 是 ZIP 第一个条目、未压缩、内容正确（EPUB 规范硬要求）。
2. `META-INF/container.xml` 指向的 OPF 存在且能解析。
3. OPF 的 manifest 里每个 href 都真实存在；spine 里每个 idref 都在 manifest 中。
4. 每个 XHTML 都能作为 XML 解析（能抓住未转义的 `&`、`<` 这类排版事故）。
5. nav.xhtml 解析通过，所有链接（含 `#锚点`）都能落到真实文件与真实 id。
6. 正文没有残留占位符 `[[fn1]]`、未映射字形 `⬚`、控制字符。
7. 统计各篇中文字数占比、脚注数、图片数，写进 qa/epub-report.txt。

用法：
    python tools/check_epub.py
    python tools/check_epub.py books/putting-auction-theory-to-work/out/xxx.epub
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bookkit.config import resolve_book  # noqa: E402

XHTML_NS = "{http://www.w3.org/1999/xhtml}"
OPF_NS = "{http://www.idpf.org/2007/opf}"
CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"

PLACEHOLDER_RE = re.compile(r"\[\[(?:fn|m|img)\d+\]\]")
UNKNOWN_GLYPH = "\u25af"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def check(path: pathlib.Path) -> tuple[list[str], list[str], dict]:
    problems: list[str] = []
    notes: list[str] = []
    stats: dict = {"path": path.name, "size_kb": round(path.stat().st_size / 1024)}

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if not names or names[0] != "mimetype":
            problems.append(f"mimetype 不是第一个条目（实际第一个是 {names[0] if names else '空'}）")
        else:
            info = archive.getinfo("mimetype")
            if info.compress_type != zipfile.ZIP_STORED:
                problems.append("mimetype 被压缩了（规范要求 stored）")
            content = archive.read("mimetype").decode("ascii", "replace")
            if content != "application/epub+zip":
                problems.append(f"mimetype 内容不正确：{content!r}")

        if "META-INF/container.xml" not in names:
            problems.append("缺少 META-INF/container.xml")
            return problems, notes, stats
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(f"{CONTAINER_NS}rootfiles/{CONTAINER_NS}rootfile")
        if rootfile is None:
            problems.append("container.xml 里没有 rootfile")
            return problems, notes, stats
        opf_path = rootfile.get("full-path")
        if opf_path not in names:
            problems.append(f"container.xml 指向的 {opf_path} 不存在")
            return problems, notes, stats

        opf = ET.fromstring(archive.read(opf_path))
        base = pathlib.PurePosixPath(opf_path).parent
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.iter(f"{OPF_NS}item")
            if item.get("id")
        }
        for identifier, href in manifest.items():
            if href and str(base / href) not in names:
                problems.append(f"manifest 中的 {identifier} → {href} 在包里不存在")
        for itemref in opf.iter(f"{OPF_NS}itemref"):
            idref = itemref.get("idref")
            if idref not in manifest:
                problems.append(f"spine 引用了 manifest 中不存在的 id：{idref}")

        ids: dict[str, set[str]] = {}
        xhtml_files = [name for name in names if name.endswith(".xhtml")]
        for name in xhtml_files:
            raw = archive.read(name)
            raw_text = raw.decode("utf-8", "replace")
            # 内容层面的检查只看原始文本，不依赖 XML 是否合法——
            # 否则一个未转义的 & 会让后面的问题全被遮住。
            if PLACEHOLDER_RE.search(raw_text):
                problems.append(f"{name} 残留占位符：{PLACEHOLDER_RE.search(raw_text).group(0)}")
            if UNKNOWN_GLYPH in raw_text:
                problems.append(f"{name} 含未映射字形 {UNKNOWN_GLYPH}：{raw_text.count(UNKNOWN_GLYPH)} 处")
            match = CONTROL_RE.search(raw_text)
            if match:
                problems.append(f"{name} 含控制字符 U+{ord(match.group(0)):04X}")
            try:
                tree = ET.fromstring(raw)
            except ET.ParseError as error:
                problems.append(f"{name} 不是合法 XML：{error}")
                text = re.sub(r"<[^>]+>", " ", raw_text)
            else:
                ids[name] = {element.get("id") for element in tree.iter() if element.get("id")}
                text = "".join(tree.itertext())
            cjk = len(CJK_RE.findall(text))
            latin = len(LATIN_WORD_RE.findall(text))
            if name.startswith("OEBPS/text/") and name != "OEBPS/text/titlepage.xhtml":
                stats.setdefault("parts", []).append(
                    {"file": name.split("/")[-1], "cjk": cjk, "latin_words": latin}
                )

        nav_name = next((name for name in names if name.endswith("nav.xhtml")), None)
        if nav_name:
            nav = ET.fromstring(archive.read(nav_name))
            nav_dir = pathlib.PurePosixPath(nav_name).parent
            missing = 0
            for link in nav.iter(f"{XHTML_NS}a"):
                href = link.get("href") or ""
                target, _, fragment = href.partition("#")
                full = str(nav_dir / target) if target else nav_name
                if full not in names:
                    problems.append(f"nav 链接指向不存在的文件：{href}")
                    missing += 1
                elif fragment and fragment not in ids.get(full, set()):
                    problems.append(f"nav 锚点不存在：{href}")
                    missing += 1
            stats["nav_links"] = sum(1 for _ in nav.iter(f"{XHTML_NS}a"))
            stats["nav_broken"] = missing
        else:
            problems.append("缺少 nav.xhtml")

        stats["entries"] = len(names)
        stats["text_files"] = len(xhtml_files)
        stats["images"] = len([name for name in names if name.startswith("OEBPS/images/")])

    total_cjk = sum(part["cjk"] for part in stats.get("parts", []))
    total_latin = sum(part["latin_words"] for part in stats.get("parts", []))
    stats["cjk_total"] = total_cjk
    stats["latin_words_total"] = total_latin
    stats["cjk_ratio"] = round(total_cjk / max(1, total_cjk + total_latin), 3)
    if path.name.endswith("zh.epub") and stats["cjk_ratio"] < 0.7:
        notes.append(f"中文版汉字占比偏低（{stats['cjk_ratio']}），可能有大段未翻译内容")
    return problems, notes, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="epub 文件；省略则检查 books/<slug>/out/*.epub")
    parser.add_argument("--book", help="书籍 slug")
    args = parser.parse_args()

    if args.paths:
        paths = [pathlib.Path(item) for item in args.paths]
    else:
        book = resolve_book(args.book)
        paths = sorted(book.out_dir.glob("*.epub"))
    if not paths:
        raise SystemExit("没有找到 EPUB，请先运行：python tools/build_epub.py")

    rows = ["文件\t篇数\tXHTML\t图片\t条目\t汉字\t英文词\t汉字占比\t目录链接\t断链\t结论"]
    exit_code = 0
    for path in paths:
        problems, notes, stats = check(path)
        for problem in problems:
            print(f"[{path.name}] {problem}")
        for note in notes:
            print(f"[{path.name}] 提示：{note}")
        verdict = "通过" if not problems else f"{len(problems)} 个问题"
        print(
            f"[{path.name}] {verdict}；{stats.get('size_kb')} KB，{len(stats.get('parts', []))} 篇，"
            f"汉字 {stats.get('cjk_total', 0):,}，英文词 {stats.get('latin_words_total', 0):,}，"
            f"占比 {stats.get('cjk_ratio')}"
        )
        rows.append(
            f"{path.name}\t{len(stats.get('parts', []))}\t{stats.get('text_files', 0)}\t"
            f"{stats.get('images', 0)}\t{stats.get('entries', 0)}\t{stats.get('cjk_total', 0)}\t"
            f"{stats.get('latin_words_total', 0)}\t{stats.get('cjk_ratio')}\t"
            f"{stats.get('nav_links', 0)}\t{stats.get('nav_broken', 0)}\t{verdict}"
        )
        if problems:
            exit_code = 1

    book = resolve_book(args.book)
    report = book.qa_dir / "epub-report.txt"
    report.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n报告：{report}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())