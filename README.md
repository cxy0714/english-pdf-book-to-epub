# english-pdf-book-to-epub

把**英文 PDF 书**做成**中英双语 Markdown + EPUB**的流水线。一本书一套配置，工具是通用的。

参考实现：<https://github.com/cxy0714/Science-A-New-Golden-Age>（那本白宫报告是公有领域，
可以直接带数据；这个仓库改成**只提交代码**，因为处理的多数是版权书）。

## 仓库约定（重要）

**只提交代码和配置，绝不提交数据文件。**

| 入库 | 不入库（`.gitignore` 兜住） |
| --- | --- |
| `tools/`、`docs/`、`config/mt.example.json`、`requirements.txt` | 源 PDF、英文底本、译文、对照稿、EPUB |
| `books/<slug>/book.json`、`parts.json`、`glossary.json` | `books/<slug>/build/`、`out/`、`qa/`、`sources/` |
| `data/glyphmap.json`（字形映射表，可复现的派生数据） | `config/mt.local.json`（含 API key） |

新增一本书时，先确认 `git status` 里没有 PDF/MD/EPUB，再提交。

## 目录结构

```
config/
  mt.example.json        翻译接口模板（入库）
  mt.local.json          含 API key 的本地配置（不入库）
  glossary.json          跨书通用术语表（可选）
data/
  glyphmap.json          数学字形还原表：TeX 字体控制字符 → Unicode
docs/
  STYLE.zh.md            译文风格约定（会注入翻译提示词，改风格改这里）
  PIPELINE.md            流水线每一步的细节与排错
books/<slug>/
  book.json              书名、作者、版面参数（字号、脚注、项目符号字体…）
  parts.json             篇章切分（由 tools/plan_parts.py 生成，可手改）
  glossary.json          本书术语表：定译、人名、必须保留英文的词
  source/*.pdf           源 PDF（不入库）
  sources/en/*.md        抽取出的英文底本（不入库）
  sources/zh/*.md        译文（不入库）
  build/                 中间产物：blocks.json、zh-blocks.json、mt-cache/、图片
  out/                   交付物：双语/纯中文 Markdown、EPUB、图片
  qa/                    质量报告
tools/
  plan_parts.py          目录 → 篇章切分
  extract_pdf.py         PDF → 结构化英文块 + 图片 + 质检报告
  build_glyphmap.py      PDF 字体 → data/glyphmap.json（自动还原数学符号）
  glyph_probe.py         字形对照图（自动还原失败时的兜底人工认字）
  translate_mt.py        逐块机器翻译（带缓存、续跑、并发）
  build_book_md.py        逐块对照稿 → Markdown
  build_epub.py          逐块对照稿 → EPUB3（双语 / 纯中文）
  check_epub.py          EPUB 合规与内容完整性校验
  bookkit/               核心库（版面解析、文本规范化、翻译接口、EPUB 打包）
```

## 快速开始

```bash
pip install -r requirements.txt

# 1. 把书放进 books/<slug>/source/，写好 book.json
# 2. 切篇章（用 PDF 书签）
python tools/plan_parts.py <slug>

# 3. 还原数学字形（只需做一次，映射表可跨书复用）
python tools/build_glyphmap.py <slug>

# 4. 抽取英文底本
python tools/extract_pdf.py --part front --part ch01

# 5. 填 API key
cp config/mt.example.json config/mt.local.json   # 然后把 key 填进 api_key

# 6. 验证接口
python tools/translate_mt.py --probe

# 7. 翻译（-n 只跑前几批先看效果）
python tools/translate_mt.py --part front

# 8. 出稿
python tools/build_book_md.py
python tools/build_epub.py
python tools/check_epub.py
```

## 设计要点

**块级对齐是结构保证的。** 抽取阶段一行一个 block，块类型有
`part / heading / paragraph / list / caption / source / row / figure / equation / note`。
翻译阶段不重排、不合并：请求里每条待译内容编号 `[B1] [B2] …`，模型按编号逐条返回，
译文按编号回填；公式、表格行、图片、参考文献、索引原样镜像。所以
`sources/zh/<slug>.zh.md` 与 `sources/en/<slug>.en.md` 的行数天然一致，
`build/zh-blocks.json` 与 `build/blocks.json` 的下标一一对应。

**批大小按译文预算算，不按输入块数。** 中文译文长度约为英文源文的 0.3 倍，
短章整章一次翻完（一致性最好），长章按 `target_output_chars` 切块——真正的上限是
接口单次**返回**长度，整章一次必然被截断。每次请求都附带全章大纲、术语表、
前若干条已译前文和后若干条原文，切块也不丢跨块一致性。

**占位符保护。** 脚注角标 `[^n]`、行内公式 `$…$`、图片 `![…](…)` 送翻译前换成
`[[fn1]]`/`[[m1]]`/`[[img1]]`，译完还原，模型改不动它们。

**数学符号自动还原。** 老式 TeX PDF 的数学字体没有 ToUnicode，PyMuPDF 只能给出
`\x04` 这类控制字符。工具链从字体里取回字形名（PDF 的 `/Encoding /Differences`，
或 CFF charset），再查字形名→Unicode 表，全自动还原 `∑`、`∈`、`Θ`、`⃗` 等；
同一字体名的不同子集码位不同，所以映射精确到「页 → 字体 → 码位」。
本书 977 处数学字符因此零人工干预还原，剩余未识别字形统一渲染成 `▯` 并计入质检。

## 已知限制

- **公式是文字，不是图片。** 分式、根号、积分上下限这类二维排版无法从 PDF 文字层
  还原（如 `∫₀¹` 会变成 `∫ 1 0`）。公式块独立成块、不翻译，原文保留原样，
  排版上居中显示；需要精确版式时应回看原 PDF。
- 公式行之后的正文续行会独立成段（公式独立成块所致），质检里单独归类为「预期」。
- 机器翻译是**初稿**。校订流程：`docs/STYLE.zh.md` 管风格，`glossary.json` 管术语，
  译完按章人工过一遍。
- 只处理有文字层的 PDF。扫描件需要先 OCR。

## 交付物

`books/<slug>/out/` 下：

- `book.bilingual.md` / `book.zh.md` / `book.en.md`，以及每篇一个 `<slug>.bilingual.md`
- `<slug>.bilingual.epub`、`<slug>.zh.epub`
- `images/*.png` 正文插图（从矢量图形裁剪渲染）

质检报告在 `books/<slug>/qa/`：`extract-report.txt`、`mt-report.txt`、`epub-report.txt`、
`dehyphenation.tsv`、`glyphmap-report.txt`。