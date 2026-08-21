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
YOLO_PY       = SCRIPT_DIR / "yoloperson.py"
MIXJSON_PY    = SCRIPT_DIR / "mixjson.py"
CLASSIFIER_PY = SCRIPT_DIR / "classifier.py"
ORGANIZER_PY  = SCRIPT_DIR / "organizer.py"

DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "output"  # app.py 在多来源合并批次时用来当暂存基底

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


class PipelineStopped(Exception):
    """使用者按了 Stop，pipeline 被主动中止（不是子程式自己失败）。"""
    def __init__(self, step_name: str):
        self.step_name = step_name
        super().__init__(f"{step_name} 被使用者中止")


def _kill_process(proc: subprocess.Popen) -> None:
    """先礼貌 terminate，给个 3 秒宽限期，还不退出就 kill。"""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_step(name: str, cmd: list[str], stop_event=None) -> None:
    """跑一个步骤的子程式。

    以前这里是 subprocess.run(cmd)，一旦跑起来就是同步阻塞、
    整条 pipeline 线程会卡在这一行直到子程式自然结束 —— 不管前端
    按了几次 Stop 都没用，因为根本没有任何机制去中断它，这就是
    「UI 已经停了、但 terminal 还在跑」的原因。

    改成 Popen + 轮询：每 0.3 秒检查一次 stop_event，如果被设置了，
    就直接把子程式砍掉并抛出 PipelineStopped，让上层 (run_pipeline /
    app.py 背景执行绪) 可以把这个 batch 标成「已停止」而不是「失败」。
    """
    if stop_event is not None and stop_event.is_set():
        raise PipelineStopped(name)

    log.info(f"▶ {name}")
    log.info("  命令: " + " ".join(str(c) for c in cmd))
    t0 = time.time()

    proc = subprocess.Popen(cmd)
    try:
        while True:
            try:
                returncode = proc.wait(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    log.warning(f"⛔ {name} 收到 Stop，正在中止子程式…")
                    _kill_process(proc)
                    raise PipelineStopped(name)
    except PipelineStopped:
        raise
    except BaseException:
        # 例如 main.py 本身被 Ctrl+C / 外部 kill：也要把子程式一起收掉，
        # 不留下孤儿进程继续在背景跑。
        _kill_process(proc)
        raise

    elapsed = time.time() - t0
    if returncode != 0:
        log.error(f"✗ {name} 失败 (退出码 {returncode}, 耗时 {elapsed:.1f}s)")
        raise PipelineError(name, returncode)
    log.info(f"✓ {name} 完成 ({elapsed:.1f}s)")


def run_pipeline(input_dir: Path, output_dir: Path, config_path: Path, stop_event=None) -> None:
    florence_dir     = output_dir / "florence"
    yolo_json        = output_dir / "yolo.json"
    aggregated_json  = output_dir / "aggregated_for_llm.json"
    manifest_json    = output_dir / "classify_manifest.json"
    organized_dir    = output_dir / "organized_photos"

    output_dir.mkdir(parents=True, exist_ok=True)

    # 每次跑之前清理 Florence 缓存目录
    if florence_dir.exists():
        shutil.rmtree(florence_dir)

    # ---- 1. Florence: 图片描述 + 物件识别 ----
    # 第三个参数把 config_path 传进去，这样 florence.py 判断哪些副档名算
    # video/audio（video_formats）用的是跟 classifier / organizer 同一份
    # config.json，不会因为各自读到不同版本而出现「同一批文件两边判断不一致」。
    run_step(
        "Florence 图片分析",
        [sys.executable, str(FLORENCE_PY), str(input_dir), str(florence_dir), str(config_path)],
        stop_event=stop_event,
    )

    run_step(
        "YOLO 精确人数检测",
        [
            sys.executable, str(YOLO_PY),
            str(input_dir),
            "--output-dir", str(output_dir),
            "--conf", "0.25",  # 较低的阈值防止漏人
        ],
        stop_event=stop_event,
    )

    # ---- 3. mixjson: 合并 Florence + YOLO 结果生成汇总表 ----
    run_step(
        "合并汇总 JSON (mixjson)",
        [
            sys.executable, str(MIXJSON_PY),
            "--florence-dir", str(florence_dir),
            "--yolo-json", str(yolo_json),
            "--output-dir", str(output_dir),
        ],
        stop_event=stop_event,
    )

    # ---- 4. classifier: 根据汇总 JSON 进行分类 ----
    run_step(
        "智能分类归类 (classifier)",
        [
            sys.executable, str(CLASSIFIER_PY),
            "--aggregated", str(aggregated_json),
            "--config", str(config_path),
            "--output-manifest", str(manifest_json),
            "--images-dir", str(input_dir),  # <--- 必須加上這一行，讓 CLIP 找到圖

        ],
        stop_event=stop_event,
    )

    # ---- 5. organizer: 最终物理改名和归档 ----
    # --skip-classify: classifier 那一步已经分类并写好 manifest 了，这里只负责搬文件，
    # 不然 organizer.py 会自己用内建的、写死的旧路径再跑一次分类，导致找不到
    # aggregated_for_llm.json 而静默失败 (退出码却是 0，容易被忽略)。
    # 仍然要传 --config：即使跳过分类，organizer 也要读 config.json 里的
    # video_formats 来扩充查找副档名，不然 video/audio 文件会被判定「找不到」。
    run_step(
        "物理改名归档 (organizer)",
        [
            sys.executable, str(ORGANIZER_PY),
            "--manifest", str(manifest_json),
            "--images-dir", str(input_dir),
            "--output-root", str(organized_dir),
            "--config", str(config_path),
            "--skip-classify",
        ],
        stop_event=stop_event,
    )

    log.info("🎉 流程全部完成！")
    log.info(f"   输出总目录: {output_dir}")
    log.info(f"   最终照片库: {organized_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="精简版一键 Pipeline: Florence -> YOLO -> MixJSON -> Classifier -> Organizer"
    )
    parser.add_argument("input", help="要处理的图片文件夹路径")
    parser.add_argument(
        "--output",
        default=None,
        help="输出根目录，默认在 input 文件夹里新开一个 output 子文件夹",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="分类规则路径")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve() if args.output else (input_dir / "output")
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
    except PipelineStopped as e:
        log.warning(f"流程被中止: {e}")
        sys.exit(130)
    log.info(f"总运行耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()