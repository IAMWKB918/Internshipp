import requests
import re
import sys
import json
import os
import threading
import queue
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, render_template
import trafilatura

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# CONFIG
# ============================================================
load_dotenv()

api_key = os.getenv("SERPER_API_KEY") or os.getenv("STRIPE_API_KEY")

MY_COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd."

CEO_NAME = "张仕国"

RESULTS_PER_PAGE = 10
MAX_PAGES = 3
FETCH_TIMEOUT = 20
DATE_WINDOW_DAYS = 30

SINGLE_DATE_MATCH_WINDOW_DAYS = 3

MAX_BROAD_MONTH_QUERIES = 6

KNOWN_SOURCE_SITES = [
    "uca.org.my",
    "sarawak.sinchew.com.my",
    "sinchew.com.my",
    "malaysiafoochow.com",
]

COMPANY_HISTORY = [
    "民都魯中華縂商會",
    "林夢福州工會",
    "砂中華縂商會",
    "詩巫販商聯合會",
    "詩巫咖啡商公會",
    "詩巫社區領袖協會",
    "詩巫盆栽協會",
]

OUTPUT_DIR = os.getenv(
    "OUTPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
)

# ============================================================
# Auto Report（生成報告）功能設定 — 跟上面搜尋/分類完全獨立，
# 只需要 user 貼連結進來，用同一個 Ollama model。
# ============================================================
OLLAMA_MODEL = "qwen3:8b"             # 跟 auto_report.py 用同一個 model
OLLAMA_URL = "http://localhost:11434/api/generate"
REPORT_MAX_CONTEXT_CHARS = 12000       # context 太長就截斷

# JSON Schemas，直接丟給 Ollama 的 structured-output "format" 欄位，
# 讓 model 的輸出被限制在這個 schema 裡，不用只靠 prompt 文字要求。
REPORT_ZH_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "accomplishment": {"type": "string"},
        "related_industry": {"type": "string"},
        "country": {"type": "string"},
        "sources_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "accomplishment", "related_industry", "country", "sources_used"],
}

REPORT_EN_SCHEMA = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "description_en": {"type": "string"},
        "accomplishment_en": {"type": "string"},
        "related_industry_en": {"type": "string"},
    },
    "required": ["title_en", "description_en", "accomplishment_en", "related_industry_en"],
}

def parse_date(date_raw):
    """ddmmyyyy -> datetime, or None if invalid."""
    try:
        return datetime.strptime(date_raw, "%d%m%Y")
    except (ValueError, TypeError):
        return None


def parse_date_range(date_raw):
    """接受單一日期 'ddmmyyyy' 或範圍 'ddmmyyyy-ddmmyyyy'。
    回傳 (start_dt, end_dt, is_range)，順序反了會自動排好。"""
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
    """一個日期能拿去搜尋/比對的所有『精準格式』字串。
    涵蓋：ddmmyyyy ddmmyy yyyymmdd yymmdd dd.mm.yyyy yyyy.mm.dd
    dd/mm/yyyy dd/mm/yy dd-mm-yyyy yyyy-mm-dd 中文 英文 等。"""
    d, m, y = dt.day, dt.month, dt.year
    yy = y % 100
    dd = f"{d:02d}"
    mm = f"{m:02d}"
    yyyy = f"{y}"
    yy2 = f"{yy:02d}"

    variants = set()

    variants.add(f"{dd}{mm}{yyyy}")   # ddmmyyyy
    variants.add(f"{dd}{mm}{yy2}")    # ddmmyy
    variants.add(f"{yyyy}{mm}{dd}")   # yyyymmdd
    variants.add(f"{yy2}{mm}{dd}")    # yymmdd

    for sep in ("/", "-", "."):
        variants.add(f"{dd}{sep}{mm}{sep}{yyyy}")
        variants.add(f"{d}{sep}{m}{sep}{yyyy}")
        variants.add(f"{dd}{sep}{mm}{sep}{yy2}")
        variants.add(f"{d}{sep}{m}{sep}{yy2}")
        variants.add(f"{yyyy}{sep}{mm}{sep}{dd}")
        variants.add(f"{yyyy}{sep}{m}{sep}{d}")

    variants.add(dt.strftime("%d %B %Y"))
    variants.add(dt.strftime("%B %d, %Y"))
    variants.add(dt.strftime("%B %d %Y"))
    variants.add(dt.strftime("%d %b %Y"))
    variants.add(dt.strftime("%b %d, %Y"))
    variants.add(dt.strftime("%b %d %Y"))

    variants.add(f"{yyyy}年{m}月{d}日")
    variants.add(f"{yyyy}年{mm}月{dd}日")
    variants.add(f"{m}月{d}日")
    variants.add(f"{mm}月{dd}日")

    # ISO 帶 T
    variants.add(f"{yyyy}-{mm}-{dd}t")

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


# ---------------- 通用日期抽取（給「大範圍分類」用）----------------

_EN_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAMES_RE = "|".join(sorted(_EN_MONTHS.keys(), key=len, reverse=True))


def _norm_2digit_year(yy):
    yy = int(yy)
    return 2000 + yy if yy < 70 else 1900 + yy


def extract_dates_from_text(text):
    """從任意文字裡抓出所有『看起來像日期』的片段，回傳
    (year_or_None, month, day_or_None) 的 set，用來跟目標日期做分類比對。
    day 抓不到就是 None，只用來輔助判斷不影響 yyyy/mm 分類。"""
    if not text:
        return set()
    found = set()
    t = text

    # yyyy-mm-dd / yyyy/mm/dd / yyyy.mm.dd
    for m in re.finditer(r'(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})', t):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((y, mo, d))

    # dd-mm-yyyy / dd/mm/yyyy / dd.mm.yyyy（馬來西亞慣例：日在前）
    for m in re.finditer(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})', t):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((y, mo, d))

    # dd-mm-yy / dd/mm/yy / dd.mm.yy（2位數年）
    for m in re.finditer(r'(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})(?!\d)', t):
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((_norm_2digit_year(yy), mo, d))

    # 緊湊 8 位數：同時嘗試 ddmmyyyy 跟 yyyymmdd 兩種解讀
    for m in re.finditer(r'(?<!\d)(\d{8})(?!\d)', t):
        s = m.group(1)
        d, mo, y = int(s[0:2]), int(s[2:4]), int(s[4:8])
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
            found.add((y, mo, d))
        y2, mo2, d2 = int(s[0:4]), int(s[4:6]), int(s[6:8])
        if 1 <= mo2 <= 12 and 1 <= d2 <= 31 and 1900 <= y2 <= 2100:
            found.add((y2, mo2, d2))

    # 緊湊 6 位數：ddmmyy / yymmdd 兩種解讀都試
    for m in re.finditer(r'(?<!\d)(\d{6})(?!\d)', t):
        s = m.group(1)
        d, mo, yy = int(s[0:2]), int(s[2:4]), int(s[4:6])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((_norm_2digit_year(yy), mo, d))
        yy2, mo2, d2 = int(s[0:2]), int(s[2:4]), int(s[4:6])
        if 1 <= mo2 <= 12 and 1 <= d2 <= 31:
            found.add((_norm_2digit_year(yy2), mo2, d2))

    # 中文 yyyy年mm月dd日
    for m in re.finditer(r'(\d{4})年(\d{1,2})月(\d{1,2})日', t):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((y, mo, d))

    # 中文 mm月dd日（沒寫年份）
    for m in re.finditer(r'(?<!\d)(\d{1,2})月(\d{1,2})日', t):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.add((None, mo, d))

    # 英文 "Month dd, yyyy" / "dd Month yyyy"
    for m in re.finditer(rf'({_MONTH_NAMES_RE})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})', t, flags=re.I):
        mo = _EN_MONTHS[m.group(1).lower()]
        d, y = int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31:
            found.add((y, mo, d))
    for m in re.finditer(rf'(\d{{1,2}})\s+({_MONTH_NAMES_RE})\.?,?\s+(\d{{4}})', t, flags=re.I):
        d = int(m.group(1))
        mo = _EN_MONTHS[m.group(2).lower()]
        y = int(m.group(3))
        if 1 <= d <= 31:
            found.add((y, mo, d))

    # 英文 "Month yyyy"（沒寫日）
    for m in re.finditer(rf'({_MONTH_NAMES_RE})\.?\s+(\d{{4}})', t, flags=re.I):
        mo = _EN_MONTHS[m.group(1).lower()]
        y = int(m.group(2))
        found.add((y, mo, None))

    return found


# ---------------- subject / query building ----------------

def build_subject(my_company_name, activity, other_company, extra_keyword):
    """依照優先順序決定要拿去搜尋的 subject：
      1) 有 other company -> other company（+活動/其他，絕不含自己公司名）
      2) 沒有 other company 但有活動/其他 -> 自己公司 + 活動/其他
      3) 都沒有 -> 只用自己公司
    回傳 (subject, mode)，mode 是 "other" / "own+extra" / "own-only"。"""
    other = (other_company or "").lstrip("-").strip()
    act = (activity or "").strip()
    extra = (extra_keyword or "").strip()
    extras = " ".join(t for t in (act, extra) if t)

    if other:
        subject = f"{other} {extras}".strip() if extras else other
        mode = "other"
    elif extras:
        subject = f"{my_company_name} {extras}".strip()
        mode = "own+extra"
    else:
        subject = my_company_name
        mode = "own-only"

    return subject, mode


def build_tbs(start_dt, end_dt, window_days):
    start = start_dt
    end = end_dt + timedelta(days=window_days)
    return f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},cd_max:{end.month}/{end.day}/{end.year}"


def build_queries(start_dt, end_dt, activity, other_company, my_company_name, extra_keyword=""):
    is_range = end_dt > start_dt
    act = (activity or "").strip()
    extra = (extra_keyword or "").strip()

    subject, mode = build_subject(my_company_name, activity, other_company, extra_keyword)

    years = sorted({start_dt.year, end_dt.year})
    year_part = f"{years[0]}" if len(years) == 1 else f"{years[0]}-{years[1]}"

    queries = []

    # ---------- 第一輪：大範圍撒網 subject + yyyy(+mm) ----------
    ym_pairs = []
    d = start_dt.replace(day=1)
    while d <= end_dt:
        ym_pairs.append((d.year, d.month))
        # 跳到下個月第一天
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    if (end_dt.year, end_dt.month) not in ym_pairs:
        ym_pairs.append((end_dt.year, end_dt.month))

    if len(ym_pairs) <= MAX_BROAD_MONTH_QUERIES:
        for y, m in ym_pairs:
            month_en = datetime(y, m, 1).strftime("%B")
            queries.append((f"{subject} {month_en} {y}", None, f"broad-mm-yyyy-{y}-{m:02d}"))
    # 年份範圍太廣時的保底大範圍查詢
    queries.append((f"{subject} {year_part}".strip(), None, "broad-yyyy"))

    # ---------- 第二輪：精準日期格式 ----------
    def add_exact(dt, suffix):
        queries.append((f"{subject} {dt.strftime('%d %B %Y')}", None, f"exact-en{suffix}"))
        queries.append((f"{subject} {dt.day}/{dt.month}/{dt.year}", None, f"exact-slash{suffix}"))
        queries.append((f"{subject} {dt.day:02d}-{dt.month:02d}-{dt.year}", None, f"exact-dash{suffix}"))
        queries.append((f"{subject} {dt.day:02d}.{dt.month:02d}.{dt.year}", None, f"exact-dot{suffix}"))
        queries.append((f"{subject} {dt.year}年{dt.month}月{dt.day}日", None, f"exact-cn{suffix}"))
        queries.append((f"{subject} {dt.day:02d}{dt.month:02d}{dt.year}", None, f"exact-compact{suffix}"))

    add_exact(start_dt, "-start" if is_range else "")
    if is_range:
        add_exact(end_dt, "-end")
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
        site_date_cn = f"{start_dt.year}年{start_dt.month}月{start_dt.day}日"

    # ---------- 第三輪：tbs 日期區間限制 ----------
    tbs = build_tbs(start_dt, end_dt, DATE_WINDOW_DAYS)
    queries.append((subject.strip(), tbs, "date-restricted"))

    # ---------- 補充查詢：other 模式下若還有活動/其他關鍵字，
    #            額外抓「自己公司 + 活動/其他」漏網的報導 ----------
    if mode == "other" and (act or extra):
        supp = " ".join(t for t in (my_company_name, act, extra) if t).strip()
        queries.append((f"{supp} {year_part}".strip(), None, "supplement-own+extra"))
        queries.append((supp, tbs, "supplement-own+extra-date-restricted"))

    # ---------- 第四輪：已知來源網站加強搜尋 ----------
    for site in KNOWN_SOURCE_SITES:
        queries.append((f"site:{site} {subject} {site_date_cn}", None, f"site:{site}"))
        queries.append((f"site:{site} {subject}", tbs, f"site:{site}-loose"))

    return queries, subject, mode


# ---------------- Serper search（未修改，沿用原本 API 串接方式）----------------

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


# ---------------- page fetch（未修改）----------------

def fetch_page_text(url):
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"     [fetch failed] {e}")
        return "", set()

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

    return text, meta_dates


# ---------------- 資料吻合度（優先序：年份 > 公會/其他公司名字 > 活動名字 > 我方公司名字 > CEO/老闆名字）----------------

IDENTITY_PRIORITY = ["other_company", "activity", "my_company", "ceo"]
IDENTITY_LABEL_ZH = {
    "other_company": "公會/其他公司名字",
    "activity": "活動名字",
    "my_company": "公司名字",
    "ceo": "CEO/老闆名字",
}


def _normalize_for_match(s):
    """比對用的正規化：去空白、去標點、轉小寫。
    中文名稱常見會有全形/半形空格差異，英文名稱（例如公司名裡的
    Sdn. Bhd. / S.K.）常見標點符號有沒有不一致（"Sdn. Bhd." vs "Sdn Bhd"），
    這些差異都會讓原本的 substring 比對失敗、造成日期明明對上卻被判定
    「沒提到辨識資料」而整篇移除。所以這裡把空白跟常見標點符號都去掉，
    只留下文字本身（中文字/英數字）再比對，減少因為符號差異造成的漏抓。"""
    if not s:
        return ""
    s = s.strip()
    # 去掉常見標點/符號：句號 逗號 括號 連字號 底線 斜線 引號 等
    s = re.sub(r"[\s.,()\[\]{}\-_/\\'\"‘’“”，。、（）【】]+", "", s)
    return s.lower()


def _identity_aliases(raw):
    """支援在同一個欄位裡填多個別名/簡稱，用 '/' 或 '、' 分隔。
    例如 other_company 填 "民都魯中華縂商會/民中總" 兩個都算數，
    只要其中一個有出現在候選網頁裡就算命中。"""
    if not raw:
        return []
    parts = re.split(r"[/、]", raw)
    return [p.strip() for p in parts if p.strip()]


def build_identity_fields(other_company, activity, my_company_name, ceo_name):
    """依優先順序（年份已經在日期比對處理，這裡不重複）：
       公會/其他公司名字 > 活動名字 > 我方公司名字 > CEO/老闆名字
    只回傳使用者這次實際有填的欄位，格式 [(label, raw, normalized), ...]。"""
    order = [
        ("other_company", other_company),
        ("activity", activity),
        ("my_company", my_company_name),
        ("ceo", ceo_name),
    ]
    fields = []
    for label, val in order:
        val = (val or "").strip()
        if not val:
            continue
        # 同一欄位可能填了多個別名/簡稱（用 / 或 、分隔），
        # 每個別名各自正規化，只要其中一個命中就算這個欄位命中
        aliases = _identity_aliases(val) or [val]
        norms = [n for n in (_normalize_for_match(a) for a in aliases) if n]
        if norms:
            fields.append((label, val, norms))
    return fields


# ---------------- 日期分類邏輯 ----------------

def _classify_date_tier(candidate, page_text, meta_dates, start_dt, end_dt, is_range, combined_text):
    """回傳 (date_tier, where)。date_tier 為 None 代表日期完全對不上，直接移除。

    tier 信心排序（高到低）：
      exact              -> 命中精準日期字串
      broad-month        -> 內文抓到日期，年份+月份都對得上
      broad-month-noyear -> 內文抓到「幾月幾日」但沒年份，月份對得上
      broad-year         -> 只有年份對得上
    年份對不上的候選一律不會進到任何 tier（等同移除，對應「年份」優先序最高）。
    """
    if is_range:
        window_start, window_end = start_dt, end_dt
    else:
        window_start = start_dt - timedelta(days=SINGLE_DATE_MATCH_WINDOW_DAYS)
        window_end = end_dt + timedelta(days=SINGLE_DATE_MATCH_WINDOW_DAYS)

    # ---- tier: exact ----
    exact_variants = date_variants_range(window_start, window_end)
    for field, label in (
        (candidate.get("title", ""), "title"),
        (candidate.get("snippet", ""), "snippet"),
        (candidate.get("link", ""), "url"),
    ):
        if text_has_exact_date(field, exact_variants):
            return "exact", label
    for md in meta_dates:
        if text_has_exact_date(md, exact_variants):
            return "exact", "meta"
    if text_has_exact_date(page_text, exact_variants):
        return "exact", "content"

    # ---- tier: broad-month / broad-month-noyear / broad-year ----
    target_years = set()
    target_yms = set()
    d = window_start
    while d <= window_end:
        target_years.add(d.year)
        target_yms.add((d.year, d.month))
        d += timedelta(days=1)

    tokens = extract_dates_from_text(combined_text)

    # 年份對不上的 token 直接忽略（等於「yy 不一樣就移除」）
    valid_tokens = [tok for tok in tokens if tok[0] is None or tok[0] in target_years]

    target_months = {mo for (_, mo) in target_yms}

    if any(y is not None and mo is not None and (y, mo) in target_yms for (y, mo, d_) in valid_tokens):
        return "broad-month", "content"

    if any(y is None and mo in target_months for (y, mo, d_) in valid_tokens):
        return "broad-month-noyear", "content"

    if any(y is not None and y in target_years for (y, mo, d_) in valid_tokens):
        return "broad-year", "content"

    return None, None


def classify_candidate(candidate, page_text, meta_dates, start_dt, end_dt, is_range,
                        other_company="", activity="", my_company_name="", ceo_name=""):
    """把「日期對得上」跟「資料吻合度」兩層都過濾過，才算真的抓到。

    現在的情況是：只靠日期比對太鬆，時間對上但內容其實毫不相關的網頁也會被抓進來。
    所以這裡多加一層 -- 候選網頁的標題/摘要/網址/meta/內文，必須至少出現使用者這次
    有填的其中一項辨識資料（優先序：公會/其他公司名字 > 活動名字 > 我方公司名字 >
    CEO/老闆名字），日期對上但完全沒提到任何一項的，視為雜訊直接移除。
    如果使用者這次完全沒填任何辨識資料（只有日期），就維持只用日期分類。

    回傳 (date_tier, identity_label, where)：
      date_tier / identity_label 任一為 None 都代表這篇被移除。
    """
    combined_text = " ".join([
        candidate.get("title", "") or "",
        candidate.get("snippet", "") or "",
        candidate.get("link", "") or "",
        " ".join(meta_dates),
        page_text or "",
    ])

    date_tier, where = _classify_date_tier(
        candidate, page_text, meta_dates, start_dt, end_dt, is_range, combined_text
    )
    if date_tier is None:
        return None, None, None, "date-not-matched"

    identity_fields = build_identity_fields(other_company, activity, my_company_name, ceo_name)
    if not identity_fields:
        # 這次沒填任何辨識資料，只能靠日期，維持原本邏輯，不因為新過濾層而移除
        return date_tier, None, where, None

    combined_norm = _normalize_for_match(combined_text)
    matched_label = None
    for label, raw, norms in identity_fields:
        if any(n in combined_norm for n in norms):
            matched_label = label
            break

    if matched_label is None:
        # 日期雖然對上，但完全沒有出現任何一項你填的辨識資料 -> 判定為雜訊，移除
        # 附上這次實際檢查過的欄位，方便你比對「是不是名稱寫法不一樣」
        checked = "; ".join(f"{IDENTITY_LABEL_ZH.get(l, l)}=\"{raw}\"" for l, raw, _ in identity_fields)
        return None, None, None, f"date-ok-but-no-identity-match (checked: {checked})"

    return date_tier, matched_label, where, None


# ---------------- main pipeline ----------------

def search_event(date_raw, my_company_name, activity="", other_company=None,
                  extra_keyword="", ceo_name="", progress_cb=None):
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

    queries, subject, mode = build_queries(
        start_dt, end_dt, activity, other_company, my_company_name, extra_keyword
    )
    emit("log", message=f"[subject] ({mode}) {subject}")

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
    print(f"--- {len(all_candidates)} unique candidates total, classifying one by one ---")

    DATE_TIER_RANK = {"exact": 0, "broad-month": 1, "broad-month-noyear": 2, "broad-year": 3}
    IDENTITY_RANK = {label: i for i, label in enumerate(IDENTITY_PRIORITY)}

    caught = []
    for i, c in enumerate(all_candidates, 1):
        page_text, meta_dates = fetch_page_text(c["link"])
        date_tier, identity_label, where, remove_reason = classify_candidate(
            c, page_text, meta_dates, start_dt, end_dt, is_range=is_range,
            other_company=other_company, activity=activity,
            my_company_name=my_company_name, ceo_name=ceo_name,
        )
        if date_tier:
            id_desc = IDENTITY_LABEL_ZH.get(identity_label, "僅日期(未填辨識資料)")
            tag = f"CATCH ({date_tier} / {id_desc})"
        elif remove_reason == "date-not-matched":
            tag = "REMOVED (日期對不上)"
        else:
            tag = f"REMOVED ({remove_reason})"
        print(f"[{i}/{len(all_candidates)}] {tag} - {c['title'][:60]}")
        if date_tier:
            c["date_tier"] = date_tier
            c["identity_match"] = identity_label
            c["tier"] = f"{date_tier}+{identity_label}" if identity_label else date_tier
            c["matched_in"] = where
            caught.append(c)
        emit(
            "verify_progress",
            index=i,
            total=len(all_candidates),
            matched_so_far=len(caught),
            status="CATCH" if date_tier else "REMOVED",
            date_tier=date_tier,
            identity_match=identity_label,
            where=where,
            title=c["title"],
        )

    caught.sort(key=lambda c: (
        DATE_TIER_RANK.get(c.get("date_tier"), 99),
        IDENTITY_RANK.get(c.get("identity_match"), 99),
    ))
    return caught


def save_to_txt(date_raw, activity, other_company, my_company_name, extra_keyword, results, ceo_name=""):
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"建立目錄: {OUTPUT_DIR}")

    act_name = activity if activity.strip() else "NA"
    co_name = other_company if other_company.strip() else "Company"

    filename = f"{date_raw}_{act_name}_{co_name}.txt"
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")

    stem, ext = os.path.splitext(filename)
    stem = stem.rstrip(" .")
    if not stem:
        stem = "untitled"
    RESERVED_NAMES = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if stem.upper() in RESERVED_NAMES:
        stem = f"_{stem}"
    filename = stem + ext

    MAX_FILENAME_LEN = 150
    if len(filename) > MAX_FILENAME_LEN:
        stem, ext = os.path.splitext(filename)
        filename = stem[: MAX_FILENAME_LEN - len(ext)] + ext

    full_path = os.path.join(OUTPUT_DIR, filename)

    start_dt, end_dt, is_range = parse_date_range(date_raw)
    if not start_dt:
        formatted_date = date_raw
    elif is_range:
        formatted_date = f"{start_dt.strftime('%d %B %Y')} - {end_dt.strftime('%d %B %Y')}"
    else:
        formatted_date = start_dt.strftime("%d %B %Y")

    DATE_TIER_LABEL = {
        "exact": "精準日期命中",
        "broad-month": "廣泛比對(年+月吻合)",
        "broad-month-noyear": "廣泛比對(月吻合,無年份)",
        "broad-year": "廣泛比對(僅年份吻合)",
    }

    with open(full_path, "w", encoding="utf-8") as f:
        f.write("================================================\n")
        f.write(f"TARGET DATE: {date_raw} -> {formatted_date}\n")
        f.write(f"MY COMPANY: {my_company_name}\n")
        f.write(f"OTHER COMPANY (公會/其他公司): {other_company or 'N/A'}\n")
        f.write(f"ACTIVITY (活動名字): {activity or 'N/A'}\n")
        f.write(f"CEO/老闆名字: {ceo_name or 'N/A'}\n")
        f.write(f"EXTRA KEYWORD: {extra_keyword or 'N/A'}\n")
        f.write(f"FOUND AT: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("優先序: 年份 > 公會/其他公司名字 > 活動名字 > 公司名字 > CEO/老闆名字\n")
        f.write("------------------------------------------------\n")

        if not results:
            f.write("No matches found.\n")
        else:
            for i, r in enumerate(results, 1):
                date_label = DATE_TIER_LABEL.get(r.get("date_tier"), r.get("date_tier", ""))
                id_label = IDENTITY_LABEL_ZH.get(r.get("identity_match"), "僅日期(未填辨識資料)")
                f.write(f"{i}. [{date_label} / 命中: {id_label}] {r['title']}\n")
                f.write(f"   URL: {r['link']}\n")
                f.write(f"   MATCHED IN: {r.get('matched_in')}\n")
                if r.get("snippet"):
                    f.write(f"   SNIPPET: {r['snippet']}\n")
                f.write("\n")

        f.write(f"TOTAL MATCHES: {len(results)}\n")
        f.write("================================================\n")

    print(f"\n[成功] 結果已儲存至: {full_path}")
    return full_path


# ============================================================
# Auto Report（生成報告）功能 — 從 auto_report.py 搬過來，邏輯不變，
# 差別只是：連結不是從 links.txt 讀，而是 user 在網頁貼上來的。
# 跟上面搜尋 / 分類功能完全獨立，沒有先後順序，user 也可以只用這個功能。
# ============================================================

def fetch_article_text(url: str) -> str | None:
    """抓取單一連結的文章內文。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            print(f"  [SKIP] 無法下載: {url}")
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if not text or len(text.strip()) < 50:
            print(f"  [SKIP] 內容太短或空白: {url}")
            return None
        return text.strip()
    except Exception as e:
        print(f"  [ERROR] 抓取失敗 {url}: {e}")
        return None


def build_report_context(links: list[str]) -> str:
    """依序抓取所有連結，合併成一個 context 給模型讀。"""
    chunks = []
    for i, url in enumerate(links, 1):
        print(f"[報告 {i}/{len(links)}] 抓取: {url}")
        text = fetch_article_text(url)
        if text:
            chunks.append(f"---Source {i}: {url}---\n{text}")

    if not chunks:
        raise RuntimeError("所有連結都無法讀取到內容，請確認連結是否正確、可公開存取")

    context = "\n\n".join(chunks)
    if len(context) > REPORT_MAX_CONTEXT_CHARS:
        print(f"[INFO] context 太長 ({len(context)} 字), 截斷至 {REPORT_MAX_CONTEXT_CHARS}")
        context = context[:REPORT_MAX_CONTEXT_CHARS]
    return context


def build_report_zh_prompt(company: str, context: str) -> str:
    """第一步：只用繁體中文抽取結構化內容。"""
    return f"""Read the source content below (news articles / reports related to "{company}").
Based ONLY on this content — do not invent facts not mentioned in it — extract:
- title: a concise Traditional Chinese title for what happened
- description: 2-4 sentences in Traditional Chinese describing the event/report
- accomplishment: concrete achievements or highlights mentioned in the source, in Traditional Chinese
- related_industry: the relevant industry sector, in Traditional Chinese
- country: the country this content is about, inferred from the content itself
- sources_used: the source URLs you actually drew content from

Write in Traditional Chinese for all text fields.

Source content:
{context}
"""


def build_report_en_prompt(zh_data: dict) -> str:
    """第二步：把中文欄位翻成英文。"""
    return f"""Translate the following Traditional Chinese fields into natural, fluent English.
Do not summarize further or add new information — translate faithfully.

title: {zh_data.get('title', '')}
description: {zh_data.get('description', '')}
accomplishment: {zh_data.get('accomplishment', '')}
related_industry: {zh_data.get('related_industry', '')}

Return the English translations as: title_en, description_en, accomplishment_en, related_industry_en.
"""


def call_ollama(prompt: str, schema: dict) -> str:
    """呼叫本地 Ollama model，用 schema 限制輸出格式。"""
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,      # 關掉 Qwen3 的 "thinking" 輸出
            "format": schema,    # structured-output：限制在這個 JSON schema
            "options": {"temperature": 0.3},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def parse_ollama_json(raw: str, debug_filename: str) -> dict:
    """把 model 回應解析成 dict，先把原始輸出存檔方便除錯。"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    Path(os.path.join(OUTPUT_DIR, debug_filename)).write_text(raw, encoding="utf-8")

    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```json|```", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"模型輸出裡找不到 JSON，請查看 {debug_filename}")
    json_str = cleaned[start : end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON 解析失敗: {e}（請查看 {debug_filename}）")


def generate_report_from_links(links: list[str]) -> dict:
    """給一批連結，跑兩段 Ollama（抽取中文 -> 翻譯英文），回傳合併後的結果並存檔。"""
    context = build_report_context(links)

    print(f"[報告 1/2] 用 {OLLAMA_MODEL} 抽取繁體中文內容...")
    zh_prompt = build_report_zh_prompt(MY_COMPANY_NAME, context)
    zh_raw = call_ollama(zh_prompt, REPORT_ZH_SCHEMA)
    zh_data = parse_ollama_json(zh_raw, "report_raw_zh.txt")

    print(f"[報告 2/2] 用 {OLLAMA_MODEL} 翻譯成英文...")
    en_prompt = build_report_en_prompt(zh_data)
    en_raw = call_ollama(en_prompt, REPORT_EN_SCHEMA)
    en_data = parse_ollama_json(en_raw, "report_raw_en.txt")

    data = {
        "title_zh": zh_data.get("title", ""),
        "title_en": en_data.get("title_en", ""),
        "description_zh": zh_data.get("description", ""),
        "description_en": en_data.get("description_en", ""),
        "accomplishment_zh": zh_data.get("accomplishment", ""),
        "accomplishment_en": en_data.get("accomplishment_en", ""),
        "related_industry_zh": zh_data.get("related_industry", ""),
        "related_industry_en": en_data.get("related_industry_en", ""),
        "country": zh_data.get("country", ""),
        "sources_used": zh_data.get("sources_used", []),
    }

    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUTPUT_DIR, f"report_{timestamp}.json")
    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    data["saved_path"] = json_path

    print(f"[報告完成] 已儲存至: {json_path}")
    return data


# ============================================================
# 網頁介面（Flask + 即時進度顯示，跟原本一樣，前端不用改）
# ============================================================

app = Flask(__name__)
# 強制每次 request 都重新讀 templates 檔案，不要快取在記憶體裡，
# 避免改了 support.html 之後，Flask process 沒重啟就一直吃舊版本。
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

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
        ceo_name = CEO_NAME

        def progress_cb(event_type, payload):
            job_queue.put({"type": event_type, **payload})

        results = search_event(
            date_raw,
            my_company_name,
            activity=activity,
            other_company=other_company,
            extra_keyword=extra_keyword,
            ceo_name=ceo_name,
            progress_cb=progress_cb,
        )

        saved_path = save_to_txt(
            date_raw, activity, other_company, my_company_name, extra_keyword, results,
            ceo_name=ceo_name,
        )

        serialized = [
            {
                "title": r["title"],
                "link": r["link"],
                "date_tier": r.get("date_tier"),
                "identity_match": r.get("identity_match"),
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
        return jsonify({"error": "找不到 API KEY，請檢查 .env 檔案裡的 SERPER_API_KEY"}), 400

    # 固定條件：date（ddmmyyyy 或 ddmmyyyy-ddmmyyyy）必填
    start_dt, _end_dt, _is_range = parse_date_range(date_raw)
    if not start_dt:
        return jsonify({"error": f"日期格式錯誤，請用 ddmmyyyy 或 ddmmyyyy-ddmmyyyy: {date_raw}"}), 400

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


@app.route("/generate_report", methods=["POST"])
def generate_report():
    """獨立功能：user 貼連結進來，生成中英文報告句子。
    跟上面的搜尋/分類任務（/start /stream）完全無關，也不用先跑過那個。"""
    data = request.get_json(force=True, silent=True) or {}
    raw_links = data.get("links") or []
    links = [str(l).strip() for l in raw_links if str(l).strip()]

    if not links:
        return jsonify({"error": "至少要貼一個連結才能生成，因為沒有 reference 內容"}), 400

    try:
        result = generate_report_from_links(links)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok", "data": result})


@app.route("/stream")
def stream():
    def gen():
        while True:
            msg = job_queue.get()
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            if msg.get("type") == "done":
                break
    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)