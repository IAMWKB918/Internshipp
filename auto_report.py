import json
import re
import sys
from pathlib import Path
import requests
import trafilatura
import io

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ============ CONFIG (edit as needed) ============
COMPANY_NAME = "S.K. Tiong Enterprise Sdn. Bhd."          # Focus the model on this company
OLLAMA_MODEL = "qwen3:8b"             # Your model name
OLLAMA_URL = "http://localhost:11434/api/generate"
LINKS_FILE = "links.txt"
OUTPUT_JSON = "output.json"
OUTPUT_TXT = "output.txt"
RAW_ZH_FILE = "raw_model_output_zh.txt"   # step 1 raw output, for debugging
RAW_EN_FILE = "raw_model_output_en.txt"   # step 2 raw output, for debugging
MAX_CONTEXT_CHARS = 12000              # Truncate context if it gets too long
# ===================================================


# JSON Schemas passed directly to Ollama's structured-output "format" field.
# This constrains the model's decoding to these exact field names/types,
# instead of just hoping the prompt instructions are followed.
ZH_SCHEMA = {
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

EN_SCHEMA = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "description_en": {"type": "string"},
        "accomplishment_en": {"type": "string"},
        "related_industry_en": {"type": "string"},
    },
    "required": ["title_en", "description_en", "accomplishment_en", "related_industry_en"],
}


def load_links(path: str) -> list[str]:
    """Load and clean the list of URLs from links.txt."""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] {path} not found. Create it first, one URL per line.")
        sys.exit(1)
    links = [line.strip() for line in p.read_text(encoding="utf-8").splitlines()]
    links = [l for l in links if l and not l.startswith("#")]
    if not links:
        print(f"[ERROR] {path} has no valid links.")
        sys.exit(1)
    return links


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


def build_zh_prompt(company: str, context: str) -> str:
    """Step 1: extract structured content in Traditional Chinese only.
    Kept single-language and simple so a small local model can follow the
    schema reliably."""
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


def build_en_prompt(zh_data: dict) -> str:
    """Step 2: translate the Chinese fields into English.
    A separate, focused call so the model isn't juggling extraction,
    schema-following, and translation all at once."""
    return f"""Translate the following Traditional Chinese fields into natural, fluent English.
Do not summarize further or add new information — translate faithfully.

title: {zh_data.get('title', '')}
description: {zh_data.get('description', '')}
accomplishment: {zh_data.get('accomplishment', '')}
related_industry: {zh_data.get('related_industry', '')}

Return the English translations as: title_en, description_en, accomplishment_en, related_industry_en.
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


def parse_json_output(raw: str, raw_debug_file: str) -> dict:
    """Parse a model response into a dict. Saves the raw response to disk first
    so nothing is lost if parsing fails."""
    Path(raw_debug_file).write_text(raw, encoding="utf-8")
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

    Path(OUTPUT_JSON).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    lines.append(f"Company: {COMPANY_NAME}")
    lines.append(f"Country: {data.get('country', '')}\n")

    lines.append("=== Title ===")
    lines.append(f"[ZH] {data.get('title_zh', '')}")
    lines.append(f"[EN] {data.get('title_en', '')}\n")

    lines.append("=== Description ===")
    lines.append(f"[ZH] {data.get('description_zh', '')}")
    lines.append(f"[EN] {data.get('description_en', '')}\n")

    lines.append("=== Accomplishment ===")
    lines.append(f"[ZH] {data.get('accomplishment_zh', '')}")
    lines.append(f"[EN] {data.get('accomplishment_en', '')}\n")

    lines.append("=== Related Industry ===")
    lines.append(f"[ZH] {data.get('related_industry_zh', '')}")
    lines.append(f"[EN] {data.get('related_industry_en', '')}\n")

    lines.append("=== Sources Used ===")
    for s in data.get("sources_used", []):
        lines.append(f"- {s}")

    Path(OUTPUT_TXT).write_text("\n".join(lines), encoding="utf-8")

    print(f"\n[DONE] Output written to: {OUTPUT_JSON}, {OUTPUT_TXT}")


def main():
    links = load_links(LINKS_FILE)
    context = build_context(links)

    print(f"\n[STEP 1/2] Extracting content in Traditional Chinese using {OLLAMA_MODEL} ...")
    zh_prompt = build_zh_prompt(COMPANY_NAME, context)
    zh_raw = call_ollama(zh_prompt, ZH_SCHEMA)
    zh_data = parse_json_output(zh_raw, RAW_ZH_FILE)

    print(f"[STEP 2/2] Translating to English using {OLLAMA_MODEL} ...")
    en_prompt = build_en_prompt(zh_data)
    en_raw = call_ollama(en_prompt, EN_SCHEMA)
    en_data = parse_json_output(en_raw, RAW_EN_FILE)

    save_results(zh_data, en_data)


if __name__ == "__main__":
    main()