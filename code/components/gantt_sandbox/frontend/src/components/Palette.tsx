// Palette.tsx — "+ CIP" / "+ Trial" buttons and mini forms.

import React, { useState } from "react";
import type { LineInfo } from "../types";

interface Props {
  lines: LineInfo[];
  cipDuration: number;
  onAddCip: (lineName: string, lineId: number, startHour: number, duration: number) => void;
  onAddTrial: (lineName: string, lineId: number, sku: string, startHour: number, duration: number) => void;
}

export const Palette: React.FC<Props> = ({ lines, cipDuration, onAddCip, onAddTrial }) => {
  const [showCipForm, setShowCipForm] = useState(false);
  const [showTrialForm, setShowTrialForm] = useState(false);
  const [cipLine, setCipLine] = useState(lines[0]?.line_name ?? "");
  const [cipStart, setCipStart] = useState(0);
  const [trialLine, setTrialLine] = useState(lines[0]?.line_name ?? "");
  const [trialSku, setTrialSku] = useState("");
  const [trialStart, setTrialStart] = useState(0);
  const [trialDuration, setTrialDuration] = useState(8);

  const handleAddCip = () => {
    const line = lines.find((l) => l.line_name === cipLine);
    if (line) onAddCip(cipLine, line.line_id, cipStart, cipDuration);
    setShowCipForm(false);
  };

  const handleAddTrial = () => {
    const line = lines.find((l) => l.line_name === trialLine);
    if (line && trialSku) onAddTrial(trialLine, line.line_id, trialSku, trialStart, trialDuration);
    setShowTrialForm(false);
  };

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexWrap: "wrap", padding: "4px 0" }}>
      <button onClick={() => setShowCipForm(!showCipForm)} style={btnStyle}>
        + CIP
      </button>
      <button onClick={() => setShowTrialForm(!showTrialForm)} style={btnStyle}>
        + Trial
      </button>

      {showCipForm && (
        <div style={formStyle}>
          <label style={lbl}>Line</label>
          <select value={cipLine} onChange={(e) => setCipLine(e.target.value)} style={input}>
            {lines.map((l) => <option key={l.line_name} value={l.line_name}>{l.line_name}</option>)}
          </select>
          <label style={lbl}>Start Hour</label>
          <input type="number" value={cipStart} min={0} onChange={(e) => setCipStart(+e.target.value)} style={input} />
          <button onClick={handleAddCip} style={{ ...btnStyle, background: "#636EFA", color: "#fff" }}>Add</button>
        </div>
      )}

      {showTrialForm && (
        <div style={formStyle}>
          <label style={lbl}>Line</label>
          <select value={trialLine} onChange={(e) => setTrialLine(e.target.value)} style={input}>
            {lines.map((l) => <option key={l.line_name} value={l.line_name}>{l.line_name}</option>)}
          </select>
          <label style={lbl}>SKU</label>
          <input value={trialSku} onChange={(e) => setTrialSku(e.target.value)} placeholder="e.g. 280573" style={input} />
          <label style={lbl}>Start Hour</label>
          <input type="number" value={trialStart} min={0} onChange={(e) => setTrialStart(+e.target.value)} style={input} />
          <label style={lbl}>Duration (h)</label>
          <input type="number" value={trialDuration} min={1} onChange={(e) => setTrialDuration(+e.target.value)} style={input} />
          <button onClick={handleAddTrial} style={{ ...btnStyle, background: "#D4A017", color: "#fff" }}>Add</button>
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
