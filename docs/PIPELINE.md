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
        normalize_zh + lint_zh ──────────────────────▶ qa/normalize-zh-report.txt / zh-lint.txt
        apply_zh ────────────────────────────────────▶ 逐章人工校订（导出/写回 build/zh-blocks.json）
        verify_all ──────────────────────────────────▶ 一条命令验收全文
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

**脚注要翻译。** `note` 块曾经被一律标上 `no_translate=True`，等于中文版里所有注释
（本书 174 条、4.5 万字符）都留英文。现在只有参考文献、索引这类不翻译的篇章才照抄，
判据与正文一致（`part.kind in ("library", "index")`）。
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
| `max_input_chars` | 单请求输入字符的保险丝（**不是**模型上下文上限）。超限的批会在发送前对半拆开 |
| `context_prev_blocks` / `context_next_blocks` | 每批附带多少已译前文 / 待译后文 |
| `glossary_in_prompt` / `chapter_brief` | 是否把术语表、本章大纲写进提示词 |
| `concurrency` | 并发批数；接口限流时降到 1 |
| `retries` | 失败重试次数；缺条目时会自动二分拆批 |

**输入给长一些更便宜。** 术语表、全章大纲、前后文在每个请求里都要重复一遍，
所以请求数越少，总输入越小、跨批断点也越少。`max_input_chars` 只用来拦住
荒唐请求，真正的瓶颈是 `max_tokens` 管的**返回**长度。本书把 24000 放宽到
60000 之后，请求数从 86 降到 58，总输入从 1.53M 降到 1.09M 字符。

**术语表按批过滤。** 整份术语表（本书 3.7k 字符）逐请求重复是最大一笔开销，
而绝大多数条目跟当前这一批无关。`format_glossary(glossary, relevant=…)` 只保留
在该批正文与前后文里真的出现的条目——某批里出现了某个术语，它的定译就一定带上，
一致性不受影响。

`--dry-run` 会把每批的请求字符数与是否超限写进 `qa/mt-plan.txt`，并在末尾给出
请求数、输入字符与粗略的 token 估算，方便先看清要花多少钱。它用的是**与真实发送
完全相同的切批逻辑**（含按输入上限拆批），不会出现「规划时看着没问题、发出去被拒」。

- 缓存：`build/mt-cache/<sha1>.json`，按「模型 + system + user」哈希。重跑不花钱，
  改提示词或改术语表会让缓存失效——这是想要的行为。
- 续跑：已有译文且块数够就跳过，`--force` 强制重译。
- 冒烟：`--limit 12` 每篇只译前 12 条（在**切批之前**就砍掉，请求里不会带上其余条目，
  所以真的省钱）；`--mirror` 不联网，把英文照抄进 `zh-blocks.json`，
  用来先验证排版产物；`--dry-run` 只打印批切分方案。
- 报告：`qa/mt-report.txt`，含每篇批数、耗时、输入/输出 token。

**返回值的键是块下标。** 接口按 `[Bn]` 逐条回答，`n` 是**批内序号**；多批时每批的
`[B1]` 都会重复。所以 `translate_part()` 必须用 `Item.block` 把批内序号映射回
`build/blocks.json` 的块下标再合并，否则译文会互相覆盖、静默错行到别的块上——
排版阶段按块下标取译文，取错也不会报错，只会出现整章张冠李戴。

常见故障：

| 现象 | 处理 |
| --- | --- |
| `HTTP 401 无效的令牌` | key 没填或填错；填 `config/mt.local.json` 的 `api_key` |
| `SSL: UNEXPECTED_EOF_WHILE_READING` | 域名不解析。魔芋的接口是 `https://moyu.cn/v1`（`api.moyu.cn` 不解析 DNS） |
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
- **语言标记**：双语版里的英文对照、以及中文版里尚未翻译（或本就不翻译）的段落，
  都会带上 `lang="en"`（`epub.lang_attr()`），CSS 给它们单独的拉丁字体栈。
  不标的话，阅读器会用中文读音念英文、按中文规则断行，字体也落到中文衬线体上。
- `check_epub.py`：mimetype 位置与压缩方式、container/OPF 解析、manifest 与 spine 一致性、
  每个 XHTML 能否作为 XML 解析、目录链接与锚点是否都存在、有无残留占位符/控制字符/`▯`，
  并统计汉字占比（纯中文版低于 0.7 会提示可能有大段未翻译内容）。
  内容检查只看原始文本，**不依赖 XML 是否合法**——否则一个未转义的 `&` 会把后面的
  问题全遮住，报出来的第一个错反而掩盖了真正的病根。

## 7. 译文后处理与质检

翻译接口给的稿子在标点上必然不统一，但其中大部分可以机械修掉，剩下的才需要人看。

- `bookkit/zh.py::normalize` —— 只做能机械判定的：
  全角标点（`中文,` → `中文，`）、汉字与拉丁词之间补空格（`收益v(x)` → `收益 v(x)`）、
  数字与汉字连写（`第 3.2 节` → `第3.2节`）、脚注角标贴紧（`中文[^1]`）、
  省略号与破折号。**不碰公式括号**：`设 y=f(x)中文` → `设 y=f(x) 中文`，`f(x)` 不会被
  写成 `f（x）`——办法是先按「左括号前是汉字」全角化左括号，再按配对关系决定哪些
  右括号跟着变。没有汉字的块原样返回。
- `tools/normalize_zh.py` —— 对已有 `build/zh-blocks.json` 重跑规范化。**公式、表行、
  图片、不翻译块一律跳过**（它们必须与英文逐字一致，改一个字就会被对齐校验抓住）。
  不联网，改规则后重跑即可，不必重新调接口。
- `tools/lint_zh.py` —— 列人工看不出来的：整块未译、术语漏译（比对术语表）、
  用了避免用词、括号/引号不配对、ASCII 标点紧贴汉字、译文过短（疑似截断）。
  只查不改，报告 `qa/zh-lint.txt`。
- `tools/verify_all.py` —— 汇总以上结论加结构对齐，一条命令验收整本。

## 7.5 逐章人工校订 `tools/apply_zh.py`

机器翻译只是初稿，最终由人逐章过。改 `sources/zh/*.md` 会被下一次翻译覆盖，直接改
`build/zh-blocks.json` 又要手抠 JSON 结构，所以提供往返闭环：

```
python tools/apply_zh.py --part ch03 --export _scratch/ch03.zh.json   # 导出（键是块下标）
# 编辑 JSON：只留要改的条目即可，未给出的块保持不变
python tools/apply_zh.py --part ch03 --from   _scratch/ch03.zh.json   # 写回
python tools/build_book_md.py && python tools/build_epub.py           # 反映到成品
```

写回时先跑中文规范化，再顺手跑一遍质检（整块未译、术语漏译、标点不配对……），
并只动指定篇章。`--export` 也会顺手统计还有多少条没汉字，方便看一章译完没有。

**校订稿属于译文数据，不要入库。** 导出的 JSON 就是一个篇章的中文正文，和
`sources/zh/*.md` 同级，放进 `_scratch/`（已被忽略）或仓库外的目录。
`.gitignore` 里也为 `books/*/revisions/` 留了忽略规则，免得误入库。

## 8. 改代码之后先跑自检

`python tools/selftest.py` 不依赖任何书数据，用一份手工构造的中文样张把
「块 → Markdown → EPUB → 校验」整条链跑通，覆盖表格、脚注、公式、图片、列表、
中英混排与 `&`/`<` 转义；还会**故意造几个坏 EPUB**（未转义 `&`、残留占位符、
控制字符、未映射字形）证明校验器确实会报错——否则"通过"可能只是校验器没工作。

这条自检是有来由的：此前整条链路只在 `--mirror` 排练产物上跑过，那时 EPUB 里
一个汉字都没有，中文渲染路径从未被真正走过。

`python tools/mt_selftest.py` 更进一步：起一个**本地假接口**（OpenAI 兼容格式，
`127.0.0.1` 随机端口），把翻译客户端整条路径走一遍——请求字段、术语表与风格是否
进了提示词、占位符屏蔽与还原、缓存命中、500 重试、缺条目拆批、`--limit` 是否真的
省了请求，最后真跑一次 `translate_mt.py` CLI 并逐块核对「译文有没有落错位置」。
不消耗任何真实 token，改翻译代码后先跑它。