import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # 一定要在 os.getenv(...) 之前调用，不然 .env 里的东西永远读不到

# ══════════════════════════════════════════════════════════════
# 阶段一：folder name 拆解 + 分类
# ══════════════════════════════════════════════════════════════

# 剩余文字「包含」这些字/词，就判定为公司/组织类。
# 长词放前面比对，比较不会被短词提前命中打断（比如先比"总商会"再比"商会"）。
ORG_SUFFIXES = [
    "股份有限公司", "有限公司",
    "总商会", "總商會", "商会", "商會",
    "会馆", "會館", "总会", "總會", "联合会", "聯合會",
    "协会", "協會", "工会", "工會", "公会", "公會",
    "宗亲会", "宗親會", "同乡会", "同鄉會",
    "Sdn. Bhd.", "Sdn Bhd", "Berhad", "Bhd",
    "Association", "Society", "Chamber of Commerce", "Chamber",
]

# 单个日期 token，支持两种顺序：
#   年.月.日  例: 2025.03.14 / 2025年3月14日
#   日.月.年  例: 14.03.2025
# 靠「哪一段是 4 位数字」来判断顺序，不是靠位置写死。
_DATE_TOKEN = (
    r"(?:\d{4}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{4})"
)

# folder name 开头：一个日期，或「日期 + 分隔符(- ~ – — 至 到) + 日期」的日期范围，
# 范围后面允许再接 空格/破折号/冒号 等，再接剩余文字。
MULTI_DATE_PATTERN = re.compile(
    rf"^\s*({_DATE_TOKEN})\s*(?:[-~–—至到]\s*({_DATE_TOKEN}))?\s*[-—_:：]?\s*(.*)$"
)


def _parse_single_date_token(token: str):
    """
    单一日期字串 -> (year, month, day) 两位数字串，判断不出来就回传 None。
    """
    token = token.strip()

    m = re.match(r"^(\d{4})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})\s*日?$", token)
    if m:
        y, mo, d = m.groups()
        return y, mo.zfill(2), d.zfill(2)

    m = re.match(r"^(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{4})$", token)
    if m:
        d, mo, y = m.groups()
        return y, mo.zfill(2), d.zfill(2)

    return None


def parse_dates(folder_name: str):
    """
    从 folder name 开头抓 一个或两个(范围)日期，回传 (dates, 剩余文字)。
    dates: list，元素是 (年, 月, 日) tuple，长度 1(单日期) 或 2(日期范围)。
    抓不到日期格式，dates=[]，剩余文字回传 folder_name 原文。

    例:
      "2025.03.14 海南会馆316庆典"              -> dates=[("2025","03","14")]
      "14.03.2025-18.03.2025 明训学校常年庆典"   -> dates=[("2025","03","14"), ("2025","03","18")]
    """
    m = MULTI_DATE_PATTERN.match(folder_name)
    if not m:
        return [], folder_name.strip()

    tok1, tok2, remainder = m.groups()
    d1 = _parse_single_date_token(tok1)
    if not d1:
        return [], folder_name.strip()

    dates = [d1]
    if tok2:
        d2 = _parse_single_date_token(tok2)
        if d2:
            dates.append(d2)

    return dates, remainder.strip(" -—_:：")


def parse_date(folder_name: str):
    """
    向下兼容旧接口：只回传「第一个」日期 + 剩余文字，(年, 月, 日, 剩余文字)。
    分类阶段(阶段一)、组 query 阶段(阶段二)都只需要一个日期起点，继续用这个。
    抓不到日期格式，就回传 (None, None, None, folder_name 原文)。
    """
    dates, remainder = parse_dates(folder_name)
    if not dates:
        return None, None, None, remainder
    year, month, day = dates[0]
    return year, month, day, remainder


def classify_remainder(text: str) -> str:
    """
    日期后面的剩余文字分类，两选一：
      "公司组织" / "活动名字"
    命中 ORG_SUFFIXES 关键字 → 公司组织；没命中 → 默认归为「活动名字」。
    这是规则法，不保证 100% 准；没命中规则的一律落到「活动名字」，不会静默漏掉。
    """
    if not text:
        return "活动名字"

    for kw in ORG_SUFFIXES:
        if kw in text:
            return "公司组织"

    return "活动名字"


def extract_folder_text(folder_name: str) -> str:
    """把 folder name 拆成人看得懂、下一步(Google search)也好读的格式。"""
    year, month, day, remainder = parse_date(folder_name)
    category = classify_remainder(remainder)

    lines = [
        f"folder: {folder_name}",
        f"年: {year or '未知'}",
        f"月: {month or '未知'}",
        f"日: {day or '未知'}",
        f"分类: {category}",
        f"内容: {remainder or '(无)'}",
    ]
    return "\n".join(lines)


def run_folder_name_task(input_dir: Path, output_dir: Path) -> Path:
    """
    input_dir  : 使用者选的资料夹
    output_dir : 跟 main.py 用的同一个 output_dir (input_dir / "output")
    输出       : output_dir / f"{input_dir.name}.txt"  —— 名字跟着 folder 走
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    text = extract_folder_text(input_dir.name)

    out_path = output_dir / f"{input_dir.name}.txt"
    out_path.write_text(text, encoding="utf-8")
    return out_path


# ══════════════════════════════════════════════════════════════
# 阶段二：组 Google 搜索 query + 打 API 抓 top 10
# ══════════════════════════════════════════════════════════════

# 硬性条件：自己公司名字。抓不到「其他公司」时，一定会带上这个当搜索主体。
MY_COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd."

# 优先来源：不是「只在」这些网站搜，是「这些网站命中率比较高」，
# 所以每条 query 会先在这些网站里搜一次(比较准)，再补一次不限网站的搜索(比较广)，
# 两边结果合并去重，命中优先来源的排前面。
KNOWN_SOURCE_SITES = [
    "uca.org.my",
    "sarawak.sinchew.com.my",
    "sinchew.com.my"
]

# Serper API (google.serper.dev) —— 跟你其他 py 用的是同一个服务，不需要 cx。
SERPER_ENDPOINT = "https://google.serper.dev/search"
API_KEY = os.getenv("STRIPE_API_KEY")  # 确认过了，你 .env 里真的就叫这个名字


_ORG_PATTERN = re.compile(
    "(.{0,15}?(?:" + "|".join(re.escape(s) for s in sorted(ORG_SUFFIXES, key=len, reverse=True)) + "))"
)


def extract_company_and_content(remainder: str):
    """
    从「日期后剩余文字」里，切出「公司/组织全名」跟「剩下的内容」。
    例:
      "海南会馆316庆典"        → ("海南会馆", "316庆典")
      "拿督张仕国创业50周年纪念" → (None, "拿督张仕国创业50周年纪念")
    规则法，抓不到公司后缀就整段当 content，不硬切。
    """
    if not remainder:
        return None, ""

    m = _ORG_PATTERN.search(remainder)
    if not m:
        return None, remainder

    company = m.group(1).strip()
    before = remainder[: m.start()].strip(" -—_:：、,，")
    after = remainder[m.end():].strip(" -—_:：、,，")
    content = " ".join(p for p in (before, after) if p)
    return company, content


def build_search_queries(year: str, month: str, remainder: str) -> dict:
    """
    回传 dict，方便过滤那一步直接拿 company / content / date_part 用，
    不用重新解析 query 字串。

    queries 是一个 list：
      - 规则一(只有公司)         → 1 条: "{公司} {date}"
      - 规则二(公司+内容)        → 最多 2 条:
            a) "{公司}；{内容} {date}"   (精准，公司+内容绑一起)
            b) "{公司} {date}"           (宽松 fallback，只带公司，避免「；」绑太死搜不到)
        两条 query 一起搜、按 url 去重合并，再交给 filter_results 精准过滤，
        不会因为放宽 query 就多留假结果。
      - 规则三(硬性条件，没公司) → 最多 2 条:
            a) "{自己公司}；{内容} {date}"   (硬性条件)
            b) "{内容} {date}"               (不带公司，单独再搜一次)
        content 也是空的话，b) 会退化成纯日期，没意义，只留 a)。
    """
    company, content = extract_company_and_content(remainder)
    date_part = f"{year}年 {month}月"

    if company and content:
        queries = [
            f"{company}；{content} {date_part}",  # a) 精准
            f"{company} {date_part}",              # b) 宽松 fallback
        ]
        rule = "公司+内容(+公司宽松fallback)"
    elif company:
        queries = [f"{company} {date_part}"]
        rule = "只有公司"
    else:
        rule = "硬性条件(自己公司) + 内容单独"
        if content:
            queries = [
                f"{MY_COMPANY_NAME}；{content} {date_part}",  # a) 硬性条件
                f"{content} {date_part}",                      # b) 内容单独
            ]
        else:
            queries = [f"{MY_COMPANY_NAME} {date_part}"]

    return {
        "queries": queries,
        "rule": rule,
        "company": company,
        "content": content,
        "year": year,
        "month": month,
    }


def search_top10(query: str, restrict_sites: bool = True) -> list[dict]:
    """
    打一次 Serper API，抓 top 10。
    restrict_sites=True  -> 限定在 KNOWN_SOURCE_SITES 范围内搜(比较准，优先来源)
    restrict_sites=False -> 不限网站，全网搜(比较广，补漏用)
    """
    if not API_KEY:
        raise RuntimeError(
            "缺少 API_KEY：请确认 .env 里有 STRIPE_API_KEY，"
            "且 auto_cmsw.py 跟 .env 在同一个专案（同一层或能被 load_dotenv() 找到的路径）。"
        )

    if restrict_sites:
        site_filter = " OR ".join(f"site:{s}" for s in KNOWN_SOURCE_SITES)
        full_query = f"{query} ({site_filter})"
    else:
        full_query = query

    headers = {
        "X-API-KEY": API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"q": full_query, "gl": "my", "num": 10}

    try:
        resp = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=15)
        if resp.status_code == 400:
            print(f"[serper error detail] Status 400: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[serper error] {e}")
        return []

    results = []
    for item in data.get("organic", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


SEARCH_MULTI_TOP_N = 10  # 合并去重后只保留前 N 条，10 名以后基本没参考价值


def search_multi(queries: list[str]) -> list[dict]:
    """
    跑多条 query（规则二/三可能有 2 条），每条各搜两次：
      1) restrict_sites=True  -> 优先来源(KNOWN_SOURCE_SITES)
      2) restrict_sites=False -> 全网，不限网站
    结果合并、按 url 去重，优先来源那批先跑，去重时排前面；
    最后只留前 SEARCH_MULTI_TOP_N 条(=10)。
    """
    all_results = []
    seen_urls = set()
    for q in queries:
        for restrict in (True, False):
            for r in search_top10(q, restrict_sites=restrict):
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
                r["matched_query"] = q
                r["site_restricted"] = restrict
                all_results.append(r)
    return all_results[:SEARCH_MULTI_TOP_N]


def run_search_for_folder(folder_name: str) -> dict:
    """给一个 folder name，回传 query 组装结果 + 搜索到的 top 10（过滤留到下一步）。"""
    year, month, day, remainder = parse_date(folder_name)
    query_info = build_search_queries(year, month, remainder)

    try:
        results = search_multi(query_info["queries"])
    except RuntimeError as e:
        results = []
        query_info["search_error"] = str(e)

    query_info["folder"] = folder_name
    query_info["results"] = results
    return query_info


# ══════════════════════════════════════════════════════════════
# 阶段三：过滤 —— 用「其他公司(或硬性条件的自己公司)」+ ddmm 去比对
# url / title / content 三个地方，命中任一个就留，全部没中就丢掉
# ══════════════════════════════════════════════════════════════

FETCH_TIMEOUT = 15


def _date_variants_ddmm(day: str, month: str) -> set[str]:
    """day/month 是 parse_date() 给的两位数字串（如 "05"、"20"）。
    产生几种常见写法，比对时用 in 判断子字串就好。"""
    d, m = int(day), int(month)
    variants = {
        f"{d:02d}{m:02d}",          # 2005 (ddmm 无分隔)
        f"{d:02d}/{m:02d}", f"{d}/{m}",
        f"{d:02d}-{m:02d}", f"{d}-{m}",
        f"{d:02d}.{m:02d}", f"{d}.{m}",
        f"{m:02d}月{d:02d}日", f"{m}月{d}日",
    }
    return {v.lower() for v in variants}


def _text_has_any(text: str, variants: set[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(v in lower for v in variants)


def _text_has(text: str, keyword: str) -> bool:
    if not text or not keyword:
        return False
    return keyword.lower() in text.lower()


def _fetch_page_text(url: str) -> str:
    """把网页抓下来，去掉 tag 只留纯文字，抓不到就回传空字串（不中断整个流程）。"""
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"     [抓取失败] {url} -> {e}")
        return ""

    clean = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    clean = re.sub(r"<style.*?</style>", " ", clean, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", clean)
    text = re.sub(r"\s+", " ", text)
    return text


def _build_per_date_variants(dates: list) -> list[dict]:
    """
    dates: parse_dates() 给的 list，元素是 (年, 月, 日) tuple。
    回传 list of {"day", "month", "variants"}，每个日期各自一份 ddmm variants，
    方便之后报告「到底是命中范围里的哪一天」。
    """
    per_date = []
    for (_year, month, day) in dates:
        per_date.append({
            "day": day,
            "month": month,
            "variants": _date_variants_ddmm(day, month),
        })
    return per_date


def _match_date_label(text: str, per_date: list[dict]):
    """回传第一个命中的日期 label（如 "14/03"），没命中回传 None。"""
    if not text:
        return None
    lower = text.lower()
    for dv in per_date:
        if any(v in lower for v in dv["variants"]):
            return f"{dv['day']}/{dv['month']}"
    return None


def filter_results(results: list[dict], company: str, dates: list) -> list[dict]:
    """
    company: 拿来比对的公司名字（有其他公司就用其他公司，没有就用 MY_COMPANY_NAME，
              由调用端决定传哪个进来）。
    dates: parse_dates() 给的 list，元素是 (年, 月, 日) tuple。
      - 只有 1 个日期(平常情况) -> 跟以前逻辑完全一样，只是包了一层 list。
      - 有 2 个日期(日期范围，例如 14.03.2025-18.03.2025)
        -> 加强：两个日期「各自」产生 ddmm variants，
           三个检查(url / title+company / content+company)只要命中
           「范围内任一个日期」就保留，不再要求一定要命中起始日。

    三个检查，命中任一个就留：
      1) url 含 ddmm(任一日期)
      2) title 含 company 且含 ddmm(任一日期)
      3) content(网页正文) 含 company 且含 ddmm(任一日期)
    过程印在 terminal，全部没中的直接丢弃，不 record。
    """
    per_date = _build_per_date_variants(dates)
    union_variants = set()
    for dv in per_date:
        union_variants |= dv["variants"]

    kept = []

    for i, r in enumerate(results, 1):
        url = r.get("url", "")
        title = r.get("title", "")
        tag_prefix = f"[{i}/{len(results)}]"

        if _text_has_any(url, union_variants):
            matched_date = _match_date_label(url, per_date)
            r["matched_filter"] = "url"
            r["matched_date"] = matched_date
            kept.append(r)
            print(f"{tag_prefix} 保留 (url 命中, 日期 {matched_date}) - {title[:60]}")
            continue

        if _text_has(title, company) and _text_has_any(title, union_variants):
            matched_date = _match_date_label(title, per_date)
            r["matched_filter"] = "title"
            r["matched_date"] = matched_date
            kept.append(r)
            print(f"{tag_prefix} 保留 (title 命中, 日期 {matched_date}) - {title[:60]}")
            continue

        content = _fetch_page_text(url)
        if _text_has(content, company) and _text_has_any(content, union_variants):
            matched_date = _match_date_label(content, per_date)
            r["matched_filter"] = "content"
            r["matched_date"] = matched_date
            kept.append(r)
            print(f"{tag_prefix} 保留 (content 命中, 日期 {matched_date}) - {title[:60]}")
            continue

        print(f"{tag_prefix} 丢弃 - {title[:60]}")

    return kept


def save_search_results(
    folder_name: str,
    output_dir: Path,
    query_info: dict,
    kept: list[dict],
    filter_company: str = "",
    dates: list = None,
    total_before_filter: int = 0,
) -> Path:
    """
    把过滤后留下的结果写 txt，同时把「过滤条件」跟「过滤前后的数量」也记下来，
    不然打开 txt 只看得到结果本身，看不出这批结果是怎么被筛出来的。
    命名跟 auto_cmsw.py 阶段一的分类 txt 分开，避免互相覆盖：
      output_dir / f"{folder_name}_search.txt"
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{folder_name}_search.txt"

    dates = dates or []
    if len(dates) >= 2:
        date_desc = " ~ ".join(f"{d}/{m}" for (_y, m, d) in dates)
        date_desc += "（日期范围，命中范围内任一天即保留）"
    elif len(dates) == 1:
        y, m, d = dates[0]
        date_desc = f"{d}/{m}"
    else:
        date_desc = "(无)"

    lines = [
        f"folder: {folder_name}",
        f"年: {query_info.get('year')}  月: {query_info.get('month')}",
        f"公司: {query_info.get('company') or '(无，用了硬性条件)'}",
        f"内容: {query_info.get('content') or '(无)'}",
        f"规则: {query_info.get('rule')}",
        f"queries: {query_info.get('queries')}",
        "------------------------------------------------",
        f"过滤条件: 公司={filter_company!r}, 日期(dd/mm)={date_desc}",
        f"过滤方式: url 含 ddmm  或  title 含公司+ddmm  或  content 含公司+ddmm，命中任一个即保留",
        f"过滤结果: 搜索到 {total_before_filter} 条 → 过滤后保留 {len(kept)} 条",
        "------------------------------------------------",
    ]

    if not kept:
        lines.append("没有过滤后仍保留的结果。")
    else:
        for i, r in enumerate(kept, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   命中位置: {r.get('matched_filter')}  (日期: {r.get('matched_date')})")
            if r.get("snippet"):
                lines.append(f"   SNIPPET: {r['snippet']}")
            lines.append("")

    lines.append(f"TOTAL: {len(kept)} 条")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def run_search_and_filter_for_folder(folder_name: str, output_dir: Path) -> Path:
    """
    完整流程：组 query → 搜索 top 10 → 过滤 → 写 txt。
    比对过滤用的 company：其他公司抓到就用其他公司，抓不到（硬性条件）就用 MY_COMPANY_NAME。
    """
    dates, remainder = parse_dates(folder_name)
    year, month, day = dates[0] if dates else (None, None, None)
    query_info = build_search_queries(year, month, remainder)

    try:
        results = search_multi(query_info["queries"])
    except RuntimeError as e:
        print(f"[搜索失败] {e}")
        results = []

    filter_company = query_info["company"] or MY_COMPANY_NAME
    date_tag = " ~ ".join(f"{d}/{m}" for (_y, m, d) in dates) if dates else "?"
    print(f"--- 过滤开始：company={filter_company!r}，日期={date_tag} ---")
    kept = filter_results(results, filter_company, dates)
    print(f"--- 过滤完成：{len(results)} 条 -> 保留 {len(kept)} 条 ---")

    out_path = save_search_results(
        folder_name, output_dir, query_info, kept,
        filter_company=filter_company, dates=dates,
        total_before_filter=len(results),
    )
    print(f"已写入: {out_path}")
    return out_path


# ══════════════════════════════════════════════════════════════
# 命令行入口
#
# 日常用法只有一种：
#   python auto_cmsw.py "<folder_path>"
# 会自动跑完 阶段一(分类) + 阶段二(搜索) + 阶段三(过滤)，
# 结果直接印在 terminal（简洁版），同时也写 txt 到 <folder>/output/。
#
# --search / --full 这两个旗标保留给需要细看中间过程时用，
# 平常不需要理会。
# ══════════════════════════════════════════════════════════════
def run_all(folder_path: str, output_dir: Path = None) -> None:
    """一次跑完 分类 + 搜索 + 过滤，terminal 印简洁结果。"""
    folder = Path(folder_path).resolve()
    out_dir = output_dir or (folder / "output")
    folder_name = folder.name

    # 阶段一：分类（只用来让下一步 search 更好组 query，不写 txt）
    dates, remainder = parse_dates(folder_name)
    year, month, day = dates[0] if dates else (None, None, None)
    category = classify_remainder(remainder)

    print(f"📁 {folder_name}")
    if len(dates) >= 2:
        date_str = " ~ ".join(f"{y}-{m}-{d}" for (y, m, d) in dates)
    else:
        date_str = f"{year or '未知'}-{month or '?'}-{day or '?'}"
    print(f"   日期: {date_str}　分类: {category}")
    print(f"   内容: {remainder or '(无)'}")

    # 阶段二 + 三：搜索 + 过滤
    query_info = build_search_queries(year, month, remainder)
    try:
        results = search_multi(query_info["queries"])
        search_error = None
    except RuntimeError as e:
        results = []
        search_error = str(e)

    if search_error:
        print(f"   ⚠ 搜索失败: {search_error}")
        print("─" * 50)
        return

    filter_company = query_info["company"] or MY_COMPANY_NAME
    kept = filter_results(results, filter_company, dates)

    save_search_results(
        folder_name, out_dir, query_info, kept,
        filter_company=filter_company, dates=dates,
        total_before_filter=len(results),
    )

    print(f"   🔍 搜索到 {len(results)} 条 → 过滤后保留 {len(kept)} 条")
    if kept:
        for i, r in enumerate(kept, 1):
            print(f"      {i}. {r['title']}")
            print(f"         {r['url']}")
    else:
        print("      (没有符合的结果)")
    print(f"   ✅ 已写入: {out_dir}")
    print("─" * 50)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print('用法: python auto_cmsw.py "<folder_path>"')
        sys.exit(1)

    if sys.argv[1] == "--search":
        if len(sys.argv) < 3:
            print('用法: python auto_cmsw.py --search "<folder_name>"')
            sys.exit(1)
        result = run_search_for_folder(sys.argv[2])
        for k, v in result.items():
            if k != "results":
                print(f"{k}: {v}")
        print(f"results: {len(result['results'])} 条")
        for r in result["results"]:
            print(" -", r["title"], r["url"], f"(query: {r.get('matched_query')})")

    elif sys.argv[1] == "--full":
        if len(sys.argv) < 3:
            print('用法: python auto_cmsw.py --full "<folder_name>" [output_dir]')
            sys.exit(1)
        folder_name = sys.argv[2]
        out_dir = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else Path.cwd() / "output"
        run_search_and_filter_for_folder(folder_name, out_dir)

    else:
        out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
        run_all(sys.argv[1], out_dir)