# 流水线细节与排错

每一步都能单独跑、单独查。产物全部落在 `books/<slug>/` 下，不污染仓库。

```
PDF ──plan_parts──▶ parts.json ──extract_pdf──▶ build/blocks.json + sources/en/*.md + out/images/
                                     │
                        build_glyphmap ──▶ data/glyphmap.json（字形还原，可跨书复用）
                                     │
                    translate_mt ──▶ build/zh-blocks.json + sources/zh/*.md + build/mt-cache/
                                     │
        build_book_md ───────────────┴───────────────▶ out/book.*.md
        build_epub ──────────────────┴───────────────▶ out/<slug>.{bilingual,zh}.epub
        check_epub ──────────────────────────────────▶ qa/epub-report.txt
```

## 1. 切篇章 `tools/plan_parts.py`

用 PDF 书签（`doc.get_toc()`）切分。规则：

- 按层级识别 `part / chapter / front / back / library / index`；
- `FRONT_TITLES` / `SKIP_TITLES` / `LIBRARY_TITLES` / `INDEX_TITLES` 决定归类（见 `bookkit/planning.py`）；
- 参考文献、索引归为 `library` / `index`，**不翻译**，只在双语稿里镜像。

产出 `parts.json` 后**人工看一眼**：页码范围、篇名、`kind` 对不对。手改 `parts.json` 是允许的，
后续步骤都以它为准。

## 2. 抽取 `tools/extract_pdf.py`

PDF 文字层 → 结构化块。要处理的版面问题（都在 `bookkit/pdfextract.py`）：

| 问题 | 判据 |
| --- | --- |
| 页眉/页码 | 全页高频重复签名（按整本书 3% 阈值）、纯数字/罗马数字页脚、与书签同名的页眉 |
| 缺空格 | 字符间距 > `max(0.45, 0.085×字号)` 处补空格；左括号/中括号后不补 |
| 断词 | 行尾 `-` + 词表判定（见下） |
| 连字 | `ﬁ ﬂ ﬀ` 等 U+FB00–FB06 还原 |
| 脚注 | 注区（页面下方 40% 以下）小字号行；正文角标按**上标位置**（中心高于正文行中心 0.12 字号以上）识别，避免误伤 `CO₂` 这类下标 |
| 列表 | 项目符号字体（`LCIRCLE10` 等）起首的行 + 缩进续行 |
| 表格 | 同一 y 上多个单元格行，按 x 区间重叠聚类成列（表头左对齐、数字右对齐也能对齐） |
| 段落 | 正文「首行缩进」，参考文献/索引「悬挂缩进」，两种惯例反向处理；跨页不断段 |
| 公式 | 数学字体占比 ≥55% 且最长英文串 ≤18 的行 |
| 图 | 矢量绘图簇聚类 + `page.get_pixmap(clip=…)` 高倍渲染 |

质检报告 `qa/extract-report.txt` 里几条线索的含义：

- **断词判定未覆盖词表**：两边形式都不在词表（见第 3 节），默认按断词处理，逐条列在
  `qa/dehyphenation.tsv`，建议人工扫一遍。
- **段落以小写字母开头**：可能是被切断的段落。公式块之后的续行天然如此，
  已单独归为「段落紧接着公式块（预期）」。
- **未映射字形 `▯`**：字形映射没覆盖到，见第 4 节。

## 3. 断词判定 `bookkit/textnorm.py`

词表由全书文本自建（`build_vocab`）。判定优先级：

1. 去掉连字符的形式在词表、保留的不在 → 去掉；
2. 保留的在词表、去掉的不在 → 保留（`quasi-linear`）；
3. 两者都在 → 保留（书里本来就这么写）；
4. 两者都不在 → 下一行开头是大写 → 保留（`Non-Combinatorial`）；
   连字符前是构词前缀（`expected`、`quasi`、`non`…）→ 保留；
   否则去掉并记录待复核。

要加前缀，改 `COMPOUND_PREFIXES`。改判定规则后重跑 `extract_pdf.py` 即可。

## 4. 数学字形还原 `tools/build_glyphmap.py`

老式 TeX PDF（Textures / dvips / pdfTeX）的数学字体往往**没有 ToUnicode**，
PyMuPDF 读到的是 `\x04`、`\uf8f1` 这类不可用字符。还原链路：

```
PyMuPDF 读到的字符 ──(/ToUnicode 反查)──▶ 字符码
字符码 ──(/Encoding /Differences 或 CFF charset)──▶ 字形名
字形名 ──(tools/bookkit/glyphnames.py)──▶ Unicode
```

- 只给**确实吐出过不可用字符**的字体子集生成映射：Utopia 这类字体本身映射完整，
  若照搬 CFF charset 顺序去猜码位会把正常文字改错。
- 同一基础字体名（如 `MTEX`）可能有多个子集，码位含义不同 → 映射精确到「页 → 字体 → 码位」，
  全局表只保留各子集一致的项。
- 认不出的字形名会留在 `qa/glyphmap-report.txt`，往 `glyphnames.py` 的表里补一行即可。
- 完全没有线索时，用 `tools/glyph_probe.py` 生成字形对照图（`qa/glyph-sheet-*.png`）
  和候选表（`qa/glyph-candidates.tsv`），人工认字后写进 `data/glyphmap.json` 的 `fonts` 段。
- 仍未识别的字符统一渲染成 `▯`（合法 XML 字符，不会让 EPUB 解析失败）。

## 5. 翻译 `tools/translate_mt.py`

配置解析顺序（后者覆盖前者）：
内置默认 → `config/mt.example.json` → `config/mt.local.json` →
`books/<slug>/mt.local.json` → 环境变量（仅补 `api_key`）→ 命令行 `--model`。

参数说明：

| 参数 | 作用 |
| --- | --- |
| `target_output_chars` | 每批译文目标长度（汉字）。切块的主要依据 |
| `max_tokens` | 接口单次返回上限；整章一次翻完的判据也会参考它 |
| `context_prev_blocks` / `context_next_blocks` | 每批附带多少已译前文 / 待译后文 |
| `glossary_in_prompt` / `chapter_brief` | 是否把术语表、本章大纲写进提示词 |
| `concurrency` | 并发批数；接口限流时降到 1 |
| `retries` | 失败重试次数；缺条目时会自动二分拆批 |

- 缓存：`build/mt-cache/<sha1>.json`，按「模型 + system + user」哈希。重跑不花钱，
  改提示词或改术语表会让缓存失效——这是想要的行为。
- 续跑：已有译文且块数够就跳过，`--force` 强制重译。
- 冒烟：`--limit 12` 每篇只译前 12 条；`--mirror` 不联网，把英文照抄进 `zh-blocks.json`，
  用来先验证排版产物；`--dry-run` 只打印批切分方案。
- 报告：`qa/mt-report.txt`，含每篇批数、耗时、输入/输出 token。

常见故障：

| 现象 | 处理 |
| --- | --- |
| `HTTP 401` | key 没填或填错位置；`config/mt.local.json` 的 `api_key` |
| `HTTP 404` | `base_url` 不对，注意要带 `/v1` |
| `响应缺少 choices` | 接口不是 OpenAI 兼容格式，或模型名不对 |
| 反复「缺少 N 条译文」 | 批太大被截断 → 调小 `target_output_chars` 或调大 `max_tokens` |
| 术语不统一 | 补 `glossary.json` 的 `terms`，然后 `--force` 重译受影响篇章 |

## 6. 排版与校验

- `build_book_md.py`：一个块一行，块间空行；标题中文 + 英文斜体；公式/表格行/图片中英共用；
  脚注在篇末统一排（`[^n]: 中文` + 缩进一段 `原文：…`）。
- `build_epub.py`：手写 EPUB3（不依赖 ebooklib）。`mimetype` 必须是首个且不压缩的条目；
  每篇一个 XHTML；目录取 H1/H2；脚注篇末成节，正文角标带锚点回链；
  双语版英文用小字灰色跟在中文后面。
- `check_epub.py`：mimetype 位置与压缩方式、container/OPF 解析、manifest 与 spine 一致性、
  每个 XHTML 能否作为 XML 解析、目录链接与锚点是否都存在、有无残留占位符/控制字符/`▯`，
  并统计汉字占比（纯中文版低于 0.7 会提示可能有大段未翻译内容）。