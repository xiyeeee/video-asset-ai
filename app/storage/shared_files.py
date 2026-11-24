from os.path import abspath
from pathlib import Path
from tempfile import TemporaryDirectory


class InvalidSourceReference(ValueError):
    """共享输入文件引用无法安全解析。"""


class SourceVideoNotFound(FileNotFoundError):
    """源视频引用未指向一个文件。"""


def resolve_source_file(
    root: Path, reference: str, *, reference_field: str = "源文件引用"
) -> Path:
    parts = reference.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(
        character in reference for character in ("\\", ":", "\0")
    ):
        raise InvalidSourceReference(
            f"{reference_field} 须为共享输入目录内的相对文件引用"
        )
    try:
        source = (root / reference).resolve()
    except (OSError, RuntimeError) as exc:
        raise InvalidSourceReference(f"{reference_field} 无法解析") from exc
    if not source.is_relative_to(root):
        raise InvalidSourceReference(
            f"{reference_field} 须为共享输入目录内的相对文件引用"
        )
    if not source.is_file():
        raise FileNotFoundError("共享输入目录中找不到源文件")
    return source


def resolve_source_video(root: Path, reference: str) -> Path:
    try:
        return resolve_source_file(root, reference, reference_field="sourceVideoRef")
    except FileNotFoundError as exc:
        raise SourceVideoNotFound("共享输入目录中找不到源视频") from exc


def checked_file(root: Path, base: Path, reference: str) -> Path:
    path = Path(reference)
    resolved = (base / path).resolve()
    if (
        path.is_absolute()
        or not resolved.is_relative_to(root)
        or resolved != Path(abspath(base / path))
        or not resolved.is_file()
    ):
        raise ValueError(f"结果清单引用了无效文件 {reference}")
    return resolved


def stage_task_directory(
    root: Path, *, prefix: str = ".character-task-"
) -> TemporaryDirectory[str]:
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() != root:
        raise ValueError("共享输出根目录在运行期间发生变化")
    return TemporaryDirectory(prefix=prefix, dir=root)


def publish_task_directory(root: Path, staged: Path, destination: Path) -> None:
    if (
        root.resolve() != root
        or staged.resolve() != staged
        or destination.resolve() != destination
    ):
        raise ValueError("任务输出路径在运行期间发生变化")
    if staged.parent.parent != root or destination.parent != root:
        raise ValueError("任务输出目录超出共享输出根目录")
    if destination.exists():
        raise FileExistsError(f"任务输出目录已存在 {destination}")
    # 临时目录与最终目录位于同一共享根目录，整批结果通过重命名一次发布。
    staged.rename(destination)
