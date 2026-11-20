# 按分镜提取背景三端契约

背景首版以已有分镜为时间索引，从每个镜头的原视频画面里挑一张背景候选图。分镜代表图可作为选帧参考，产物保留原画构图、真实来源时间和遮挡状态。去人补图、多帧重建和跨镜头背景去重留给后续迭代。

## Python 与 Java

沿用 `POST /internal/v1/asset-extraction/tasks` 和 `GET /internal/v1/asset-extraction/tasks/{taskId}`，Java 与 Python 均使用 `BACKGROUND`。源视频引用相对于共享输入根目录。请求示例：

```json
{
  "taskId": "background-001",
  "sourceVideoRef": "uploads/episode-01.mp4",
  "outputs": ["BACKGROUND"],
  "shotManifestRef": "shot-001-shots-run1/shots.json"
}
```

- `shotManifestRef` 可选，相对于共享输出根目录。Java 从同一原视频最新成功的正式分镜执行中获取该值。Python 校验实际文件、符号链接和路径范围，并按 [分镜契约](shot-extraction-contract.md) 校验清单与源视频时长。显式提交无效引用时返回失败，便于定位旧产物或配置问题。
- 同一 Python 任务同时选择 `SHOTS`、`BACKGROUND` 时，背景复用本次分镜，`shotManifestRef` 省略。提交这两个分支时又指定旧清单，或者任务没有选择 `BACKGROUND` 却传此字段，返回 `422 REQUEST_INVALID`。
- 只选背景且省略分镜引用时，Python 复用现有分镜检测生成本次镜头区间。一个视频没有切点时按一个镜头处理。不同视频或版本的分镜由 Java 在业务层隔离。
- 首次受理返回 HTTP 202；同 ID、同有效参数复用已有任务，变更分镜引用或其他有效参数返回 409。背景采样使用各镜头自己的区间，`frameIntervalSeconds` 只影响人物分支。
- 背景阶段为 `RESOLVING_SHOTS`、`SELECTING_BACKGROUND_FRAMES`、`WRITING_RESULTS`。未开始时阶段为 `null`，首版 `progress` 为 `null`。独立分支的状态与部分成功语义沿用素材任务接口。

成功响应：

```json
{
  "taskId": "background-001",
  "status": "SUCCEEDED",
  "branches": {
    "BACKGROUND": {
      "status": "SUCCEEDED",
      "stage": "WRITING_RESULTS",
      "progress": null,
      "result": {
        "manifestRef": "background-001-background-run1/backgrounds.json",
        "backgroundCount": 2
      },
      "error": null
    }
  }
}
```

| 情况 | 错误码 |
| --- | --- |
| 显式分镜清单缺失、越界或格式不符 | `BACKGROUND_SHOT_MANIFEST_INVALID` |
| 本次分镜依赖失败 | `BACKGROUND_SHOTS_FAILED` |
| 候选帧提取或必要的遮挡检测失败 | `BACKGROUND_CANDIDATE_FAILED` |
| 处理超时 | `BACKGROUND_EXTRACTION_TIMEOUT` |
| 清单或图片校验、发布失败 | `BACKGROUND_WRITE_FAILED` |
| Java 导入的结果不符合契约 | `BACKGROUND_MANIFEST_INVALID` |

## 背景清单 v1

```json
{
  "schemaVersion": 1,
  "durationMs": 20000,
  "backgrounds": [
    {
      "shotIndex": 1,
      "startMs": 0,
      "endMs": 8000,
      "timestampMs": 4000,
      "imagePath": "images/shot-000001.jpg",
      "method": "SOURCE_FRAME",
      "personStatus": "NOT_DETECTED",
      "subtitleStatus": "NOT_DETECTED",
      "reviewStatus": "CLEAN_CANDIDATE"
    },
    {
      "shotIndex": 2,
      "startMs": 8000,
      "endMs": 20000,
      "timestampMs": 14000,
      "imagePath": "images/shot-000002.jpg",
      "method": "SOURCE_FRAME",
      "personStatus": "DETECTED",
      "subtitleStatus": "UNKNOWN",
      "reviewStatus": "NEEDS_REVIEW"
    }
  ]
}
```

- 每个镜头发布一张 JPEG，按 `shotIndex` 排序，`backgroundCount` 等于数组长度。镜头区间与所用分镜一致，所选原帧时间满足 `startMs <= timestampMs < endMs`。所有时间均为相对原视频播放起点的整数毫秒。
- `method` 首版为 `SOURCE_FRAME`。`personStatus` 和 `subtitleStatus` 使用 `DETECTED`、`NOT_DETECTED`、`UNKNOWN`。只有人物和字幕都实际检查且均未检出时，状态才是 `CLEAN_CANDIDATE`。缺少画面字幕检测时保留 `UNKNOWN`。有可读候选图但存在遮挡或未知项时，照常发布为 `NEEDS_REVIEW`。干净候选表示当前检测结果，供用户预览确认。
- `manifestRef` 相对于共享输出根，`imagePath` 相对于清单所在目录。Python 在同次执行的临时目录写入清单和实际图片，校验后一次发布。Java 导入时再次校验路径、数量、镜头范围和图片。

## Java 与前端

| 用途 | 方法与完整网关路径 |
| --- | --- |
| 能力查询 | GET `/api/media/extraction-capabilities` |
| 发起提取 | POST `/api/ai/tasks`，请求体 `{"episodeId":"135","extractTypes":["BACKGROUND"]}` |
| 查询和重试 | GET `/api/ai/tasks/{taskId}`、POST `/api/ai/tasks/{taskId}/retry` |
| 读取背景 | GET `/api/media/episodes/{episodeId}/assets?type=BACKGROUND&pageNum=1&pageSize=20`，可选 `sourceTaskId` |

沿用鉴权、业务响应包装、摘要和素材分页规则。Python 部署支持背景后，Media 与 Java AI 同步启用 `EXTRACTION_SUPPORTED_TYPES` 中的 `BACKGROUND`。默认读当前原视频最新成功版本，重跑失败保留旧结果。列表按镜头顺序，分页用 `sourceTaskId` 固定版本。`assetCount` 包含待复核候选。

`data.items[0]` 示例：

```json
{
  "projectId": "41",
  "episodeId": "135",
  "assetId": "819",
  "assetType": "BACKGROUND",
  "createdAt": "2026-11-20T07:00:15Z",
  "sourceTaskId": "925",
  "sizeBytes": 139842,
  "expiresInSeconds": 600,
  "details": {
    "shotId": "814",
    "shotIndex": 1,
    "startMs": 0,
    "endMs": 8000,
    "timestampMs": 4000,
    "method": "SOURCE_FRAME",
    "personStatus": "NOT_DETECTED",
    "subtitleStatus": "NOT_DETECTED",
    "reviewStatus": "CLEAN_CANDIDATE",
    "imageUrl": "https://media.example/signed.jpg"
  }
}
```

`shotId` 是能关联到同一源视频、同一分镜版本的正式镜头 ID，未知时为 `null`。`imageUrl` 是 Java 生成的签名素材 URL。前端在背景 Tab 按镜头展示卡片与状态，复用播放器。点击来源时间跳转到候选帧的 `timestampMs`。

## 开发与集中验收

三端先按契约各自开发并完成必要的本端检查，无需等待其他端。契约示例用于字段核对和独立测试。等用户通知后，用同一集真实漫剧集中联调一次。验收依次核对 Python 选帧与分类、Java 导入及查询、前端预览与跳转。记录 Python 清单路径、Java 执行 ID、剧集 ID 和图片时间。上游未就绪时标明待联调的接口或资料。
