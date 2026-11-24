import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryFile
from time import perf_counter

from app.core.config import Settings
from app.live2d.package import runtime_files, verify_image, write_archive

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    PREPARING = "PREPARING"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    EXPORTING = "EXPORTING"


class GenerationError(RuntimeError):
    def __init__(self, code: str, message: str, stage: Stage) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


def run_tool(
    argv: list[str], timeout: int, job_id: str, *, env: dict[str, str] | None = None
) -> None:
    # 转换器可能启动 Python、SSH 或 Java 子进程。超时后清理本机进程组。
    with TemporaryFile() as log:
        with subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        ) as process:
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise subprocess.TimeoutExpired(argv[:1], timeout) from None
            finally:
                log.seek(max(0, log.tell() - 16_384))
                output = log.read().decode(errors="replace").strip()
                for key, value in (env or os.environ).items():
                    if len(value) >= 4 and any(
                        part in key.upper()
                        for part in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")
                    ):
                        output = output.replace(value, "[redacted]")
                output = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", output)
                output = re.sub(r"(?i)bearer\s+[^\s,]+", "Bearer [redacted]", output)
                if output:
                    logger.info("taskId=%s 工具输出末尾\n%s", job_id, output)
        if status:
            raise subprocess.CalledProcessError(status, argv[:1])


def generate_model(
    source: Path,
    workspace: Path,
    destination: Path,
    settings: Settings,
    job_id: str,
    report: Callable[[Stage], None],
    *,
    model_name: str,
) -> str:
    stage = Stage.PREPARING
    stage_started = perf_counter()

    def set_stage(next_stage: Stage) -> None:
        nonlocal stage, stage_started
        now = perf_counter()
        if next_stage != Stage.PREPARING:
            logger.info(
                "taskId=%s stage=%s 完成，耗时 %.2f 秒",
                job_id,
                stage,
                now - stage_started,
            )
        stage, stage_started = next_stage, now
        report(stage)
        logger.info("taskId=%s stage=%s 开始", job_id, stage)

    set_stage(Stage.PREPARING)
    try:
        verify_image(source, ("PNG", "JPEG", "WEBP"))
    except ImportError as exc:
        raise GenerationError(
            "IMAGE_DECODER_UNAVAILABLE", "请安装 live2d 扩展", stage
        ) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise GenerationError(
            "SOURCE_IMAGE_INVALID", "源文件须为可解码的 PNG、JPEG 或 WebP", stage
        ) from exc
    runtime = workspace / "runtime"
    set_stage(Stage.GENERATING)
    if not settings.live2d_converter_argv:
        raise GenerationError(
            "CONVERTER_UNAVAILABLE", "尚未配置图片转 Live2D 转换器", stage
        )
    try:
        run_tool(
            [
                *settings.live2d_converter_argv,
                "--image",
                str(source),
                "--output",
                str(runtime),
            ],
            settings.live2d_timeout_seconds,
            job_id,
            env={**os.environ, "LIVE2D_MODEL_NAME": model_name},
        )
    except FileNotFoundError as exc:
        raise GenerationError(
            "CONVERTER_UNAVAILABLE", "转换器可执行文件不可用", stage
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GenerationError("CONVERTER_TIMEOUT", "图片转 Live2D 超时", stage) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GenerationError(
            "CONVERSION_FAILED", "图片转 Live2D 工具执行失败", stage
        ) from exc

    set_stage(Stage.VALIDATING)
    try:
        if runtime.resolve() != runtime:
            raise ValueError("转换输出目录被替换")
        manifests = list(runtime.glob(settings.live2d_manifest_glob))
        if not manifests:
            raise GenerationError(
                "RUNTIME_NOT_EXPORTED",
                "转换器未导出 .model3.json，分层 PSD 不能作为运行时模型",
                stage,
            )
        if len(manifests) != 1 or not manifests[0].name.endswith(".model3.json"):
            raise ValueError("必须匹配一个 .model3.json")
        manifest = manifests[0]
        moc, files = runtime_files(runtime, manifest)
    except GenerationError:
        raise
    except (
        OSError,
        ValueError,
        LookupError,
        TypeError,
        AttributeError,
        RuntimeError,
    ) as exc:
        raise GenerationError(
            "RUNTIME_INVALID", "Cubism 入口或文件引用无效", stage
        ) from exc
    library, web_core = settings.live2d_core_library, settings.live2d_web_core_js
    if library is not None and library.is_file():
        validator = [sys.executable, "-m", "app.live2d.core", str(library)]
    elif web_core is not None and web_core.is_file():
        validator = [
            settings.live2d_node_path,
            str(Path(__file__).with_name("web_core.cjs")),
            str(web_core.resolve()),
        ]
    else:
        raise GenerationError(
            "CUBISM_CORE_UNAVAILABLE", "尚未配置可用的 Cubism Native 或 Web Core", stage
        )
    try:
        run_tool([*validator, str(moc)], 60, job_id)
    except FileNotFoundError as exc:
        raise GenerationError(
            "CUBISM_CORE_UNAVAILABLE", "Cubism Core 校验程序不可用", stage
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GenerationError(
            "MOC3_VALIDATION_FAILED", "Cubism Core 校验或加载 MOC3 失败", stage
        ) from exc

    set_stage(Stage.EXPORTING)
    try:
        destination.mkdir()
        for path in files:
            copied = destination / path.relative_to(runtime)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, copied)
        write_archive(
            destination,
            {destination / path.relative_to(runtime) for path in files},
            destination / "model.zip",
        )
    except (OSError, ValueError) as exc:
        raise GenerationError(
            "ARTIFACT_WRITE_FAILED", "模型包保存或校验失败", stage
        ) from exc
    logger.info(
        "taskId=%s stage=%s 完成，耗时 %.2f 秒",
        job_id,
        stage,
        perf_counter() - stage_started,
    )
    return manifest.relative_to(runtime).as_posix()
