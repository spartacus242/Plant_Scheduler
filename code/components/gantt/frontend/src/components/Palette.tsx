// Palette.tsx — Add production-adjacent and window blocks.

import React, { useState } from "react";
import type { BlockType, LineInfo } from "../types";

interface Props {
  lines: LineInfo[];
  cipDuration: number;
  onAddCip: (lineName: string, lineId: number, startHour: number, duration: number) => void;
  onAddTrial: (lineName: string, lineId: number, sku: string, startHour: number, duration: number) => void;
  onAddWindowBlock: (
    blockType: BlockType,
    lineName: string,
    lineId: number,
    startHour: number,
    duration: number,
    label: string,
  ) => void;
}

export const Palette: React.FC<Props> = ({
  lines, cipDuration, onAddCip, onAddTrial, onAddWindowBlock,
}) => {
  const [mode, setMode] = useState<string | null>(null);
  const [line, setLine] = useState(lines[0]?.line_name ?? "");
  const [start, setStart] = useState(0);
  const [duration, setDuration] = useState(6);
  const [label, setLabel] = useState("");
  const [sku, setSku] = useState("");

  const findLine = () => lines.find((l) => l.line_name === line);

  const submit = () => {
    const ln = findLine();
    if (!ln || !mode) return;
    if (mode === "cip") {
      onAddCip(line, ln.line_id, start, cipDuration);
    } else if (mode === "trial") {
      if (!sku) return;
      onAddTrial(line, ln.line_id, sku, start, duration);
    } else {
      onAddWindowBlock(mode as BlockType, line, ln.line_id, start, duration, label || mode);
    }
    setMode(null);
  };

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexWrap: "wrap", padding: "4px 0" }}>
      {(["cip", "trial", "maintenance", "contractor", "line_down"] as const).map((t) => (
        <button key={t} onClick={() => setMode(mode === t ? null : t)} style={btnStyle}>
          + {t === "line_down" ? "Line Down" : t.charAt(0).toUpperCase() + t.slice(1)}
        </button>
      ))}

      {mode && (
        <div style={formStyle}>
          <label style={lbl}>Line</label>
          <select value={line} onChange={(e) => setLine(e.target.value)} style={input}>
            {lines.map((l) => <option key={l.line_name} value={l.line_name}>{l.line_name}</option>)}
          </select>
          <label style={lbl}>Start Hour</label>
          <input type="number" value={start} min={0} onChange={(e) => setStart(+e.target.value)} style={input} />
          {mode !== "cip" && (
            <>
              <label style={lbl}>Duration (h)</label>
              <input type="number" value={duration} min={1} onChange={(e) => setDuration(+e.target.value)} style={input} />
            </>
          )}
          {mode === "trial" && (
            <>
              <label style={lbl}>SKU</label>
              <input value={sku} onChange={(e) => setSku(e.target.value)} placeholder="SKU" style={input} />
            </>
          )}
          {(mode === "maintenance" || mode === "contractor" || mode === "line_down") && (
            <>
              <label style={lbl}>Label</label>
              <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={mode} style={input} />
            </>
          )}
          <button onClick={submit} style={{ ...btnStyle, background: "#636EFA", color: "#fff" }}>Add</button>
        </div>
      )}
    </div>
  );
};

const btnStyle: React.CSSProperties = {
  padding: "6px 14px",
  border: "1px solid #ccc",
  borderRadius: 6,
  background: "#fff",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 600,
};

const formStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "center",
  padding: "6px 10px",
  border: "1px solid #e0e0e5",
  borderRadius: 6,
  background: "#fafafa",
  flexWrap: "wrap",
};

const lbl: React.CSSProperties = { fontSize: 11, color: "#666" };
const input: React.CSSProperties = { fontSize: 12, padding: "4px 6px", borderRadius: 4, border: "1px solid #ccc", width: 80 };
