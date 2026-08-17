import sys
import requests

if sys.platform == "win32":
    # stdout 和 stderr 都要 reconfigure，否则打印中文（比如下面的中文标签、
    # query）或者未捕获异常的 traceback 时，Windows 默认 cp936/cp1252
    # 编码会报 UnicodeEncodeError('charmap', ...)。
    # errors="replace" 是兜底：真遇到编码不了的字符时显示 ? 而不是崩溃。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_KEY = "a94310be37b794f733c1bac104cb4a9c58a19bc4"  # 换成你的真实 key

tests = [
    # (标签, query, gl, hl)
    ("纯中文引号+年份 (原始 pattern)", '"诗巫盆栽园艺协会" 2025', "my", "zh-cn"),
    ("去掉引号", "诗巫盆栽园艺协会 2025", "my", "zh-cn"),
    ("引号短语但加一个普通词", '"诗巫盆栽园艺协会" news 2025', "my", "zh-cn"),
    ("hl 改成 en", '"诗巫盆栽园艺协会" 2025', "my", "en"),
    ("gl 改成 sg", '"诗巫盆栽园艺协会" 2025', "sg", "zh-cn"),
    ("纯英文引号短语 (排除中文因素)", '"Anthropic" 2025', "my", "en"),
    ("纯英文引号短语 + hl=zh-cn", '"Anthropic" 2025', "my", "zh-cn"),
]

url = "https://google.serper.dev/search"
headers = {"X-API-KEY": API_KEY, "Content-Type": "application/json"}

for label, q, gl, hl in tests:
    payload = {"q": q, "gl": gl, "hl": hl, "num": 20}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        status = r.status_code
        detail = r.text[:150] if status != 200 else f"OK, {len(r.json().get('organic', []))} results"
    except Exception as e:
        status, detail = "ERR", str(e)
    print(f"[{status}] {label:40s} q={q!r} gl={gl} hl={hl}")
    print(f"        -> {detail}")