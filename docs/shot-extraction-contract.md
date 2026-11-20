# 分镜提取三端契约

本文约定本轮分镜功能的交付接口。Python 生成逐镜头时间线，Java 导入素材并提供任务和查询接口，前端展示镜头索引和播放器跳转。三端可依据同一契约并行开发。

## Python 与 Java

沿用 `POST /internal/v1/asset-extraction/tasks` 和 `GET /internal/v1/asset-extraction/tasks/{taskId}`。Java 业务类型 `SHOT` 映射为 Python 分支 `SHOTS`。Java 为一次分镜执行分配稳定的 Python 任务 ID，并复用现有共享输入、重试和幂等机制。

```json
{
  "taskId": "shot-001",
  "sourceVideoRef": "uploads/episode-01.mp4",
  "outputs": ["SHOTS"]
}
```

首次提交返回 HTTP 202。分镜扫描使用原视频帧时间，`frameIntervalSeconds` 只影响人物分支。相同输入和有效参数重复提交只执行一次，冲突返回 HTTP 409。分镜可与音频、人物并行调度。各分支单独发布，整单状态沿用 [素材任务协议](java-asset-extraction-api.md)。`SHOTS` 分支沿用 `QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`；处理阶段为 `DETECTING_SHOTS`、`EXTRACTING_PREVIEWS`、`WRITING_RESULTS`，排队时阶段为 `null`。本轮分镜分支的 `progress` 为 `null`，完成数量从 `result.shotCount` 读取。

查询成功示例：

```json
{
  "taskId": "shot-001",
  "status": "SUCCEEDED",
  "branches": {
    "SHOTS": {
      "status": "SUCCEEDED",
      "stage": "WRITING_RESULTS",
      "progress": null,
      "result": {
        "manifestRef": "shot-001-shots-run1/shots.json",
        "shotCount": 2
      },
      "error": null
    }
  }
}
```

失败时 `result` 为 `null`，`error` 使用现有 `{code,message}` 结构。分镜处理错误码为 `SHOT_DETECTION_FAILED`、`SHOT_PREVIEW_FAILED`、`SHOT_WRITE_FAILED` 或 `SHOT_EXTRACTION_TIMEOUT`。Java 清单校验不通过时将分镜步骤标记为失败，使用 `SHOT_MANIFEST_INVALID`。源文件、任务冲突和未找到任务等情况沿用现有通用状态和错误码。

## 分镜清单 v1

```json
{
  "schemaVersion": 1,
  "durationMs": 20000,
  "shots": [
    {
      "shotIndex": 1,
      "startMs": 0,
      "endMs": 8000,
      "previewTimestampMs": 4000,
      "previewPath": "previews/shot-000001.jpg"
    },
    {
      "shotIndex": 2,
      "startMs": 8000,
      "endMs": 20000,
      "previewTimestampMs": 14000,
      "previewPath": "previews/shot-000002.jpg"
    }
  ]
}
```

- 时间字段均为相对源视频播放起点的整数毫秒，`durationMs` 为正整数。镜头区间使用 `[startMs, endMs)`。
- `shotIndex` 从 1 开始连续递增，数组按开始时间排序。首段从 0 开始，段间首尾相接，末段结束于 `durationMs`。有效视频没有切点时输出一段覆盖全片的镜头。
- 每段一张 JPEG。代表帧目标为区间中点，`previewTimestampMs` 记录实际选中帧的时间，满足 `startMs <= previewTimestampMs < endMs`。
- `shotCount` 等于 `shots.length`。`manifestRef` 相对于共享输出根，`previewPath` 相对于清单所在目录。引用使用 `/` 和相对路径。Python 发布前和 Java 导入前都校验实际文件及路径边界，包括符号链接。
- Python 完整写入并校验后发布分镜分支目录。Java 只在分支成功后导入，全部镜头确认可用后再发布该执行的素材版本。失败重试沿用现有任务规则，旧成功版本继续可读。

## Java 与前端

以下路径是浏览器通过网关调用的完整路径。沿用当前鉴权、业务响应包装和分页规则。

| 用途 | 方法与完整路径 |
| --- | --- |
| 读取支持能力 | GET `/api/media/extraction-capabilities` |
| 提交一次分镜任务 | POST `/api/ai/tasks`，请求体 `{"episodeId":"135","extractTypes":["SHOT"]}` |
| 查询执行和重试 | GET `/api/ai/tasks/{taskId}`、POST `/api/ai/tasks/{taskId}/retry` |
| 查看分镜素材 | GET `/api/media/episodes/{episodeId}/assets?type=SHOT&pageNum=1&pageSize=20`，可选 `sourceTaskId` |

`SHOT` 由现有 `EXTRACTION_SUPPORTED_TYPES` 控制。Media 与 Java AI 在部署确认 Python 支持 `SHOTS` 后同步启用。原首帧接口和历史素材继续可读。

正式分镜使用现有素材分页结构，默认返回同一剧集当前原视频的最新成功分镜版本，按 `shotIndex` 升序。当分页超过一页时，客户端从首个正式条目获得 `sourceTaskId`，并用它固定后续页版本。示例首条 `data.items[0]`：

```json
{
  "projectId": "41",
  "episodeId": "135",
  "assetId": "814",
  "assetType": "SHOT",
  "createdAt": "2026-11-20T07:00:15Z",
  "sourceTaskId": "922",
  "sizeBytes": 139842,
  "expiresInSeconds": 600,
  "details": {
    "shotId": "814",
    "shotIndex": 1,
    "startMs": 0,
    "endMs": 8000,
    "previewTimestampMs": 4000,
    "frameUrl": "https://media.example/signed.jpg"
  }
}
```

正式分镜的 `shotId` 为稳定的字符串标识，`shotIndex`、`startMs`、`endMs`、`previewTimestampMs` 均为整数。`frameUrl` 沿用现有签名素材 URL。没有正式分镜版本时，默认可回读历史首帧，其 `shotIndex` 和 `previewTimestampMs` 可缺省或为 `null`，`endMs` 为 `null`。前端可展示历史首帧预览，比例分段使用正式分镜区间。

## 三端验收

用上面的两镜头示例先做 Java 导入和前端接口替身验证，再用《末班车之后》的真实结果联调。阈值 8 的初步探测有 11 个候选切点。目视核对后排除了 100 秒和 110 秒的字幕变化，Python 默认配置实际输出 10 个镜头。检查从选中 `SHOT` 到看到时间线的完整流程，核对短镜头、跨边界高亮、点击跳转、代表帧时间和切换剧集后的状态。
