import requests
import re
import sys
import json
import os
from datetime import datetime, timedelta
from html import unescape
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# CONFIG
# ============================================================
load_dotenv()
api_key = os.getenv("STRIPE_API_KEY")
MY_COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd."
OUTPUT_FILE = "links_found.txt"
RESULTS_PER_PAGE = 10          # what Serper actually gives a free account, per page
MAX_PAGES = 3                  # -> up to ~30 candidates per query variant
FETCH_TIMEOUT = 20
DATE_WINDOW_DAYS = 30          # for the tbs-restricted query: event date .. +N days
                                # (articles/posts are usually published during or
                                # shortly after the event, not before it)

# Known source sites worth hitting with a dedicated site: query, since they
# aggregate a lot of local Sarawak/Sibu association news that generic
# keyword search doesn't always surface in the first ~30 results.
KNOWN_SOURCE_SITES = [
    "uca.org.my",
    "sarawak.sinchew.com.my",
    "sinchew.com.my"
]
# ============================================================
# FLOW (updated)
#
#   For a given event we now run SEVERAL query variants instead of one,
#   and merge+dedupe the candidates by URL before verifying:
#
#     A) subject + year                              (broad, original)
#     B) subject + year, Google-date-restricted       (tbs cd_min/cd_max)
#     C) subject + activity + year   (if activity given, even when
#        other_company is also given -> catches posts that name the
#        activity but not necessarily the counterpart)
#
#   subject priority (unchanged):
#     - other company name (if given)           -> priority 1
#     - own company name + activity (if given)  -> priority 2
#     - own company name only                   -> priority 3
#
#   Each variant is paginated (page=1..MAX_PAGES) since Serper free
#   accounts cap organic results at ~10/request regardless of `num`.
#
#   Verification order per candidate: title -> snippet -> URL ->
#   page meta/JSON-LD publish date -> full page text.
# ============================================================


# ---------------- date helpers ----------------

def parse_date(date_raw):
    """ddmmyyyy -> datetime, or None if invalid."""
    try:
        return datetime.strptime(date_raw, "%d%m%Y")
    except (ValueError, TypeError):
        return None


def parse_date_range(date_raw):
    """Accepts either a single date 'ddmmyyyy' or a range 'ddmmyyyy-ddmmyyyy'
    (e.g. '14032025-18032025' for 14-18 March 2025).

    Returns (start_dt, end_dt, is_range):
      - single date  -> (dt, dt, False)
      - valid range  -> (start_dt, end_dt, True)   [swapped if given reversed]
      - invalid      -> (None, None, False)
    """
    date_raw = (date_raw or "").strip()

    if "-" in date_raw:
        parts = date_raw.split("-")
        if len(parts) != 2:
            return None, None, False
        start_dt = parse_date(parts[0].strip())
        end_dt = parse_date(parts[1].strip())
        if not start_dt or not end_dt:
            return None, None, False
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
        return start_dt, end_dt, True

    dt = parse_date(date_raw)
    if not dt:
        return None, None, False
    return dt, dt, False


def date_variants(dt):
    """Every common way this exact date might appear on a page."""
    d, m, y = dt.day, dt.month, dt.year
    variants = {
        f"{d:02d}/{m:02d}/{y}", f"{d}/{m}/{y}",
        f"{d:02d}-{m:02d}-{y}", f"{d}-{m}-{y}",
        f"{d:02d}.{m:02d}.{y}", f"{d}.{m}.{y}",
        f"{y}-{m:02d}-{d:02d}", f"{y}/{m:02d}/{d:02d}",
        dt.strftime("%d %B %Y"), dt.strftime("%B %d, %Y"), dt.strftime("%B %d %Y"),
        dt.strftime("%d %b %Y"), dt.strftime("%b %d, %Y"), dt.strftime("%b %d %Y"),
        # Chinese, with and without leading zeros, with and without year
        f"{y}年{m}月{d}日", f"{y}年{m:02d}月{d:02d}日",
        f"{m}月{d}日", f"{m:02d}月{d:02d}日",
        # ISO datetime prefix (matches article:published_time / JSON-LD dateISO)
        f"{y}-{m:02d}-{d:02d}t",
    }
    return {v.lower() for v in variants}


def date_variants_range(start_dt, end_dt):
    """Union of date_variants() for every day from start_dt to end_dt
    inclusive. For a single-day event (start_dt == end_dt) this is
    identical to date_variants(start_dt)."""
    variants = set()
    d = start_dt
    while d <= end_dt:
        variants |= date_variants(d)
        d += timedelta(days=1)
    return variants


def text_has_exact_date(text, variants):
    if not text:
        return False
    lower = text.lower()
    return any(v in lower for v in variants)


# ---------------- query building ----------------

def _subject(activity, other_company):
    other = other_company.lstrip("-").strip() if other_company else ""
    act = activity.strip() if activity else ""
    if other:
        return other
    if act:
        return f"{MY_COMPANY_NAME} {act}"
    return MY_COMPANY_NAME


def build_queries(start_dt, end_dt, activity, other_company):
    """Return an ordered list of (query, tbs_or_None, label) variants to try.
    Serper free accounts 400 on quoted "..." phrases, so we never quote.

    Putting the FULL date straight into the query (not just the year) turns
    out to work fine on Google itself -- Google normalizes "22 June 2025" /
    "22/6/2025" / "2025年6月22日" as the same date when ranking results, even
    though our own text_has_exact_date() substring check can't do that kind
    of fuzzy matching against raw page text. So: let Google do the fuzzy
    date matching at search time via these full-date variants, then still
    run the strict text_has_exact_date() check afterwards to confirm.

    start_dt/end_dt may be the same day (single-day event) or span several
    days (multi-day event, e.g. a 5-day festival). When they differ, we
    additionally query the end date and a "start-end" range phrase, and the
    tbs date-restriction covers the whole span (+ DATE_WINDOW_DAYS after the
    LAST day, since coverage is usually published during/after the event)."""
    is_range = end_dt > start_dt
    other = other_company.lstrip("-").strip() if other_company else ""
    act = activity.strip() if activity else ""

    subject = _subject(activity, other_company)

    # "broad" query: cover both years in case a range straddles new year's
    if start_dt.year == end_dt.year:
        year_part = f"{start_dt.year}"
    else:
        year_part = f"{start_dt.year}-{end_dt.year}"
    queries = [(f"{subject} {year_part}".strip(), None, "broad")]

    def _full_date_variants(dt, label_suffix):
        date_en = dt.strftime("%d %B %Y")                # 22 June 2025
        date_slash = f"{dt.day}/{dt.month}/{dt.year}"     # 22/6/2025
        date_cn = f"{dt.year}年{dt.month}月{dt.day}日"     # 2025年6月22日
        queries.append((f"{subject} {date_en}", None, f"full-date-en{label_suffix}"))
        queries.append((f"{subject} {date_cn}", None, f"full-date-cn{label_suffix}"))
        queries.append((f"{subject} {date_slash}", None, f"full-date-slash{label_suffix}"))
        return date_cn

    start_date_cn = _full_date_variants(start_dt, "-start" if is_range else "")

    if is_range:
        end_date_cn = _full_date_variants(end_dt, "-end")

        # range phrase queries, e.g. "14-18 March 2025" / "2025年3月14日至18日"
        if start_dt.year == end_dt.year and start_dt.month == end_dt.month:
            range_en = f"{start_dt.day}-{end_dt.day} {start_dt.strftime('%B %Y')}"
            range_cn = f"{start_dt.year}年{start_dt.month}月{start_dt.day}日至{end_dt.day}日"
        else:
            range_en = f"{start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')}"
            range_cn = (
                f"{start_dt.year}年{start_dt.month}月{start_dt.day}日至"
                f"{end_dt.year}年{end_dt.month}月{end_dt.day}日"
            )
        queries.append((f"{subject} {range_en}", None, "range-en"))
        queries.append((f"{subject} {range_cn}", None, "range-cn"))
        site_date_cn = range_cn
    else:
        site_date_cn = start_date_cn

    tbs = build_tbs(start_dt, end_dt, DATE_WINDOW_DAYS)
    queries.append((subject.strip(), tbs, "date-restricted"))

    for site in KNOWN_SOURCE_SITES:
        queries.append((f"site:{site} {subject} {site_date_cn}", None, f"site:{site}"))

    # extra variant: if BOTH other_company and activity were given, also
    # search "own company + activity" so posts that skip the counterpart's
    # name still get caught
    if other and act:
        queries.append((f"{MY_COMPANY_NAME} {act} {year_part}".strip(), None, "activity-only"))

    return queries


def build_tbs(start_dt, end_dt, window_days):
    """Google custom date range: event start .. event end + window_days.
    Format Google expects is mm/dd/yyyy. For a single-day event start_dt
    == end_dt, so this collapses to the original behaviour."""
    start = start_dt
    end = end_dt + timedelta(days=window_days)
    return f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},cd_max:{end.month}/{end.day}/{end.year}"


# ---------------- Serper search (paginated) ----------------

def serper_search_page(query, page, tbs=None, num=RESULTS_PER_PAGE):
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json"
    }
    payload = {"q": query, "gl": "my", "num": num, "page": page}
    if tbs:
        payload["tbs"] = tbs

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=FETCH_TIMEOUT)

        if r.status_code == 400:
            print(f"[serper error detail] Status 400: {r.text}")
            if "not allowed for free accounts" in r.text.lower():
                raise RuntimeError(
                    f"Serper blocked this query pattern: {query!r} "
                    f"(gl={payload.get('gl')}, tbs={tbs}). "
                    "Try simplifying the query before retrying."
                )

        r.raise_for_status()
        data = r.json()

    except RuntimeError:
        raise
    except Exception as e:
        print(f"[serper error] {e}")
        return []

    return [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in data.get("organic", [])
    ]


def serper_search_all_pages(query, tbs=None, max_pages=MAX_PAGES):
    """Paginate until a page comes back short of a full page (last page)
    or max_pages is hit. This is what actually gets you more than ~9-10
    candidates per query."""
    results = []
    for page in range(1, max_pages + 1):
        batch = serper_search_page(query, page, tbs=tbs)
        results.extend(batch)
        if len(batch) < RESULTS_PER_PAGE:
            break
    return results


# ---------------- page fetch ----------------

def fetch_page_text(url):
    """Best-effort plain-text + meta-date pull of a page. Returns
    (body_text, meta_dates) where meta_dates is a set of raw date-ish
    strings pulled from <meta ... published...> tags and JSON-LD, so the
    date check isn't only relying on visible article text (which often
    fails on JS-rendered pages like Facebook/Instagram)."""
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"     [fetch failed] {e}")
        return "", set()

    meta_dates = set()

    # <meta property="article:published_time" content="...">  and similar
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:updated_time|'
        r'datePublished|pubdate|date)["\'][^>]+content=["\']([^"\']+)["\']',
        html, flags=re.I,
    ):
        meta_dates.add(m.group(1).lower())

    # JSON-LD "datePublished": "..."
    for m in re.finditer(r'"datePublished"\s*:\s*"([^"]+)"', html, flags=re.I):
        meta_dates.add(m.group(1).lower())

    clean_html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    clean_html = re.sub(r"<style.*?</style>", " ", clean_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", clean_html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)

    return text, meta_dates


# ---------------- date verification ----------------

def verify_date(candidate, start_dt, end_dt):
    """Check title -> snippet -> URL -> page meta dates -> full page
    content, in that order. Matches if ANY day within [start_dt, end_dt]
    is found (so multi-day events keep every day, not just day one).
    Returns (matched: bool, where: str|None)."""
    variants = date_variants_range(start_dt, end_dt)

    if text_has_exact_date(candidate["title"], variants):
        return True, "title"
    if text_has_exact_date(candidate["snippet"], variants):
        return True, "snippet"
    if text_has_exact_date(candidate["link"], variants):
        return True, "url"

    page_text, meta_dates = fetch_page_text(candidate["link"])

    for md in meta_dates:
        if text_has_exact_date(md, variants):
            return True, "meta"

    if text_has_exact_date(page_text, variants):
        return True, "content"

    return False, None


# ---------------- main pipeline ----------------

def search_event(date_raw, activity="", other_company=None):
    """date_raw: a single date 'ddmmyyyy' (e.g. '16032025') OR a range
    'ddmmyyyy-ddmmyyyy' (e.g. '14032025-18032025' for a multi-day event
    running 14-18 March 2025). Search queries and the date-match window
    both expand to cover every day in the range."""
    start_dt, end_dt, is_range = parse_date_range(date_raw)
    if not start_dt:
        print(f"Invalid date: {date_raw}")
        return []

    if is_range:
        print(f"--- Event spans {start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')} ---")

    queries = build_queries(start_dt, end_dt, activity, other_company)

    all_candidates = []
    seen_links = set()

    for query, tbs, label in queries:
        print(f"--- Searching [{label}]: {query!r} (tbs={tbs}) ---")
        try:
            batch = serper_search_all_pages(query, tbs=tbs)
        except RuntimeError as e:
            print(f"--- BLOCKED (not 0 results, request was rejected): {e} ---")
            continue

        new = 0
        for c in batch:
            if c["link"] and c["link"] not in seen_links:
                seen_links.add(c["link"])
                all_candidates.append(c)
                new += 1
        print(f"    {len(batch)} raw, {new} new after dedupe (total so far: {len(all_candidates)})")

    print(f"--- {len(all_candidates)} unique candidates total, verifying exact date one by one ---")

    caught = []
    for i, c in enumerate(all_candidates, 1):
        matched, where = verify_date(c, start_dt, end_dt)
        tag = f"CATCH ({where})" if matched else "PASS"
        print(f"[{i}/{len(all_candidates)}] {tag} - {c['title'][:60]}")
        if matched:
            c["matched_in"] = where
            caught.append(c)

    return caught


def save_to_txt(date_raw, activity, other_company, results):
    """
    優化後的儲存函數：自動建立目錄並根據輸入參數命名檔案
    """
    # 1. 設定目標目錄 (使用原始字串 r'' 處理路徑)
    base_dir = r"C:\Users\wkb75\Documents\intern cck record\florence\output"
    
    # 如果資料夾不存在則建立
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
        print(f"建立目錄: {base_dir}")

    # 2. 處理檔名內容 (如果 activity 為空，用 'NA' 代替，避免雙底線或檔名奇怪)
    act_name = activity if activity.strip() else "NA"
    co_name = other_company if other_company.strip() else "Company"
    
    # 組合檔名
    filename = f"{date_raw}_{act_name}_{co_name}.txt"
    
    # 清洗檔名（移除 Windows 不允許的特殊字元）
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")

    full_path = os.path.join(base_dir, filename)

    # 3. 寫入內容
    start_dt, end_dt, is_range = parse_date_range(date_raw)
    if not start_dt:
        formatted_date = date_raw
    elif is_range:
        formatted_date = f"{start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')}"
    else:
        formatted_date = start_dt.strftime("%d %B %Y")

    # 注意：這裡改用 "w" (覆蓋寫入)，因為檔名已經包含參數了；
    # 如果你希望多次執行結果累加在同一個檔案，請改回 "a"
    with open(full_path, "w", encoding="utf-8") as f:
        f.write("================================================\n")
        f.write(f"TARGET DATE: {date_raw} -> {formatted_date}\n")
        f.write(f"ACTIVITY: {activity or 'N/A'}\n")
        f.write(f"COMPANY: {other_company or MY_COMPANY_NAME}\n")
        f.write(f"FOUND AT: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("------------------------------------------------\n")

        if not results:
            f.write("No exact-date matches found.\n")
        else:
            for i, r in enumerate(results, 1):
                f.write(f"{i}. {r['title']}\n")
                f.write(f"   URL: {r['link']}\n")
                f.write(f"   MATCHED IN: {r.get('matched_in')}\n")
                if r.get("snippet"):
                    f.write(f"   SNIPPET: {r['snippet']}\n")
                f.write("\n")

        f.write(f"TOTAL EXACT MATCHES: {len(results)}\n")
        f.write("================================================\n")

    print(f"\n[成功] 結果已儲存至: {full_path}")

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    # ------------ INPUT ------------
    input_date = "22062025"
    input_activity = ""              # optional
    input_other_co = "诗巫盆栽协会"  # optional
    # --------------------------------

    results = search_event(input_date, input_activity, input_other_co)

    print()
    print("================================================")
    print("FINAL RESULTS (exact-date matches only)")
    print("================================================")

    if not results:
        print("No exact-date matches found.")
    else:
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r['matched_in']}] {r['title']}")
            print(f"   {r['link']}")
            print()

    save_to_txt(input_date, input_activity, input_other_co, results)