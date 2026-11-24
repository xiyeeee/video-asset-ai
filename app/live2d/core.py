"""在独立进程中调用 Cubism Native Core。"""

import argparse
import ctypes as ct
from pathlib import Path


def validate_moc(library: Path, path: Path) -> None:
    core = ct.CDLL(str(library.resolve(strict=True)))
    core.csmHasMocConsistency.argtypes = (ct.c_void_p, ct.c_uint)
    core.csmHasMocConsistency.restype = ct.c_int
    core.csmReviveMocInPlace.argtypes = (ct.c_void_p, ct.c_uint)
    core.csmReviveMocInPlace.restype = ct.c_void_p
    core.csmGetSizeofModel.argtypes = (ct.c_void_p,)
    core.csmGetSizeofModel.restype = ct.c_uint
    core.csmInitializeModelInPlace.argtypes = (ct.c_void_p, ct.c_void_p, ct.c_uint)
    core.csmInitializeModelInPlace.restype = ct.c_void_p
    core.csmGetDrawableCount.argtypes = (ct.c_void_p,)
    core.csmGetDrawableCount.restype = ct.c_int

    data = path.read_bytes()
    if not data or len(data) > 0xFFFFFFFF:
        raise ValueError("MOC3 文件大小无效")
    # Core 要求 MOC 和模型缓冲区分别按 64 和 16 字节对齐。
    moc_buffer = ct.create_string_buffer(len(data) + 63)
    moc_address = (ct.addressof(moc_buffer) + 63) & ~63
    ct.memmove(moc_address, data, len(data))
    if core.csmHasMocConsistency(moc_address, len(data)) != 1:
        raise ValueError("Cubism Core 一致性检查失败")
    moc = core.csmReviveMocInPlace(moc_address, len(data))
    if not moc:
        raise ValueError("Cubism Core 无法加载 MOC3")
    size = core.csmGetSizeofModel(moc)
    if not size:
        raise ValueError("Cubism 模型大小无效")
    model_buffer = ct.create_string_buffer(size + 15)
    model_address = (ct.addressof(model_buffer) + 15) & ~15
    model = core.csmInitializeModelInPlace(moc, model_address, size)
    if not model or core.csmGetDrawableCount(model) <= 0:
        raise ValueError("Cubism Core 无法初始化含可绘制部件的模型")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("moc", type=Path)
    args = parser.parse_args()
    validate_moc(args.library, args.moc)


if __name__ == "__main__":
    main()
