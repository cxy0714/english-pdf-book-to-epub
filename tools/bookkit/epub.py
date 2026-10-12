"""手写 EPUB3 打包器。

不依赖 ebooklib：EPUB 就是一个 ZIP，手工写反而能完全控制双语排版
（英文用小字灰色跟在中文后面）、脚注锚点和目录层级。

结构：

    mimetype                     （必须是第一个条目，且不压缩）
    META-INF/container.xml
    OEBPS/content.opf
    OEBPS/nav.xhtml
    OEBPS/style.css
    OEBPS/text/*.xhtml           （每篇一个文件）
    OEBPS/images/*.png
"""

from __future__ import annotations

import html
import pathlib
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from . import zh
from .pairing import Pair, Part
from .textnorm import placeholderize

XHTML_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" \
xml:lang="{lang}" lang="{lang}">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/>
</head>
<body>
"""
XHTML_TAIL = """</body>
</html>
"""

MATH_RE = re.compile(r"\$([^$\n]{1,200})\$")
FN_RE = re.compile(r"\[\^(\d+)\]")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")

STYLE_CSS = """/* 中英双语对照排版 */
html { font-size: 100%; }
body {
  font-family: "Songti SC", "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", serif;
  line-height: 1.75;
  margin: 0 4%;
  text-align: justify;
  word-wrap: break-word;
}
h1, h2, h3, h4 { font-family: "Heiti SC", "Source Han Sans SC", "Noto Sans CJK SC", "SimHei", sans-serif;
  line-height: 1.35; text-align: left; page-break-after: avoid; }
h1 { font-size: 1.5em; margin: 1.2em 0 0.6em; }
h2 { font-size: 1.25em; margin: 1.1em 0 0.5em; }
h3 { font-size: 1.1em; margin: 1em 0 0.4em; }
.en-head { color: #666; font-size: 0.82em; font-style: italic; margin: -0.2em 0 0.9em; text-align: left;
  font-family: Georgia, "Times New Roman", "Songti SC", serif; }
p { margin: 0 0 0.55em; }
p.zh { text-indent: 2em; }
p.en { color: #555; font-size: 0.86em; line-height: 1.5; text-indent: 0; margin: 0 0 1.1em;
  font-family: Georgia, "Times New Roman", "Songti SC", serif; }
li { margin: 0 0 0.35em; }
li .en { color: #555; font-size: 0.86em; display: block; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figcaption { font-size: 0.85em; color: #444; margin-top: 0.3em; text-align: left; }
table { border-collapse: collapse; margin: 0.8em 0; font-size: 0.85em; width: 100%; }
th, td { border: 1px solid #999; padding: 0.15em 0.4em; text-align: left; }
th { background: #f0f0f0; }
.eq { margin: 0.7em 0; text-align: center; font-style: italic; }
.math { font-style: italic; }
.fnref { font-size: 0.7em; vertical-align: super; text-decoration: none; }
.notes { margin-top: 1.5em; border-top: 1px solid #ccc; padding-top: 0.6em; }
.note { font-size: 0.86em; margin: 0 0 0.6em; }
.note .en { color: #555; font-size: 0.95em; margin: 0.15em 0 0; }
.note .label { font-weight: bold; margin-right: 0.3em; }
.titlepage { text-align: center; margin-top: 22%; }
.titlepage h1 { font-size: 2em; text-align: center; }
.titlepage .en-title { color: #666; font-style: italic; margin-top: 0.5em; }
.titlepage .author { margin-top: 2em; font-size: 1.1em; }
.titlepage .meta { margin-top: 3em; color: #888; font-size: 0.85em; }
"""


EN_ATTR = ' lang="en"'


def lang_attr(text: str) -> str:
    """整块没有汉字时标 ``lang="en"``。

    双语版里的英文对照、以及中文版里尚未翻译（或本就不翻译）的段落，都是英文内容；
    不标语言的话，阅读器会用中文读音念英文、按中文规则断行，字体也会落到中文衬线体上。
    """
    return "" if zh.has_cjk(text) else EN_ATTR


def inline(text: str, mode: str, zh_first: bool = True) -> str:
    """把一行块文本转成 HTML：清洗非法字形、转义、行内公式、脚注锚点、强调。"""
    escaped = html.escape(placeholderize(text), quote=False)
    escaped = MATH_RE.sub(lambda m: f'<span class="math">{m.group(1)}</span>', escaped)
    escaped = FN_RE.sub(
        lambda m: f'<a class="fnref" id="fnref-{m.group(1)}" href="#fn-{m.group(1)}">[{m.group(1)}]</a>',
        escaped,
    )
    escaped = BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", escaped)
    return escaped


@dataclass
class Rendered:
    """一篇渲染好的 XHTML。"""

    slug: str
    title: str
    body: str
    headings: list[tuple[int, str, str]] = field(default_factory=list)  # (level, anchor, text)


def render_part(part: Part, mode: str) -> Rendered:
    """渲染一篇。mode: bilingual | zh"""
    bilingual = mode == "bilingual"
    out: list[str] = []
    headings: list[tuple[int, str, str]] = []
    index = 0
    rows: list[Pair] = []

    def flush_table() -> None:
        nonlocal rows
        if not rows:
            return
        out.append("<table>")
        body_rows = [row for row in rows if not re.match(r"^\|\s*-{2,}", row.en_text)]
        for position, row in enumerate(body_rows):
            cells = [cell.strip() for cell in row.en_text.strip().strip("|").split("|")]
            tag = "th" if position == 0 else "td"
            out.append("<tr>" + "".join(f"<{tag}>{inline(cell, mode)}</{tag}>" for cell in cells) + "</tr>")
        out.append("</table>")
        rows = []

    for pair in part.pairs:
        index += 1
        kind = pair.type
        anchor = f"h{index}"
        if kind == "row":
            rows.append(pair)
            continue
        flush_table()

        if kind in ("part", "heading"):
            level = min(4, max(1, pair.level + (0 if kind == "part" else 1)))
            text = pair.zh_text if mode != "en" else pair.en_text
            headings.append((level, anchor, text))
            out.append(f'<h{level} id="{anchor}"{lang_attr(text)}>{inline(text, mode)}</h{level}>')
            if bilingual and pair.en_text and pair.en_text != text:
                out.append(f'<p class="en-head"{EN_ATTR}>{inline(pair.en_text, mode)}</p>')
        elif kind == "list":
            out.append("<ul>")
            if bilingual:
                out.append(
                    f'<li><span class="zh">{inline(pair.zh_text, mode)}</span>'
                    f'<span class="en"{EN_ATTR}>{inline(pair.en_text, mode)}</span></li>'
                )
            else:
                out.append(f'<li{lang_attr(pair.zh_text)}>{inline(pair.zh_text, mode)}</li>')
            out.append("</ul>")
        elif kind in ("caption", "source"):
            out.append(f'<p class="caption"{lang_attr(pair.zh_text)}>{inline(pair.zh_text, mode)}</p>')
            if bilingual and pair.en_text:
                out.append(f'<p class="en"{EN_ATTR}>{inline(pair.en_text, mode)}</p>')
        elif kind == "figure":
            alt = (pair.en.get("meta") or {}).get("alt") or "figure"
            src = pair.image.split("/")[-1]
            out.append(
                f'<figure><img src="images/{html.escape(src)}" alt="{html.escape(alt)}"/>'
                f"</figure>"
            )
        elif kind == "equation":
            out.append(f'<div class="eq"{EN_ATTR}>{inline(pair.en_text, mode)}</div>')
        elif kind == "note":
            continue  # 篇末统一排
        else:
            if bilingual:
                out.append(f'<p class="zh"{lang_attr(pair.zh_text)}>{inline(pair.zh_text, mode)}</p>')
                if pair.en_text and pair.en_text != pair.zh_text:
                    out.append(f'<p class="en"{EN_ATTR}>{inline(pair.en_text, mode)}</p>')
            else:
                out.append(f'<p class="zh"{lang_attr(pair.zh_text)}>{inline(pair.zh_text, mode)}</p>')
    flush_table()

    notes = part.notes
    if notes:
        out.append('<section class="notes" epub:type="endnotes">')
        out.append(f"<h2>注释 / Notes</h2>" if bilingual else "<h2>注释</h2>")
        for pair in notes:
            number = pair.number
            out.append(f'<div class="note" id="fn-{number}">')
            out.append(
                f'<p{lang_attr(pair.zh_text)}><span class="label">[{number}]</span>{inline(pair.zh_text, mode)}</p>'
            )
            if bilingual and pair.en_text:
                out.append(f'<p class="en"{EN_ATTR}>{inline(pair.en_text, mode)}</p>')
            out.append("</div>")
        out.append("</section>")

    return Rendered(slug=part.slug, title=part.title_zh, body="\n".join(out), headings=headings)


def title_page(book, mode: str) -> str:
    title = book.title_zh if mode != "en" else book.title_en
    body = [f'<div class="titlepage">']
    body.append(f"<h1>{html.escape(title)}</h1>")
    if mode == "bilingual":
        body.append(f'<p class="en-title">{html.escape(book.title_en)}</p>')
    body.append(f'<p class="author">{html.escape(book.author)}</p>')
    body.append(f'<p class="meta">{html.escape((book.data.get("publisher") or ""))}</p>')
    body.append("</div>")
    return "\n".join(body)


def build(
    book,
    parts: list[Part],
    out_path: pathlib.Path,
    mode: str = "bilingual",
    lang: str = "zh-CN",
) -> dict:
    """打包一本 EPUB，返回统计信息。"""
    rendered = [render_part(part, mode) for part in parts]
    identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{book.slug}-{mode}')}"

    files: dict[str, bytes] = {}
    # 正文
    for item in rendered:
        head = XHTML_HEAD.format(lang=lang, title=html.escape(item.title))
        files[f"OEBPS/text/{item.slug}.xhtml"] = (head + item.body + "\n" + XHTML_TAIL).encode("utf-8")
    # 扉页
    files["OEBPS/text/titlepage.xhtml"] = (
        XHTML_HEAD.format(lang=lang, title=html.escape(book.title_zh))
        + title_page(book, mode)
        + "\n"
        + XHTML_TAIL
    ).encode("utf-8")

    # 图片
    image_names: list[str] = []
    for part in parts:
        for pair in part.pairs:
            if pair.type == "figure" and pair.image:
                source = book.out_dir / pair.image
                if source.exists():
                    name = pair.image.split("/")[-1]
                    files[f"OEBPS/images/{name}"] = source.read_bytes()
                    if name not in image_names:
                        image_names.append(name)

    files["OEBPS/style.css"] = STYLE_CSS.encode("utf-8")

    # 目录
    nav: list[str] = [
        XHTML_HEAD.format(lang=lang, title="目录"),
        '<nav epub:type="toc" id="toc"><h1>目录 / Contents</h1><ol>',
    ]
    for item in rendered:
        nav.append(f'<li><a href="text/{item.slug}.xhtml">{html.escape(item.title)}</a>')
        children = [h for h in item.headings if h[0] >= 2][:40]
        if children:
            nav.append("<ol>")
            for level, anchor, text in children:
                nav.append(f'<li><a href="text/{item.slug}.xhtml#{anchor}">{html.escape(text)}</a></li>')
            nav.append("</ol>")
        nav.append("</li>")
    nav.append("</ol></nav>")
    nav.append(XHTML_TAIL)
    files["OEBPS/nav.xhtml"] = "\n".join(nav).encode("utf-8")

    # OPF
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="titlepage" href="text/titlepage.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="titlepage"/>', '<itemref idref="nav"/>']
    for item in rendered:
        manifest.append(
            f'<item id="{item.slug}" href="text/{item.slug}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{item.slug}"/>')
    for name in image_names:
        stem = re.sub(r"[^A-Za-z0-9]+", "-", pathlib.Path(name).stem)
        manifest.append(f'<item id="img-{stem}" href="images/{name}" media-type="image/png"/>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{identifier}</dc:identifier>
<dc:title>{html.escape(book.title_zh)}</dc:title>
<dc:title id="en">{html.escape(book.title_en)}</dc:title>
<dc:creator>{html.escape(book.author)}</dc:creator>
<dc:language>{lang}</dc:language>
<dc:publisher>{html.escape(book.data.get("publisher") or "")}</dc:publisher>
<dc:description>{html.escape("中英双语对照（机器翻译初稿 + 人工校订）" if mode == "bilingual" else "中文译本")}</dc:description>
<meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine>
{chr(10).join(spine)}
</spine>
</package>
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>\n',
        )
        archive.writestr("OEBPS/content.opf", opf.encode("utf-8"))
        for name, data in sorted(files.items()):
            archive.writestr(name, data)

    return {
        "path": str(out_path),
        "parts": len(rendered),
        "images": len(image_names),
        "bytes": out_path.stat().st_size,
        "notes": sum(len(part.notes) for part in parts),
    }