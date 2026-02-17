// useDragHandlers.ts — @dnd-kit onDragEnd logic.

import { useCallback } from "react";
import type { DragEndEvent } from "@dnd-kit/core";
import type { ScheduleBlock, SandboxArgs } from "../types";
import type { ScheduleStateActions } from "./useScheduleState";
import { isCapable, recalcDuration, findOverlapsOnLine } from "../utils/validation";
import { snapToHour } from "../utils/layout";

export function useDragHandlers(
  args: SandboxArgs | null,
  schedule: ScheduleBlock[],
  cipWindows: ScheduleBlock[],
  actions: ScheduleStateActions,
  hourWidth: number,
  viewStart: number,
) {
  const caps = args?.capabilities ?? {};
  const lines = args?.lines ?? [];

  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over, delta } = event;
      if (!over || !active) return;

      const blockId = active.id as string;
      const block =
        schedule.find((b) => b.id === blockId) ??
        cipWindows.find((b) => b.id === blockId);
      if (!block) return;

      // Calculate horizontal displacement in hours
      const deltaHours = snapToHour(delta.x / hourWidth);

      // Determine target line from droppable ID (format: "line_LINENAME")
      const targetLineStr = (over.id as string).replace("line_", "");
      const targetLine = lines.find((l) => l.line_name === targetLineStr);
      const sameLine = targetLineStr === block.line_name;

      if (sameLine) {
        // Horizontal move only
        const newStart = snapToHour(block.start_hour + deltaHours);
        if (newStart < 0) return;
        const newEnd = newStart + block.run_hours;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, block.line_name, block.id, newStart, newEnd)) return;
        actions.moveBlock(block.id, block.line_name, block.line_id, newStart, block.run_hours);
      } else if (targetLine) {
        // Cross-line move
        if (block.block_type !== "cip" && !isCapable(targetLine.line_name, block.sku, caps)) return;

        let dur = block.run_hours;
        if (block.block_type !== "cip") {
          const newDur = recalcDuration(block, targetLine.line_name, caps);
          if (newDur === null) return;
          dur = newDur;
        }

        const newStart = snapToHour(block.start_hour + deltaHours);
        if (newStart < 0) return;
        const newEnd = newStart + dur;
        const allBlocks = [...schedule, ...cipWindows];
        if (findOverlapsOnLine(allBlocks, targetLine.line_name, block.id, newStart, newEnd)) return;
        actions.moveBlock(block.id, targetLine.line_name, targetLine.line_id, newStart, dur);
      }
    },
    [schedule, cipWindows, actions, hourWidth, viewStart, caps, lines],
  );

  return { onDragEnd };
}
