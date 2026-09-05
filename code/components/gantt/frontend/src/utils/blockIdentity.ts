// blockIdentity.ts — which ONE block does an edit target?
//
// Split MO pieces share one id (calendar_blocks.csv `cs_…;split`: 8 ids x
// 2-3 pieces on the live board), so "every block with this id" is the wrong
// unit for an edit: a typed change, drag or resize on one piece hit its
// siblings too, and remove-to-holding dropped the siblings without parking
// them. The stable unit is the PIECE — object identity when the caller holds
// the state object, else (id, start_hour): the same key the supply verdicts
// use (`${id}|${start_hour}`, contract 2026-09-01 §5).
//
// A bare id still resolves (every caller passed one before 2026-09-01) to the
// FIRST piece carrying it in list order. Documented fallback, not a promise —
// callers that hold the block should pass it. No React, no imports:
// tests/test_block_identity.py pins this module under node via the
// frontend's own tsc.

export interface BlockLike {
  id: string;
  start_hour: number;
  end_hour?: number;
}

/** How a caller names the block to edit: the state object itself (identity
 * wins), an {id, start_hour} pair, or a bare id (first piece). */
export type BlockRef = string | { id: string; start_hour: number };

export interface ResolveOpts {
  /** A resize keeps one edge fixed: with a bare id shared by several pieces,
   * prefer the piece whose start equals edges[0] or whose end equals
   * edges[1] (the fixed edge passes through the resize hook unchanged, so
   * exact equality is right). Falls back to the first piece. */
  edges?: [number, number];
}

export function blockKey(b: { id: string; start_hour: number }): string {
  return `${b.id}|${b.start_hour}`;
}

// --------------------------------------------------------------------------
// id minting (fix FE / audit writeback-8)
// --------------------------------------------------------------------------
//
// useScheduleState used to mint `blk_<n>` from a module-scope counter that
// started at 1 on every mount (page reload, cal_reset_gen bump) and was
// never seeded from the ids already on the board, so a second session
// re-minted blk_1, blk_2 ... and calendar_blocks.csv ended up with two
// unrelated rows per id (set_float_link then pinned BOTH and measured the
// gap off the wrong one). Ids are now collision-free by construction:
// millisecond time + 64 random bits, base36, and never one the caller says
// is taken. crypto.getRandomValues when the page has it (an http://LAN
// mount has no crypto.randomUUID), Math.random otherwise.

const B36 = "0123456789abcdefghijklmnopqrstuvwxyz";

function randomB36(n: number): string {
  const c = (globalThis as { crypto?: { getRandomValues?: (a: Uint32Array) => Uint32Array } }).crypto;
  let out = "";
  if (c && typeof c.getRandomValues === "function") {
    const buf = new Uint32Array(n);
    c.getRandomValues(buf);
    for (let i = 0; i < n; i++) out += B36[buf[i] % 36];
    return out;
  }
  for (let i = 0; i < n; i++) out += B36[Math.floor(Math.random() * 36)];
  return out;
}

/** A fresh block id: `<prefix>_<ms base36>_<13 random base36 chars>`, never
 * present in `taken`. Two ids minted in the same millisecond differ in the
 * random tail (36^13 ~ 1.7e20 values). */
export function mintBlockId(prefix = "blk", taken?: { has(id: string): boolean } | null): string {
  for (;;) {
    const id = `${prefix}_${Date.now().toString(36)}_${randomB36(13)}`;
    if (!taken || !taken.has(id)) return id;
  }
}

/** Ids shared by blocks that are NOT pieces of one run — the collision a
 * save must refuse. Split pieces of one MO legitimately share an id
 * (`cs_…;split`), so an id repeated across the same order_id / sku /
 * block_type is allowed; the same id on two different runs is not. */
export function findIdCollisions(
  blocks: readonly { id: string; order_id?: string; sku?: string; block_type?: string }[],
): string[] {
  const sig = new Map<string, string>();
  const bad = new Set<string>();
  for (const b of blocks) {
    const id = String(b.id ?? "");
    if (!id) continue;
    const s = `${b.order_id ?? ""}|${b.sku ?? ""}|${b.block_type ?? ""}`;
    const prev = sig.get(id);
    if (prev === undefined) sig.set(id, s);
    else if (prev !== s) bad.add(id);
  }
  return [...bad].sort();
}

/** Display id of a ref (action messages). */
export function refId(ref: BlockRef): string {
  return typeof ref === "string" ? ref : ref.id;
}

/** Same piece: the identical object, or the same (id, start_hour). */
export function sameBlock(a: BlockLike, b: BlockLike): boolean {
  return a === b || blockKey(a) === blockKey(b);
}

/** Index of the ONE block `ref` names in `list`, or -1. */
export function findBlockIndex<T extends BlockLike>(
  list: readonly T[], ref: BlockRef, opts?: ResolveOpts,
): number {
  if (typeof ref !== "string") {
    const byIdentity = list.indexOf(ref as T);
    if (byIdentity >= 0) return byIdentity;
    const key = blockKey(ref);
    return list.findIndex((b) => blockKey(b) === key);
  }
  const first = list.findIndex((b) => b.id === ref);
  if (first < 0 || !opts?.edges) return first;
  const [s, e] = opts.edges;
  const edge = list.findIndex((b) => b.id === ref && (b.start_hour === s || b.end_hour === e));
  return edge >= 0 ? edge : first;
}

export function findBlock<T extends BlockLike>(
  list: readonly T[], ref: BlockRef, opts?: ResolveOpts,
): T | undefined {
  const i = findBlockIndex(list, ref, opts);
  return i >= 0 ? list[i] : undefined;
}

/** Copy of `list` with the ONE block `ref` names replaced by `patch` (a
 * partial to spread, or a function of the old block). Returns `list` itself
 * when nothing matches so a state setter can bail out of the render. */
export function patchOne<T extends BlockLike>(
  list: T[], ref: BlockRef, patch: Partial<T> | ((b: T) => T), opts?: ResolveOpts,
): T[] {
  const i = findBlockIndex(list, ref, opts);
  if (i < 0) return list;
  const next = list.slice();
  next[i] = typeof patch === "function" ? patch(list[i]) : { ...list[i], ...patch };
  return next;
}

/** Copy of `list` without the ONE block `ref` names; `list` itself when
 * nothing matches. */
export function removeOne<T extends BlockLike>(list: T[], ref: BlockRef): T[] {
  const i = findBlockIndex(list, ref);
  if (i < 0) return list;
  return [...list.slice(0, i), ...list.slice(i + 1)];
}

/** Indices of the pieces `refs` name — one per ref, unresolved refs
 * skipped: the shift set of insert-between / CIP re-forecast. Two bare
 * copies of a shared id both land on the first piece (see header). */
export function resolveIndices<T extends BlockLike>(
  list: readonly T[], refs: readonly BlockRef[],
): Set<number> {
  const out = new Set<number>();
  for (const r of refs) {
    const i = findBlockIndex(list, r);
    if (i >= 0) out.add(i);
  }
  return out;
}
