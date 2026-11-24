import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.storage.shared_files import checked_file

SINGLE_FILE_REFERENCES = (
    "Physics",
    "Pose",
    "DisplayInfo",
    "UserData",
    "MotionSync",
    "Controller",
)


def verify_image(path: Path, formats: tuple[str, ...]) -> None:
    from PIL import Image

    try:
        with Image.open(path, formats=formats) as image:
            image.verify()
        with Image.open(path, formats=formats) as image:
            image.load()
    except Image.DecompressionBombError as exc:
        raise ValueError("图片尺寸过大") from exc


def runtime_files(root: Path, manifest: Path) -> tuple[Path, set[Path]]:
    manifest = checked_file(root, root, manifest.relative_to(root).as_posix())
    model = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(model, dict) or model.get("Version") != 3:
        raise ValueError("入口须为 Cubism 3 格式的 JSON")
    references = model["FileReferences"]
    if not isinstance(references, dict):
        raise ValueError("FileReferences 须为对象")
    unknown = references.keys() - {
        "Moc",
        "Textures",
        "Expressions",
        "Motions",
        *SINGLE_FILE_REFERENCES,
    }
    if unknown:
        raise ValueError(f"入口含未支持的文件引用类型 {', '.join(sorted(unknown))}")
    files = {manifest}

    def add(reference: str, suffix: str | None = None) -> Path:
        if (
            not isinstance(reference, str)
            or any(part in {"", ".", ".."} for part in reference.split("/"))
            or any(char in reference for char in "\\:\0")
            or (suffix is not None and not reference.lower().endswith(suffix))
        ):
            raise ValueError("Cubism 文件引用格式无效")
        path = checked_file(root, manifest.parent, reference)
        if not path.stat().st_size:
            raise ValueError(f"模型引用了空文件 {reference}")
        files.add(path)
        return path

    moc = add(references["Moc"], ".moc3")
    textures = references["Textures"]
    if not isinstance(textures, list) or not textures:
        raise ValueError("Cubism 入口没有贴图")
    for texture in textures:
        verify_image(add(texture, ".png"), ("PNG",))
    for key in SINGLE_FILE_REFERENCES:
        if key in references:
            add(references[key])
    for expression in references.get("Expressions", []):
        add(expression["File"])
    for motions in references.get("Motions", {}).values():
        for motion in motions:
            add(motion["File"])
            if "Sound" in motion:
                add(motion["Sound"])
    return moc, files


def write_archive(root: Path, files: set[Path], target: Path) -> None:
    # 以转换输出根目录为基准打包，保留入口与引用文件的相对位置。
    names = {path.relative_to(root).as_posix().casefold() for path in files}
    if len(names) != len(files) or target.name.casefold() in names:
        raise ValueError("模型包内存在冲突路径")
    if any("\\" in name or ":" in name for name in names):
        raise ValueError("模型包内存在不兼容的路径")
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(root).as_posix())
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError("模型 ZIP 校验失败")
