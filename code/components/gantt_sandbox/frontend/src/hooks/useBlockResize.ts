// useBlockResize.ts — Pointer-based edge resize on block edges.

import { useState, useCallback, useRef } from "react";
import { snapToHour } from "../utils/layout";

export interface ResizeState {
  blockId: string | null;
  edge: "left" | "right" | null;
  previewStart: number;
  previewEnd: number;
}

export function useBlockResize(
  minRunHours: number,
  onCommit: (id: string, newStart: number, newEnd: number) => void,
) {
  const [resizing, setResizing] = useState<ResizeState>({
    blockId: null, edge: null, previewStart: 0, previewEnd: 0,
  });
  const origRef = useRef<{ start: number; end: number }>({ start: 0, end: 0 });
  const startXRef = useRef(0);
  const hourWidthRef = useRef(6);

  const startResize = useCallback(
    (
      blockId: string,
      edge: "left" | "right",
      startHour: number,
      endHour: number,
      clientX: number,
      hourWidth: number,
    ) => {
      origRef.current = { start: startHour, end: endHour };
      startXRef.current = clientX;
      hourWidthRef.current = hourWidth;
      setResizing({ blockId, edge, previewStart: startHour, previewEnd: endHour });

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
        setResizing({ blockId, edge, previewStart: newStart, previewEnd: newEnd });
      };

      const onUp = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        setResizing((cur) => {
          if (cur.blockId) onCommit(cur.blockId, cur.previewStart, cur.previewEnd);
          return { blockId: null, edge: null, previewStart: 0, previewEnd: 0 };
        });
      };

      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    },
    [minRunHours, onCommit],
  );

  return { resizing, startResize };
}
