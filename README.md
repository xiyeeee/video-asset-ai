# video-asset-ai

漫剧视频素材处理服务，与 Java 后端 `video-asset-server` 同级。项目提供 HTTP 健康检查、本地视频抽帧、单镜头人物提取 CLI 和异步人物提取接口。Java 提交及轮询任务的契约见 [Java 对接文档](docs/java-character-extraction-api.md)。人物、音轨和分镜可通过 [并行素材提取接口](docs/java-asset-extraction-api.md) 按需提交。

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
make dev
```

默认监听 `127.0.0.1:19000`，以单 worker 启动，并由 uv 按锁文件同步人物和对白依赖。

另开一个终端请求健康检查：

```bash
curl --fail http://127.0.0.1:19000/health
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

## 单镜头人物提取

安装人物提取依赖，先用上一节命令生成 `frames.json`，再选取清单中列出的截图：

```bash
uv sync --extra characters

uv run --extra characters python -m scripts.extract_characters \
  --frames ./outputs/demo/frames.json \
  --frame frame-000001.jpg \
  --frame frame-000002.jpg \
  --output ./outputs/demo-characters
```

指定一次 `--frame` 可处理一张截图。多次指定时按参数顺序处理，路径必须与 `frames.json` 的 `path` 一致。Grounding DINO 使用 `person.` 检测人物，`--box-threshold` 默认是 `0.25`，可按样片调节。每张截图生成一张带框、编号和分数的 `detections.jpg`。SAM 2 为每个人物框生成一张与原截图同尺寸的透明 PNG。输出目录需为空或尚不存在，整批处理成功后发布 `characters.json`。

清单顶层 `framesManifest` 相对输出目录指向原始抽帧清单，`frames` 包含所有选中截图的 `sourceFrame`、`timestampMs`、`debugImage`，包含零检测的截图。`characters` 在同样的来源信息上补充 `characterId`、`box`、`score`、`pngPath`。`sourceFrame` 相对原始抽帧清单，`debugImage` 和 `pngPath` 相对输出目录。`box` 是原截图像素坐标 `[left, top, right, bottom]`，右、下边界不含在框内。每张截图的编号从 1 开始，时间戳沿用抽帧清单中的毫秒数，包括原有小数。

### 模型与设备

通过 `.env` 或环境变量配置以下选项。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GROUNDING_DINO_MODEL` | `IDEA-Research/grounding-dino-tiny` | 默认权重修订版 `a2bb814dd30d776dcf7e30523b00659f4f141c71` |
| `SAM2_MODEL` | `facebook/sam2.1-hiera-tiny` | 默认权重修订版 `de431c4043854a71d8101e17995dfe596bf101a5` |
| `CHARACTER_DEVICE` | `cpu` | 可设为 `mps` 以使用兼容的 Apple Silicon GPU |

模型路径也可指向 Hugging Face 格式的本地目录，其中应包含模型配置、预处理器配置及 `model.safetensors` 等文件。默认公开权重首次运行会通过 Hugging Face 下载，之后可使用 `HF_HUB_OFFLINE=1` 从缓存加载。模型与样片均存放在 Git 忽略的目录或用户缓存中。

实际运行环境为 Apple M5（arm64，macOS 26.5）、Python 3.11.18、PyTorch 2.15.0、TorchVision 0.29.2、Transformers 5.23.0、Pillow 12.3.0。CPU 完成两帧真实权重推理，MPS 完成清晰同框帧真实推理。SAM 2.1 官方检查点同时包含图像和视频权重，Transformers 加载图像子模型时会提示配置类型差异，本次图像分割完成。

### 本地样片验收

使用 Blender Foundation 的真实二维动画短片 [《Hero》，CC BY 4.0](https://commons.wikimedia.org/wiki/File:HERO_-_Blender_Open_Movie-full_movie.webm)。调试图增加了检测框，PNG 按原画面分割。项目自有漫剧样片缺失，业务片源上的漏检、合并框和相邻人物残留仍需验收。

将影片放在 `samples/blender-hero.webm` 后，可复现 CPU 验收：

```bash
uv run --extra characters python -m scripts.extract_frames \
  --input samples/blender-hero.webm --output outputs/hero-frames --interval-seconds 1
uv run --extra characters python -m scripts.extract_characters \
  --frames outputs/hero-frames/frames.json \
  --frame frame-000092.jpg --frame frame-000128.jpg \
  --output outputs/hero-character-validation
```

- 清晰双人同框（`91007 ms`）输出 2 框和 2 张独立 PNG。未见漏框、合并框或明显邻人残片，闪电高光处的手部轮廓有缺损。
- 轻度相互遮挡（`127002 ms`）输出 2 框和 2 张独立 PNG。未见漏框、合并框或明显邻人残片。两个人的 PNG 都从同一帧取色，各自的 Alpha 保留可见区域。

实际产物位于 `outputs/hero-character-validation/`。两个截图目录分别为 `frame-000001/` 和 `frame-000002/`，各含 `detections.jpg`、`person-001.png`、`person-002.png`。根目录包含 `characters.json`。

## 检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run --extra characters pytest
```

## 图片转 Live2D

`make dev` 启动后可以提交和轮询 Live2D 任务。转换 CLI、执行环境、真实验证记录及 Java 文件引用约定见 [图片转 Live2D 联调文档](docs/live2d-python.md)。
