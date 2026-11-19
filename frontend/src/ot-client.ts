import DiffMatchPatch from "diff-match-patch";
export type Edit = { pos: number; delete: number; insert: string };

// Convert between JavaScript UTF-16 and Python's Unicode code-point positions.
export function makeEdit(before: string, after: string): Edit | null {
  const a = Array.from(before),
    b = Array.from(after);
  let prefix = 0;
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix])
    prefix++;
  let suffix = 0;
  while (
    suffix < a.length - prefix &&
    suffix < b.length - prefix &&
    a[a.length - suffix - 1] === b[b.length - suffix - 1]
  )
    suffix++;
  if (prefix === a.length && prefix === b.length) return null;
  return {
    pos: prefix,
    delete: a.length - prefix - suffix,
    insert: b.slice(prefix, b.length - suffix).join(""),
  };
}

const patcher = new DiffMatchPatch();
export function rebaseDraft(
  base: string,
  edited: string,
  newBase: string,
): string {
  if (base === edited) return newBase;
  const patches = patcher.patch_make(base, edited);
  const [result, applied] = patcher.patch_apply(patches, newBase);
  if (!applied.every(Boolean))
    throw new Error(
      "Could not merge pending edits; copied draft may need manual reconciliation",
    );
  return result;
}
