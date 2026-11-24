你负责「视频转素材」项目现有的 Python AI 服务。本轮交付图片转 Live2D 的技术验证和任务接口。先阅读仓库的 AGENTS.md、开发规范、任务模块和文件存储实现，复用现有工程与依赖管理。\
\
一、先证明转换工具能用\
\
1\. 评估 Lv2D-png-to-model、image-to-live2d 等候选工具。核对实际安装方式、许可证、拆层与绑定各自的运行依赖、是否支持无界面运行，以及能否导出完整的 Cubism 运行时文件。第三方工具的 README 只能作为候选依据，实际测试结果才算通过。\
2\. 先用工具自带的输入示例跑一次，再用一张清晰、接近正面的角色半身图跑一次。开发机是 M5 MacBook Air 16 GB。如果拆层或绑定依赖 NVIDIA CUDA、Windows 或外部服务，记录各步骤的真实运行环境，把该步骤放到匹配的执行环境。第三方 API 密钥通过环境变量读取。\
3\. 先提供一个可独立运行的命令行入口，输入图片路径和输出目录。输出包含 .model3.json、有效的 .moc3、贴图及入口实际引用的其他文件。保留目录结构和相对路径。记录每个步骤的耗时和实际产物。\
\
二、接入现有任务流程\
\
1\. 通过现有机制接受 Java 提交的任务。输入包含 taskId、projectId、modelName、sourceImageRef。sourceImageRef 使用现有服务间可访问的图片引用。\
2\. 异步执行生成，保证可以按 taskId 查询状态。重复提交同一个 taskId 时复用已存在的任务。\
3\. 与 Java 使用同一套业务字段：\
&#x20;  \- status：QUEUED、RUNNING、SUCCEEDED、FAILED。\
&#x20;  \- stage：可选，使用实际能观测到的阶段，例如 PREPARING、DECOMPOSING、RIGGING、EXPORTING、VALIDATING。工具只提供一个黑盒调用时，如实报告正在生成。\
&#x20;  \- 成功返回 result.manifestPath 和 result.artifactRef。manifestPath 是模型包内 .model3.json 的相对路径，artifactRef 是 Java 可读取的产物引用。\
&#x20;  \- 失败返回 error.code、error.message，可选 error.stage。详细堆栈和第三方响应放服务端日志。\
4\. 校验入口 JSON、MOC3、贴图和声明的依赖文件齐全，下载包内部路径有效。保存最终产物后才返回 SUCCEEDED。模型能否在网页渲染、有哪些参数实际可动，由 React 端联调确认。\
\
三、验收与交付\
\
\- 给出示例图和实际角色图的执行命令、结果与产物位置。\
\- 如果工具只能出分层 PSD、缺少绑定依赖，或无法导出可加载的运行时文件，将任务标为失败并指出卡在哪一步。Haru 只用于播放器联调。\
\- 本轮聚焦图片转模型。把提示词生成动作留给后续单独接入。\
\- 将本轮开发提示词原样保留到当前仓库 docs/prompts/live2d-python.md，并输出启动、配置和联调说明。