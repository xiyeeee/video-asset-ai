# video-asset-ai

漫剧视频素材处理服务，与 Java 后端 `video-asset-server` 同级。本轮提供 HTTP 健康检查和本地视频抽帧 CLI。后续由 Java 创建业务 `taskId`，再接入 Python 素材提取任务接口。

## macOS 环境准备

```bash
brew install uv
brew install ffmpeg
uv sync
uv run python --version
ffmpeg -version
ffprobe -version
```

项目固定使用 Python 3.11。FFmpeg 和 FFprobe 默认通过 `PATH` 查找。使用自定义安装路径时，可复制 `.env.example` 为 `.env`，设置 `FFMPEG_PATH` 和 `FFPROBE_PATH`，也可通过同名环境变量配置。

## HTTP 服务

在项目根目录运行：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另开一个终端请求健康检查：

```bash
curl --fail http://127.0.0.1:8000/health
```

两个工具均能执行时，响应如下。任一工具缺失或运行失败时，`status` 为 `degraded`，对应工具字段为 `false`，HTTP 仍返回 200。

```json
{"status":"ok","ffmpegAvailable":true,"ffprobeAvailable":true}
```

## 本地抽帧

把样片放在 `samples/demo.mp4`，然后运行：

```bash
uv run python -m scripts.extract_frames \
  --input ./samples/demo.mp4 \
  --output ./outputs/demo \
  --interval-seconds 2
```

也可以将 `--input` 指向前端现有的样片 `../video-asset-web/public/mock/demo.mp4`。输出包括 JPEG 截图和 `frames.json`。输出路径可为新目录或空目录，含有文件时 CLI 会退出并保留已有内容。

以下是前端样片的实际结果。时长使用秒，分辨率使用像素。`path` 相对于 `frames.json` 所在目录。`timestampMs` 是截图原始帧相对文件播放起点的显示时间，单位为毫秒，可以有小数。每个采样区间保留第一张实际帧，空区间跳过。

```json
{
  "durationSeconds": 5.055,
  "width": 960,
  "height": 540,
  "frames": [
    {"path": "frame-000001.jpg", "timestampMs": 0.0},
    {"path": "frame-000002.jpg", "timestampMs": 2002.0},
    {"path": "frame-000003.jpg", "timestampMs": 4004.0}
  ]
}
```

## 检查

```bash
uv run ruff check .
uv run pytest
```
