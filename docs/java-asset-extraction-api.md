# 并行素材提取任务

使用统一的素材任务接口按需提取人物、音轨、分镜或对白。人物路线复用现有抽帧、检测、分割及清单发布逻辑。音频路线直接读取同一源视频，导出第一条音轨为 16 位 PCM WAV，保留采样率和声道数。分镜路线扫描源视频并为每个镜头生成一张 JPEG。各分支可以并行运行，并独立发布文件。

启动、模型配置及共享目录沿用 [Java 人物提取对接文档](java-character-extraction-api.md)。新旧人物任务共用一个人物执行线程，音频和分镜各使用一个独立执行线程。服务仍按单进程部署。

## 提交和查询

提交返回 HTTP 202。以下示例假设源视频已写入共享输入根目录。

```bash
curl --fail-with-body -X POST http://127.0.0.1:19000/internal/v1/asset-extraction/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"episode-assets-001","sourceVideoRef":"uploads/episode-01.mp4","outputs":["CHARACTERS","AUDIO"],"frameIntervalSeconds":2}'

curl --fail-with-body http://127.0.0.1:19000/internal/v1/asset-extraction/tasks/episode-assets-001
```

`outputs` 至少包含一个分支，可选 `CHARACTERS`、`AUDIO`、`SHOTS` 和 `DIALOGUE`。只传 `AUDIO` 时只导出音轨。人物抽帧间隔可省略，服务使用 `CHARACTER_FRAME_INTERVAL_SECONDS`。源视频引用相对于共享输入根目录。提交同 ID、同源文件引用、同分支集合和同生效间隔时返回已有快照。分支顺序不影响幂等判断。只在包含人物分支时比较有效抽帧间隔。有效参数冲突返回 `409 TASK_CONFLICT`。非法源引用返回 `422 SOURCE_VIDEO_REF_INVALID`，源文件缺失或任务不存在返回 `404` 及对应错误码。

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

## 分镜扩展

分镜成功返回 `manifestRef` 和 `shotCount`，字段、阶段、错误码和相对路径见 [分镜提取契约](shot-extraction-contract.md)。

- `SHOT_SCENE_THRESHOLD` 默认为 `10`，提高阈值会减少候选切点。
- `SHOT_IGNORE_BOTTOM_FRACTION` 默认为 `0.2`，检测时排除底部字幕区域。设为 `0` 可扫描整帧。代表图始终保留完整画面。
- 首版识别明显硬切，按画面统计过滤孤立近白帧。慢速叠化、长闪白或切换仅发生在裁掉区域时需要用实际片源校准。已验证 FFmpeg 9.0.2，批量抽取代表帧使用从文件加载过滤器的 `-/filter:v` 选项。

本地验收按共享目录设置启动服务。以下请求使用实际保存在共享输入目录的视频引用：

```bash
curl --fail-with-body -X POST http://127.0.0.1:19000/internal/v1/asset-extraction/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"last-bus-shots","sourceVideoRef":"java-character-5.mp4","outputs":["SHOTS"]}'

curl --fail-with-body http://127.0.0.1:19000/internal/v1/asset-extraction/tasks/last-bus-shots
```

《末班车之后》120 秒实测获得 10 个镜头和 10 张 JPEG，切点位于 10 至 90 秒，每 10 秒一个。90–120 秒是连续镜头，100 秒和 110 秒的字幕变化经过目视复核后排除。经 FFprobe 核对代表帧时间，`SHOTS` 单独执行及与 `AUDIO` 并行执行均成功。

## 对白扩展

对白任务、时间轴、匿名说话人和素材查询字段见 [对白提取三端契约](dialogue-extraction-contract.md)。

在项目根目录安装依赖并启动支持人物和对白的服务：

```bash
uv sync --locked --extra characters --extra dialogue
uv run --locked --extra characters --extra dialogue uvicorn app.main:app --host 127.0.0.1 --port 19000 --workers 1
```

对白在独立的单线程中用 CPU 推理，首次执行时加载模型。以下配置接受 FunASR 的 ModelScope 模型名或本地权重目录。

| 配置 | 默认值 |
| --- | --- |
| `DIALOGUE_ASR_MODEL` | `paraformer-zh` |
| `DIALOGUE_VAD_MODEL` | `fsmn-vad` |
| `DIALOGUE_PUNCTUATION_MODEL` | `ct-punc` |
| `DIALOGUE_SPEAKER_MODEL` | `cam++`，配置空字符串时只转写 |

同一任务选择 `AUDIO` 和 `DIALOGUE` 时，音频分支先发布 WAV，对白复用这份音频。对白清单对齐原视频播放时间；说话人识别不可用时继续生成带时间戳的台词。Python 3.11.18、Apple Silicon 已检查依赖导入和本地任务行为，真实权重转写与三端联调待集中验收。
