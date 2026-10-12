# Putting Auction Theory to Work（Milgrom 2004）

本书流水线的**版面档案**。以后遇到同年代（Textures/Distiller 4.05 时代）的 PDF 可以直接参考。

- 源 PDF：`source/putting-auction-theory-to-work.pdf`（393 页，不可入库）
- 书名：*Putting Auction Theory to Work*，Paul Milgrom，Cambridge University Press，2004
- 中文书名：让拍卖理论付诸实践

## 版面实测参数（已写进 `book.json`）

| 项目 | 实测值 |
| --- | --- |
| 页面 | 360 × 604.8 pt |
| 正文 | 9.7 pt `Utopia-Regular`，行距 13.45 pt |
| 左边距 | 24.0–24.4 pt；段首缩进 36.3 pt（≈12 pt） |
| 脚注正文 | 7.5 pt；正文脚注角标 **6.8 pt 上标**；脚注锚点 **5.2 pt** |
| 图表标题 | 8.0 / 8.5 pt（`Symbol-Bold` / `Symbol-Medium`） |
| 表格正文 | 8.0 pt |
| 参考文献 | 8.5 pt，悬挂缩进（首行 24.4，续行 32.8） |
| 项目符号 | `LCIRCLE10` 字体的 `r`（渲染为实心圆） |
| 数学字体 | `MTEX` `MTSY` `RMTMI` `MSAM10` `MSBM10` `CMSY10` `CMBSY10` `MSBM7` |
| 插图 | 全书仅 8 张，均为矢量绘图（用 `page.get_pixmap(clip=…)` 渲染） |

## 踩过的坑

1. **缺空格**：约 186 页的整行文字没有空格字符（两端对齐的旧式 Distiller 输出）。
   词间空隙 1.1–1.4 pt、词内 0.0–0.2 pt，用间距阈值补回。
2. **脚注角标 vs 数学下标**：两者同为 6.8 pt，靠字号无法区分。
   按垂直位置区分——角标在正文行中心上方 0.29 em，下标（`CO₂`）在下方 0.23 em。
3. **表格列**：表头左对齐、数字右对齐，`255,124` 与 `2,371,000` 的 x0 不同，
   只能按 x 区间重叠聚类成列。
4. **缩进惯例有两种**：正文是首行缩进（首行更靠右），参考文献/索引是悬挂缩进
   （续行更靠右）。用「离左边距的绝对距离」判断必然切碎其中一种，要按**相邻两行
   的相对缩进变化方向**判断。
5. **数学字形**：`MTEX`/`RMTMI` 没有可用的 ToUnicode，靠 `/Encoding /Differences`
   取回字形名（如 `summationdisplay`、`bracketleftBigg`、`Theta`）再还原成 Unicode。
   同一基础字体名有 7 个子集，码位含义各不相同，映射必须精确到页。
   977 处数学字符全部自动还原。

## 篇章切分（`parts.json`）

14 篇：`front`（前言/序）、`ch01`–`ch08`、`part1`/`part2`（两部分导言）、
`bibliography`、`author-index`、`subject-index`。后三篇与索引不翻译，双语稿里镜像原文。

## 翻译注意

- 术语表 `glossary.json` 已收 100+ 条拍卖理论术语与 60+ 位学者译名（含前言/致谢里
  新补的人名；`Hui Li`、`Charles Zheng` 这类读音没把握的，校订时复核）。
- `FCC`、`PCS`、`VCG`、`Sky Network TV` 等机构/机制名保留英文。
- 章节标题带编号（`1.2.3`），译文保留编号。
- 脚注（174 条）要翻译。它们此前被抽取器一律标成不翻译，等于中文版注释全是英文。

## 已完成的人工译文

`front` 的序言部分（块 0–12 与块 34 的脚注）已有人工译文，用途是在真 key 到位之前
把「中文 → Markdown → EPUB → 校验」这条路径走通；续言（Foreword，块 13–33）
留给机器翻译。译文只存在本地 `build/zh-blocks.json` 与 `sources/zh/front.zh.md`，
不入库。机器翻译上线后用 `translate_mt.py --part front --force` 覆盖即可。

逐章校订的往返命令见 `docs/PIPELINE.md` §7.5（`tools/apply_zh.py --export/--from`）。

## 整本翻译的请求规划（`qa/mt-plan.txt`，由 `--dry-run` 生成）

| 项目 | 数值 |
| --- | --- |
| 请求数 | 60（`ch01` 7 批、`ch04` 9 批，`front` 整章一批） |
| 最大单请求 | 31,266 字符（front 整章一次翻完） |
| 请求合计 | 1,273,054 字符 ≈ 424k input tokens |
| 译文估算 | 173,726 字 ≈ 124k output tokens |
| 待译块 | 3,775 块（公式、表行、图片、参考文献、索引不翻译；174 条脚注要翻译） |

同一本书四种设定的实测对比（都用当前代码）：

| 设定 | 批数 | 输入字符 | ≈input tokens |
| --- | --- | --- | --- |
| `max_input_chars` 60000 + 前文带中文（当前） | 60 | 1,273,054 | 424k |
| 60000 + 前文不带中文 | 60 | 1,150,522 | 384k |
| 24000 + 前文不带中文 | 64 | 1,178,287 | 393k |
| 24000 + 前文带中文 | 82 | 1,469,074 | 490k |

结论：`max_input_chars` 卡到 24000 会让请求数暴涨（60→82），**总输入反而更贵**
（+16%）；前文带中文要额外 10% 输入，换来跨批用词一致。术语表按批过滤省下的量
在 24000 那一档更明显——整份 3.7k 字符的表逐请求重复是最大一笔浪费。