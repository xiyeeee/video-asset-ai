from fastapi import HTTPException

from app.pipeline.character_task_models import TaskError


def task_http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=TaskError(code=code, message=message).model_dump(),
    )
