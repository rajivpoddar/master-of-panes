import { exec } from "node:child_process";

export interface ExecShellOptions {
  timeout?: number;
  maxBuffer?: number;
  cwd?: string;
  env?: NodeJS.ProcessEnv;
}

export interface ExecShellResult {
  stdout: string;
  stderr: string;
}

export function execShell(command: string, options: ExecShellOptions = {}): Promise<ExecShellResult> {
  return new Promise((resolve, reject) => {
    exec(
      command,
      {
        timeout: options.timeout,
        maxBuffer: options.maxBuffer,
        cwd: options.cwd,
        env: options.env,
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(error);
          return;
        }
        resolve({ stdout, stderr });
      },
    );
  });
}

export async function execShellOk(command: string, options: ExecShellOptions = {}): Promise<boolean> {
  try {
    await execShell(command, options);
    return true;
  } catch {
    return false;
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
