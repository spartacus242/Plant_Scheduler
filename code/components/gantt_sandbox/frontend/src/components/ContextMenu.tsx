// ContextMenu.tsx — Right-click menu (split, remove, details).

import React from "react";
import type { ContextMenuState } from "../hooks/useContextMenu";

interface Props {
  menu: ContextMenuState;
  onSplit: (blockId: string, splitHour: number) => void;
  onRemove: (blockId: string) => void;
  onDetails: (blockId: string) => void;
  onClose: () => void;
  minRunHours: number;
}

const itemStyle: React.CSSProperties = {
  padding: "6px 16px",
  cursor: "pointer",
  fontSize: 13,
  borderBottom: "1px solid #f0f0f0",
};

export const ContextMenu: React.FC<Props> = ({
  menu, onSplit, onRemove, onDetails, onClose, minRunHours,
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
          position: "fixed",
          left: menu.x,
          top: menu.y,
          background: "#fff",
          border: "1px solid #ccc",
          borderRadius: 6,
          boxShadow: "0 4px 12px rgba(0,0,0,0.12)",
          zIndex: 1000,
          minWidth: 160,
          overflow: "hidden",
        }}
      >
        {canSplit && (
          <div
            style={itemStyle}
            onClick={() => { onSplit(menu.blockId!, midpoint); onClose(); }}
            onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "#f0f4ff"; }}
            onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
          >
            ✂ Split at h{midpoint}
          </div>
        )}
        <div
          style={itemStyle}
          onClick={() => { onRemove(menu.blockId!); onClose(); }}
          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "#fff0f0"; }}
          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
        >
          🗑 Remove to holding
        </div>
        <div
          style={{ ...itemStyle, borderBottom: "none" }}
          onClick={() => { onDetails(menu.blockId!); onClose(); }}
          onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "#f0f4ff"; }}
          onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
        >
          ℹ Details
        </div>
      </div>
    </>
  );
};
