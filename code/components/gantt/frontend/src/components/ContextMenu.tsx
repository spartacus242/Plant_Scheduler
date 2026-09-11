// ContextMenu.tsx — Right-click menu (split, remove, details).

import React from "react";
import type { ContextMenuState } from "../hooks/useContextMenu";
import { hourToStamp } from "../utils/layout";
import { POPOVER, T } from "../utils/theme";

interface Props {
  menu: ContextMenuState;
  onSplit: (blockId: string, splitHour: number) => void;
  onRemove: (blockId: string) => void;
  onDetails: (blockId: string) => void;
  onClose: () => void;
  minRunHours: number;
  /** Planning anchor, so the split point reads as a wall-clock moment. */
  anchor: Date;
}

const itemStyle: React.CSSProperties = {
  padding: "7px 16px",
  cursor: "pointer",
  fontSize: 13,
  borderBottom: `1px solid ${T.surface2}`,
  color: T.ink,
};

export const ContextMenu: React.FC<Props> = ({
  menu, onSplit, onRemove, onDetails, onClose, minRunHours, anchor,
}) => {
  if (!menu.visible || !menu.blockId) return null;

  const canSplit = (menu.endHour - menu.startHour) >= minRunHours * 2;
  const midpoint = Math.round((menu.startHour + menu.endHour) / 2);

  return (
    <>
      {/* Backdrop to close menu on click */}
      <div
        style={{ position: "fixed", inset: 0, zIndex: 999 }}
        onClick={onClose}
        onContextMenu={(e) => { e.preventDefault(); onClose(); }}
      />
      <div
        style={{
          ...POPOVER,
          left: menu.x,
          top: menu.y,
          borderRadius: 8,
          minWidth: 180,
          overflow: "hidden",
          padding: 0,
        }}
      >
        {canSplit && (
          <div
            style={itemStyle}
            onClick={() => { onSplit(menu.blockId!, midpoint); onClose(); }}
            onMouseEnter={(e) => { (e.target as HTMLElement).style.background = T.accentSoft; }}
            onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
          >
            ✂ Split at {hourToStamp(midpoint, anchor)}
          </div>
        )}
        <div
          style={itemStyle}
          onClick={() => { onRemove(menu.blockId!); onClose(); }}
          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = T.badSoft; }}
          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
        >
          🗑 Remove to holding
        </div>
        <div
          style={{ ...itemStyle, borderBottom: "none" }}
          onClick={() => { onDetails(menu.blockId!); onClose(); }}
          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = T.accentSoft; }}
          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
        >
          ℹ Details
        </div>
      </div>
    </>
  );
};
