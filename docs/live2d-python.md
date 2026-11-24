# 图片转 Live2D · Python 与 Java 联调

## 启动与配置

从 `video-asset-ai` 根目录启动。日常联调继续用 `make dev`，端口为 19000。使用本机拆层时也可运行下面的命令。Pillow 与人物提取共用已锁定版本，第三方转换器使用独立环境。

```bash
uv sync --locked --extra characters --extra live2d
uv run --locked --extra characters --extra live2d uvicorn app.main:app --host 127.0.0.1 --port 19000 --workers 1
```

| 环境变量 | 默认值 | 用法 |
| --- | --- | --- |
| `LIVE2D_INPUT_ROOT` | `samples` | Java 写入原图，Python 读取 |
| `LIVE2D_OUTPUT_ROOT` | `outputs/live2d-tasks` | Python 发布模型，Java 读取 ZIP |
| `LIVE2D_CONVERTER_ARGV` | `[]` | 受信任的转换命令前缀，JSON 字符串数组 |
| `LIVE2D_MANIFEST_GLOB` | `**/*.model3.json` | 在转换器输出目录匹配唯一最终入口 |
| `LIVE2D_CORE_LIBRARY` | 未配置 | 本机架构对应的 Cubism Native Core 动态库绝对路径 |
| `LIVE2D_WEB_CORE_JS` | 未配置 | 可用于替代 Native Core 的官方 Web Core 脚本绝对路径 |
| `LIVE2D_NODE_PATH` | `node` | 执行 Web Core 的 Node 可执行文件，仅在配置 Web Core 时使用 |
| `LIVE2D_TIMEOUT_SECONDS` | `1800` | 转换子进程超时秒数 |

本机和同级 Java 项目联调时，将 Python 的 `LIVE2D_INPUT_ROOT` 和
`LIVE2D_OUTPUT_ROOT` 设为 Java 仓库下 `.local/live2d-input` 与 `.local/live2d-output` 的绝对路径。
修改 Python `.env` 后重启 Python 服务。

执行器将 `--image <绝对路径> --output <尚不存在的目录>` 追加到 argv 后，以无界面模式启动，并提供 `LIVE2D_MODEL_NAME` 环境变量。包装器可用该值作为显示名，文件路径由生成工具确定。Java 请求不接受命令、权重路径或第三方凭据。供应商密钥及运行参数通过启动进程的环境变量注入。两套共享目录在容器或不同主机上可有不同的绝对路径，双方以相对引用读取同一存储。

下例展示候选 `image-to-live2d` 的命令映射，完整运行条件及实测阻塞见后文。

```bash
export LIVE2D_CONVERTER_ARGV='["/absolute/tool-venv/bin/python","/absolute/image-to-live2d/live2d_pipeline.py","build"]'
export LIVE2D_MANIFEST_GLOB='body-motion/model/*.model3.json'
export LIVE2D_CORE_LIBRARY='/absolute/path/libLive2DCubismCore.dylib'
```


### image2live2d 适配器

已验证的本机路线用现有人物模型 Grounding DINO 和 SAM 2 拆出可见部件，随后用 image2live2d 无界面绑定并导出。实测环境为 M5 MacBook Air 16 GB、Python 3.11.18、MPS 和 Node 20.20.2。输入优先用单人、正面或近正面图，有透明通道时沿用角色轮廓。基础拆层处理可见的脸、头发、躯干及模型检测到的五官。无头发角色需要能识别出独立头部，机器人样图已验证可导出头部、躯干和双眼。遮挡后的内容、完整肢体切分及精细表情需要另行验证。

```bash
uv venv --python 3.11 outputs/image2live2d-env
uv pip install --python outputs/image2live2d-env/bin/python \
  'image2live2d @ git+https://github.com/Wzhang3912/image2live2d.git@714e7cc9191f1ef6c9a1732c80e8b566ec127d6b' \
  'pillow<14' numpy psd-tools
uv sync --locked --extra characters --extra live2d
export CHARACTER_DEVICE=mps
export LIVE2D_CONVERTER_ARGV='["/absolute/video-asset-ai/outputs/image2live2d-env/bin/python","-m","scripts.convert_image2live2d"]'
export IMAGE2LIVE2D_LAYER_SPLITTER_ARGV='["/absolute/video-asset-ai/.venv/bin/python","-m","scripts.split_live2d_layers"]'
export LIVE2D_WEB_CORE_JS='/absolute/path/live2dcubismcore.min.js'
export LIVE2D_NODE_PATH='/absolute/path/to/node'
```

可改用现有的 See-through 服务作为拆层入口。此时清除 `IMAGE2LIVE2D_LAYER_SPLITTER_ARGV` 并设置 `IMAGE2LIVE2D_DECOMPOSE_URL`，需要鉴权时从部署密钥存储注入 `IMAGE2LIVE2D_DECOMPOSE_TOKEN`。服务端运行在配置权重的 CUDA 主机。两个拆层入口选其一。新部署可用 `LIVE2D_CORE_LIBRARY` 配置 Native Core。两个 Core 都配置时优先使用可用的 Native 库。Web Core 已在 Node v20.20.2 实测，读取官方 SDK 的未修改脚本，项目自身不分发 SDK。 [See-through 拆层服务协议](https://github.com/Wzhang3912/image2live2d/tree/714e7cc9191f1ef6c9a1732c80e8b566ec127d6b/service/seethrough) · [Cubism Web SDK](https://www.live2d.com/en/sdk/download/web/)

## 独立 CLI

```bash
uv run --locked --extra characters --extra live2d python -m scripts.generate_live2d \
  --input outputs/live2d-eval/inputs/see-through-example.png \
  --output outputs/live2d-eval/example-model

uv run --locked --extra characters --extra live2d python -m scripts.generate_live2d \
  --input outputs/live2d-eval/inputs/last-bus-character.png \
  --output outputs/live2d-eval/character-model --name '末班车角色'
```

输出目录须为新目录。成功时保留入口、MOC3、PNG 和入口声明文件的相对路径，同时生成 `model.zip`。CLI 记录每个阶段及总耗时。仅有 PSD、引用缺失或Cubism Core 加载失败时退出非零。

## Java 请求与响应

```bash
curl -i http://127.0.0.1:19000/internal/v1/live2d/tasks \
  -H 'Content-Type: application/json' \
  -d '{"taskId":"live2d-001","projectId":42,"modelName":"末班车角色","sourceImageRef":"uploads/actor.png"}'
curl -i http://127.0.0.1:19000/internal/v1/live2d/tasks/live2d-001
```

- `taskId` 使用 1–128 位 ASCII 字母、数字、下划线或短横线，以字母或数字开头。 `projectId` 接受 JSON 整数或同格式字符串，`modelName` 为 1–255 字符非空白显示名。
- `sourceImageRef` 相对 `LIVE2D_INPUT_ROOT`，用 `/` 分隔。支持可解码 PNG、JPEG 和 WebP，拒绝 URL、目录穿越及指向共享目录外的符号链接。Java 在提交前写完整图片，并保持源文件可读直至任务终态。
- POST 首次和同参数重复提交均为 HTTP 202。并发重复提交只排队一次。相同 ID 的字段发生变化返回 HTTP 409 `TASK_CONFLICT`。
- GET 返回 HTTP 200，包括终态失败。任务不存在返回 HTTP 404 `TASK_NOT_FOUND`。请求结构不合法返回 HTTP 422 `REQUEST_INVALID`，非法图片引用返回 HTTP 422 `SOURCE_IMAGE_REF_INVALID`，图片缺失返回 HTTP 404 `SOURCE_IMAGE_NOT_FOUND`。HTTP 错误为 `{"detail":{"code":"…","message":"…"}}`。

```json
{"taskId":"live2d-001","status":"QUEUED","stage":null,"result":null,"error":null}
{"taskId":"live2d-001","status":"RUNNING","stage":"GENERATING","result":null,"error":null}
{"taskId":"live2d-real-last-bus-character","status":"SUCCEEDED","stage":"EXPORTING","result":{"manifestPath":"model.model3.json","artifactRef":"live2d-real-last-bus-character-c539bdcfce264575abb565f74de84e26/model.zip"},"error":null}
{"taskId":"live2d-001","status":"FAILED","stage":"GENERATING","result":null,"error":{"code":"CONVERTER_UNAVAILABLE","message":"尚未配置图片转 Live2D 转换器","stage":"GENERATING"}}
```

`PREPARING` 解码原图，`GENERATING` 调用转换器。转换器作为黑盒执行，Python 阶段合并为 `GENERATING`，第三方阶段细节进入服务日志。 `VALIDATING` 检查入口、可读 PNG、依赖和 MOC3。 `EXPORTING` 校验并原子发布 ZIP。任务失败码包括 `SOURCE_IMAGE_INVALID`、`IMAGE_DECODER_UNAVAILABLE`、`CONVERTER_UNAVAILABLE`、`CONVERTER_TIMEOUT`、`CONVERSION_FAILED`、`RUNTIME_NOT_EXPORTED`、`RUNTIME_INVALID`、`CUBISM_CORE_UNAVAILABLE`、`MOC3_VALIDATION_FAILED`、`ARTIFACT_WRITE_FAILED`、`LIVE2D_TASK_FAILED`。排队期间丢失的图片使用 `SOURCE_IMAGE_NOT_FOUND`。

Java 用 `LIVE2D_OUTPUT_ROOT / artifactRef` 读 ZIP，用 `manifestPath` 找 ZIP 内入口。入口 `FileReferences` 以入口所在目录为基准，导入或解压时保持相对目录结构。成功回包示例展示契约，实测 ZIP 与状态见验收段落。React 负责验证 Web 渲染和具体可动参数。

沿用单进程部署，Live2D 有独立的单线程执行器。进程重启会清空内存状态和幂等记录，已发布 ZIP 留在原路径。Java 应保存成功返回的引用。重启后重投会使用新的输出目录，可按 Java 业务状态决定是否重投或清理旧包。

## 候选工具验证

验证机为 Apple M5 MacBook Air（arm64、16 GB），Python 服务与两个候选的基础环境使用 Python 3.11.18。另用独立 Python 3.12.13 环境测试 See-through 的 MPS 路线。指定的两款候选均有命令行入口。两个仓库检出的源码均未附输入角色成图，示例采用共同拆层依赖 See-through 自带的 `common/assets/test_image.png`，第二张为本地《末班车之后》人物素材裁出的近正面图，源时间 52 秒，图像仅在本机做本次验证。

| 候选 | 拆层与绑定条件 | 许可证与完整导出 |
| --- | --- | --- |
| Lv2D-png-to-model `fc4437e` | 安装独立环境的 `requirements.txt`。本机拆层使用 See-through 的固定版本和 MPS 补丁，云端拆层也可使用 Gradio。语义拆分与 RigKit 绑定需要对齐素材、部件/网格和参数配置。上游另有 Windows 的 PSD2Live 路线 | 项目为 PolyForm Noncommercial 1.0.0，第三方 RigKit、导出器、PSD2Live 各自带许可证。源码存在 `moc3`、贴图、入口和 `cdi3` 的导出路径，需要 Core 实测验证。[项目源码](https://github.com/Bani61/Lv2D-png-to-model/tree/fc4437e) |
| image-to-live2d `3c32db1` | 安装独立环境的 `requirements.txt`。完整 `build` 需要视觉规划/图像编辑服务，配置 `PLANNER_MODEL`、`IMAGE_MODEL`、`LLM_API_KEY` 等。以 SSH 在有权重的 Linux CUDA 机器执行 See-through，使用 JDK 21、Live2D Agent Kit、锁定的 PSD2Live `5526f2e` 和 Cubism Java Core/JNI 绑定导出 | 主代码 MIT，PSD2Live Kotlin 扩展为 GPL-3.0-only，Cubism SDK 和权重各有条款。源码存在生成 `body-motion/model/MotionCharacter.model3.json` 的导出路径，尚无本机端到端产物。[项目源码](https://github.com/lvhaojie456/image-to-live2d/tree/3c32db1) |

另测了 **Wzhang3912/image2live2d** `714e7cc`，主代码 Apache-2.0。基础 CLI 用内置分层样例生成 .inp 和贴图/JSON，但未生成 MOC3。它自带 Web 后端的 MOC3 导出在样例上报 `deform_head_turn` 未定义。本仓库适配器复用其分层读取、含变形器的绑定和底层二进制导出 API，随后用官方 Web Core 成功加载导出的 MOC3。适配器可以接已部署的 See-through 服务，也可以使用本机现有人物模型完成基础拆层。 [项目源码](https://github.com/Wzhang3912/image2live2d/tree/714e7cc9191f1ef6c9a1732c80e8b566ec127d6b)

Cubism 校验在独立子进程调用官方 Native 或 Web Core，检查 MOC 一致性、恢复 MOC、初始化模型并检查可绘制部件数量。网页渲染和可动参数由 React 联调确认。Haru 用于播放器联调。 [官方 Core 校验说明](https://docs.live2d.com/en/cubism-sdk-manual/moc3-consistency/) · [Cubism Native SDK](https://www.live2d.com/en/sdk/download/native/)

### 本机整图到运行时验收

用 `scripts.generate_live2d` 先跑 See-through 自带示例，再跑《末班车之后》角色图。两个输入也通过启动在 19129 的测试服务实际提交、轮询并返回 `SUCCEEDED`。验收使用真实 Grounding DINO、SAM 2、image2live2d 和 Web Core。重复提交返回已有状态，ZIP 引用检查和 Core 加载通过。Core 记录的是模型声明的参数，网页中实际能动的参数由 React 联调确认。

| 样图 | 可见分层 | CLI 总耗时 | CLI 拆层 / 绑定 / 导出 | HTTP 总耗时 | Core 加载结果 |
| --- | ---: | ---: | --- | ---: | --- |
| 上游示例 | 7 | 9.68 秒 | 7.87 / 1.25 / 0.22 秒 | 8.47 秒 | 12 个可绘制部件、18 个参数 |
| 《末班车之后》 | 9 | 6.32 秒 | 5.70 / 0.23 / 0.11 秒 | 7.87 秒 | 14 个可绘制部件、20 个参数 |

实测文件均位于本地 `outputs/live2d-eval/`。两个 ZIP 均含 `model.model3.json`、`model.moc3`、`textures/atlas.png`、`model.cdi3.json`、`model.physics3.json`。

- 上游示例 HTTP ZIP 为 `http-real-output/live2d-real-see-through-example-2382099209ba40159e4fcc41bb642623/model.zip`。
- 项目角色 HTTP ZIP 为 `http-real-output/live2d-real-last-bus-character-c539bdcfce264575abb565f74de84e26/model.zip`。
- CLI ZIP 在 `real-cli/see-through-example/model.zip` 和 `real-cli/last-bus-character/model.zip`。独立拆层保存在 `attempts/grounded-see-through-example/` 与 `attempts/grounded-last-bus-character/`。
- CLI 阶段日志在 `attempts/real-cli-*.log`，HTTP 状态快照在 `attempts/http-real-*.json`，服务阶段日志在 `attempts/http-real-service.log`。

### 上游命令和实测记录

以下命令在 `video-asset-ai` 根目录运行。候选仓库及其独立环境的位置用环境变量指定。

```bash
eval_root="$PWD/outputs/live2d-eval"
for input in see-through-example last-bus-character; do
  SEETHROUGH_DIR="$SEE_THROUGH_MPS_DIR" SEETHROUGH_PYTHON="$SEE_THROUGH_PYTHON" \
    "$RIGKIT_PYTHON" "$RIGKIT_DIR/src/run_pipeline.py" seethrough fetch \
    "$eval_root/inputs/$input.png" "$eval_root/attempts/lv2d-$input" --site local --res 768
  "$PIPELINE_PYTHON" "$PIPELINE_DIR/live2d_pipeline.py" build \
    --image "$eval_root/inputs/$input.png" --output "$eval_root/attempts/image-to-live2d-$input"
done
```

- `image-to-live2d build` 在示例和项目图上分别执行，均在规划配置检查退出，原因为缺少 `PLANNER_MODEL`，未调用远端 CUDA 或供应商 API。首轮两个进程各约 21 秒。参见本地 `outputs/live2d-eval/attempts/image-to-live2d-*.log`。
- `Lv2D-png-to-model doctor` 找到基础库，未找到 Cubism Core。基础 Python 环境执行拆层时两次均在导入 torch 阶段退出。另在 See-through `df019de` 应用上游 MPS 补丁，安装 Torch 2.8.0，并确认 MPS 可用、CUDA 不可用。原版运行进入权重下载，在本机替代路线完成实测后结束下载实验，未产生 PSD。参见 `attempts/lv2d-*.log` 和 `attempts/mps-see-through-example.log`。
- 用官方 Web Core 5.1.0 实测绑定后的 MOC3，内置样例得到 30 个可绘制部件、40 个参数。绑定约 0.52 秒，导出约 0.50 秒。参见 `outputs/live2d-eval/attempts/alternative/adapter-runtime/`。
- 真实 HTTP 成功验证使用内置样例的分层 PSD 模拟拆层服务的响应。Python 使用真实 image2live2d 绑定、导出、官方 Web Core 校验及 ZIP 发布，返回 `SUCCEEDED`。校验得到 31 个可绘制部件、43 个参数，任务约 14.39 秒。Java 文件引用可按 `outputs/live2d-eval/attempts/alternative/http-verified-state.json` 核对。ZIP 位于 `outputs/live2d-eval/http-fixture-output/live2d-layer-fixture-65984fecbb1046f3bea9317a1f810364/model.zip`。
- 《末班车之后》角色图走真实 HTTP 请求，任务由 `QUEUED` 到 `RUNNING` 后以 `FAILED / CONVERSION_FAILED` 结束，候选工具日志为缺少 `PLANNER_MODEL`。再次提交返回已有失败状态，未发布 ZIP。参见 `outputs/live2d-eval/attempts/http-state.json` 及 `http-service.log`。
- 两张输入也从项目独立 CLI 跑过，使用 image-to-live2d 在规划检查退出，耗时分别为 0.81 秒和 0.46 秒。日志为 `outputs/live2d-eval/attempts/cli-*.log`。使用内置预拆层 PSD 模拟拆层响应时，独立 CLI 跑通，约 15.54 秒，ZIP 位于 `outputs/live2d-eval/attempts/alternative/verified-cli/model.zip`。

可单独复验真实绑定和导出，使用候选内置分层样例。

```bash
eval_root="$PWD/outputs/live2d-eval"
converter_python="$eval_root/altern-env/bin/python"
sample_dir="$eval_root/attempts/alternative"
"$converter_python" -m image2live2d --sample "$sample_dir/repro-layers" --fullbody \
  -o "$sample_dir/repro-sample.inp"
"$converter_python" -m scripts.convert_image2live2d --layers "$sample_dir/repro-layers" \
  --output "$sample_dir/repro-runtime"
"${LIVE2D_NODE_PATH:-node}" app/live2d/web_core.cjs "$LIVE2D_WEB_CORE_JS" \
  "$sample_dir/repro-runtime/model.moc3"
```

原版 See-through 的实际拆层、未配置的远端 CUDA 方案、Native Core、React 渲染和网页中具体可动参数仍待验证。
