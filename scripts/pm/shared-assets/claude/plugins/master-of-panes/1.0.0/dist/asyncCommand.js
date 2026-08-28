import { exec } from "node:child_process";
export function execShell(command, options = {}) {
    return new Promise((resolve, reject) => {
        exec(command, {
            timeout: options.timeout,
            maxBuffer: options.maxBuffer,
            cwd: options.cwd,
            env: options.env,
        }, (error, stdout, stderr) => {
            if (error) {
                reject(error);
                return;
            }
            resolve({ stdout, stderr });
        });
    });
}
export async function execShellOk(command, options = {}) {
    try {
        await execShell(command, options);
        return true;
    }
    catch {
        return false;
    }
}
export function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
//# sourceMappingURL=asyncCommand.js.map