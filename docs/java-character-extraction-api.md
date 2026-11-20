# Java 对接异步人物提取

Java 生成业务 `taskId`，将视频写入共享输入目录，再通过 HTTP 提交任务。Python 依次抽帧、检测、分割和发布结果。Java 轮询任务状态，成功后从共享输出目录读取清单和 PNG。

## 启动及目录配置

在 `video-asset-ai` 根目录运行：

```bash
uv sync --extra characters
CHARACTER_INPUT_ROOT=/shared/video-input \
CHARACTER_OUTPUT_ROOT=/shared/video-output \
CHARACTER_FRAME_INTERVAL_SECONDS=2 \
uv run --extra characters uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

| 配置项 | 本地默认值 | 用途 |
| --- | --- | --- |
| `CHARACTER_INPUT_ROOT` | `samples` | Java 写入原视频，Python 读取 |
| `CHARACTER_OUTPUT_ROOT` | `outputs/character-tasks` | Python 发布结果，Java 读取 |
| `CHARACTER_FRAME_INTERVAL_SECONDS` | `2` | 省略任务采样间隔时使用的秒数 |
| `CHARACTER_DEVICE` | `cpu` | 人物模型设备，可配 `mps` |
| `FFMPEG_PATH`、`FFPROBE_PATH` | `ffmpeg`、`ffprobe` | 系统视频工具 |

也可通过 `.env` 加载配置，模型权重配置见项目 README。相对的根目录以服务进程启动目录为基准。Java 与 Python 可以用不同的挂载绝对路径，但须看到同一批文件。Python 需要在输出根目录下创建目录和重命名的权限，Java 需要读取权限。验收和部署时使用单个 Uvicorn worker，启动命令不带 `--reload`。每个进程只有一个后台执行线程，同一时刻处理一个任务，模型在该线程首次使用时加载并在后续任务中复用。

## 提交任务

`POST /internal/v1/character-extraction/tasks`，请求头 `Content-Type: application/json`。

```json
{
  "taskId": "task-001",
  "sourceVideoRef": "uploads/episode-01.mp4",
  "frameIntervalSeconds": 2
}
```

- `taskId` 为 1～128 个 ASCII 字符，首字符为字母或数字，余下可含字母、数字、连字符和下划线。Java 应为同一次业务提交保留同一 ID。
- `sourceVideoRef` 使用以共享输入根目录为基准的相对引用，各路径段以 `/` 分隔。空路径段、`.`、`..`、绝对路径和解析后跳出根目录的符号链接会被拒绝。该文件需在提交前写入完成，任务期间保留对应内容。
- `frameIntervalSeconds` 可省略或传 `null`。显式设置时须为有限正数，支持小数。

首次受理返回 `202 Accepted`：

```json
{
  "taskId": "task-001",
  "status": "QUEUED",
  "stage": null,
  "progress": {"frameCount": null, "detectedCharacterCount": null},
  "result": null,
  "error": null
}
```

同一进程内，并发提交相同 ID、同一 `sourceVideoRef` 字符串和同一生效间隔时，只排队一次。缺省间隔与显式传入当前默认值等效。重复提交返回 `202` 和已有状态，已成功或已失败的任务也遵循此规则。若同 ID 的生效参数不同，返回 `409 TASK_CONFLICT`。服务按引用和间隔判断幂等，文件内容版本由 Java 管理。

## 查询任务

`GET /internal/v1/character-extraction/tasks/{taskId}`。已知任务始终返回 HTTP `200`。响应带 `X-Request-ID`，服务日志可按请求 ID 和任务 ID 检索。

| `status` | `stage` | 含义 |
| --- | --- | --- |
| `QUEUED` | `null` | 已受理，等待执行 |
| `RUNNING` | `EXTRACTING_FRAMES` | 探测视频并抽帧 |
| `RUNNING` | `DETECTING_CHARACTERS` | 加载模型并检测各帧人物 |
| `RUNNING` | `SEGMENTING_CHARACTERS` | 分割各人物并生成 PNG |
| `RUNNING` | `WRITING_RESULTS` | 写清单、校验文件和发布目录 |
| `SUCCEEDED` | `WRITING_RESULTS` | 整套产物已发布，可以读取 |
| `FAILED` | 失败阶段 | 处理失败，`error` 包含错误信息 |

工作线程刚领取任务、校验源视频时，阶段可能短暂为 `null`。阶段变化很快时，一次轮询可跨过多个阶段。计数在对应阶段完成前为 `null`。同时只存在一个终态字段，成功填写 `result`，失败填写 `error`。

运行中示例：

```json
{
  "taskId": "task-001",
  "status": "RUNNING",
  "stage": "SEGMENTING_CHARACTERS",
  "progress": {"frameCount": 3, "detectedCharacterCount": 6},
  "result": null,
  "error": null
}
```

HTTP 验收任务 `hero-delivery-001` 的实际成功响应。Java 读取自己那次任务返回的 `manifestRef` 原值。

```json
{
  "taskId": "hero-delivery-001",
  "status": "SUCCEEDED",
  "stage": "WRITING_RESULTS",
  "progress": {"frameCount": 3, "detectedCharacterCount": 6},
  "result": {
    "manifestRef": "hero-delivery-001-8de49f309f1c4a1ba08affe18ddac43d/characters/characters.json",
    "characterAssetCount": 6
  },
  "error": null
}
```

视频可正常处理但无人物时同样为 `SUCCEEDED`。此时 `characterAssetCount` 为 `0`，清单仍包含截图记录，`characters` 为 `[]`。

处理失败示例：

```json
{
  "taskId": "task-001",
  "status": "FAILED",
  "stage": "EXTRACTING_FRAMES",
  "progress": {"frameCount": null, "detectedCharacterCount": null},
  "result": null,
  "error": {"code": "FRAME_EXTRACTION_FAILED", "message": "视频抽帧失败，请检查源视频"}
}
```

其他任务错误码包括 `FRAME_EXTRACTION_TIMEOUT`、`CHARACTER_MODELS_UNAVAILABLE`、`CHARACTER_DETECTION_FAILED`、`CHARACTER_SEGMENTATION_FAILED`、`RESULT_WRITE_FAILED`、`SOURCE_VIDEO_REF_INVALID`、`SOURCE_VIDEO_NOT_FOUND` 和 `TASK_PROCESSING_FAILED`。源文件在受理后被移除时，错误会出现在任务状态里。异常详情由服务日志保留，Java 可按 `error.code` 处理。

### HTTP 拒绝响应示例

| 触发条件 | HTTP | 响应体 |
| --- | --- | --- |
| 参数无效，例如间隔为 0 | 422 | `{"detail":{"code":"REQUEST_INVALID","message":"请求参数格式错误或缺少必填字段"}}` |
| 文件引用非法，例如 `../episode.mp4` | 422 | `{"detail":{"code":"SOURCE_VIDEO_REF_INVALID","message":"sourceVideoRef 须为共享输入目录内的相对文件引用"}}` |
| 提交时找不到源视频 | 404 | `{"detail":{"code":"SOURCE_VIDEO_NOT_FOUND","message":"共享输入目录中找不到源视频"}}` |
| 同 ID 的有效参数冲突 | 409 | `{"detail":{"code":"TASK_CONFLICT","message":"该 taskId 已绑定其他人物提取参数"}}` |
| 查询未知任务 | 404 | `{"detail":{"code":"TASK_NOT_FOUND","message":"找不到人物提取任务"}}` |

## 从共享目录读取结果

任务返回 `SUCCEEDED` 后，按下表解析文件。Java 侧先验证引用为相对路径，再用 `Path.toRealPath()` 按路径段校验结果仍在对应的共享根目录或任务目录内。

| 文件引用 | 解析基准 | 示例 |
| --- | --- | --- |
| 响应的 `result.manifestRef` | 共享输出根目录 | `hero-delivery-001-8de49f309f1c4a1ba08affe18ddac43d/characters/characters.json` |
| 清单的 `framesManifest` | `characters.json` 的父目录 | `../frames/frames.json`，解析后仍在同一任务目录内 |
| 清单的 `sourceFrame` 或抽帧清单的 `frames[].path` | `frames.json` 的父目录 | `frame-000002.jpg` |
| 清单的 `debugImage` | `characters.json` 的父目录 | `frame-000002/detections.jpg` |
| 清单的 `pngPath` | `characters.json` 的父目录 | `frame-000002/person-001.png` |

`frames` 列出处理过的截图，包括无人帧。`characters` 可按 `sourceFrame` 归帧。同一份结果中，`(sourceFrame, characterId)` 唯一标识一个人物素材，`characterId` 每帧从 1 开始。

`timestampMs` 是实际选中帧相对提交视频播放起点的时间，按四舍五入写为整数毫秒，可映射为 Java `long`。原始抽帧清单保留小数精度。极高帧率下不同帧可能落在同一整数毫秒，仍可通过 `sourceFrame` 区分。`box` 为原图像素坐标 `[left, top, right, bottom]`，右下边界不包含在框内。`score` 为检测分数。

[实际生成的 characters.json](examples/character-extraction/characters.json) 是上述 HTTP 任务原样生成的完整清单，包含 3 帧和 6 条素材，可直接供 Java 核对反序列化。清单引用的截图、调试图和 PNG 由下方验收命令在共享输出目录生成。

## 进程重启与落盘结果

状态、排队记录和幂等映射存在当前进程内。进程重启后，旧任务在 HTTP 查询中返回 `TASK_NOT_FOUND`。成功发布的目录仍可通过 Java 已保存的 `manifestRef` 读取。Java 需要在业务状态中记录成功响应的引用，进程重启后仍须处理的任务可重新提交。同 ID 重跑使用新的独立目录，之前的成功目录和 PNG 不会被覆盖。运行时失败会清理临时目录。进程被强制终止时可能留下 `.character-task-*` 临时目录，服务重启也不会复用这些文件。

## 本地 HTTP 验收

将待验证的短视频放到 `samples/uploads/episode-01.mp4`，使用默认共享目录启动后执行：

```bash
curl --fail-with-body -X POST http://127.0.0.1:8000/internal/v1/character-extraction/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"task-001","sourceVideoRef":"uploads/episode-01.mp4","frameIntervalSeconds":1}'

curl --fail-with-body http://127.0.0.1:8000/internal/v1/character-extraction/tasks/task-001
```

重复查询直至终态。从 `outputs/character-tasks/` 下的 `manifestRef` 读取清单，按清单路径打开透明 PNG。对照原视频画面和抽帧清单核对时间戳。

本次使用 Blender Foundation 二维短片《Hero》的 2.25 秒片段和真实 Grounding DINO、SAM 2 权重做 HTTP 验收。在项目根目录已有 `samples/blender-hero.webm` 时，可按以下命令生成同一输入：

```bash
mkdir -p samples/uploads
ffmpeg -nostdin -n -ss 126.4 -i samples/blender-hero.webm -t 2.25 \
  -an -vf 'scale=1280:-2' -c:v libx264 -pix_fmt yuv420p -fps_mode passthrough \
  samples/uploads/hero-character-http.mp4
```

将提交参数中的 `sourceVideoRef` 改为 `uploads/hero-character-http.mp4`，间隔设为 1 秒。Apple M5 CPU 实测返回 3 帧、6 个透明 PNG，帧时间为 0、1001、2002 毫秒。另用合成灰色视频和真实检测模型验证了成功空清单，用空 MP4 验证了失败状态。HTTP 并发重复提交只发布了一份结果。自动测试通过替身控制排队、失败和发布时机。业务漫剧样片和 Spring Boot 调用端尚未参与联调。
