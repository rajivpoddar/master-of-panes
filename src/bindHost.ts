// F8 (Rajiv approved, DM D0AMF0XE6TS thread 1792765422.348619): the MoP HTTP
// server listens on loopback only. MOP_BIND_HOST is the explicit override
// (not MOP_HOST, which clients use as a full URL).
export const DEFAULT_BIND_HOST = "127.0.0.1";

export function resolveBindHost(env: NodeJS.ProcessEnv = process.env): string {
  const v = env.MOP_BIND_HOST?.trim();
  return v ? v : DEFAULT_BIND_HOST;
}
