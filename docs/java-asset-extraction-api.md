# 并行素材提取任务

使用统一的素材任务接口按需提取人物或音轨。人物路线复用现有抽帧、检测、分割及清单发布逻辑。音频路线直接读取同一源视频，导出第一条音轨为 16 位 PCM WAV，保留采样率和声道数。两个分支可以并行运行，并各自发布文件。

启动、模型配置及共享目录沿用 [Java 人物提取对接文档](java-character-extraction-api.md)。新旧人物任务共用一个人物执行线程，音频使用独立的执行线程。服务仍按单进程部署。

## 提交和查询

提交返回 HTTP 202。以下示例假设源视频已写入共享输入根目录。

```bash
curl --fail-with-body -X POST http://127.0.0.1:19000/internal/v1/asset-extraction/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"episode-assets-001","sourceVideoRef":"uploads/episode-01.mp4","outputs":["CHARACTERS","AUDIO"],"frameIntervalSeconds":2}'

curl --fail-with-body http://127.0.0.1:19000/internal/v1/asset-extraction/tasks/episode-assets-001
```

`outputs` 至少包含一个分支，可选 `CHARACTERS` 和 `AUDIO`。只传 `AUDIO` 时只导出音轨。人物抽帧间隔可省略，服务使用 `CHARACTER_FRAME_INTERVAL_SECONDS`。源视频引用相对于共享输入根目录。提交同 ID、同源文件引用、同分支集合和同生效间隔时返回已有快照。分支顺序不影响幂等判断。音频单分支任务的抽帧间隔不参与判断。有效参数冲突返回 `409 TASK_CONFLICT`。非法源引用返回 `422 SOURCE_VIDEO_REF_INVALID`，源文件缺失或任务不存在返回 `404` 及对应错误码。

查询响应示例。音频完成后即可读取文件，整单仍在运行。

```json
{
  "taskId": "episode-assets-001",
  "status": "RUNNING",
  "branches": {
    "AUDIO": {
      "status": "SUCCEEDED",
      "stage": "WRITING_RESULTS",
      "progress": null,
      "result": {"audioRef": "episode-assets-001-audio-<执行标识>/audio.wav"},
      "error": null
    },
    "CHARACTERS": {
      "status": "RUNNING",
      "stage": "DETECTING_CHARACTERS",
      "progress": {"frameCount": 60, "detectedCharacterCount": null},
      "result": null,
      "error": null
    }
  }
}
```

| 整单状态 | 含义 |
| --- | --- |
| `QUEUED` | 所有分支在排队 |
| `RUNNING` | 已有分支开始或完成，仍有分支未结束 |
| `SUCCEEDED` | 所有分支成功 |
| `PARTIALLY_SUCCEEDED` | 部分分支失败，成功分支的结果可用 |
| `FAILED` | 所有分支失败 |

人物成功结果包含 `manifestRef` 和 `characterAssetCount`，其清单格式沿用 [人物契约](java-character-extraction-api.md)。音频成功结果包含 `audioRef`。这两个引用都相对于共享输出根目录。音频分支无音轨时报告 `AUDIO_STREAM_NOT_FOUND`，执行超时为 `AUDIO_EXTRACTION_TIMEOUT`，处理失败为 `AUDIO_EXTRACTION_FAILED`，发布失败为 `AUDIO_WRITE_FAILED`。分支无法排队时报告 `BRANCH_QUEUE_FAILED`。

失败分支可用新的任务 ID 单独补跑。查询和幂等状态位于进程内，重启后清空。已发布的结果可通过之前保存的引用读取。素材接口与原人物接口各自维护任务 ID，单个业务任务应通过其中一个接口提交。

当前 Java 和前端仍在使用原人物接口。对白识别和画面 OCR 是后续可接入的流程。
