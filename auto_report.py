import json
import re
import sys
from pathlib import Path
import requests
import trafilatura
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

OLLAMA_MODEL = "qwen3:8b"             # Your model name
OLLAMA_URL = "http://localhost:11434/api/generate"
LINKS_FILE = "links.txt"
MAX_CONTEXT_CHARS = 12000

# === 輸出資料夾設定 ===
OUTPUT_DIR = Path(r"C:\Users\wkb75\Documents\intern cck record\florence\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)   # 資料夾不存在就自動建立

OUTPUT_JSON = OUTPUT_DIR / "output.json"
OUTPUT_TXT = OUTPUT_DIR / "output.txt"
RAW_ZH_FILE = OUTPUT_DIR / "raw_zh.txt"
RAW_EN_FILE = OUTPUT_DIR / "raw_en.txt"

# === 公司 / CEO 常數（寫死）===
COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd." 
CEO_NAME = "丹斯里拿督张仕国"           

CATEGORY_OPTIONS = [
    "Business Awards",
    "Commemorative Items",
    "Community Service",
    "Corporate Milestones",
    "Investor Relations",
    "National Honors",
    "Notable Events",
]

ZH_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORY_OPTIONS},
        "description": {"type": "string"},
        "accomplishment": {"type": "string"},
        "country": {"type": "string"},
        "related_industry": {"type": "string"},
        "related_year": {"type": "string"},
        "additional_source": {"type": "string"},
        "acquisition_date": {"type": "string"},
    },
    "required": [
        "name", "category", "description", "accomplishment",
        "country", "related_industry", "related_year",
        "additional_source", "acquisition_date",
    ],
}

EN_SCHEMA = {
    "type": "object",
    "properties": {
        "name_en": {"type": "string"},
        "description_en": {"type": "string"},
        "accomplishment_en": {"type": "string"},
        "country_en": {"type": "string"},
        "related_industry_en": {"type": "string"},
    },
    "required": [
        "name_en", "description_en", "accomplishment_en",
        "country_en", "related_industry_en",
    ],
}


def load_links(path: str) -> list[str]:
    """Extract all http(s) URLs found anywhere in the file, regardless of
    surrounding text/format (handles report-style output like 'URL: https://...')."""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] {path} not found. Create it first.")
        sys.exit(1)

    raw = p.read_text(encoding="utf-8")
    url_pattern = re.compile(r'https?://[^\s\u4e00-\u9fff"\'）)]+')
    links = url_pattern.findall(raw)

    seen = set()
    deduped = []
    for l in links:
        l = l.rstrip('.,;')
        if l not in seen:
            seen.add(l)
            deduped.append(l)

    if not deduped:
        print(f"[ERROR] No valid http(s) links found in {path}.")
        sys.exit(1)

    print(f"[INFO] Extracted {len(deduped)} URL(s) from {path}:")
    for l in deduped:
        print(f"  - {l}")

    return deduped


def fetch_article_text(url: str) -> str | None:
    """Fetch and extract the article body text from a single URL."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            print(f"  [SKIP] Could not download: {url}")
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if not text or len(text.strip()) < 50:
            print(f"  [SKIP] Content too short or empty: {url}")
            return None
        return text.strip()
    except Exception as e:
        print(f"  [ERROR] Failed to fetch {url}: {e}")
        return None


def build_context(links: list[str]) -> str:
    """Fetch all links in order and combine their text into one context block."""
    chunks = []
    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] Fetching: {url}")
        text = fetch_article_text(url)
        if text:
            chunks.append(f"---Source {i}: {url}---\n{text}")

    if not chunks:
        print("[ERROR] All links failed to fetch. Cannot continue.")
        sys.exit(1)

    context = "\n\n".join(chunks)
    if len(context) > MAX_CONTEXT_CHARS:
        print(f"[INFO] Context too long ({len(context)} chars), truncating to {MAX_CONTEXT_CHARS}")
        context = context[:MAX_CONTEXT_CHARS]
    return context


def build_zh_prompt(company: str, ceo_name: str, context: str) -> str:
    """Step 1: 用簡體中文抽取結構化內容。"""
    return f"""你是專門為企業撰寫獎項/榮譽紀錄的編輯。以下是關於「{company}」獲得的一個獎項或榮譽的來源內容。只根據內容本身抽取資訊，不要編造未提及的事實。

【重要】所有中文欄位一律使用**简体中文**输出，不要使用繁体字。

請依照以下規則抽取：

1. name（活动/奖项名称）：
说明这个活动是由谁、什么公司或俱乐部举办的，为了什么目的举办（例如：三年利润最高奖、20周年纪念奖品）。也可以是活动本身的名字。语气正式、简洁。

2. category（分类）：
从以下选项中，选出最符合这次活动性质的一个分类，只能选一个，原样输出选项文字（保持英文，不要翻译）：
{", ".join(CATEGORY_OPTIONS)}

3. description（描述）：
说明这个奖项代表了什么、有多重要或多具荣誉性。是否由公众/业界评选？在什么地点、什么场地颁发？有没有重要人物（政要、行业领袖等）主礼颁奖？

4. accomplishment（成就）：
说明这次获奖/事件背后的卓越表现——可以是「{ceo_name}」（公司CEO）个人的领导或决策，也可以是公司整体在某个领域/行业的具体成果、突破或贡献（例如：业务增长、技术创新、市场拓展、社会影响力等）。优先使用文章中明确提到的内容；如果文章同时提到CEO个人与公司整体的表现，两者都可以写入。请具体说明是在什么领域、什么行业，带来了什么实际好处或发展，不要泛泛而谈。如果来源完全没有提及任何具体成就，请如实填写"来源中未明确提及具体成就"，不要编造。
5. country（国家）：
根据文章内容判断这个奖项/活动发生在哪个国家。

6. related_industry（相关行业）：
说明是在什么类型的场地/场合举办的、在哪里颁的奖，据此判断相关行业领域。

7. related_year（年份）：
活动/颁奖发生的年份。

8. additional_source（补充来源）：
来源内容中每段前面都标注了"---Source N: URL---"，请从中选出信息量最丰富、内容最完整的那一个来源，填写它的URL。

9. acquisition_date（获奖/取得日期）：
明确抓出活动或颁奖发生的具体日期（尽量精确到年月日）。如果来源中没有明确日期，填写"未提及"。

来源内容：
{context}
"""


def build_en_prompt(zh_data: dict) -> str:
    """Step 2: 把中文欄位翻成英文，category/related_year/additional_source/acquisition_date 不需要翻。"""
    return f"""Translate the following Chinese fields into natural, fluent English.
Do not summarize further or add new information — translate faithfully.

name: {zh_data.get('name', '')}
description: {zh_data.get('description', '')}
accomplishment: {zh_data.get('accomplishment', '')}
country: {zh_data.get('country', '')}
related_industry: {zh_data.get('related_industry', '')}

Return the English translations as: name_en, description_en, accomplishment_en, country_en, related_industry_en.
"""


def call_ollama(prompt: str, schema: dict) -> str:
    """Send a prompt to the local Ollama model, constrained to the given JSON schema."""
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,      # disable Qwen3 "thinking" output
            "format": schema,    # structured-output mode: constrains decoding to this
                                  # exact JSON schema, not just a generic "valid JSON" hint
            "options": {"temperature": 0.3},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def parse_json_output(raw: str, raw_debug_file: Path) -> dict:
    """Parse a model response into a dict. Saves the raw response to disk first
    so nothing is lost if parsing fails."""
    raw_debug_file.write_text(raw, encoding="utf-8")
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```json|```", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        print(f"[WARNING] Could not find JSON in model output. See {raw_debug_file}")
        sys.exit(1)
    json_str = cleaned[start : end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parsing failed: {e}. See {raw_debug_file}")
        sys.exit(1)


def save_results(zh_data: dict, en_data: dict):
    """Merge the Chinese and English passes and save output.json / output.txt."""
    data = {
        "name_zh": zh_data.get("name", ""),
        "name_en": en_data.get("name_en", ""),
        "category": zh_data.get("category", ""),
        "description_zh": zh_data.get("description", ""),
        "description_en": en_data.get("description_en", ""),
        "accomplishment_zh": zh_data.get("accomplishment", ""),
        "accomplishment_en": en_data.get("accomplishment_en", ""),
        "country_zh": zh_data.get("country", ""),
        "country_en": en_data.get("country_en", ""),
        "related_industry_zh": zh_data.get("related_industry", ""),
        "related_industry_en": en_data.get("related_industry_en", ""),
        "related_year": zh_data.get("related_year", ""),
        "additional_source": zh_data.get("additional_source", ""),
        "acquisition_date": zh_data.get("acquisition_date", ""),
    }

    OUTPUT_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    lines.append(f"Company: {COMPANY_NAME}")
    lines.append(f"Category: {data.get('category', '')}")
    lines.append(f"Related Year: {data.get('related_year', '')}")
    lines.append(f"Acquisition Date: {data.get('acquisition_date', '')}\n")

    lines.append("=== Name ===")
    lines.append(f"[ZH] {data.get('name_zh', '')}")
    lines.append(f"[EN] {data.get('name_en', '')}\n")

    lines.append("=== Description ===")
    lines.append(f"[ZH] {data.get('description_zh', '')}")
    lines.append(f"[EN] {data.get('description_en', '')}\n")

    lines.append("=== Accomplishment ===")
    lines.append(f"[ZH] {data.get('accomplishment_zh', '')}")
    lines.append(f"[EN] {data.get('accomplishment_en', '')}\n")

    lines.append("=== Country ===")
    lines.append(f"[ZH] {data.get('country_zh', '')}")
    lines.append(f"[EN] {data.get('country_en', '')}\n")

    lines.append("=== Related Industry ===")
    lines.append(f"[ZH] {data.get('related_industry_zh', '')}")
    lines.append(f"[EN] {data.get('related_industry_en', '')}\n")

    lines.append("=== Additional Source ===")
    lines.append(data.get("additional_source", ""))

    OUTPUT_TXT.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n[DONE] Output written to: {OUTPUT_JSON}")
    print(f"[DONE] Output written to: {OUTPUT_TXT}")


def main():
    links = load_links(LINKS_FILE)
    context = build_context(links)

    print(f"\n[STEP 1/2] Extracting content in Simplified Chinese using {OLLAMA_MODEL} ...")
    zh_prompt = build_zh_prompt(COMPANY_NAME, CEO_NAME, context)
    zh_raw = call_ollama(zh_prompt, ZH_SCHEMA)
    zh_data = parse_json_output(zh_raw, RAW_ZH_FILE)

    print(f"[STEP 2/2] Translating to English using {OLLAMA_MODEL} ...")
    en_prompt = build_en_prompt(zh_data)
    en_raw = call_ollama(en_prompt, EN_SCHEMA)
    en_data = parse_json_output(en_raw, RAW_EN_FILE)

    save_results(zh_data, en_data)


if __name__ == "__main__":
    main()