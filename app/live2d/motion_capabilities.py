import json
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.live2d.motion_models import (
    Capabilities,
    Capability,
    ModelParameters,
    ModelRef,
    MotionError,
)
from app.storage.shared_files import checked_file, resolve_source_file

ACTION_PARAMETERS = {
    "blink": ("ParamEyeLOpen", "ParamEyeROpen"),
    "smile": ("ParamMouthForm",),
    "nod": ("ParamAngleY",),
    "speech": ("ParamMouthOpenY",),
    "raise-left-arm": ("ParamArmLA",),
    "raise-right-arm": ("ParamArmRA",),
    "left-hand-shape": (),
    "right-hand-shape": (),
}


def read_model_entry(model: ModelRef, settings: Settings) -> tuple[Path, Path, dict]:
    root = settings.live2d_output_root.resolve()
    manifest = resolve_source_file(
        root, model.model_manifest_ref, reference_field="modelManifestRef"
    )
    try:
        if (
            not manifest.name.endswith(".model3.json")
            or manifest.stat().st_size > 1_048_576
        ):
            raise ValueError("模型入口文件无效")
        content = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if content["Version"] != 3:
            raise ValueError("不支持的模型清单版本")
        ref = content["FileReferences"]["Moc"]
        moc = checked_file(manifest.parent, manifest.parent, ref)
        if not moc.name.endswith(".moc3") or moc.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("MOC3 文件无效")
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise MotionError("MODEL_INVALID", "模型入口或 MOC3 引用无效") from exc

    return manifest, moc, content


def read_runtime_parameters(moc: Path, settings: Settings) -> ModelParameters:
    core = settings.live2d_web_core_js
    if core is None or not core.is_file():
        raise MotionError("CUBISM_CORE_UNAVAILABLE", "动作编译需要配置 Cubism Web Core")
    try:
        result = subprocess.run(
            [
                settings.live2d_node_path,
                str(Path(__file__).with_name("web_core.cjs")),
                str(core.resolve()),
                str(moc),
                "--parameters",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        parameters = ModelParameters.model_validate_json(result.stdout.splitlines()[-1])
    except FileNotFoundError as exc:
        raise MotionError(
            "CUBISM_CORE_UNAVAILABLE", "无法启动 Cubism Web Core"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MotionError("MODEL_INSPECTION_TIMEOUT", "读取模型参数超时") from exc
    except (subprocess.CalledProcessError, IndexError, ValueError, OSError) as exc:
        raise MotionError("MODEL_INSPECTION_FAILED", "无法读取模型绑定参数") from exc

    return parameters


def read_capabilities(model: ModelRef, settings: Settings) -> Capabilities:
    _, moc, _ = read_model_entry(model, settings)
    parameters = read_runtime_parameters(moc, settings)
    if model.model_moc_sha256 and parameters.moc_sha256 != model.model_moc_sha256:
        raise MotionError("MODEL_VERSION_MISMATCH", "模型文件与指定版本的摘要不一致")
    all_active = {
        parameter.id for parameter in parameters.parameters if parameter.active
    }
    decreasing = {
        parameter.id
        for parameter in parameters.parameters
        if parameter.active_decreasing
    }
    increasing = {
        parameter.id
        for parameter in parameters.parameters
        if parameter.active_increasing
    }
    action_directions = {
        "blink": decreasing,
        "nod": decreasing,
        "smile": increasing,
        "speech": increasing,
    }
    capabilities = []
    for action, ids in ACTION_PARAMETERS.items():
        available = action_directions.get(action, all_active)
        missing = [item for item in ids if item not in available]
        capabilities.append(
            Capability(
                id=action,
                supported=bool(ids) and not missing,
                missing_parameter_ids=missing,
                parameter_ids=list(ids),
            )
        )
    return Capabilities(
        model_version_id=model.model_version_id,
        moc_version=parameters.moc_version,
        moc_sha256=parameters.moc_sha256,
        parameters=parameters.parameters,
        capabilities=capabilities,
    )
