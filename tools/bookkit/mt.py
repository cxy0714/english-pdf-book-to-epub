"""机器翻译接口：批切分、提示词构造、占位符保护、调用与缓存。

设计要点
--------
1. **批大小按译文预算算**，不按输入块数。中文译文长度约等于英文源文长度的 0.3 倍
   （100 个英文字符 ≈ 16 个词 ≈ 27 个汉字），据此估算；短章自动整章一次翻完，
   长章按 ``target_output_chars`` 切块。真正的上限是接口单次**返回**长度
   （``max_tokens``），整章一次必然被截断，所以按输出预算切块是必须的。
2. **每批都带全章上下文**：本章大纲、术语表、前若干条已译后文、后若干条原文。
   这样即使切块，也能保持称呼、术语、指代一致。
3. **逐块编号协议**：请求里待译条目写成 ``[B1] …``，要求模型按 ``[B1] 译文``
   逐条返回。块级对齐因此是结构保证的，不靠事后猜测。
4. **占位符保护**：脚注角标 ``[^n]``、行内公式 ``$…$``、图片 ``![…](…)``
   在送翻译前替换成 ``[[fn1]]``/``[[m1]]``/``[[img1]]``，译完还原，
   避免模型翻译或改动它们。
5. **缓存与续跑**：请求按内容哈希缓存到 ``build/mt-cache/``，重跑不花钱；
   失败自动退避重试，仍失败则二分拆批。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from . import zh

# 可翻译的块类型；row/equation/figure 与 no_translate 块一律照抄
TRANSLATABLE = {"part", "heading", "subhead", "paragraph", "list", "caption", "source", "note"}
MATH_HEAVY = re.compile(r"[∑∫∏√≤≥≠≈∈∀∃∞∂∇⊗⊙→←↔αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ]")


def is_translatable(block: dict) -> bool:
    if block.get("no_translate"):
        return False
    return block.get("type") in TRANSLATABLE and bool((block.get("text") or "").strip())


def estimate_zh(text: str) -> int:
    """估算译文长度（汉字数）。"""
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    return int(cjk + latin * 0.30)


# ---------------------------------------------------------------------------
# 批切分
# ---------------------------------------------------------------------------
@dataclass
class Item:
    number: int          # 请求内的编号（1 起）
    block: int           # 在篇章块列表中的下标
    text: str


@dataclass
class Batch:
    items: list[Item] = field(default_factory=list)
    est_chars: int = 0


def plan_batches(blocks: list[dict], cfg: dict[str, Any], limit: int | None = None) -> list[Batch]:
    """按译文预算切批。整章能一次装下就一次装下。

    ``limit`` 只取前 N 条待译块（冒烟测试用）——必须在**切批之前**就砍掉，
    否则请求里仍会带上全部条目，`--limit` 省不了钱。
    """
    target = int(cfg.get("target_output_chars", 3200))
    max_tokens = int(cfg.get("max_tokens", 8192))
    # 一次返回的安全上限：中文约 1.4 汉字/token，留 25% 余量
    ceiling = int(max_tokens * 1.4 * 0.75)
    indices = [i for i, block in enumerate(blocks) if is_translatable(block)]
    if limit is not None:
        indices = indices[:limit]
    estimates = {i: estimate_zh(blocks[i]["text"]) for i in indices}
    total = sum(estimates.values())

    batches: list[Batch] = []
    if total <= min(target * 3, ceiling):
        batch = Batch()
        for i in indices:
            batch.items.append(Item(len(batch.items) + 1, i, blocks[i]["text"]))
            batch.est_chars += estimates[i]
        if batch.items:
            batches.append(batch)
        return batches

    batch = Batch()
    for i in indices:
        if batch.items and batch.est_chars + estimates[i] > target:
            batches.append(batch)
            batch = Batch()
        batch.items.append(Item(len(batch.items) + 1, i, blocks[i]["text"]))
        batch.est_chars += estimates[i]
    if batch.items:
        batches.append(batch)
    return batches


def prior_allowance(blocks: list[dict], batch: Batch, cfg: dict[str, Any]) -> int:
    """估算「前文带中文语境」会给这次请求增加多少字符。

    规划时这些中文还不存在（还没译），但请求发出去时它们在，所以报出来的请求大小和
    花费要把它算进去，否则规划偏乐观。中文约是英文的 0.32 倍，再加上「原：」那行英文。
    """
    if not cfg.get("context_prev_zh", True):
        return 0
    prev_count = int(cfg.get("context_prev_blocks", 0))
    if prev_count <= 0:
        return 0
    first = batch.items[0].block
    window = [i for i in range(max(0, first - prev_count), first) if is_translatable(blocks[i])]
    english = sum(min(len(blocks[i].get("text") or ""), 600) for i in window)
    return int(english * 0.32) + sum(min(len(blocks[i].get("text") or ""), 400) for i in window) + 40 * len(window)


def prompt_size(book, part: dict, blocks: list[dict], batch: Batch, glossary, cfg, style: str = "") -> int:
    """一次请求的字符数（system + user），含前文中文语境的估算。"""
    prompt = build_prompt(book, part, blocks, batch, glossary, cfg, style)
    return len(prompt.system) + len(prompt.user) + prior_allowance(blocks, batch, cfg)


def fit_input_budget(
    book,
    part: dict,
    blocks: list[dict],
    batches: list[Batch],
    glossary,
    cfg,
    style: str = "",
    log=None,
) -> list[Batch]:
    """按接口输入长度上限继续拆批。

    `plan_batches` 只管译文预算（输出侧）；输入侧还受 `max_input_chars` 约束：
    术语表、全章大纲、前后文各占一大块，批本身正文越大越可能撞上限。超限的请求会被
    接口直接拒掉，所以必须在发出去之前拆开——不能等跑到一半才发现。
    """
    budget = int(cfg.get("max_input_chars") or 0)
    if budget <= 0:
        return batches
    fitted: list[Batch] = []
    queue = list(batches)
    splits = 0
    while queue:
        batch = queue.pop(0)
        size = prompt_size(book, part, blocks, batch, glossary, cfg, style)
        if size <= budget or len(batch.items) <= 1:
            if size > budget and log:
                log(f"    ! 单条条目请求仍超输入上限（{size} > {budget} 字符），接口可能拒绝")
            fitted.append(batch)
            continue
        splits += 1
        half = len(batch.items) // 2
        queue.insert(0, Batch(items=batch.items[half:], est_chars=0))
        queue.insert(0, Batch(items=batch.items[:half], est_chars=0))
    if splits and log:
        log(f"  按输入上限拆批 {splits} 次：{len(batches)} → {len(fitted)} 批")
    fitted.sort(key=lambda item: item.items[0].block)
    return fitted


def plan_requests(book, part: dict, blocks: list[dict], cfg, limit: int | None = None,
                  glossary=None, style: str = "") -> list[tuple[Batch, int]]:
    """完整规划一篇的请求：先按译文预算切批，再按输入上限拆批。

    返回 ``[(批, 请求字符数)]``。`--dry-run` 与成本估算都用它，保证「看到的就是
    真要发出去的」——两边用同一套切批逻辑，不各算一遍。
    """
    glossary = book.glossary() if glossary is None else glossary
    style = style or book.style_guide()
    batches = plan_batches(blocks, cfg, limit=limit)
    batches = fit_input_budget(book, part, blocks, batches, glossary, cfg, style)
    return [
        (batch, prompt_size(book, part, blocks, batch, glossary, cfg, style)) for batch in batches
    ]


# ---------------------------------------------------------------------------
# 占位符保护
# ---------------------------------------------------------------------------
PROTECT_PATTERNS = (
    ("fn", re.compile(r"\[\^(\d+)\]")),
    ("m", re.compile(r"\$[^$\n]{1,120}\$")),
    ("img", re.compile(r"!\[[^\]]*\]\([^)]*\)")),
)
RESTORE_RE = re.compile(r"\[\[(fn|m|img)(\d+)\]\]")


def mask(text: str) -> tuple[str, dict[str, str]]:
    store: dict[str, str] = {}
    counters: dict[str, int] = {}

    def replace(kind: str, match: re.Match) -> str:
        counters[kind] = counters.get(kind, 0) + 1
        token = f"[[{kind}{counters[kind]}]]"
        store[token[2:-2]] = match.group(0)
        return token

    result = text
    for kind, pattern in PROTECT_PATTERNS:
        result = pattern.sub(lambda m, k=kind: replace(k, m), result)
    return result, store


def unmask(text: str, store: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        return store.get(f"{match.group(1)}{match.group(2)}", match.group(0))

    return RESTORE_RE.sub(replace, text)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是经济学与博弈论领域的学术译者，正在把 Paul Milgrom 的《Putting Auction Theory to Work》译成中文。

翻译要求：
1. 准确、忠实、完整。不增删内容，不写解释，不加译注。
2. 学术书面语。避免口语化，避免"我们"式的随意表述，保持原作严谨的论证语气。
3. 术语按【术语表】统一。术语表未列出的专有名词（机制名、机构名、软件名）可保留英文。
   人名按术语表；术语表未收的人名音译，首次出现时可用"中文（English）"格式。
4. 数学内容一律照抄：公式、变量、函数名、希腊字母、集合符号、$…$ 内的内容都不翻译，
   只翻译公式周围的说明文字。
5. 形如 [[fn1]]、[[m2]]、[[img3]] 的占位符必须原样保留、位置不变——它们代表脚注角标、
   行内公式和图片。不要翻译、不要改写、不要删除。
6. 脚注条目以 `[^n]` 的还原形式出现在译文里，保持它在该条最前面。
7. 标题保留原有编号（如 "2.3"），只译文字部分。
8. 表格行、公式块、图片行不会发给你，不要臆测。

输出格式（必须严格遵守）：
- 每条译文以 `[B编号]` 开头，编号与输入的编号一一对应。
- 不合并条目、不遗漏条目、不新增条目，条目的顺序与输入一致。
- 一条译文写成一行；不要输出额外的空行、分隔线、代码块标记或任何说明文字。
"""


WORD_BOUNDARY = r"(?<![A-Za-z]){}(?![A-Za-z])"


def glossary_hits(glossary: dict[str, Any], text: str) -> dict[str, int]:
    """统计术语表里哪些条目在该文本中出现了，返回 ``{条目: 出现次数}``。

    全大写条目（US、FCC、PCS…）**区分大小写**：不区分的话，英文里常见的 "us"
    （"let us consider"）会命中「US => 美国」，等于往提示词里塞了一条错提示，
    模型真有可能把 us 译成"美国"。
    """
    hits: dict[str, int] = {}
    if not text:
        return hits

    def count(key: str) -> int:
        flags = 0 if key.isupper() else re.IGNORECASE
        return len(re.findall(WORD_BOUNDARY.format(re.escape(key)), text, flags))

    for section in ("terms", "names", "avoid"):
        for key in (glossary.get(section) or {}):
            found = count(str(key))
            if found:
                hits[key] = found
    for key in glossary.get("keep_english") or []:
        if count(str(key)):
            hits[str(key)] = 1
    return hits


def format_glossary(glossary: dict[str, Any], relevant: str | None = None) -> str:
    """把术语表渲染成提示词片段。

    ``relevant`` 给出一段文本时，只保留**在这段文本里真的出现**的条目。整份术语表
    每个请求都重复一遍是最贵的一笔开销（本书 3.7k 字符 × 80+ 请求），而绝大多数
    条目跟当前这一批毫不相干；按需过滤既省钱又不影响一致性——某批里出现了某个术语，
    它的定译就一定会带上。
    """
    if relevant is not None:
        hits = glossary_hits(glossary, relevant)
        if not hits:
            return ""
        glossary = {
            "terms": {k: v for k, v in (glossary.get("terms") or {}).items() if k in hits},
            "names": {k: v for k, v in (glossary.get("names") or {}).items() if k in hits},
            "avoid": {k: v for k, v in (glossary.get("avoid") or {}).items() if k in hits},
            "keep_english": [k for k in (glossary.get("keep_english") or []) if str(k) in hits],
        }

    lines: list[str] = []
    terms = glossary.get("terms") or {}
    if terms:
        lines.append("【术语表：统一译法】")
        for source, target in terms.items():
            lines.append(f"  {source} => {target}")
    names = glossary.get("names") or {}
    if names:
        lines.append("【人名】")
        for source, target in names.items():
            lines.append(f"  {source} => {target}")
    keep = glossary.get("keep_english") or []
    if keep:
        lines.append("【必须保留英文原样】")
        lines.append("  " + "、".join(keep))
    avoid = glossary.get("avoid") or {}
    if avoid:
        lines.append("【不要这样译】")
        for source, target in avoid.items():
            # avoid 的值本身就是完整说明（如「机制（不要译成“机理”）」），原样给模型
            lines.append(f"  {source} => {target}")
    return "\n".join(lines)


def chapter_brief(part: dict, blocks: list[dict]) -> str:
    headings = [
        block["text"] for block in blocks if block.get("type") in ("heading", "part") and block.get("text")
    ]
    if not headings:
        return ""
    return "【本章大纲】\n" + "\n".join(f"  {title}" for title in headings[:40])


@dataclass
class Prompt:
    system: str
    user: str
    store: dict[str, str]

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


def build_prompt(
    book,
    part: dict,
    blocks: list[dict],
    batch: Batch,
    glossary: dict[str, Any],
    cfg: dict[str, Any],
    style: str = "",
    prior: dict[int, str] | None = None,
) -> Prompt:
    """组装一次请求。

    ``prior`` 是**已经译好的块**（``{块下标: 译文}``）。给了它，前文语境就会带上中文，
    模型能照着自己的用词往下译；不给就只给英文原文。
    """
    store: dict[str, str] = {}
    pieces: list[str] = []
    pieces.append(f"【书籍】{book.title_en}（{book.title_zh}），{book.author}")
    pieces.append(f"【当前篇章】{part.get('title_en') or part['slug']}")
    if cfg.get("chapter_brief", True):
        brief = chapter_brief(part, blocks)
        if brief:
            pieces.append(brief)

    first, last = batch.items[0].block, batch.items[-1].block
    prev_count = int(cfg.get("context_prev_blocks", 0))
    next_count = int(cfg.get("context_next_blocks", 0))
    previous: list[tuple[int, dict]] = []
    if prev_count:
        previous = [
            (i, blocks[i]) for i in range(max(0, first - prev_count), first) if is_translatable(blocks[i])
        ]
    following: list[dict] = []
    if next_count:
        following = [
            blocks[i]
            for i in range(last + 1, min(len(blocks), last + 1 + next_count * 3))
            if is_translatable(blocks[i])
        ][:next_count]

    if cfg.get("glossary_in_prompt", True):
        # 只把「这一批加上前后文里真的出现」的条目写进提示词：
        # 整份术语表逐请求重复是最贵的一笔开销，而无关条目只是噪音。
        relevant_text = " ".join(
            [blocks[item.block].get("text") or "" for item in batch.items]
            + [block.get("text") or "" for _, block in previous]
            + [block.get("text") or "" for block in following]
        )
        text = format_glossary(glossary, relevant=relevant_text)
        if text:
            pieces.append(text)

    if previous:
        # 已译的前文要连中文一起给出：模型看到自己上一批的用词，才会在批与批之间
        # 保持一致（术语、指代、重复句式的处理）。只给英文原文时，批边界最容易漂。
        translated_prev = [(i, block) for i, block in previous if prior and prior.get(i)]
        if translated_prev:
            lines = ["【前文（已译，供衔接语气与统一用词，不要重译）】"]
            for offset, (index, block) in enumerate(translated_prev, start=1):
                lines.append(f"  ({offset}) 中：{prior[index][:600]}")
                lines.append(f"      原：{block['text'][:400]}")
            pieces.append("\n".join(lines))
        untranslated_prev = [(i, block) for i, block in previous if not (prior and prior.get(i))]
        if untranslated_prev:
            lines = ["【前文（英文，仅供衔接语气与指代，不要翻译）】"]
            for offset, (_, block) in enumerate(untranslated_prev, start=1):
                lines.append(f"  ({offset}) {block['text'][:600]}")
            pieces.append("\n".join(lines))
    if following:
        lines = ["【后文（英文，仅供衔接，不要翻译）】"]
        for offset, block in enumerate(following, start=1):
            lines.append(f"  <{offset}> {block['text'][:600]}")
        pieces.append("\n".join(lines))

    lines = [f"【待译条目】共 {len(batch.items)} 条，请逐条按 [B编号] 输出。"]
    for item in batch.items:
        masked, item_store = mask(item.text)
        store.update(item_store)
        block = blocks[item.block]
        tag = ""
        if block["type"] == "heading" or block["type"] == "part":
            tag = "（标题）"
        elif block["type"] == "note":
            tag = "（脚注）"
        elif block["type"] == "caption":
            tag = "（图题）"
        elif block["type"] == "list":
            tag = "（列表项）"
        lines.append(f"[B{item.number}]{tag} {masked}")
    lines.append(
        "【输出要求】每条只输出译文本身，行首不要重复类型标记（（标题）（脚注）（图题）（列表项）是给"
        "你看的，不是译文的一部分），也不要输出任何解释或总结。"
    )
    pieces.append("\n".join(lines))

    system = SYSTEM_PROMPT
    if style:
        system = f"{SYSTEM_PROMPT}\n\n【本书译稿风格约定】\n{style.strip()}"
    return Prompt(system=system, user="\n\n".join(pieces), store=store)


MARKER_RE = re.compile(r"^\s*\[\s*B\s*(\d+)\s*\]\s*(.*)$")
FENCE_RE = re.compile(r"^\s*```")
# 行首的类型标记是给模型看的元信息（（标题）（脚注）…）。模型有时会把它照抄进译文，
# 那就成了成书里的正文——必须剥掉。中文括号与方括号两种写法都认。
TYPE_TAG_RE = re.compile(
    r"^\s*[（(\[【]?\s*(标题|脚注|图题|列表项|列表|篇章|正文|公式|表格行|来源)\s*[）)\]】]\s*[:：]?\s*"
)


def parse_response(text: str, expected: list[int]) -> tuple[dict[int, str], list[int]]:
    """解析 `[Bn] 译文` 响应，返回 (编号→译文, 缺失编号)。"""
    cleaned = "\n".join(line for line in text.splitlines() if not FENCE_RE.match(line))
    result: dict[int, str] = {}
    current: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        joined = ""
        for piece in buffer:
            piece = piece.strip()
            if not piece:
                continue
            if joined and joined[-1].isascii() and joined[-1].isalnum() and piece[0].isascii() and piece[0].isalnum():
                joined += " "
            joined += piece
        if joined:
            result[current] = TYPE_TAG_RE.sub("", joined)

    for line in cleaned.splitlines():
        match = MARKER_RE.match(line)
        if match:
            flush()
            current = int(match.group(1))
            buffer = [match.group(2)]
        elif current is not None:
            buffer.append(line)
    flush()
    missing = [number for number in expected if number not in result]
    return result, missing


# ---------------------------------------------------------------------------
# 接口调用
# ---------------------------------------------------------------------------
class MTError(RuntimeError):
    pass


def chat(cfg: dict[str, Any], messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.15),
        "max_tokens": cfg.get("max_tokens", 8192),
        "stream": False,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.get("timeout", 600)) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise MTError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise MTError(f"网络错误：{error.reason}") from error
    if "choices" not in body:
        raise MTError(f"响应缺少 choices：{json.dumps(body, ensure_ascii=False)[:400]}")
    content = body["choices"][0].get("message", {}).get("content") or ""
    usage = body.get("usage") or {}
    return content, usage


def cache_key(cfg: dict[str, Any], prompt: Prompt) -> str:
    digest = hashlib.sha1()
    digest.update(cfg["model"].encode("utf-8"))
    digest.update(prompt.system.encode("utf-8"))
    digest.update(prompt.user.encode("utf-8"))
    return digest.hexdigest()[:20]


REVIEW_SYSTEM = """你是资深中英学术翻译审校。用户给你若干条【英文原文 + 现有中文译文】。

只做一件事：**指出必须改的问题**，不要重译、不要改写、不要给"更好的译法"全文。

要挑的问题，按严重程度：
1. 漏译（原文有、译文没有）或明显误译、意思译反；
2. 术语与给定术语表不一致，或同一术语在前后文里译法不统一；
3. 数字、单位、人名、年份、引用编号、公式符号与原文不符；
4. 中文不通顺、生硬到影响理解，或指代不清（"它/这"指不明白）；
5. 译文里混入了不该有的东西（原英文整句照抄、占位符残留）。

不要挑：风格偏好、可以更好的表达、无伤大雅的标点。
每条问题一句话说清，不超过 40 字。

输出格式：每条一行，以 [B编号] 开头，编号用给你的编号。某条没问题就不要输出它。
只输出问题行，不要任何前言、总结或解释。若全部没问题，只输出 NOISSUES。"""


def build_review_prompt(
    book,
    part: dict,
    items: list[tuple[int, str, str]],
    glossary: dict[str, Any],
    cfg: dict[str, Any],
    style: str = "",
) -> Prompt:
    """组装一次审校请求。``items`` 是 ``(块下标, 英文, 中文)``。"""
    pieces: list[str] = []
    pieces.append(f"【书籍】{book.title_en}（{book.title_zh}），{book.author}")
    pieces.append(f"【当前篇章】{part.get('title_en') or part['slug']}")
    relevant_text = "\n".join(f"{en}\n{zh_text}" for _, en, zh_text in items)
    if cfg.get("glossary_in_prompt", True):
        text = format_glossary(glossary, relevant=relevant_text)
        if text:
            pieces.append(text)
    lines = [f"【待审条目】共 {len(items)} 条。只指出必须改的问题，按 [B编号] 输出。"]
    for number, (_, en, zh) in enumerate(items, start=1):
        lines.append("")
        lines.append(f"[B{number}]")
        lines.append(f"  原：{en[:2000]}")
        lines.append(f"  译：{zh[:2000]}")
    pieces.append("\n".join(lines))
    system = REVIEW_SYSTEM
    if style:
        system = f"{style}\n\n{system}"
    return Prompt(system=system, user="\n".join(pieces), store={})


REVIEW_LINE_RE = re.compile(r"^\[\s*B\s*(\d+)\s*\]\s*(.+)$")
REVIEW_NOISSUE = "NOISSUES"


def parse_review(text: str, expected: list[int]) -> dict[int, list[str]]:
    """解析审校输出：``{批内编号: [问题, …]}``。编号不在 expected 里的一律忽略。"""
    issues: dict[int, list[str]] = {}
    if not text or REVIEW_NOISSUE in text.upper():
        return issues
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = REVIEW_LINE_RE.match(line)
        if not match:
            # 没编号的行并进上一条问题的续行
            if issues and list(issues)[-1] in issues:
                last = list(issues)[-1]
                issues[last][-1] = (issues[last][-1] + " " + line)[:300]
            continue
        number = int(match.group(1))
        if number not in expected:
            continue
        issues.setdefault(number, []).append(match.group(2).strip()[:300])
    return issues


def review_batches(items: list[tuple[int, str, str]], cfg: dict[str, Any]) -> list[list[tuple[int, str, str]]]:
    """按原文长度切审校批（审校的输入比翻译更省：不带前后文、不要求重译输出）。"""
    budget = int(cfg.get("review_batch_chars", 12000))
    batches: list[list[tuple[int, str, str]]] = []
    current: list[tuple[int, str, str]] = []
    size = 0
    for item in items:
        length = len(item[1]) + len(item[2])
        if current and size + length > budget:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        batches.append(current)
    return batches


def merge_partial(existing: dict[int, str], patch: dict[int, str]) -> dict[int, str]:
    """把「部分块」的校订合并进已有译文，其余块原样保留。

    ``apply_zh.py --from`` 交出的是**部分**块（用户只改了几段），若直接拿它重建整篇，
    没提到的块会退回英文原文——等于把整章译文清掉。这个函数就是为此存在的：
    只覆盖 patch 里出现的下标。
    """
    merged = dict(existing)
    merged.update(patch)
    return merged


@dataclass
class BatchResult:
    batch: Batch
    translations: dict[int, str] = field(default_factory=dict)
    missing: list[int] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    error: str = ""
    seconds: float = 0.0


class Translator:
    """带缓存、重试、拆批与并发的翻译器。"""

    def __init__(self, book, cfg: dict[str, Any], logger=print):
        self.book = book
        self.cfg = cfg
        self.log = logger
        self.cache_dir = book.build_dir / "mt-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"requests": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0, "failures": 0}
        self.style = book.style_guide()
        self.glossary = book.glossary()

    # --- 单批 -----------------------------------------------------------
    def translate_batch(
        self, part: dict, blocks: list[dict], batch: Batch, prior: dict[int, str] | None = None
    ) -> BatchResult:
        prompt = build_prompt(
            self.book, part, blocks, batch, self.glossary, self.cfg, self.style, prior=prior
        )
        key = cache_key(self.cfg, prompt)
        cache_path = self.cache_dir / f"{key}.json"
        started = time.time()
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            self.stats["cached"] += 1
            return BatchResult(
                batch=batch,
                translations={int(k): v for k, v in data["translations"].items()},
                missing=data.get("missing", []),
                usage=data.get("usage", {}),
                cached=True,
                seconds=time.time() - started,
            )

        expected = [item.number for item in batch.items]
        attempt = 0
        last_error = ""
        working = batch
        while attempt <= int(self.cfg.get("retries", 4)):
            attempt += 1
            try:
                if working is not batch:
                    prompt = build_prompt(
                        self.book, part, blocks, working, self.glossary, self.cfg, self.style, prior=prior
                    )
                    expected = [item.number for item in working.items]
                content, usage = chat(self.cfg, prompt.messages())
                self.stats["requests"] += 1
                self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                raw, missing = parse_response(content, expected)
                if missing and len(working.items) > 1:
                    raise MTError(f"缺少 {len(missing)} 条译文：{missing[:8]}")
                if missing:
                    raise MTError("单条批次仍缺译文")
                translations = {number: zh.normalize(unmask(text, prompt.store)) for number, text in raw.items()}
                result = BatchResult(
                    batch=working, translations=translations, usage=usage, seconds=time.time() - started
                )
                payload = {
                    "translations": {str(k): v for k, v in translations.items()},
                    "missing": [],
                    "usage": usage,
                    "model": self.cfg["model"],
                }
                cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                return result
            except MTError as error:
                last_error = str(error)
                if attempt > int(self.cfg.get("retries", 4)):
                    break
                if "缺少" in last_error and len(working.items) > 1:
                    # 拆批重试：把当前批一分为二，分别翻
                    self.log(f"    ! {last_error}，拆批重试")
                    half = len(working.items) // 2
                    left = Batch(items=working.items[:half], est_chars=0)
                    right = Batch(items=working.items[half:], est_chars=0)
                    merged: dict[int, str] = {}
                    for piece in (left, right):
                        sub = self.translate_batch(part, blocks, piece, prior=prior)
                        if sub.error:
                            return sub
                        merged.update(sub.translations)
                    return BatchResult(batch=working, translations=merged, seconds=time.time() - started)
                wait = min(2 ** attempt, 30)
                self.log(f"    ! 第 {attempt} 次失败：{last_error[:120]}；{wait}s 后重试")
                time.sleep(wait)
        self.stats["failures"] += 1
        return BatchResult(batch=batch, error=last_error, seconds=time.time() - started)

    # --- 整篇 -----------------------------------------------------------
    def translate_part(
        self, part: dict, blocks: list[dict], limit: int | None = None
    ) -> tuple[dict[int, str], list[BatchResult]]:
        """翻译一篇，返回 ``{块下标: 译文}``。

        注意返回值的键是**块下标**（与 ``build/blocks.json`` 的下标一致），
        不是 ``[Bn]`` 里的批内序号。多批时各批的 ``[B1]`` 会重复，必须用
        ``Item.block`` 映射回去，否则译文会互相覆盖、错位到别的块上。
        """
        batches = [
            batch
            for batch, _ in plan_requests(
                self.book, part, blocks, self.cfg, limit=limit,
                glossary=self.glossary, style=self.style,
            )
        ]
        results: list[BatchResult] = []
        merged: dict[int, str] = {}

        def absorb(result: BatchResult) -> None:
            for item in result.batch.items:
                text = result.translations.get(item.number)
                if text is not None:
                    merged[item.block] = text

        if not batches:
            return merged, results
        concurrency = max(1, int(self.cfg.get("concurrency", 1)))
        # 「前文带中文」需要**按顺序**译：后一批要看得见前一批已经定下来的中文用词。
        # 并发会把批打乱，这个语境就没了，所以开了它就把并发压到 1。
        want_prior = bool(self.cfg.get("context_prev_zh", True)) and int(
            self.cfg.get("context_prev_blocks", 0)
        ) > 0
        if want_prior and concurrency > 1 and len(batches) > 1:
            self.log(f"  前文带中文语境开启：并发由 {concurrency} 降为 1（保证批按顺序译）")
            concurrency = 1
        if concurrency == 1 or len(batches) == 1:
            for number, batch in enumerate(batches, start=1):
                self.log(f"  批 {number}/{len(batches)}：{len(batch.items)} 条，约 {batch.est_chars} 字")
                result = self.translate_batch(part, blocks, batch, prior=merged if want_prior else None)
                self.log_result(result)
                results.append(result)
                absorb(result)
            return merged, results
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {}
            for number, batch in enumerate(batches, start=1):
                futures[pool.submit(self.translate_batch, part, blocks, batch)] = number
                self.log(f"  批 {number}/{len(batches)}：{len(batch.items)} 条，约 {batch.est_chars} 字")
            for future in as_completed(futures):
                result = future.result()
                self.log_result(result, prefix=f"  批 {futures[future]}/{len(batches)}")
                results.append(result)
                absorb(result)
        return merged, results

    # --- 审校 -----------------------------------------------------------
    def review_part(
        self, part: dict, blocks: list[dict], zh_texts: dict[int, str], limit: int | None = None
    ) -> tuple[dict[int, list[str]], int]:
        """让模型只挑错不重译，返回 ``({块下标: [问题]}, 实际审了几块)``。

        与翻译的区别：不需要 1:1 完整返回（少一条只意味着那条没问题），所以不走拆批
        那套；但同样走缓存与重试。没有中文译文的块直接跳过——未译是已知事实，
        让模型再报一遍纯属浪费。
        """
        items: list[tuple[int, str, str]] = []
        for index, block in enumerate(blocks):
            if not is_translatable(block):
                continue
            zh_text = zh_texts.get(index) or ""
            if not zh.has_cjk(zh_text):
                continue
            items.append((index, block.get("text") or "", zh_text))
        if limit:
            items = items[:limit]
        result: dict[int, list[str]] = {}
        if not items:
            return result, 0
        batches = review_batches(items, self.cfg)
        for number, batch in enumerate(batches, start=1):
            prompt = build_review_prompt(
                self.book, part, batch, self.glossary, self.cfg, self.style
            )
            key = cache_key(self.cfg, prompt)
            cache_path = self.cache_dir / f"review-{key}.json"
            expected = list(range(1, len(batch) + 1))
            self.log(f"  审校批 {number}/{len(batches)}：{len(batch)} 条")
            if cache_path.exists():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                raw = {int(k): v for k, v in data["issues"].items()}
            else:
                raw = self._review_request(prompt, expected, cache_path)
            for local, problems in raw.items():
                index = batch[local - 1][0]
                result.setdefault(index, []).extend(problems)
        return result, len(items)

    def _review_request(self, prompt: Prompt, expected: list[int], cache_path: pathlib.Path) -> dict[int, list[str]]:
        last_error = ""
        for attempt in range(1, int(self.cfg.get("retries", 4)) + 1):
            try:
                content, usage = chat(self.cfg, prompt.messages())
                self.stats["requests"] += 1
                self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                issues = parse_review(content, expected)
                cache_path.write_text(
                    json.dumps({"issues": {str(k): v for k, v in issues.items()}}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                return issues
            except MTError as error:
                last_error = str(error)
                if attempt >= int(self.cfg.get("retries", 4)):
                    break
                wait = min(2 ** attempt, 30)
                self.log(f"    ! 审校第 {attempt} 次失败：{last_error[:120]}；{wait}s 后重试")
                time.sleep(wait)
        self.log(f"    ! 审校放弃：{last_error[:120]}")
        return {}

    def log_result(self, result: BatchResult, prefix: str = "  ") -> None:
        if result.error:
            self.log(f"{prefix} 失败：{result.error[:160]}")
            return
        token_note = ""
        if result.usage:
            reasoning = (result.usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            reasoning_note = f"，其中思考 {reasoning}" if reasoning else ""
            token_note = (
                f"，in {result.usage.get('prompt_tokens', '?')} / "
                f"out {result.usage.get('completion_tokens', '?')} tokens{reasoning_note}"
            )
        self.log(
            f"{prefix} {'命中缓存' if result.cached else f'{result.seconds:.1f}s'}，"
            f"译出 {len(result.translations)} 条{token_note}"
        )