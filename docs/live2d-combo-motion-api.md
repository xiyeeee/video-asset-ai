# Live2D 组合动作编译 · Java 对接

浏览器路由、持久化结构和前端恢复流程以 [共享组合动作 API 契约](../../video-asset-server/docs/live2d-combo-motion-api.md) 为准；本文补充 Python 内部编译器协议和运行条件。

## 启动和文件引用

`make dev` 启动 Python 服务，默认 19000。配置 `LIVE2D_WEB_CORE_JS`、`LIVE2D_NODE_PATH`、`FFMPEG_PATH`、`LIVE2D_INPUT_ROOT` 和 `LIVE2D_OUTPUT_ROOT`。Java 校验项目权限、固定模型及素材版本，再把对象存储中的文件放到共享目录。模型沿用已生成的共享文件时可以直接引用，保留模型入口与 MOC3 的相对位置。

| 字段 | 相对根目录 | 说明 |
| --- | --- | --- |
| `modelManifestRef` | `LIVE2D_OUTPUT_ROOT` | 已固定模型版本的 `.model3.json` 路径。保留模型入口到 MOC3 的目录结构。可传 `modelMocSha256` 校验版本 |
| 台词 `sourceRef` | `LIVE2D_INPUT_ROOT` | Java 按来源版本放入同一份音频。可传 `sourceSha256` 固定内容 |
| 已保存动作 `sourceRef` | `LIVE2D_OUTPUT_ROOT` | 现有 `.motion3.json` 路径，必须传 `sourceSha256` |
| `artifactRef` | `LIVE2D_OUTPUT_ROOT` | 已发布的不可变动作文件。Java 可按权限代理读取或复制到对象存储，再提供播放器可访问的地址 |

Java 将 `modelVersionId` 与授权文件引用绑定。Python 回传实际读到的 `modelMocSha256`。路径使用相对引用，Python 的调试端口和共享目录留在服务内网。

## 模型能力

`POST /internal/v1/live2d/motion-capabilities` 接收 `modelVersionId`、`modelManifestRef` 和可选 `modelMocSha256`。返回同一个 `modelVersionId`、MOC3 内部版本码 `mocVersion`、MOC3 的 SHA-256、参数和动作能力。Core 在默认姿态向参数的两侧分别取中点和端点，检查顶点或不透明度是否变化。由此区分主动绑定和只声明了名字的参数。

```json
{
  "id": "ParamMouthForm",
  "min": -1.0,
  "max": 1.0,
  "defaultValue": 0.0,
  "active": true,
  "activeDecreasing": true,
  "activeIncreasing": true
}
```

`capabilities[]` 的字段为 `id`、`supported`、`parameterIds` 和 `missingParameterIds`。支持的预设基于当前导出链路锚定以下实际参数。

| 能力或预设 | 参数 | 曲线方向 |
| --- | --- | --- |
| `blink` | `ParamEyeLOpen`、`ParamEyeROpen` | 从默认值向闭眼侧 |
| `smile` | `ParamMouthForm` | 向正向嘴形 |
| `nod` | `ParamAngleY` | 向低头侧 |
| `speech` | `ParamMouthOpenY` | 向正向张嘴 |
| `raise-left-arm`、`raise-right-arm` | `ParamArmLA`、`ParamArmRA` | 报告手臂绑定能力 |
| `left-hand-shape`、`right-hand-shape` | 当前导出器没有独立手型参数 | 返回缺失的能力 ID |

手臂能力用于说明绑定状态。首版预设只提供眨眼、微笑、点头。参数有可见驱动是能力判定依据，用户仍可用实际预览核对动作幅度和部件质量。

## 异步任务

- `POST /internal/v1/live2d/motion-tasks` 返回 HTTP 202，接收 `taskId`、上述模型引用字段、`durationMs`、`clips`。同一任务号和请求复用同一状态，不同请求用同一任务号返回 409。
- `GET /internal/v1/live2d/motion-tasks/{taskId}` 返回 `QUEUED`、`RUNNING`、`SUCCEEDED` 或 `FAILED`，查询未知任务为 404。状态与生成任务一样保存在当前 Python 进程内。重启后已发布的文件可继续按原引用读取。
- 共用现有 Live2D 执行器。阶段为 `INSPECTING_MODEL`、`COMPILING`、`PUBLISHING`。日志记录 `taskId`、`modelVersionId`、阶段及错误码。
- 总时长最多 120000 毫秒、32 个片段。片段至少 2 毫秒、`clipId` 唯一、结束时刻在总时长范围内。返回的 `formatVersion: 1` 是服务产物契约版本，文件内部 `Version: 3` 是 Cubism 格式。

每个片段包含 `clipId`、`trackType`、`sourceId`、`startMs`、`durationMs`。

- `PRESET` 的 `sourceId` 是 `blink`、`smile` 或 `nod`。
- `SPEECH_AUDIO` 用 FFmpeg 解码源音频，按 20 毫秒采样 RMS，驱动嘴巴开合。可选 `sourceStartMs` 是源音频相对起点，`startMs` 是组合内起点。短音频结束后嘴巴恢复默认。Java 给播放器同一份音频及同一裁切起点。
- `SAVED_ACTION` 用文件 SHA-256 固定不可变版本，支持首版编译器输出的线性 `Parameter` 曲线。按片段长度缩放播放时间，片段结尾最多 120 毫秒恢复默认值。遇到非线性曲线、非参数轨或超出范围的值，返回明确诊断。

片段按半开区间 `[startMs, startMs + durationMs)` 占用参数。不同参数可同时生效，共用参数的重叠片段会收到 `PARAMETER_CONFLICT`，诊断包含 `parameterIds`、两个 `clipIds`、`startMs` 及 `endMs`。微笑占用嘴形，口型占用嘴巴开合。输出覆盖完整时轴，空挡与组合末尾按默认值求值。播放器按统一的毫秒时钟驱动音频和动作，读取 Cubism 文件时换算为秒，定位到总时长时采样最后一个关键帧。

HTTP 错误沿用 `detail.code` 和 `detail.message`。异步任务终态失败由 GET 返回 HTTP 200，`error` 包含 `code`、`message` 和 `stage`，`diagnostics` 列出片段问题。主要错误如下。

| 错误码 | 含义 |
| --- | --- |
| `REQUEST_INVALID`、`TASK_CONFLICT`、`TASK_NOT_FOUND` | 请求格式、重复编号、任务不存在 |
| `MODEL_REFERENCE_INVALID`、`MODEL_NOT_FOUND`、`MODEL_INVALID` | 模型引用或模型包无效 |
| `MODEL_VERSION_MISMATCH`、`MOTION_SOURCE_VERSION_MISMATCH` | 模型或来源摘要不一致 |
| `CUBISM_CORE_UNAVAILABLE`、`MODEL_INSPECTION_TIMEOUT`、`MODEL_INSPECTION_FAILED` | Core 执行或绑定读取失败 |
| `MOTION_COMPILE_REJECTED` | 详见 `diagnostics`，常见为 `PARAMETER_CONFLICT`、`CAPABILITY_MISSING`、`PRESET_UNKNOWN` 或 `SAVED_ACTION_INVALID` |
| `MOTION_SOURCE_INVALID`、`FFMPEG_UNAVAILABLE`、`AUDIO_DECODE_FAILED`、`AUDIO_DECODE_TIMEOUT` | 音频来源或解码异常 |
| `MOTION_TOO_LARGE`、`MOTION_PUBLISH_FAILED`、`MOTION_TASK_FAILED` | 产物大小、文件发布或内部异常 |

## Java 现有编译入口

现有 `POST /internal/v1/live2d/combo-motions/compile` 使用 `requestId`、`projectId`、`modelVersionId`、`name`、`durationMs`、`segments`。它接受上述两个模型引用字段及片段的 `sourceRef`、`sourceSha256`、`presetId`。沿用 `segmentId`、`startTimeMs`、`sourceVersionId`、`sourceStartMs` 和 `sourceDurationMs`。

- `MOTION` 带 `sourceRef` 表示已保存动作，同时提交 SHA-256。预设片段提交 `presetId`，或以预设名作为 `sourceId`。
- `DIALOGUE` 提交音频 `sourceRef`。如果引用整段音轨，`sourceStartMs` 是该台词在音轨内的起始时间。
- 实际运行时响应仍为 `READY` 或 `CONFLICT`。`playbackManifest` 含 `schemaVersion: 2`、`compileMode: "PARAMETER_CURVES"`。成功时读取 `compiledMotion`，失败时读取 `diagnostics`，其中 `clipIds` 对应原 `segmentId`。`issues` 保持当前 Java DTO 形状。
- 缺少 `modelManifestRef` 的旧请求返回 `compileMode: "TIMELINE_ONLY"`，用于原时间轴校验。前端的实际动作预览和保存需要 `PARAMETER_CURVES` 的产物。同步入口等候最多 15 秒，超时返回 HTTP 504 `MOTION_COMPILE_PENDING`，可使用 `requestId` 从异步路由查询最终状态。

Java 保存时把 `compiledMotion.artifactRef`、`artifactSha256`、`modelMocSha256` 和草稿版本关联，再给前端可播放地址。组合 JSON 中的台词来源沿用 Java 的文件鉴权与签名。更新测试中的 Java 客户端 DTO 时，模型与来源文件引用由 Java 根据项目和素材版本解析，前端只提交业务 ID。

## 真实联调记录

用《末班车之后》现有音轨的 11.220–15.285 秒对白和已经生成的都市术士模型验收。请求、状态、ZIP 外的动作 JSON 及浏览器帧存于 `outputs/live2d-combo-acceptance/`。以下是内部同步入口的真实请求与回包摘录。完整表单见本地 `java-request.json`，完整回包见 `java-response.json`。

```json
{
  "modelVersionId": "421",
  "modelManifestRef": "saved-model-421/model.model3.json",
  "modelMocSha256": "c89555f8bc3ae15298d340362bce33557ee6db1ae40e897b39a04ff4c21649a1",
  "requestId": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "projectId": 12,
  "name": "微笑点头说话",
  "durationMs": 3500,
  "segments": [
    {
      "segmentId": "smile",
      "trackType": "MOTION",
      "sourceId": "motion-preset:smile",
      "sourceVersionId": "1",
      "startTimeMs": 0,
      "durationMs": 3000,
      "presetId": "smile"
    },
    {
      "segmentId": "nod",
      "trackType": "MOTION",
      "sourceId": "motion-preset:nod",
      "sourceVersionId": "1",
      "startTimeMs": 500,
      "durationMs": 800,
      "presetId": "nod"
    },
    {
      "segmentId": "audio",
      "trackType": "DIALOGUE",
      "sourceId": "dialogue:997",
      "sourceVersionId": "13",
      "startTimeMs": 200,
      "durationMs": 3100,
      "sourceRef": "episode-dialogue.wav"
    },
    {
      "segmentId": "blink",
      "trackType": "MOTION",
      "sourceId": "motion-preset:blink",
      "sourceVersionId": "1",
      "startTimeMs": 1600,
      "durationMs": 320,
      "presetId": "blink"
    }
  ]
}
```

```json
{
  "requestId": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "compilerVersion": 1,
  "status": "READY",
  "durationMs": 3500,
  "issues": [],
  "missingCapabilities": [],
  "playbackManifest": {
    "schemaVersion": 2,
    "compileMode": "PARAMETER_CURVES",
    "compiledMotion": {
      "formatVersion": 1,
      "format": "cubism-motion3",
      "durationMs": 3500,
      "artifactRef": "motion-tasks/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-18ddf144cc6430aed79a9441159dfc3ac4cfb70ec7fd5811deb902da9bb2161d/combo.motion3.json",
      "artifactSha256": "18ddf144cc6430aed79a9441159dfc3ac4cfb70ec7fd5811deb902da9bb2161d",
      "modelMocSha256": "c89555f8bc3ae15298d340362bce33557ee6db1ae40e897b39a04ff4c21649a1"
    },
    "diagnostics": []
  }
}
```

实测浏览器通过现有 Pixi / Cubism 库直接解析动作文件并渲染真实模型。199 毫秒时嘴巴开合为 0。870 毫秒时嘴形为 0.75、开合为 1、头部俯仰为 -19.5。定位到 3500 毫秒时三个参数都为默认值 0。参见本地 `browser-verification.json` 和 `browser-870ms.png`。实测两个微笑片段在 1000–1800 毫秒冲突，同时返回右手手型能力缺失。参见 `conflict-result.json`。

这次使用真实 Python HTTP 服务和独立浏览器验证页。Java 调用方仍需传入模型和素材引用，React 编辑器中播放、拖动与历史恢复需联合验收。
