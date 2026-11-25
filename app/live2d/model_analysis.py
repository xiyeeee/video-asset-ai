import hashlib
import json
import math
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.live2d.analysis_models import (
    AnalysisDiagnostic,
    AnalysisRequest,
    AnalysisResult,
    AssetKind,
    ModelAsset,
    ModelPart,
    ParameterEvidence,
)
from app.live2d.capability_catalog import classify_capabilities
from app.live2d.motion_capabilities import read_model_entry, read_runtime_parameters
from app.live2d.motion_models import ModelParameters, MotionError
from app.storage.shared_files import checked_file, resolve_source_file

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MOTION_ASSETS = 128


def objects(value: object) -> list[dict]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def identifiers(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def read_json(path: Path) -> tuple[dict, str]:
    with path.open("rb") as source:
        payload = source.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("元数据文件超出大小限制")
    data = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("元数据须为 JSON 对象")
    return data, hashlib.sha256(payload).hexdigest()


def read_asset(
    path: Path, root: Path, expected_kind: AssetKind | None = None
) -> ModelAsset:
    if path.name.endswith(".motion3.json"):
        kind: AssetKind = "MOTION"
    elif path.name.endswith(".exp3.json"):
        kind = "EXPRESSION"
    else:
        raise ValueError("文件须为 Cubism 动作或表情")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError("文件类型与模型引用不一致")
    data, fingerprint = read_json(path)
    if kind == "MOTION":
        meta = data.get("Meta")
        seconds = meta.get("Duration") if isinstance(meta, dict) else None
        if (
            data.get("Version") != 3
            or not isinstance(data.get("Curves"), list)
            or type(seconds) not in (float, int)
            or not math.isfinite(seconds)
            or not 0 < seconds <= 86_400
        ):
            raise ValueError("动作结构或时长无效")
        params = [
            item.get("Id")
            for item in objects(data["Curves"])
            if item.get("Target") == "Parameter"
        ]
        duration = round(seconds * 1000)
    else:
        if data.get("Type") != "Live2D Expression" or not isinstance(
            data.get("Parameters"), list
        ):
            raise ValueError("表情结构无效")
        params = [item.get("Id") for item in objects(data["Parameters"])]
        duration = None
    return ModelAsset(
        artifact_ref=path.relative_to(root).as_posix(),
        kind=kind,
        sha256=fingerprint,
        parameter_ids=sorted(
            {item for item in params if isinstance(item, str) and item}
        ),
        duration_ms=duration,
    )


def runtime_inventory(
    request: AnalysisRequest,
    moc: Path,
    moc_digest: str,
    root: Path,
    settings: Settings,
    diagnostics: list[AnalysisDiagnostic],
) -> tuple[ModelParameters | None, str]:
    try:
        runtime = read_runtime_parameters(moc, settings)
        if runtime.moc_sha256 != moc_digest:
            raise MotionError("MODEL_VERSION_MISMATCH", "读取期间 MOC3 内容发生变化")
        return runtime, "RUNTIME_PROBE"
    except MotionError as exc:
        if exc.code == "MODEL_VERSION_MISMATCH":
            raise
        diagnostics.append(AnalysisDiagnostic(code=exc.code, message=str(exc)))

    if request.runtime_parameter_ref:
        try:
            path = resolve_source_file(
                root,
                request.runtime_parameter_ref,
                reference_field="runtimeParameterRef",
            )
            data, _ = read_json(path)
            runtime = ModelParameters.model_validate(
                {
                    key: data.get(key)
                    for key in ("mocVersion", "mocSha256", "parameters")
                }
            )
            if len({parameter.id for parameter in runtime.parameters}) != len(
                runtime.parameters
            ) or any(
                not parameter.min <= parameter.default_value <= parameter.max
                for parameter in runtime.parameters
            ):
                raise ValueError("Runtime 参数清单不一致")
            if runtime.moc_sha256 != moc_digest:
                raise ValueError("Runtime 清单属于另一个模型")
            return runtime, "RUNTIME_EXPORT"
        except (ValueError, OSError, RuntimeError):
            diagnostics.append(
                AnalysisDiagnostic(
                    code="RUNTIME_EXPORT_INVALID",
                    message="无法读取与当前 MOC3 摘要一致的 Runtime 清单",
                    reference=request.runtime_parameter_ref,
                )
            )
    diagnostics.append(
        AnalysisDiagnostic(
            code="PARAMETER_INVENTORY_INCOMPLETE",
            message="仅获得包内的部分参数线索，未检出的能力仍需核对",
        )
    )
    return None, ""


def analyze_model(request: AnalysisRequest, settings: Settings) -> AnalysisResult:
    started = perf_counter()
    manifest, moc, content = read_model_entry(request, settings)
    root = settings.live2d_output_root.resolve()
    with moc.open("rb") as source:
        moc_digest = hashlib.file_digest(source, "sha256").hexdigest()
    if request.model_moc_sha256 and request.model_moc_sha256 != moc_digest:
        raise MotionError("MODEL_VERSION_MISMATCH", "模型文件与指定版本的摘要不一致")
    diagnostics: list[AnalysisDiagnostic] = []
    runtime, runtime_source = runtime_inventory(
        request, moc, moc_digest, root, settings, diagnostics
    )
    digest_files = {
        manifest.relative_to(root).as_posix(): hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest(),
        moc.relative_to(root).as_posix(): moc_digest,
    }
    parameters: dict[str, ParameterEvidence] = {}
    assets: dict[str, ModelAsset] = {}
    parts: dict[str, ModelPart] = {}

    def record(param_id: str, evidence: str, name: str | None = None) -> None:
        item = parameters.setdefault(param_id, ParameterEvidence(id=param_id))
        if evidence not in item.evidence:
            item.evidence.append(evidence)
        if name:
            item.name = name

    for hit_area in objects(content.get("HitAreas")):
        part_id, name = hit_area.get("Id"), hit_area.get("Name")
        if isinstance(part_id, str) and isinstance(name, str):
            parts[part_id] = ModelPart(
                id=part_id, name=name, evidence="MODEL3_HIT_AREA"
            )

    for item in objects(content.get("Groups")):
        if item.get("Target") == "Parameter":
            for param_id in identifiers(item.get("Ids")):
                record(param_id, "MODEL3_GROUP")

    def record_metadata(item: dict, evidence: str) -> None:
        param_id = item.get("Id")
        if not isinstance(param_id, str) or not param_id:
            return
        name = item.get("Name")
        record(param_id, evidence, name if isinstance(name, str) else None)
        bounds = tuple(
            item.get(key) for key in ("MinimumValue", "DefaultValue", "MaximumValue")
        )
        if all(type(value) in (int, float) and abs(value) <= 1e12 for value in bounds):
            minimum, neutral, maximum = bounds
            if minimum <= neutral <= maximum:
                parameter = parameters[param_id]
                parameter.min = float(minimum)
                parameter.default_value = float(neutral)
                parameter.max = float(maximum)

    for item in objects(content.get("Parameters")):
        record_metadata(item, "MODEL3_METADATA")

    references = content["FileReferences"]
    display = references.get("DisplayInfo")
    if isinstance(display, str):
        try:
            path = checked_file(manifest.parent, manifest.parent, display)
            info, digest = read_json(path)
            digest_files[path.relative_to(root).as_posix()] = digest
            for item in objects(info.get("Parameters")):
                record_metadata(item, "DISPLAY_INFO")
            for item in objects(info.get("Parts")):
                part_id, name = item.get("Id"), item.get("Name")
                if isinstance(part_id, str) and isinstance(name, str):
                    parts[part_id] = ModelPart(
                        id=part_id, name=name, evidence="DISPLAY_INFO"
                    )
        except (OSError, ValueError, RuntimeError):
            diagnostics.append(
                AnalysisDiagnostic(
                    code="DISPLAY_INFO_UNAVAILABLE",
                    message="无法读取模型随包参数元数据",
                    reference=display,
                )
            )

    motions = references.get("Motions")
    declared = [
        (item.get("File"), "EXPRESSION")
        for item in objects(references.get("Expressions"))
    ]
    if isinstance(motions, dict):
        declared.extend(
            (item.get("File"), "MOTION")
            for group in motions.values()
            for item in objects(group)
        )
    if len(declared) > MAX_MOTION_ASSETS:
        diagnostics.append(
            AnalysisDiagnostic(
                code="ASSET_SCAN_LIMIT", message="部分动作或表情超过扫描数量限制"
            )
        )
    for ref, kind in declared[:MAX_MOTION_ASSETS]:
        if not isinstance(ref, str):
            continue
        try:
            path = checked_file(manifest.parent, manifest.parent, ref)
            asset = read_asset(path, root, kind)
            assets[asset.artifact_ref] = asset
            digest_files[asset.artifact_ref] = asset.sha256
            for param_id in asset.parameter_ids:
                record(param_id, "ASSET_CURVE")
        except (OSError, ValueError, RuntimeError, OverflowError):
            diagnostics.append(
                AnalysisDiagnostic(
                    code="ACTION_ASSET_UNAVAILABLE",
                    message="无法核验随包动作或表情文件",
                    reference=ref,
                )
            )

    if runtime:
        for runtime_param in runtime.parameters:
            record(runtime_param.id, runtime_source)
            parameter = parameters[runtime_param.id]
            parameter.min = runtime_param.min
            parameter.max = runtime_param.max
            parameter.default_value = runtime_param.default_value
            parameter.active = runtime_param.active
            parameter.active_increasing = runtime_param.active_increasing
            parameter.active_decreasing = runtime_param.active_decreasing

    configured = {binding.semantic_id: binding for binding in request.bindings}
    for binding in request.bindings:
        if (
            binding.asset_ref is None
            or binding.asset_ref in assets
            or not binding.asset_sha256
        ):
            continue
        try:
            path = resolve_source_file(
                root, binding.asset_ref, reference_field="assetRef"
            )
            asset = read_asset(path, root)
            if asset.sha256 != binding.asset_sha256:
                raise ValueError("动作文件摘要不一致")
            assets[binding.asset_ref] = asset
            digest_files[asset.artifact_ref] = asset.sha256
        except (ValueError, OSError, RuntimeError, OverflowError):
            diagnostics.append(
                AnalysisDiagnostic(
                    code="BOUND_ASSET_UNAVAILABLE",
                    message="确认映射指向的动作文件无法核验",
                    semantic_id=binding.semantic_id,
                    reference=binding.asset_ref,
                )
            )

    groups = classify_capabilities(
        parameters, assets, parts, configured, runtime is not None, diagnostics
    )
    fingerprint = hashlib.sha256(
        json.dumps(digest_files, sort_keys=True).encode()
    ).hexdigest()
    return AnalysisResult(
        model_version_id=request.model_version_id,
        binding_version_id=request.binding_version_id,
        asset_fingerprint=fingerprint,
        model_moc_sha256=moc_digest,
        parameter_inventory_complete=runtime is not None,
        parameters=list(parameters.values()),
        assets=list(assets.values()),
        parts=list(parts.values()),
        capabilities=groups,
        diagnostics=diagnostics,
        elapsed_ms=round((perf_counter() - started) * 1000),
    )
