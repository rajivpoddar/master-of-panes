/** Applies a REST `gh api` label call (POST adds, DELETE removes) to a label list. */
export function applyLabelsApi(args: string[], labels: string[]): string[] {
  const method = args[args.indexOf("-X") + 1];
  if (method === "POST") {
    const added = args.flatMap((arg, i) => (args[i - 1] === "-f" && arg.startsWith("labels[]=") ? [arg.slice(9)] : []));
    return [...new Set([...labels, ...added])];
  }
  if (method === "DELETE") {
    const name = decodeURIComponent(args.find((arg) => /\/labels\/.+/.test(arg))!.split("/labels/")[1]);
    return labels.filter((label) => label !== name);
  }
  throw new Error(`unexpected gh api invocation: ${args.join(" ")}`);
}
