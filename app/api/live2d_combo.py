from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.api.live2d_motion import MotionService
from app.api.task_errors import task_http_error
from app.live2d.motion_models import Clip, MotionRequest
from app.pipeline.character_task_models import TaskStatus
from app.pipeline.character_tasks import TaskConflict
from app.pipeline.live2d_combo import (
    ComboCompileIssue,
    ComboCompileRequest,
    ComboCompileResult,
    compile_combo_motion,
)

router = APIRouter(prefix="/internal/v1/live2d/combo-motions")


@router.post("/compile", response_model=ComboCompileResult)
def compile_motion(
    payload: ComboCompileRequest, request: Request, service: MotionService
) -> ComboCompileResult:
    request.state.task_id = payload.request_id
    request.state.project_id = payload.project_id
    request.state.model_version_id = payload.model_version_id
    request.state.combo_request_id = payload.request_id

    # 旧 Java 调用只提交版本 ID 时沿用原时间轴协议。带可信文件引用时走参数编译。
    if payload.model_manifest_ref is None:
        return compile_combo_motion(payload)

    timeline = compile_combo_motion(payload, check_motion_overlap=False)
    if timeline.issues:
        return timeline
    try:
        clips = [
            Clip(
                clip_id=segment.segment_id,
                track_type="SPEECH_AUDIO"
                if segment.track_type == "DIALOGUE"
                else "SAVED_ACTION"
                if segment.source_ref
                else "PRESET",
                source_id=segment.preset_id or segment.source_id,
                start_ms=segment.start_time_ms,
                duration_ms=segment.duration_ms,
                source_start_ms=segment.source_start_ms or 0,
                source_ref=segment.source_ref,
                source_sha256=segment.source_sha256,
            )
            for segment in payload.segments
        ]
        task = MotionRequest(
            task_id=payload.request_id,
            model_version_id=payload.model_version_id,
            model_manifest_ref=payload.model_manifest_ref,
            model_moc_sha256=payload.model_moc_sha256,
            duration_ms=payload.duration_ms,
            clips=clips,
        )
    except ValidationError as exc:
        raise task_http_error(
            422, "REQUEST_INVALID", "动作编译引用或时间轴参数无效"
        ) from exc

    try:
        service.submit(task)
    except TaskConflict as exc:
        raise task_http_error(409, "TASK_CONFLICT", str(exc)) from exc

    state = service.wait(task.task_id, timeout=15)
    if state is None:
        raise task_http_error(
            504, "MOTION_COMPILE_PENDING", "编译仍在后台运行，可按 requestId 查询"
        )

    manifest = dict(
        timeline.playback_manifest, schemaVersion=2, compileMode="PARAMETER_CURVES"
    )
    if state.status == TaskStatus.SUCCEEDED:
        manifest["compiledMotion"] = (
            state.result.model_dump(mode="json") if state.result else None
        )
        manifest["diagnostics"] = []
        return timeline.model_copy(update={"playback_manifest": manifest})

    manifest["diagnostics"] = [
        item.model_dump(mode="json") for item in state.diagnostics
    ]
    issues = [
        ComboCompileIssue(
            code=item.code, message=item.message, segment_ids=item.clip_ids
        )
        for item in state.diagnostics
    ]
    if not issues and state.error:
        issues.append(
            ComboCompileIssue(
                code=state.error.code, message=state.error.message, segment_ids=[]
            )
        )
    missing = sorted(
        {
            item.capability_id
            for item in state.diagnostics
            if item.code == "CAPABILITY_MISSING" and item.capability_id
        }
    )
    return timeline.model_copy(
        update={
            "status": "CONFLICT",
            "issues": issues,
            "missing_capabilities": missing,
            "playback_manifest": manifest,
        }
    )
