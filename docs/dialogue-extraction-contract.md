# 对白提取三端契约

本轮从剧集原视频的音轨识别对白，生成带时间戳的句子和可选的匿名说话人编号。Python 负责转写，Java 负责任务和素材版本，前端在「对话」Tab 用原视频时间线展示。本文是三端共同的实现契约。

## Python 与 Java

沿用 `POST /internal/v1/asset-extraction/tasks` 和 `GET /internal/v1/asset-extraction/tasks/{taskId}`。Java 业务类型 `DIALOGUE` 对应 Python 分支 `DIALOGUE`。输入视频引用相对于共享输入目录，`frameIntervalSeconds` 只影响人物分支。

```json
{
  "taskId": "dialogue-001",
  "sourceVideoRef": "uploads/episode-01.mp4",
  "outputs": ["DIALOGUE"]
}
```

首次提交返回 HTTP 202。任务幂等、路径校验和状态汇总沿用 [素材任务协议](java-asset-extraction-api.md)。同一次 Python 任务同时选择 `AUDIO` 和 `DIALOGUE` 时，共享一次原音轨解码；只有 `DIALOGUE` 时中间 WAV 可在任务结束后清理。音频产物准备好即可发布，语音识别失败时音频仍可读取。现有 Java 分类型执行器可先各自提交，独立 Python 任务之间的复用留待后续。

对白分支报告 `PREPARING_AUDIO`、`TRANSCRIBING_DIALOGUE`、`WRITING_RESULTS`，排队时阶段为 `null`。本轮分支的 `progress` 为 `null`，完成数量取 `result.segmentCount`。成功响应示例：

```json
{
  "taskId": "dialogue-001",
  "status": "SUCCEEDED",
  "branches": {
    "DIALOGUE": {
      "status": "SUCCEEDED",
      "stage": "WRITING_RESULTS",
      "progress": null,
      "result": {
        "manifestRef": "dialogue-001-dialogue-run1/dialogue.json",
        "segmentCount": 3
      },
      "error": null
    }
  }
}
```

| 情况 | 结果 |
| --- | --- |
| 视频没有音轨 | `FAILED`，`AUDIO_STREAM_NOT_FOUND` |
| 有音轨且成功分析，未检测到有效语音 | `SUCCEEDED`，`segments: []`、`segmentCount: 0` |
| 有有效转写，但部分或全部说话人未知 | `SUCCEEDED`，对应段的 `speakerId` 为 `null` |
| 准备音轨失败 | `DIALOGUE_AUDIO_PREP_FAILED` |
| 语音模型无法加载 | `DIALOGUE_MODEL_UNAVAILABLE` |
| 语音转写失败 | `DIALOGUE_TRANSCRIPTION_FAILED` |
| 执行超时 | `DIALOGUE_EXTRACTION_TIMEOUT` |
| 清单校验或发布失败 | `DIALOGUE_WRITE_FAILED` |
| Java 导入清单校验失败 | Java 步骤返回 `DIALOGUE_MANIFEST_INVALID` |

## 对白清单 v1

```json
{
  "schemaVersion": 1,
  "durationMs": 20000,
  "segments": [
    {
      "segmentIndex": 1,
      "startMs": 1200,
      "endMs": 3100,
      "text": "你怎么还在这？",
      "speakerId": "speaker-1"
    },
    {
      "segmentIndex": 2,
      "startMs": 5000,
      "endMs": 7200,
      "text": "我在等末班车。",
      "speakerId": "speaker-2"
    },
    {
      "segmentIndex": 3,
      "startMs": 7000,
      "endMs": 7550,
      "text": "来了。",
      "speakerId": null
    }
  ]
}
```

- `durationMs` 是源视频总时长。时间统一为相对**原视频播放起点**的整数毫秒，满足 `0 <= startMs < endMs <= durationMs`。显式处理音轨晚于视频起点和 VAD 分段偏移，避免遗漏或重复累加时间偏移。
- 按 `startMs`、`endMs` 升序输出，`segmentIndex` 从 1 连续递增。对白区间使用 `[startMs,endMs)`，允许留白和重叠，`text` 去除首尾空白后须非空。
- `speakerId` 取 `speaker-1`、`speaker-2` 等按已识别说话人首次出现顺序分配的编号，作用域限于本次执行。无法可靠分配时用 `null`。本轮按匿名编号展示，后续再接角色关联。
- `segmentCount` 等于 `segments.length`。`manifestRef` 相对于共享输出根目录。清单仅包含文本和时间，Python 校验后一次发布；Java 在导入前再次校验共享路径、版本、字段和数量。

## Java 与前端

| 用途 | 方法与完整网关路径 |
| --- | --- |
| 能力查询 | GET `/api/media/extraction-capabilities` |
| 发起对白提取 | POST `/api/ai/tasks`，请求体 `{"episodeId":"135","extractTypes":["DIALOGUE"]}` |
| 查询和重试步骤 | GET `/api/ai/tasks/{taskId}`、POST `/api/ai/tasks/{taskId}/retry` |
| 读取对白 | GET `/api/media/episodes/{episodeId}/assets?type=DIALOGUE&pageNum=1&pageSize=20`，可选 `sourceTaskId` |

沿用鉴权、响应包装和素材分页结构。`DIALOGUE` 的部署能力由现有 `EXTRACTION_SUPPORTED_TYPES` 控制，Python 可用后再在 Media 和 Java AI 同步开启。列表默认读取当前原视频最新的成功版本，失败重跑保留旧成功版本，最新成功的零条版本返回空列表。按 `segmentIndex` 排序，多页读取用首个正式条目的 `sourceTaskId` 固定版本。

`data.items[0]` 示例：

```json
{
  "projectId": "41",
  "episodeId": "135",
  "assetId": "816",
  "assetType": "DIALOGUE",
  "createdAt": "2026-11-20T07:00:15Z",
  "sourceTaskId": "923",
  "sizeBytes": 21,
  "expiresInSeconds": 0,
  "details": {
    "segmentIndex": 1,
    "startMs": 1200,
    "endMs": 3100,
    "text": "你怎么还在这？",
    "speakerId": "speaker-1",
    "characterId": null
  }
}
```

`assetId` 使用稳定的对白记录 ID 字符串，`sizeBytes` 为 UTF-8 文本字节数。对白直接返回文本，`expiresInSeconds` 固定为 0，`characterId` 首版为 `null`。前端用现有播放器的时长和时间事件绘制句段时间线，按原视频时间保留空白；点句段或台词跳到 `startMs`，同时高亮所有覆盖当前播放位置的句段。说话人可显示为「说话人 1」「说话人 2」和「未知说话人」。

## 真实联调与交接

三端按本文并行实现，先交付各自代码及必要的本端检查，开发时无需等待上游。用户通知后再集中做一次真实联调。示例 JSON 用于说明协议和独立自动测试。集中验收使用同一集真实带人声的视频，从模型识别走到浏览器播放器。浏览器访问 Java 网关，Java 调用真实 Python 服务并导入该次运行生成的文件。

| 交接 | 就绪条件 | 留下的核对信息 |
| --- | --- | --- |
| Python → Java | 已启动支持 `DIALOGUE` 的版本，真实模型权重可用，HTTP 已发布有效清单 | 实际服务地址、共享目录、源视频引用、执行 ID、`manifestRef`、句段数量 |
| Java → 前端 | 从 Java 发起真实任务，Python 完成识别，Java 已导入并能查询对白 | 网关地址、`episodeId`、主任务 ID、Python 执行 ID、`sourceTaskId`、实际句段时间 |
| 前端验收 | 浏览器从实际任务查询加载对白，点击一条句子能跳到同一集原视频的开始时间 | 真实剧集和任务 ID、素材查询结果、点击跳转和高亮的核对结果 |

上游未就绪时，各端可先完成代码和独立测试，记录具体阻塞并标为「待真实联调」。联调阶段直接检查真实接口与产物。测试数据、手工入库或页面内置台词仅能证明局部测试行为。遇到字段或路径不一致时先按本文核对，若必须改协议，由维护者在一处修订后通知三端同步。
