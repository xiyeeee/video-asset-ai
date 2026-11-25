// 在独立 Node 进程中加载运维配置的官方 Cubism Web Core。
const fs = require("node:fs");
const path = require("node:path");

function inspectParameters(model) {
    const { parameters, drawables } = model;
    const pose = () => drawables.vertexPositions.map((vertices, index) =>
        [...vertices, drawables.opacities[index]]);
    model.update();
    const baseline = pose();
    return parameters.ids.map((id, index) => {
        const min = parameters.minimumValues[index];
        const max = parameters.maximumValues[index];
        const defaultValue = parameters.defaultValues[index];
        // 有些模型声明了可调参数，但参数没有实际绑定到可绘制部件。
        const movesToward = (endpoint) => [(endpoint + defaultValue) / 2, endpoint].some(value => {
            if (value === defaultValue) return false;
            parameters.values[index] = value;
            model.update();
            return drawables.vertexPositions.some((vertices, part) =>
                Math.abs(drawables.opacities[part] - baseline[part][vertices.length]) > 1e-5
                || vertices.some((coordinate, axis) => Math.abs(coordinate - baseline[part][axis]) > 1e-5));
        });
        const activeDecreasing = movesToward(min);
        const activeIncreasing = movesToward(max);
        parameters.values[index] = defaultValue;
        return { id, min, max, defaultValue, active: activeDecreasing || activeIncreasing,
            activeDecreasing, activeIncreasing };
    });
}

async function validate(corePath, mocPath, mode) {
    const script = fs.readFileSync(corePath, "utf8");
    const loadCore = new Function(
        "module", "exports", "require", "__filename", "__dirname",
        script + "\nreturn Live2DCubismCore;"
    );
    const coreModule = { exports: {} };
    const core = loadCore(coreModule, coreModule.exports, require, corePath, path.dirname(corePath));
    // Core 初始化 WASM 是异步的，公开 API 就绪后再加载模型。
    for (let attempt = 0; ; attempt++) {
        try {
            core.Version.csmGetVersion();
            break;
        } catch (error) {
            if (attempt === 100) {
                throw error;
            }
            await new Promise((resolve) => setTimeout(resolve, 25));
        }
    }

    const bytes = fs.readFileSync(mocPath);
    const data = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    const moc = core.Moc.fromArrayBuffer(data);
    if (!moc) {
        throw new Error("Cubism Core 无法加载 MOC3");
    }
    try {
        if (!moc.hasMocConsistency(data)) {
            throw new Error("Cubism Core 一致性检查失败");
        }
        const model = core.Model.fromMoc(moc);
        if (!model) {
            throw new Error("Cubism Core 无法初始化模型");
        }
        try {
            if (model.drawables.count < 1) {
                throw new Error("模型没有可绘制部件");
            }
            if (mode === "--parameters") {
                console.log(JSON.stringify({
                    mocVersion: bytes[4],
                    mocSha256: require("node:crypto").createHash("sha256").update(bytes).digest("hex"),
                    parameters: inspectParameters(model),
                }));
                return;
            }
            console.log(`drawables=${model.drawables.count} parameters=${model.parameters.count}`);
        } finally {
            model.release();
        }
    } finally {
        moc._release();
    }
}

validate(...process.argv.slice(2)).catch((error) => {
    console.error(error.stack);
    process.exitCode = 1;
});
