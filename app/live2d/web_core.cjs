// 在独立 Node 进程中加载运维配置的官方 Cubism Web Core。
const fs = require("node:fs");
const path = require("node:path");

async function validate(corePath, mocPath) {
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
