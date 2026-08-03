// index.tsx — Entry point: listens for Streamlit render messages, renders GanttSandbox.

import React, { useState, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { GanttSandbox } from "./GanttSandbox";
import { onStreamlitRender, sendReady, setFrameHeight } from "./streamlit";
import type { SandboxArgs } from "./types";

// Extend types to include the render message shape
interface RenderMessage {
  type: string;
  args?: Record<string, unknown>;
}

const App: React.FC = () => {
  const [args, setArgs] = useState<SandboxArgs | null>(null);

  useEffect(() => {
    onStreamlitRender((msg: { type: string; args?: Record<string, unknown> }) => {
      if (msg.args) {
        setArgs(msg.args as unknown as SandboxArgs);
      }
    });
    sendReady();
    setFrameHeight(200); // initial height until component renders
  }, []);

  if (!args) {
    return (
      <div style={{ padding: 20, textAlign: "center", color: "#888" }}>
        Waiting for schedule data from Streamlit...
      </div>
    );
  }

  return <GanttSandbox args={args} />;
};

const root = createRoot(document.getElementById("root")!);
root.render(<App />);
