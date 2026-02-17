// useContextMenu.ts — Right-click context menu state.

import { useState, useCallback } from "react";

export interface ContextMenuState {
  visible: boolean;
  x: number;
  y: number;
  blockId: string | null;
  blockType: string;
  startHour: number;
  endHour: number;
}

const INITIAL: ContextMenuState = {
  visible: false, x: 0, y: 0, blockId: null, blockType: "sku", startHour: 0, endHour: 0,
};

export function useContextMenu() {
  const [menu, setMenu] = useState<ContextMenuState>(INITIAL);

  const openMenu = useCallback(
    (x: number, y: number, blockId: string, blockType: string, startHour: number, endHour: number) => {
      setMenu({ visible: true, x, y, blockId, blockType, startHour, endHour });
    },
    [],
  );

  const closeMenu = useCallback(() => setMenu(INITIAL), []);

  return { menu, openMenu, closeMenu };
}
