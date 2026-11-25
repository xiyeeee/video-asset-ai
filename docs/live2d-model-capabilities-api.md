# Live2D 模型能力分析与动作模板解析

Java 动作资产、版本映射和兼容性业务接口见 [Live2D 动作资产 API](../../video-asset-server/docs/live2d-motion-assets-api.md)。

## 调用与资源

Python 现有服务通过 `make dev` 启动，开发端口 19000。两个接口均为同步、只读调用，沿用现有内部路由和模型资产读取方式。重复调用相同版本可以重算，接口不创建任务或改写共享资源。已有组合动作编译的异步任务和幂等规则继续有效。

- `POST /internal/v1/live2d/model-capabilities/analyze` 返回模型能力分析。
- `POST /internal/v1/live2d/motion-templates/resolve` 解析一份已保存的动作定义和模型绑定。
- Java 校验项目权限，选择模型版本和已确认的绑定版本，再传可信文件引用。共享根目录沿用 `LIVE2D_OUTPUT_ROOT`。已有工作台绑定和模板仍由 Java 持久化。
- `modelManifestRef` 指向共享根目录内的 `.model3.json`，包内 MOC3、DisplayInfo、动作和表情文件保持清单中的相对位置。可传 `modelMocSha256`，摘要不符返回 HTTP 409 `MODEL_VERSION_MISMATCH`。
- `runtimeParameterRef` 是可选的共享目录相对路径。当前进程无法运行 Cubism Core 时，读取现有 Core 导出的 `mocVersion`、`mocSha256` 和 `parameters`，并核对 MOC3 摘要。使用现有 `LIVE2D_WEB_CORE_JS` 和 `LIVE2D_NODE_PATH` 配置。
- 对包内动作与表情，`artifactRef` 相对于 `LIVE2D_OUTPUT_ROOT`。对包外已保存的动作，确认映射须同时提供 `assetRef` 与 `assetSha256`。返回经核验的文件类型、摘要、目标参数和可用的原生时长。

## 模型分析

```json
{
  "modelVersionId": "current",
  "modelManifestRef": "current/model.model3.json"
}
```

响应包含 `modelVersionId`、`bindingVersionId`、`assetFingerprint`、`modelMocSha256`、`parameterInventoryComplete`、`elapsedMs`、`diagnostics`，以及 `parameters`、`assets`、`parts` 和九类 `capabilities`。指纹根据此次读取的模型入口、MOC3、包内参数元数据和相关动作/表情文件的路径与摘要计算，可用于核对后续解析使用的资产快照。

| 能力组 | ID | 首版语义 |
| --- | --- | --- |
| 视线 | `gaze` | `gaze-horizontal`、`gaze-vertical` |
| 眼皮 | `eyelids` | `eyelid-left`、`eyelid-right`、`eyelid-detail` |
| 眉毛 | `brows` | `brow-left`、`brow-right` |
| 嘴 | `mouth` | `mouth-open`、`mouth-form`、`mouth-corner-left`、`mouth-corner-right`、`mouth-purse` |
| 脸颊 | `cheeks` | `blush`、`cheek-detail` |
| 头 | `head` | `head-yaw`、`head-pitch`、`head-roll` |
| 肩颈 | `shoulders-neck` | `shoulders`、`neck` |
| 呼吸躯干 | `breathing-torso` | `breathing`、`torso` |
| 手部道具 | `hands-props` | `arms`、`hand-shape`、`prop` |

子能力包含 `status`、候选参数和动作资产、相关部件 `partIds`、`evidence`、`reason`、`missingItems` 及已传入的 `binding`。工作台应根据子能力展示可用项和缺项，组状态用于列表概览。

| 状态 | 显示建议 |
| --- | --- |
| `CONFIRMED` | 使用 Java 传来的工作台确认映射，目标能从当前资产核验 |
| `CANDIDATE` | 有名称、元数据或曲线线索，等待核对实际语义和播放 |
| `UNKNOWN` | 参数清单不足、只识别到部件，或已保存映射无法核对 |
| `MISSING` | 已读取完整参数清单，当前资产中未找到相关有效绑定或动作 |

参数的 `min`、`max`、`defaultValue` 与实际驱动信息仅在有依据时填值。Runtime 的 `active` 只说明在默认姿态附近观察到网格或不透明度变化。检查嘴角能否单独移动、能否抿嘴、红晕是否符合预期仍需要按对应语义确认。找不到 Runtime、导出清单摘要不符、动作引用无效或清单不完整时会附带诊断。

## 模板解析

请求沿用模型分析的文件引用，在 `bindingVersionId` 中指定 Java 持久化的绑定版本。每条绑定包含 `semanticId`，并指定 `parameterId` 或 `assetRef` 之一。参数方向 `direction` 支持 `1` 和 `-1`。

```json
{
  "modelVersionId": "web-sample",
  "modelManifestRef": "web-sample/model.model3.json",
  "bindingVersionId": "glance-reverse",
  "bindings": [
    {"semanticId": "gaze-horizontal", "parameterId": "ParamEyeBallX", "direction": -1}
  ],
  "templateId": "side-glance",
  "durationMs": 1200,
  "tracks": [
    {
      "semanticId": "gaze-horizontal",
      "startMs": 100,
      "durationMs": 650,
      "startValue": 0,
      "endValue": 0.8,
      "amplitude": 0.75,
      "easing": "EASE_OUT"
    }
  ]
}
```

- 时间用整数毫秒，轨道须落在总时长内。参数值归一化在 `[-1, 1]`，幅度在 `[0, 1]`。乘以方向和幅度后，负半轴从默认值映射至最小值，正半轴从默认值映射至最大值，支持非对称范围。
- 支持 `LINEAR`、`EASE_IN`、`EASE_OUT`、`EASE_IN_OUT`。回包保留缓动和实际起止值，前端按区间插值。慢眨或轻点头可以用先去后回的两个连续轨道表示。
- 对资产轨保留定位及持续时间，并返回带摘要的 `asset`。文件使用自带曲线，`startValue`、`endValue`、额外幅度和缓动交由发布该动作时确定。资产原生时长通过 `asset.durationMs` 单独返回。
- 可用 `expectedAssetFingerprint` 比对能力分析后的资产快照。版本或摘要不一致时返回 HTTP 409。模型路径不安全为 HTTP 422，入口不存在为 HTTP 404。
- 解析成功返回 `READY`。存在未确认映射、方向不可驱动、范围缺失或多个轨道同时写同一参数时，返回 HTTP 200 和 `UNRESOLVED`，附诊断且不给可播放轨道。

实测反向「侧瞟」返回 `parameterId: "ParamEyeBallX"`、`startValue: 0`、`endValue: -0.6000000000000001`、`easing: "EASE_OUT"`。同模型同模板切换正向确认版本，结束值为 `0.6000000000000001`。

## 验收

本地真实请求、响应及运行日志位于 `outputs/live2d-capability-acceptance/`。

- `current-response.json` 来自都市术士模型，列出 20 个参数、一个实际编译的说话动作，以及九类能力。嘴部开合和通用嘴形为候选，单侧嘴角和抿嘴缺失。
- `limited-response.json` 来自现有缺少嘴部和手部绑定的模型，两个能力组均报告缺项。
- `glance-forward-response.json`、`glance-reverse-response.json` 来自项目网页示例模型，使用同一个真实 `ParamEyeBallX`。
- `runtime-fallback-response.json` 使用实际 Runtime 导出的清单补齐参数。相同模型在 `metadata-only-response.json` 标记清单不完整。

## 与现有 Java 动作版本衔接

现有 Java 组合编译客户端仍可使用 `/internal/v1/live2d/motion-capabilities` 的简单能力结果。九类报告和绑定解析使用本页的新接口。对于 Java 已保存的 `MotionDefinition.tracks[].keyframes`，调用方可将相邻两帧转换成一个模板区间，起始值和结束值沿用关键帧。原 `INCREASING`、`DECREASING` 和 `BOTH` 的模板约定，分别转换成正值、负值、有符号值，再应用已确认绑定的实际方向。

已有 `blink` 可展开为左右眼皮两条轨道，`smile` 对应 `mouth-form`，`nod` 对应 `head-pitch`。现有 `AUDIO_RMS_TO_MOUTH` 继续走音频口型或组合动作编译入口。Java 和前端接入本页的新接口后可联调九类展示、工作台绑定保存及模板预览。
