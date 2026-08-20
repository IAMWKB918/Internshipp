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

# 年.月.日 开头，分隔符可以是 . - / 或中文的 年月日，日期后面允许接
# 空格/破折号/冒号 等再接剩余文字
DATE_PATTERN = re.compile(
    r"^\s*(\d{4})[.\-/年]\s*(\d{1,2})[.\-/月]\s*(\d{1,2})\s*[日]?\s*[-—_:：]?\s*(.*)$"
)


def parse_date(folder_name: str):
    """
    从 folder name 开头抓 年/月/日，回传 (年, 月, 日, 剩余文字)。
    抓不到日期格式，就回传 (None, None, None, folder_name 原文)。
    """
    m = DATE_PATTERN.match(folder_name)
    if not m:
        return None, None, None, folder_name.strip()

    year, month, day, remainder = m.groups()
    return year, month.zfill(2), day.zfill(2), remainder.strip(" -—_:：")


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

# 硬性来源：只在这些网站范围内搜索
KNOWN_SOURCE_SITES = [
    "uca.org.my",
    "sarawak.sinchew.com.my",
    "sinchew.com.my",
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
      - 规则二(公司+内容)        → 1 条: "{公司}；{内容} {date}"
      - 规则三(硬性条件，没公司) → 最多 2 条:
            a) "{自己公司}；{内容} {date}"   (硬性条件)
            b) "{内容} {date}"               (不带公司，单独再搜一次)
        content 也是空的话，b) 会退化成纯日期，没意义，只留 a)。
    """
    company, content = extract_company_and_content(remainder)
    date_part = f"{year}年 {month}月"

    if company and content:
        queries = [f"{company}；{content} {date_part}"]
        rule = "公司+内容"
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


def search_top10(query: str) -> list[dict]:
    """打一次 Serper API，限定在 KNOWN_SOURCE_SITES 范围内，抓 top 10。"""
    if not API_KEY:
        raise RuntimeError(
            "缺少 API_KEY：请确认 .env 里有 STRIPE_API_KEY，"
            "且 auto_cmsw.py 跟 .env 在同一个专案（同一层或能被 load_dotenv() 找到的路径）。"
        )

    site_filter = " OR ".join(f"site:{s}" for s in KNOWN_SOURCE_SITES)
    full_query = f"{query} ({site_filter})"

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


def search_multi(queries: list[str]) -> list[dict]:
    """跑多条 query（规则三会有 2 条），结果合并、按 url 去重。"""
    all_results = []
    seen_urls = set()
    for q in queries:
        for r in search_top10(q):
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            r["matched_query"] = q
            all_results.append(r)
    return all_results


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


def filter_results(results: list[dict], company: str, day: str, month: str) -> list[dict]:
    """
    company: 拿来比对的公司名字（有其他公司就用其他公司，没有就用 MY_COMPANY_NAME，
              由调用端决定传哪个进来）。
    day/month: parse_date() 给的两位数字串。

    三个检查，命中任一个就留：
      1) url 含 ddmm
      2) title 含 company 且含 ddmm
      3) content(网页正文) 含 company 且含 ddmm
    过程印在 terminal，全部没中的直接丢弃，不 record。
    """
    ddmm_variants = _date_variants_ddmm(day, month)
    kept = []

    for i, r in enumerate(results, 1):
        url = r.get("url", "")
        title = r.get("title", "")
        tag_prefix = f"[{i}/{len(results)}]"

        if _text_has_any(url, ddmm_variants):
            r["matched_filter"] = "url"
            kept.append(r)
            print(f"{tag_prefix} 保留 (url 命中) - {title[:60]}")
            continue

        if _text_has(title, company) and _text_has_any(title, ddmm_variants):
            r["matched_filter"] = "title"
            kept.append(r)
            print(f"{tag_prefix} 保留 (title 命中) - {title[:60]}")
            continue

        content = _fetch_page_text(url)
        if _text_has(content, company) and _text_has_any(content, ddmm_variants):
            r["matched_filter"] = "content"
            kept.append(r)
            print(f"{tag_prefix} 保留 (content 命中) - {title[:60]}")
            continue

        print(f"{tag_prefix} 丢弃 - {title[:60]}")

    return kept


def save_search_results(
    folder_name: str,
    output_dir: Path,
    query_info: dict,
    kept: list[dict],
    filter_company: str = "",
    day: str = "",
    month: str = "",
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

    lines = [
        f"folder: {folder_name}",
        f"年: {query_info.get('year')}  月: {query_info.get('month')}",
        f"公司: {query_info.get('company') or '(无，用了硬性条件)'}",
        f"内容: {query_info.get('content') or '(无)'}",
        f"规则: {query_info.get('rule')}",
        f"queries: {query_info.get('queries')}",
        "------------------------------------------------",
        f"过滤条件: 公司={filter_company!r}, 日期(dd/mm)={day}/{month}",
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
            lines.append(f"   命中位置: {r.get('matched_filter')}")
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
    year, month, day, remainder = parse_date(folder_name)
    query_info = build_search_queries(year, month, remainder)

    try:
        results = search_multi(query_info["queries"])
    except RuntimeError as e:
        print(f"[搜索失败] {e}")
        results = []

    filter_company = query_info["company"] or MY_COMPANY_NAME
    print(f"--- 过滤开始：company={filter_company!r}，日期={day}/{month} ---")
    kept = filter_results(results, filter_company, day, month)
    print(f"--- 过滤完成：{len(results)} 条 -> 保留 {len(kept)} 条 ---")

    out_path = save_search_results(
        folder_name, output_dir, query_info, kept,
        filter_company=filter_company, day=day, month=month,
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
    year, month, day, remainder = parse_date(folder_name)
    category = classify_remainder(remainder)

    print(f"📁 {folder_name}")
    print(f"   日期: {year or '未知'}-{month or '?'}-{day or '?'}　分类: {category}")
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
    kept = filter_results(results, filter_company, day, month)

    save_search_results(
        folder_name, out_dir, query_info, kept,
        filter_company=filter_company, day=day, month=month,
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