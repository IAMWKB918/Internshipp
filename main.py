#!/usr/bin/env python3
"""
main.py — 精简版 Pipeline (Florence -> MixJSON -> YOLO -> Classifier -> Organizer)
"""

import argparse
import shutil
import subprocess
import sys
import logging
import time
from pathlib import Path

# ────────────────────────────────────────────────────────────────
# 脚本位置定义
# ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

FLORENCE_PY   = SCRIPT_DIR / "florence.py"
MIXJSON_PY    = SCRIPT_DIR / "mixjson.py"
YOLO_PY       = SCRIPT_DIR / "yoloperson.py"   # 新加入的 YOLO 脚本
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
    def __init__(self, step_name: str, returncode: int):
        self.step_name = step_name
        self.returncode = returncode
        super().__init__(f"{step_name} 失败 (退出码 {returncode})")


def run_step(name: str, cmd: list[str]) -> None:
    log.info(f"▶ {name}")
    log.info("  命令: " + " ".join(str(c) for c in cmd))
    t0 = time.time()

    # 子进程执行
    result = subprocess.run(cmd)

    elapsed = time.time() - t0
    if result.returncode != 0:
        log.error(f"✗ {name} 失败 (退出码 {result.returncode}, 耗时 {elapsed:.1f}s)")
        raise PipelineError(name, result.returncode)
    log.info(f"✓ {name} 完成 ({elapsed:.1f}s)")


def run_pipeline(input_dir: Path, output_dir: Path, config_path: Path) -> None:
    # 定义中间文件路径
    florence_dir     = output_dir / "florence"
    aggregated_json  = output_dir / "aggregated_for_llm.json"
    sorted_dir       = output_dir / "sorted"
    manifest_json    = sorted_dir / "classify_manifest.json"
    organized_dir    = output_dir / "organized_photos"

    output_dir.mkdir(parents=True, exist_ok=True)

    # 每次跑之前清理 Florence 缓存目录
    if florence_dir.exists():
        shutil.rmtree(florence_dir)

    # ---- 1. Florence: 图片描述 + 物件识别 ----
    run_step(
        "Florence 图片分析",
        [sys.executable, str(FLORENCE_PY), str(input_dir), str(florence_dir)],
    )

    # ---- 2. mixjson: 合并 Florence 结果生成基础汇总表 ----
    run_step(
        "合并基础 JSON (mixjson)",
        [
            sys.executable, str(MIXJSON_PY),
            "--florence-dir", str(florence_dir),
            "--output-dir", str(output_dir),
        ],
    )

    # ---- 3. YOLO: 补充人数和面积数据 (关键步骤) ----
    # 它会读取 aggregated_for_llm.json 并修改其中的人数为空的字段
    run_step(
        "YOLO 精确人数检测",
        [
            sys.executable, str(YOLO_PY),
            "--images-dir", str(input_dir),
            "--aggregated", str(aggregated_json),
            "--conf", "0.25",  # 较低的阈值防止漏人
        ],
    )

    # ---- 4. classifier: 根据含有 YOLO 结果的 JSON 进行分类 ----
    run_step(
        "智能分类归类 (classifier)",
        [
            sys.executable, str(CLASSIFIER_PY),
            "--aggregated", str(aggregated_json),
            "--config", str(config_path),
            "--images-dir", str(input_dir),
            "--output-dir", str(sorted_dir),
        ],
    )

    # ---- 5. organizer: 最终物理改名和归档 ----
    run_step(
        "物理改名归档 (organizer)",
        [
            sys.executable, str(ORGANIZER_PY),
            "--manifest", str(manifest_json),
            "--images-dir", str(input_dir),
            "--output-root", str(organized_dir),
        ],
    )

    log.info("🎉 流程全部完成！")
    log.info(f"   最终照片库: {organized_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="精简版一键 Pipeline: Florence -> MixJSON -> YOLO -> Classifier -> Organizer"
    )
    parser.add_argument("input", help="要处理的图片文件夹路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出根目录")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="分类规则路径")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    config_path = Path(args.config).resolve()

    if not input_dir.is_dir():
        log.error(f"输入路径不是文件夹: {input_dir}")
        sys.exit(1)
    if not config_path.exists():
        log.error(f"找不到 config.json: {config_path}")
        sys.exit(1)

    t0 = time.time()
    try:
        run_pipeline(input_dir, output_dir, config_path)
    except PipelineError as e:
        log.error(f"流程异常中止: {e}")
        sys.exit(1)
    log.info(f"总运行耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()