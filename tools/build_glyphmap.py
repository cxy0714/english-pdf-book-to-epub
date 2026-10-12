#!/usr/bin/env python3
"""自动生成 data/glyphmap.json：把数学字体的控制字符还原成真 Unicode。

背景
----
本书（Textures/Distiller 4.05 输出）的数学字体 MTEX/MTSY/RMTMI/MSAM/MSBM 在 PDF 里
没有可用的 ToUnicode 映射，PyMuPDF 取到的是 `\\x04`、`\\uf8f1` 这类不可用字符。
但这些字体是 CFF，字形名还在，PDF 里还有 `/Encoding /Differences` 与 `/ToUnicode`
两份线索：

    字符码 --(/Encoding /Differences)--> 字形名 --(名字表)--> Unicode
    PyMuPDF 拿到的字符 --(/ToUnicode 反查)--> 字符码

所以可以**全自动**还原，不必人工认字形。字形名→Unicode 表在
`tools/bookkit/glyphnames.py`，是通用的，以后遇到同源 PDF 直接复用。

同一个基础字体名（如 MTEX）在文档里可能有多个子集，各自的码位含义不同，
因此按「页 → 字体 → 码位」记录覆盖项，冲突时精确到页。

用法：
    python tools/build_glyphmap.py            # 写 data/glyphmap.json 与 qa/glyphmap-report.txt
    python tools/build_glyphmap.py --print    # 只打印统计
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fitz  # noqa: E402

from bookkit import cff, glyphnames  # noqa: E402
from bookkit.config import REPO_ROOT, resolve_book  # noqa: E402

DIFF_RE = re.compile(r"/Differences\s*\[(.*?)\]", re.S)


def is_unusable(char: str) -> bool:
    """PyMuPDF 给出的字符是否不可用（控制字符 / 私用区 / 未定义）。"""
    code = ord(char)
    return code < 0x20 or code == 0x7F or 0xE000 <= code <= 0xF8FF


def parse_differences(obj: str) -> dict[int, str]:
    """解析 /Differences 数组：`[ 1 /space /summationdisplay ... ]` → 码位到字形名。"""
    match = DIFF_RE.search(obj)
    if not match:
        return {}
    result: dict[int, str] = {}
    code = 0
    for token in match.group(1).split():
        if token.startswith("/"):
            result[code] = token[1:]
            code += 1
        else:
            try:
                code = int(token)
            except ValueError:
                continue
    return result


def parse_tounicode(data: bytes) -> dict[int, str]:
    """解析 ToUnicode CMap，返回 字符码 → 映射后字符。"""
    text = data.decode("latin1", "replace")
    mapping: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            try:
                mapping[int(src, 16)] = "".join(
                    chr(int(dst[index:index + 4], 16)) for index in range(0, len(dst), 4)
                )
            except ValueError:
                continue
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for src_lo, src_hi, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            try:
                low, high, base = int(src_lo, 16), int(src_hi, 16), int(dst, 16)
            except ValueError:
                continue
            for offset in range(high - low + 1):
                mapping[low + offset] = chr(base + offset)
    return mapping


def unusable_by_page(doc) -> dict[int, dict[str, Counter]]:
    """先扫一遍全书，统计「页 → 字体 → 不可用字符」的实际出现次数。

    这一步是必要的：不能对每个字体子集都生成映射。像 Utopia 这类字体本身有完整的
    ToUnicode，若照搬 CFF charset 的顺序去猜码位，会把正常文字改错。只有**确实
    吐出过不可用字符**的字体子集才需要映射。
    """
    found: dict[int, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for page_no in range(doc.page_count):
        raw = doc[page_no].get_text("rawdict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font = span.get("font", "").split("+", 1)[-1]
                    for char in span.get("chars", []):
                        value = char["c"]
                        if is_unusable(value):
                            found[page_no + 1][font][value] += 1
    return found


def subset_mapping(doc, xref: int, strict: bool = True) -> dict[str, str]:
    """一个字体子集的「PyMuPDF 字符 → 可用 Unicode」映射。"""
    obj = doc.xref_object(xref)
    code_to_name: dict[int, str] = {}
    match = re.search(r"/Encoding (\d+) 0 R", obj)
    if match:
        code_to_name = parse_differences(doc.xref_object(int(match.group(1))))
    if not code_to_name:
        try:
            _, _, _, buffer = doc.extract_font(xref)
            code_to_name = cff.glyph_names(buffer, strict=strict)
        except Exception:  # noqa: BLE001 - 字体解析失败不该中断整本书
            return {}

    inverse: dict[str, int] = {}
    match = re.search(r"/ToUnicode (\d+) 0 R", obj)
    if match:
        try:
            for raw, mapped in parse_tounicode(doc.xref_stream(int(match.group(1)))).items():
                if mapped and mapped not in inverse:
                    inverse[mapped] = raw
        except Exception:  # noqa: BLE001
            pass

    result: dict[str, str] = {}
    for code, name in code_to_name.items():
        unicode_char = glyphnames.to_unicode(name)
        if unicode_char is None:
            continue
        key_char = chr(code)  # 没有 ToUnicode 时，PyMuPDF 直接给出字符码
        result[key_char] = unicode_char
    for mapped, raw in inverse.items():
        name = code_to_name.get(raw)
        if name is None:
            continue
        unicode_char = glyphnames.to_unicode(name)
        if unicode_char is None:
            continue
        result[mapped] = unicode_char
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("book", nargs="?", help="书籍 slug（只有一本书时可省略）")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印统计")
    args = parser.parse_args()

    book = resolve_book(args.book)
    doc = fitz.open(book.pdf_path)

    global_map: dict[str, dict[str, str]] = defaultdict(dict)
    conflicts: set[tuple[str, str]] = set()
    per_page: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    unresolved: Counter = Counter()
    used: Counter = Counter()

    wanted = unusable_by_page(doc)
    print(f"需要映射的页：{len(wanted)}，涉及字体：{sorted({name for fonts in wanted.values() for name in fonts})}")

    for page_no in range(doc.page_count):
        page_wanted = wanted.get(page_no + 1)
        if not page_wanted:
            continue
        page = doc[page_no]
        for entry in page.get_fonts(full=True):
            xref, base = entry[0], entry[3]
            name = base.split("+", 1)[-1]
            actual = page_wanted.get(name)
            if not actual:
                continue
            mapping = subset_mapping(doc, xref)
            if not mapping:
                continue
            # 只保留「这一页确实出现过、且本来不可用」的字符
            usable = {
                key: mapping[key]
                for key in actual
                if is_unusable(key) and mapping.get(key)
            }
            if not usable:
                for key in actual:
                    unresolved[f"{name}:U+{ord(key):04X}"] += actual[key]
                continue
            per_page[str(page_no + 1)][name] = usable
            for key, value in usable.items():
                used[f"{name}:{key}"] += actual[key]
                previous = global_map[name].get(key)
                if previous is None:
                    global_map[name][key] = value
                elif previous != value:
                    conflicts.add((name, key))

    # 有冲突的码位从全局表里剔除，只留按页覆盖
    for name, key in conflicts:
        global_map[name].pop(key, None)

    for name, mapping in global_map.items():
        for key, value in mapping.items():
            if not value:
                unresolved[f"{name}:{key}"] += 1

    payload = {
        "_comment": (
            "由 tools/build_glyphmap.py 自动生成：TeX 数学字体的控制字符 → Unicode。"
            "fonts 为全局表（按基础字体名），pages 为按页覆盖（同一字体名有多个子集时码位含义不同）。"
            "字形名→Unicode 的规则在 tools/bookkit/glyphnames.py。"
        ),
        "source": book.data.get("source_pdf"),
        "fonts": {name: dict(sorted(mapping.items())) for name, mapping in sorted(global_map.items())},
        "pages": {
            page: {name: dict(sorted(mapping.items())) for name, mapping in sorted(fonts.items())}
            for page, fonts in sorted(per_page.items(), key=lambda item: int(item[0]))
        },
    }

    stats = {
        "fonts": len(global_map),
        "codes": sum(len(mapping) for mapping in global_map.values()),
        "pages": len(per_page),
        "conflicts": len(conflicts),
    }
    print(json.dumps(stats, ensure_ascii=False))
    print("全局映射：")
    for name, mapping in sorted(global_map.items()):
        sample = " ".join(f"{key!r}→{value}" for key, value in list(sorted(mapping.items()))[:12])
        print(f"  {name:12} {len(mapping):3} 个：{sample}")

    if args.print_only:
        return 0

    target = REPO_ROOT / "data" / "glyphmap.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n写入 {target}（{target.stat().st_size / 1024:.0f} KB）")

    rows = ["字体\t码位\t替换为\t出现次数"]
    for key, count in used.most_common():
        name, _, char = key.partition(":")
        rows.append(f"{name}\tU+{ord(char):04X}\t{global_map[name].get(char, '（按页）')}\t{count}")
    (book.qa_dir / "glyphmap-report.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"报告：{book.qa_dir / 'glyphmap-report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())