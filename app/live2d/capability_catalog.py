import re
from pathlib import Path

from app.live2d.analysis_models import (
    AnalysisDiagnostic,
    Binding,
    CapabilityGroup,
    ModelAsset,
    ModelPart,
    ParameterEvidence,
    Semantic,
    Status,
)

# 名称规则只产生候选。细节能力不能从通用嘴形、红晕或手臂参数推断出来。
CATALOG = {
    "gaze": (
        "视线",
        {
            "gaze-horizontal": ("水平视线", ("ParamEyeBallX",), ""),
            "gaze-vertical": ("垂直视线", ("ParamEyeBallY",), ""),
        },
    ),
    "eyelids": (
        "眼皮",
        {
            "eyelid-left": ("左眼开合", ("ParamEyeLOpen",), ""),
            "eyelid-right": ("右眼开合", ("ParamEyeROpen",), ""),
            "eyelid-detail": ("眼睑细节", (), "通用眼睛开合不能证明眼睑有独立形变"),
        },
    ),
    "brows": (
        "眉毛",
        {
            "brow-left": (
                "左眉",
                ("ParamBrowLY", "ParamBrowLForm", "ParamBrowLAngle"),
                "",
            ),
            "brow-right": (
                "右眉",
                ("ParamBrowRY", "ParamBrowRForm", "ParamBrowRAngle"),
                "",
            ),
        },
    ),
    "mouth": (
        "嘴",
        {
            "mouth-open": ("嘴部开合", ("ParamMouthOpenY",), ""),
            "mouth-form": ("通用嘴形", ("ParamMouthForm",), ""),
            "mouth-corner-left": (
                "左嘴角",
                ("ParamMouthCornerL",),
                "未找到左嘴角专用绑定",
            ),
            "mouth-corner-right": (
                "右嘴角",
                ("ParamMouthCornerR",),
                "未找到右嘴角专用绑定",
            ),
            "mouth-purse": (
                "轻抿嘴",
                ("ParamMouthPurse",),
                "通用嘴形或开合不能证明轻抿嘴可播",
            ),
        },
    ),
    "cheeks": (
        "脸颊",
        {
            "blush": ("红晕", ("ParamCheek", "ParamBlush"), ""),
            "cheek-detail": (
                "脸颊细节",
                ("ParamCheekPuff",),
                "未找到鼓腮等脸颊专用绑定",
            ),
        },
    ),
    "head": (
        "头",
        {
            "head-yaw": ("转头", ("ParamAngleX",), ""),
            "head-pitch": ("低头或抬头", ("ParamAngleY",), ""),
            "head-roll": ("偏头", ("ParamAngleZ",), ""),
        },
    ),
    "shoulders-neck": (
        "肩颈",
        {
            "shoulders": ("肩部动作", ("ParamShoulderL", "ParamShoulderR"), ""),
            "neck": ("颈部动作", ("ParamNeck", "ParamNeckAngle"), ""),
        },
    ),
    "breathing-torso": (
        "呼吸躯干",
        {
            "breathing": ("呼吸", ("ParamBreath",), ""),
            "torso": (
                "躯干角度",
                ("ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ"),
                "",
            ),
        },
    ),
    "hands-props": (
        "手部道具",
        {
            "arms": (
                "手臂",
                ("ParamArmLA", "ParamArmRA", "ParamArmLB", "ParamArmRB"),
                "",
            ),
            "hand-shape": (
                "手型或手指",
                ("ParamHandL", "ParamHandR"),
                "独立手部图层也需要核对手型或手指驱动",
            ),
            "prop": ("道具交互", (), "未找到确认的袖口或道具交互动作为此模型驱动"),
        },
    ),
}


ASSET_HINTS = {
    "gaze-horizontal": ("gaze", "glance", "侧瞟", "视线"),
    "eyelid-left": ("blink", "眨眼"),
    "eyelid-right": ("blink", "眨眼"),
    "mouth-open": ("talk", "speak", "说话"),
    "mouth-form": ("smile", "微笑"),
    "mouth-purse": ("purse", "抿嘴"),
    "blush": ("blush", "红晕"),
    "head-pitch": ("nod", "点头", "低头"),
    "head-roll": ("tilt", "偏头"),
    "breathing": ("breath", "呼吸"),
    "arms": ("arm", "arms", "抬手"),
    "hand-shape": ("hand", "finger", "手型", "手指"),
    "prop": ("prop", "sleeve", "道具", "袖口"),
}


NAME_HINTS = {
    "gaze-horizontal": ("eye ball x", "eyeball x", "horizontal gaze", "视线水平"),
    "gaze-vertical": ("eye ball y", "eyeball y", "vertical gaze", "视线垂直"),
    "mouth-open": ("mouth open", "嘴部开合"),
    "mouth-form": ("mouth form", "通用嘴形"),
    "mouth-purse": ("lip purse", "抿嘴"),
    "head-yaw": ("head yaw", "转头"),
    "head-pitch": ("head pitch", "低头", "俯仰"),
    "head-roll": ("head roll", "偏头"),
    "shoulders": ("shoulder", "肩部", "肩膀"),
    "neck": ("neck", "颈部"),
    "breathing": ("breath", "breathing", "呼吸"),
    "blush": ("blush", "红晕"),
    "hand-shape": ("hand shape", "手型", "手指"),
}


def matches_hint(name: str, hints: tuple[str, ...]) -> bool:
    words = " " + re.sub(r"[^a-z0-9]+", " ", name.lower()).strip() + " "
    return any(
        f" {hint} " in words if hint.isascii() else hint in name for hint in hints
    )


def classify_capabilities(
    parameters: dict[str, ParameterEvidence],
    assets: dict[str, ModelAsset],
    parts: dict[str, ModelPart],
    configured: dict[str, Binding],
    inventory_complete: bool,
    diagnostics: list[AnalysisDiagnostic],
) -> list[CapabilityGroup]:
    groups: list[CapabilityGroup] = []
    statuses: tuple[Status, ...] = ("CONFIRMED", "CANDIDATE", "UNKNOWN", "MISSING")
    for group_id, (group_label, rules) in CATALOG.items():
        results: list[Semantic] = []
        for semantic_id, (label, aliases, missing_reason) in rules.items():
            candidates = {
                item.id: item
                for item in parameters.values()
                if item.id in aliases
                or (
                    item.name is not None
                    and matches_hint(item.name, NAME_HINTS.get(semantic_id, ()))
                )
            }
            hints = ASSET_HINTS.get(semantic_id, ())
            matches = [
                asset
                for asset in assets.values()
                if set(asset.parameter_ids).intersection(candidates)
                or matches_hint(Path(asset.artifact_ref).name, hints)
            ]
            part_hints = {
                "arms": ("arm", "forearm", "手臂"),
                "hand-shape": ("hand", "finger", "手部", "手指"),
                "prop": ("prop", "sleeve", "袖口", "道具"),
            }.get(semantic_id, ())
            part_ids = [
                part.id
                for part in parts.values()
                if matches_hint(f"{part.id} {part.name}", part_hints)
            ]
            matched_name = bool(candidates)
            binding = configured.pop(semantic_id, None)
            status: Status
            if binding:
                if binding.parameter_id is not None:
                    bound = parameters.get(binding.parameter_id)
                    if bound is not None:
                        candidates[bound.id] = bound
                    valid = (
                        bound is not None
                        and bound.active is not False
                        and (not inventory_complete or bound.active is True)
                    )
                else:
                    bound_asset = assets.get(binding.asset_ref)
                    valid = (
                        bound_asset is not None
                        and (
                            binding.asset_sha256 is None
                            or bound_asset.sha256 == binding.asset_sha256
                        )
                        and (
                            not inventory_complete
                            or all(
                                pid in parameters and parameters[pid].active is not None
                                for pid in bound_asset.parameter_ids
                            )
                        )
                    )
                    if valid and bound_asset not in matches:
                        matches.append(bound_asset)
                status = "CONFIRMED" if valid else "UNKNOWN"
                reason = (
                    "使用工作台已保存的确认映射"
                    if valid
                    else "已保存映射的目标无法确认，请核对模型版本或重新绑定"
                )
            elif (
                any(
                    item.active is not False
                    and (not inventory_complete or item.active is True)
                    for item in candidates.values()
                )
                or matches
            ):
                status = "CANDIDATE"
                reason = "参数名称或资产内容与语义相关，需要核对实际播放和方向"
            else:
                status = "MISSING" if inventory_complete and not part_ids else "UNKNOWN"
                reason = missing_reason or f"未找到可确认的{label}绑定或动作文件"
                if part_ids:
                    reason = f"找到相关部件，{reason}"
                elif not inventory_complete:
                    reason = f"参数清单不完整，{reason}"
                elif candidates:
                    reason = f"找到{label}相关参数，当前 Runtime 未确认其可驱动部件"

            evidence = {
                item for parameter in candidates.values() for item in parameter.evidence
            }
            if matched_name:
                evidence.add("NAME_MATCH")
            if matches:
                evidence.add("ACTION_ASSET")
            if part_ids:
                evidence.add("MODEL_PART")
            if binding:
                evidence.add("WORKBENCH_BINDING")
                if status != "CONFIRMED":
                    diagnostics.append(
                        AnalysisDiagnostic(
                            code="CONFIRMED_BINDING_UNVERIFIED",
                            message=reason,
                            semantic_id=semantic_id,
                            reference=binding.parameter_id or binding.asset_ref,
                        )
                    )
            results.append(
                Semantic(
                    id=semantic_id,
                    label=label,
                    status=status,
                    required_parameter_ids=list(aliases),
                    reason=reason,
                    parameters=list(candidates.values()),
                    assets=matches,
                    part_ids=part_ids,
                    binding=binding,
                    evidence=sorted(evidence),
                    missing_items=[label] if status in {"UNKNOWN", "MISSING"} else [],
                )
            )
        groups.append(
            CapabilityGroup(
                id=group_id,
                label=group_label,
                capabilities=results,
                status=next(
                    status
                    for status in statuses
                    if any(item.status == status for item in results)
                ),
            )
        )
    for unknown in sorted(configured):
        diagnostics.append(
            AnalysisDiagnostic(
                code="SEMANTIC_UNKNOWN",
                message="绑定语义未列入当前能力目录",
                semantic_id=unknown,
            )
        )

    return groups
