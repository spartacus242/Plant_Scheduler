// GanttSandbox.tsx — Placeholder for the custom Streamlit v2 React component.
//
// This file will be the main entry point for the drag-and-drop Gantt
// scheduler sandbox.  It will use gantt-task-react for rendering and
// native HTML5 DnD for block interactions.
//
// To implement:
//   1. npm install in this directory
//   2. Wire up @streamlit/component-v2-lib for bidirectional communication
//   3. Render gantt-task-react with tasks mapped from SandboxData
//   4. Handle onDateChange / onTaskMove to update state
//   5. Implement holding area panel and palette for CIP/trial drops
//   6. Compute KPIs client-side for instant feedback
//   7. Call setStateValue() to push changes back to Python
//
// See types.ts for the data contracts.

import React from "react";

const GanttSandbox: React.FC = () => {
  return (
    <div style={{ padding: "1rem", border: "1px dashed #ccc", borderRadius: 8 }}>
      <p>
        <strong>Gantt Sandbox Component</strong> — React component not yet
        built. Using Streamlit native sandbox (pages/sandbox.py) in the
        meantime.
      </p>
    </div>
  );
};

export default GanttSandbox;
