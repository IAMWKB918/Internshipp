#!/usr/bin/env python3
"""
main.py — 一键跑完整个照片整理流程

用法:
    python main.py "输入图片文件夹路径" [--output 输出根目录] [--config config.json]

流程:
    florence.py  ┐
    exif.py       ├─► mixjson.py (合并) ─► classifier.py (按 config.json 分类)
    paddleocr_extractor.py ┘                              │
                                                            ▼
                                          organizer.py (最终改名归档)
"""

import argparse
import shutil
import subprocess
import sys
import logging
import time
from pathlib import Path

# ────────────────────────────────────────────────────────────────
# 脚本位置（假设都和 main.py 放在同一个文件夹）
# ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

FLORENCE_PY   = SCRIPT_DIR / "florence.py"
EXIF_PY       = SCRIPT_DIR / "exif.py"
PADDLEOCR_PY  = SCRIPT_DIR / "paddleocr_extractor.py"
MIXJSON_PY    = SCRIPT_DIR / "mixjson.py"
CLASSIFIER_PY = SCRIPT_DIR / "classifier.py"
ORGANIZER_PY  = SCRIPT_DIR / "organizer.py"

DEFAULT_OUTPUT = SCRIPT_DIR / "output"
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


class PipelineError(Exception):
    """某个子脚本执行失败时抛出。main.py 命令行模式会捕获它并 sys.exit(1)；
    被网页后端 (app.py) import 调用时，则可以捕获它返回错误信息而不会让整个服务器进程被杀掉。"""
    def __init__(self, step_name: str, returncode: int):
        self.step_name = step_name
        self.returncode = returncode
        super().__init__(f"{step_name} 失败 (退出码 {returncode})")


def run_step(name: str, cmd: list[str]) -> None:
    """跑一个子脚本；失败就抛出 PipelineError，交给调用方决定怎么处理。"""
    log.info(f"▶ {name}")
    log.info("  命令: " + " ".join(str(c) for c in cmd))
    t0 = time.time()

    # 不吃掉子进程的 stdout/stderr，直接原样打印出来（可以看到进度条/log）
    result = subprocess.run(cmd)

    elapsed = time.time() - t0
    if result.returncode != 0:
        log.error(f"✗ {name} 失败 (退出码 {result.returncode}, 耗时 {elapsed:.1f}s)")
        raise PipelineError(name, result.returncode)
    log.info(f"✓ {name} 完成 ({elapsed:.1f}s)")


def run_pipeline(input_dir: Path, output_dir: Path, config_path: Path) -> None:
    florence_dir     = output_dir / "florence"
    exif_json        = output_dir / "exif_results.json"
    paddle_json      = output_dir / "paddle_results.json"
    aggregated_json  = output_dir / "aggregated_for_llm.json"
    sorted_dir       = output_dir / "sorted"
    manifest_json    = sorted_dir / "classify_manifest.json"
    organized_dir    = output_dir / "organized_photos"

    output_dir.mkdir(parents=True, exist_ok=True)

    # florence 每张图输出一个独立 json、从不覆盖旧文件，如果不清空，
    # 上一批甚至几个月前测试留下的旧图片分析结果会一直混进这一批的合并结果里。
    # 这里每次跑之前先清空重建，保证 aggregated_for_llm.json 只包含"这一批"的图片。
    if florence_dir.exists():
        shutil.rmtree(florence_dir)

    # ---- 1a. Florence: 图片描述 + OCR + 物件识别 ----
    run_step(
        "Florence 图片分析",
        [sys.executable, str(FLORENCE_PY), str(input_dir), str(florence_dir)],
    )

    # ---- 1b. EXIF: 拍摄时间 ----
    run_step(
        "EXIF 时间抓取",
        [sys.executable, str(EXIF_PY), "--dir", str(input_dir), "--out", str(exif_json)],
    )

    # ---- 1c. PaddleOCR: 文字识别 ----
    run_step(
        "PaddleOCR 文字识别",
        [sys.executable, str(PADDLEOCR_PY), "--dir", str(input_dir), "--out", str(paddle_json)],
    )

    # ---- 2. mixjson: 合并三份结果 ----
    run_step(
        "合并 JSON (mixjson)",
        [
            sys.executable, str(MIXJSON_PY),
            "--florence-dir", str(florence_dir),
            "--exif", str(exif_json),
            "--paddle", str(paddle_json),
            "--output-dir", str(output_dir),
        ],
    )

    # ---- 3. classifier: 按 config.json 规则分类 + 复制一份 ----
    run_step(
        "分类归类 (classifier)",
        [
            sys.executable, str(CLASSIFIER_PY),
            "--aggregated", str(aggregated_json),
            "--config", str(config_path),
            "--images-dir", str(input_dir),
            "--output-dir", str(sorted_dir),
        ],
    )

    # ---- 4. organizer: 最终改名归档（一对多） ----
    run_step(
        "改名归档 (organizer)",
        [
            sys.executable, str(ORGANIZER_PY),
            "--manifest", str(manifest_json),
            "--images-dir", str(input_dir),
            "--output-root", str(organized_dir),
        ],
    )

    log.info("🎉 全部完成！")
    log.info(f"   最终结果 (改名归档): {organized_dir}")
    log.info(f"   分类副本 (中间产物): {sorted_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="一键跑完 florence → exif → paddleocr → mixjson → classifier → organizer"
    )
    parser.add_argument("input", help="要处理的图片文件夹路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                         help="输出根目录 (默认: ./output)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                         help="分类规则 config.json 路径 (默认: ./config.json)")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    config_path = Path(args.config).resolve()

    if not input_dir.is_dir():
        log.error(f"输入文件夹不存在或不是文件夹: {input_dir}")
        sys.exit(1)
    if not config_path.exists():
        log.error(f"找不到 config.json: {config_path}")
        sys.exit(1)

    t0 = time.time()
    try:
        run_pipeline(input_dir, output_dir, config_path)
    except PipelineError as e:
        log.error(f"流程中止: {e}")
        sys.exit(1)
    log.info(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()