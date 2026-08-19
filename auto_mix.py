import requests
import re
import sys
import json
import os
import threading
import queue
from datetime import datetime, timedelta
from html import unescape
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, render_template

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# CONFIG
# ============================================================
load_dotenv()
api_key = os.getenv("STRIPE_API_KEY")  # 注意：這其實是 Serper API 的 key，變數名稱沿用你原本 .env 的設定

# ↓↓↓ 只要改這一行就好，這是「自己公司」的名稱，網頁上不會再有輸入框要你貼 ↓↓↓
MY_COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd."

RESULTS_PER_PAGE = 10
MAX_PAGES = 3
FETCH_TIMEOUT = 20
DATE_WINDOW_DAYS = 30

# 如果使用者只填「單一日期」（沒有填 end date），代表日期可能不是 100% 精準
# （報導可能早幾天或晚幾天才刊出），所以比對日期時，
# 會把「單一日期」前後這幾天也一併算是符合。
# 如果使用者填的是「日期範圍」（有 start date 也有 end date），
# 就不會再額外往前後擴充，只認範圍內的日期。
SINGLE_DATE_MATCH_WINDOW_DAYS = 2

KNOWN_SOURCE_SITES = [
    "uca.org.my",
    "sarawak.sinchew.com.my",
    "sinchew.com.my",
    "malaysiafoochow.com",
]

# 「其他公司」欄位的歷史紀錄，會出現在網頁表單的建議清單裡（可以直接選，也可以自己打新的）。
# 之後有新的公司名稱，直接加進這個 list 就會出現在下拉建議中。
COMPANY_HISTORY = [
    "民都魯中華縂商會",
    "林夢福州工會",
    "砂中華縂商會",
    "詩巫販商聯合會",
    "詩巫咖啡商公會",
    "詩巫社區領袖協會",
    "詩巫盆栽協會",
]

# 結果 txt 輸出資料夾（預設放在這個 .py 檔案旁邊的 output 資料夾，
# 也可以用環境變數 OUTPUT_DIR 指定其他路徑，例如你原本的 Windows 路徑）
OUTPUT_DIR = os.getenv(
    "OUTPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
)

# ============================================================
# 搜尋規則（跟 auto_search.py 完全一樣，只是 MY_COMPANY_NAME 改成
# 從網頁表單傳進來的參數，不再是寫死的常數）：
#
#   subject 優先順序：
#     1) 有「對方公司名稱」            -> 用對方公司名稱
#     2) 沒對方公司，但有「活動名稱」  -> 用「自己公司 + 活動名稱」
#     3) 都沒有                        -> 只用「自己公司」+ 日期
#
#   如果另外填了「其他關鍵字」（不屬於公司名稱或活動名稱的額外字詞），
#   會附加在上面任何一種 subject 的後面，一起拿去搜尋。
# ============================================================


# ---------------- date helpers（跟 auto_search.py 相同，未修改）----------------

def parse_date(date_raw):
    """ddmmyyyy -> datetime, or None if invalid."""
    try:
        return datetime.strptime(date_raw, "%d%m%Y")
    except (ValueError, TypeError):
        return None


def parse_date_range(date_raw):
    """Accepts either a single date 'ddmmyyyy' or a range 'ddmmyyyy-ddmmyyyy'.
    Returns (start_dt, end_dt, is_range). 若順序相反會自動排好 start/end。"""
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
    d, m, y = dt.day, dt.month, dt.year
    yy = y % 100  # 2位數年份，例如 2026 -> 26（有些網站文章標題只會寫 dd/mm/yy）
    variants = {
        f"{d:02d}/{m:02d}/{y}", f"{d}/{m}/{y}",
        f"{d:02d}-{m:02d}-{y}", f"{d}-{m}-{y}",
        f"{d:02d}.{m:02d}.{y}", f"{d}.{m}.{y}",
        f"{y}-{m:02d}-{d:02d}", f"{y}/{m:02d}/{d:02d}",
        dt.strftime("%d %B %Y"), dt.strftime("%B %d, %Y"), dt.strftime("%B %d %Y"),
        dt.strftime("%d %b %Y"), dt.strftime("%b %d, %Y"), dt.strftime("%b %d %Y"),
        f"{y}年{m}月{d}日", f"{y}年{m:02d}月{d:02d}日",
        f"{m}月{d}日", f"{m:02d}月{d:02d}日",
        f"{y}-{m:02d}-{d:02d}t",
        # ---- 2位數年份版本 ----
        f"{d:02d}/{m:02d}/{yy:02d}", f"{d}/{m}/{yy}",
        f"{d:02d}-{m:02d}-{yy:02d}", f"{d}-{m}-{yy}",
        f"{d:02d}.{m:02d}.{yy:02d}", f"{d}.{m}.{yy}",
    }
    return {v.lower() for v in variants}


def date_variants_range(start_dt, end_dt):
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

def _subject(my_company_name, activity, other_company, extra_keyword=""):
    other = other_company.lstrip("-").strip() if other_company else ""
    act = activity.strip() if activity else ""
    extra = extra_keyword.strip() if extra_keyword else ""

    if other:
        base = other
    elif act:
        base = f"{my_company_name} {act}"
    else:
        base = my_company_name

    if extra:
        base = f"{base} {extra}"
    return base


def build_queries(start_dt, end_dt, activity, other_company, my_company_name, extra_keyword=""):
    is_range = end_dt > start_dt
    other = other_company.lstrip("-").strip() if other_company else ""
    act = activity.strip() if activity else ""

    subject = _subject(my_company_name, activity, other_company, extra_keyword)

    if start_dt.year == end_dt.year:
        year_part = f"{start_dt.year}"
    else:
        year_part = f"{start_dt.year}-{end_dt.year}"
    queries = [(f"{subject} {year_part}".strip(), None, "broad")]

    def _full_date_variants(dt, label_suffix):
        date_en = dt.strftime("%d %B %Y")
        date_slash = f"{dt.day}/{dt.month}/{dt.year}"
        date_cn = f"{dt.year}年{dt.month}月{dt.day}日"
        queries.append((f"{subject} {date_en}", None, f"full-date-en{label_suffix}"))
        queries.append((f"{subject} {date_cn}", None, f"full-date-cn{label_suffix}"))
        queries.append((f"{subject} {date_slash}", None, f"full-date-slash{label_suffix}"))
        return date_cn

    start_date_cn = _full_date_variants(start_dt, "-start" if is_range else "")

    if is_range:
        _full_date_variants(end_dt, "-end")
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

    # 如果有「活動名稱」但沒有「對方公司」，除了原本的「自家公司 + 活動」，
    # 再額外加一組「只用活動名稱」去搜（不含自家公司名稱），
    # 因為有些報導可能完全沒提到我方公司名稱，只寫活動名稱。
    if act and not other:
        activity_only_subject = f"{act} {extra_keyword}".strip() if extra_keyword else act
        queries.append(
            (f"{activity_only_subject} {year_part}".strip(), None, "activity-alone")
        )
        queries.append(
            (activity_only_subject.strip(), tbs, "activity-alone-date-restricted")
        )
        # 跟主 subject 一樣，也補上完整日期版本（英文日期 / 中文日期 / 斜線日期），
        # 而不是只有「年份」跟「tbs 區間」這兩種比較粗略的搜法。
        date_en = start_dt.strftime("%d %B %Y")
        date_slash = f"{start_dt.day}/{start_dt.month}/{start_dt.year}"
        date_cn = f"{start_dt.year}年{start_dt.month}月{start_dt.day}日"
        queries.append((f"{activity_only_subject} {date_en}", None, "activity-alone-full-date-en"))
        queries.append((f"{activity_only_subject} {date_cn}", None, "activity-alone-full-date-cn"))
        queries.append((f"{activity_only_subject} {date_slash}", None, "activity-alone-full-date-slash"))

    for site in KNOWN_SOURCE_SITES:
        # 原本這條:要求「對方公司/自家公司名稱」+「完整中文日期」同時出現在網頁上，
        # 但有些網站文章根本不會寫完整中文日期(例如只寫 24/07/26 這種格式)，
        # 這樣搜尋引擎直接 0 結果，連候選都抓不到。
        queries.append((f"site:{site} {subject} {site_date_cn}", None, f"site:{site}"))
        # 所以再加一條「寬鬆版」:不要求特定日期文字，改用 tbs 讓搜尋引擎自己抓
        # 這段時間內收錄的頁面，抓到候選之後再交給 verify_date() 做精準比對。
        queries.append((f"site:{site} {subject}", tbs, f"site:{site}-loose"))

    # 如果「對方公司」跟「活動」都有填，額外多搜一次「自己公司 + 活動」，
    # 抓那些沒提到對方名字、但有寫活動名稱的文章
    if other and act:
        extra_subject = f"{my_company_name} {act}"
        if extra_keyword:
            extra_subject = f"{extra_subject} {extra_keyword}"
        queries.append((f"{extra_subject} {year_part}".strip(), None, "activity-only"))

    return queries


def build_tbs(start_dt, end_dt, window_days):
    start = start_dt
    end = end_dt + timedelta(days=window_days)
    return f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},cd_max:{end.month}/{end.day}/{end.year}"


# ---------------- Serper search (paginated)（未修改）----------------

def serper_search_page(query, page, tbs=None, num=RESULTS_PER_PAGE):
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
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
    results = []
    for page in range(1, max_pages + 1):
        batch = serper_search_page(query, page, tbs=tbs)
        results.extend(batch)
        if len(batch) < RESULTS_PER_PAGE:
            break
    return results


# ---------------- page fetch ----------------

def fetch_page_text(url):
    """回傳 (純文字, meta 日期集合, 原始 html)。
    html 會拿去判斷這頁是不是「文章列表/年份目錄頁」，並抓出裡面的連結繼續往下找。"""
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"     [fetch failed] {e}")
        return "", set(), ""

    meta_dates = set()

    for m in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:updated_time|'
        r'datePublished|pubdate|date)["\'][^>]+content=["\']([^"\']+)["\']',
        html, flags=re.I,
    ):
        meta_dates.add(m.group(1).lower())

    for m in re.finditer(r'"datePublished"\s*:\s*"([^"]+)"', html, flags=re.I):
        meta_dates.add(m.group(1).lower())

    clean_html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    clean_html = re.sub(r"<style.*?</style>", " ", clean_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", clean_html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)

    return text, meta_dates, html


# ---------------- 目錄/列表頁 往下鑽（新增）----------------
# 有些網站的搜尋結果會命中「年份目錄」、「分類列表」這種頁面
# （例如 malaysiafoochow.com/news_2018/），這種頁面本身通常不會有
# 完整日期文字可比對，需要再往下點進裡面的文章連結，逐篇檢查日期。

MAX_LISTING_LINKS_TO_CHECK = 12
LISTING_URL_HINTS = ("news_", "category", "archive", "page", "tag")


def extract_same_site_links(html, base_url):
    """從 html 抓出「同網域」的連結，過濾掉圖片/PDF/錨點等非文章連結。"""
    links = []
    seen = set()
    base_domain = re.match(r"https?://[^/]+", base_url)
    base_domain = base_domain.group(0) if base_domain else ""

    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        href = m.group(1).strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = base_domain + href
        elif not href.lower().startswith(("http://", "https://")):
            continue
        if not href.startswith(base_domain):
            continue
        if re.search(r"\.(jpg|jpeg|png|gif|pdf|zip|css|js)(\?|$)", href, flags=re.I):
            continue
        href = href.split("#")[0]
        if href in seen or href == base_url:
            continue
        seen.add(href)
        links.append(href)
    return links


def looks_like_listing_page(url, links):
    """粗略判斷這頁是不是「目錄/列表頁」而不是單篇文章頁。"""
    if any(hint in url.lower() for hint in LISTING_URL_HINTS):
        return True
    # 單篇文章頁通常不會有幾十個站內連結；列表頁常常會有一堆文章連結
    return len(links) >= 15


def rank_links_by_relevance(links, year):
    """把網址裡有出現目標年份的連結排前面（比較可能是該年份的文章）。"""
    year_str = str(year)
    with_year = [l for l in links if year_str in l]
    without_year = [l for l in links if year_str not in l]
    return with_year + without_year



# ---------------- date verification ----------------

def verify_date(candidate, start_dt, end_dt, is_range=False, _depth=0):
    """比對候選結果裡有沒有出現符合的日期。

    - 如果是「日期範圍」(is_range=True，使用者有填 start date 和 end date)：
      只認範圍內的日期，不額外往前後擴充。
    - 如果是「單一日期」(is_range=False)：
      因為報導刊出日可能跟活動日期差個幾天，所以會把
      SINGLE_DATE_MATCH_WINDOW_DAYS 天前後也一併視為符合。
    - 如果這頁其實是「目錄/列表頁」(例如 news_2018 這種年份文章列表)，
      本身通常不會有完整日期文字，會再往下點進頁面裡的連結，
      逐一檢查是不是有符合日期的文章（最多檢查 MAX_LISTING_LINKS_TO_CHECK 篇，
      且只往下鑽一層，避免無止盡地爬）。
    """
    if is_range:
        window_start, window_end = start_dt, end_dt
    else:
        window_start = start_dt - timedelta(days=SINGLE_DATE_MATCH_WINDOW_DAYS)
        window_end = end_dt + timedelta(days=SINGLE_DATE_MATCH_WINDOW_DAYS)

    variants = date_variants_range(window_start, window_end)

    if text_has_exact_date(candidate["title"], variants):
        return True, "title"
    if text_has_exact_date(candidate["snippet"], variants):
        return True, "snippet"
    if text_has_exact_date(candidate["link"], variants):
        return True, "url"

    page_text, meta_dates, html = fetch_page_text(candidate["link"])

    for md in meta_dates:
        if text_has_exact_date(md, variants):
            return True, "meta"

    if text_has_exact_date(page_text, variants):
        return True, "content"

    if _depth == 0 and html:
        links = extract_same_site_links(html, candidate["link"])
        if looks_like_listing_page(candidate["link"], links):
            ranked = rank_links_by_relevance(links, start_dt.year)
            to_check = ranked[:MAX_LISTING_LINKS_TO_CHECK]
            print(f"     [目錄頁] {candidate['link']} 像是列表頁，往下檢查 {len(to_check)} 個連結...")
            for sub_link in to_check:
                sub_candidate = {"title": "", "snippet": "", "link": sub_link}
                matched, where = verify_date(
                    sub_candidate, start_dt, end_dt, is_range=is_range, _depth=1
                )
                if matched:
                    candidate["link"] = sub_link  # 把命中的實際文章連結換上去
                    return True, f"listing->{where}"

    return False, None


# ---------------- main pipeline ----------------

def search_event(date_raw, my_company_name, activity="", other_company=None,
                  extra_keyword="", progress_cb=None):
    """跟 auto_search.py 的 search_event 邏輯相同，多了：
       - my_company_name / extra_keyword 兩個參數
       - progress_cb(event_type, payload_dict) callback，
         每個關鍵步驟都會呼叫一次，讓網頁可以即時顯示進度。
    """

    def emit(event_type, **payload):
        if progress_cb:
            try:
                progress_cb(event_type, payload)
            except Exception:
                pass

    start_dt, end_dt, is_range = parse_date_range(date_raw)
    if not start_dt:
        print(f"Invalid date: {date_raw}")
        emit("error", message=f"日期格式錯誤: {date_raw}")
        return []

    if is_range:
        msg = f"活動期間: {start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')}"
        print(f"--- {msg} ---")
        emit("log", message=msg)

    queries = build_queries(start_dt, end_dt, activity, other_company, my_company_name, extra_keyword)

    all_candidates = []
    seen_links = set()

    for query, tbs, label in queries:
        print(f"--- Searching [{label}]: {query!r} (tbs={tbs}) ---")
        emit("query_start", label=label, query=query)
        try:
            batch = serper_search_all_pages(query, tbs=tbs)
        except RuntimeError as e:
            print(f"--- BLOCKED (not 0 results, request was rejected): {e} ---")
            emit("log", message=f"[被封鎖] {label}: {e}")
            continue

        new = 0
        for c in batch:
            if c["link"] and c["link"] not in seen_links:
                seen_links.add(c["link"])
                all_candidates.append(c)
                new += 1
        print(f"    {len(batch)} raw, {new} new after dedupe (total so far: {len(all_candidates)})")
        emit("query_result", label=label, raw=len(batch), new=new, total=len(all_candidates))

    emit("candidates_done", total=len(all_candidates))
    print(f"--- {len(all_candidates)} unique candidates total, verifying exact date one by one ---")

    caught = []
    for i, c in enumerate(all_candidates, 1):
        matched, where = verify_date(c, start_dt, end_dt, is_range=is_range)
        tag = f"CATCH ({where})" if matched else "PASS"
        print(f"[{i}/{len(all_candidates)}] {tag} - {c['title'][:60]}")
        if matched:
            c["matched_in"] = where
            caught.append(c)
        emit(
            "verify_progress",
            index=i,
            total=len(all_candidates),
            matched_so_far=len(caught),
            status="CATCH" if matched else "PASS",
            where=where,
            title=c["title"],
        )

    return caught


def sanitize_filename(name, max_len=120):
    """把字串清乾淨成 Windows/Mac/Linux 都能用的合法檔名片段。
    除了原本擋掉的 <>:"/\\|?* 之外，這裡再多處理：
      - 控制字元（\\n \\t 之類複製貼上不小心帶進來的看不見字元）
      - 結尾的空格或句點（Windows 會直接拒絕這種檔名，是 Errno 22 常見原因）
      - 過長的片段（避免整個路徑超過 Windows 260 字元限制）
    """
    if not name:
        return ""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)  # 控制字元
    name = name.strip().rstrip(" .")  # 結尾空格/句點
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def save_to_txt(date_raw, activity, other_company, my_company_name, extra_keyword, results):
    """跟 auto_search.py 的 save_to_txt 邏輯相同，只是輸出資料夾改用 OUTPUT_DIR
    （預設放在這個檔案旁邊的 output 資料夾，可用環境變數 OUTPUT_DIR 改成你原本的路徑）。"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"建立目錄: {OUTPUT_DIR}")

    act_name = sanitize_filename(activity) or "NA"
    co_name = sanitize_filename(other_company) or "Company"
    date_part = sanitize_filename(date_raw) or "date"

    filename = f"{date_part}_{act_name}_{co_name}.txt"
    full_path = os.path.join(OUTPUT_DIR, filename)

    try:
        start_dt, end_dt, is_range = parse_date_range(date_raw)
        if not start_dt:
            formatted_date = date_raw
        elif is_range:
            formatted_date = f"{start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')}"
        else:
            formatted_date = start_dt.strftime("%d %B %Y")

        with open(full_path, "w", encoding="utf-8") as f:
            f.write("================================================\n")
            f.write(f"TARGET DATE: {date_raw} -> {formatted_date}\n")
            f.write(f"MY COMPANY: {my_company_name}\n")
            f.write(f"OTHER COMPANY: {other_company or 'N/A'}\n")
            f.write(f"ACTIVITY: {activity or 'N/A'}\n")
            f.write(f"EXTRA KEYWORD: {extra_keyword or 'N/A'}\n")
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
        return full_path

    except OSError as e:
        # 存檔失敗（例如檔名/路徑不合法、太長），印出詳細資訊方便除錯，
        # 並改用一個保證合法的保底檔名重試一次，不讓整個任務直接掛掉。
        print(f"[存檔失敗] path={full_path!r} error={e}")
        fallback_name = f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        fallback_path = os.path.join(OUTPUT_DIR, fallback_name)
        print(f"[改用保底檔名重試] {fallback_path}")
        with open(fallback_path, "w", encoding="utf-8") as f:
            f.write(f"[原本存檔失敗，錯誤: {e}]\n")
            f.write(f"[原本檔名: {filename}]\n\n")
            f.write(f"TARGET DATE: {date_raw}\n")
            f.write(f"MY COMPANY: {my_company_name}\n")
            f.write(f"OTHER COMPANY: {other_company or 'N/A'}\n")
            f.write(f"ACTIVITY: {activity or 'N/A'}\n")
            f.write(f"EXTRA KEYWORD: {extra_keyword or 'N/A'}\n")
            f.write("------------------------------------------------\n")
            if not results:
                f.write("No exact-date matches found.\n")
            else:
                for i, r in enumerate(results, 1):
                    f.write(f"{i}. {r['title']}\n   URL: {r['link']}\n")
                    f.write(f"   MATCHED IN: {r.get('matched_in')}\n\n")
        return fallback_path


# ============================================================
# 網頁介面（下半部：Flask + 即時進度顯示）
# ============================================================

app = Flask(__name__)

job_lock = threading.Lock()
job_running = False
job_queue = queue.Queue()


def run_job(params):
    global job_running
    try:
        date_raw = params["date_raw"]
        my_company_name = MY_COMPANY_NAME
        activity = params["activity"]
        other_company = params["other_company"]
        extra_keyword = params["extra_keyword"]

        def progress_cb(event_type, payload):
            job_queue.put({"type": event_type, **payload})

        results = search_event(
            date_raw,
            my_company_name,
            activity=activity,
            other_company=other_company,
            extra_keyword=extra_keyword,
            progress_cb=progress_cb,
        )

        saved_path = save_to_txt(
            date_raw, activity, other_company, my_company_name, extra_keyword, results
        )

        serialized = [
            {
                "title": r["title"],
                "link": r["link"],
                "matched_in": r.get("matched_in"),
                "snippet": r.get("snippet", ""),
            }
            for r in results
        ]
        job_queue.put({
            "type": "done",
            "count": len(results),
            "results": serialized,
            "saved_path": saved_path,
        })
    except Exception as e:
        import traceback
        print("=== [run_job 發生錯誤，完整 traceback] ===")
        traceback.print_exc()
        print("==========================================")
        job_queue.put({"type": "error", "message": str(e)})
        job_queue.put({"type": "done", "count": 0, "results": [], "saved_path": None})
    finally:
        with job_lock:
            job_running = False


@app.route("/")
def index():
    current_year = datetime.now().year
    years = list(range(current_year, current_year - 15, -1))
    months = [
        (1, "January"), (2, "February"), (3, "March"), (4, "April"),
        (5, "May"), (6, "June"), (7, "July"), (8, "August"),
        (9, "September"), (10, "October"), (11, "November"), (12, "December"),
    ]
    return render_template(
        "support.html",
        history=COMPANY_HISTORY,
        years=years,
        months=months,
        my_company_name=MY_COMPANY_NAME,
    )


@app.route("/start", methods=["POST"])
def start():
    global job_running
    data = request.get_json(force=True, silent=True) or {}

    date_raw = (data.get("date_raw") or "").strip()
    activity = (data.get("activity") or "").strip()
    other_company = (data.get("other_company") or "").strip()
    extra_keyword = (data.get("extra_keyword") or "").strip()

    if not MY_COMPANY_NAME or MY_COMPANY_NAME == "請在這裡填入你的公司名稱":
        return jsonify({"error": "還沒在 auto_mix.py 裡設定 MY_COMPANY_NAME，請先改那一行"}), 400
    if not api_key:
        return jsonify({"error": "找不到 API KEY，請檢查 .env 檔案裡的 STRIPE_API_KEY"}), 400

    start_dt, _end_dt, _is_range = parse_date_range(date_raw)
    if not start_dt:
        return jsonify({"error": f"日期格式錯誤: {date_raw}"}), 400

    with job_lock:
        if job_running:
            return jsonify({"error": "已經有搜尋任務在執行中，請稍候再試"}), 409
        job_running = True
        while not job_queue.empty():
            job_queue.get()

    params = {
        "date_raw": date_raw,
        "activity": activity,
        "other_company": other_company,
        "extra_keyword": extra_keyword,
    }
    threading.Thread(target=run_job, args=(params,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/stream")
def stream():
    def gen():
        while True:
            msg = job_queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg.get("type") == "done":
                break
    return Response(gen(), mimetype="text/event-stream")


# 網頁的 HTML/CSS/JS 拆到 templates/support.html，跟這個檔案放在一起即可
# (auto_mix.py 跟 templates/ 資料夾要在同一層)。

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)