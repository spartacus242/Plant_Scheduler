// useBlockResize.ts — Pointer-based edge resize on block edges.
//
// The hook carries the BLOCK (a BlockRef: the state object, or {id,
// start_hour}) from pointer-down to commit, never a bare id: split MO
// pieces share one id, and a resize committed by id alone landed on the
// first piece (2026-09-01 split-piece follow-through).

import { useState, useCallback, useRef } from "react";
import { snapToHour } from "../utils/layout";
import { blockKey, type BlockRef } from "../utils/blockIdentity";

export interface ResizeState {
  /** The piece being resized (identity/pair), null when idle. */
  block: BlockRef | null;
  /** blockIdentity.blockKey of that piece — what the chart compares. */
  key: string | null;
  edge: "left" | "right" | null;
  previewStart: number;
  previewEnd: number;
}

const IDLE: ResizeState = { block: null, key: null, edge: null, previewStart: 0, previewEnd: 0 };

export function useBlockResize(
  minRunHours: number,
  onCommit: (block: BlockRef, newStart: number, newEnd: number) => void,
) {
  const [resizing, setResizing] = useState<ResizeState>(IDLE);
  const origRef = useRef<{ start: number; end: number }>({ start: 0, end: 0 });
  const startXRef = useRef(0);
  const hourWidthRef = useRef(6);

  const startResize = useCallback(
    (
      block: { id: string; start_hour: number },
      edge: "left" | "right",
      startHour: number,
      endHour: number,
      clientX: number,
      hourWidth: number,
    ) => {
      origRef.current = { start: startHour, end: endHour };
      startXRef.current = clientX;
      hourWidthRef.current = hourWidth;
      const key = blockKey(block);
      setResizing({ block, key, edge, previewStart: startHour, previewEnd: endHour });

      const onMove = (e: PointerEvent) => {
        const dx = e.clientX - startXRef.current;
        const dh = snapToHour(dx / hourWidthRef.current);
        let newStart = origRef.current.start;
        let newEnd = origRef.current.end;
        if (edge === "left") {
          newStart = origRef.current.start + dh;
          if (newEnd - newStart < minRunHours) newStart = newEnd - minRunHours;
          if (newStart < 0) newStart = 0;
        } else {
          newEnd = origRef.current.end + dh;
          if (newEnd - newStart < minRunHours) newEnd = newStart + minRunHours;
        }
        setResizing({ block, key, edge, previewStart: newStart, previewEnd: newEnd });
      };

      const onUp = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        setResizing((cur) => {
          if (cur.block) onCommit(cur.block, cur.previewStart, cur.previewEnd);
          return IDLE;
        });
      };

      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    },
    [minRunHours, onCommit],
  );

  return { resizing, startResize };
}
