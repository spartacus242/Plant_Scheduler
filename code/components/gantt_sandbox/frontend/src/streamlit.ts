// streamlit.ts — Streamlit v1 component communication helpers.
// Based on the official streamlit-component-lib protocol.
// Key: every outgoing message MUST include `isStreamlitMessage: true`.

export interface StreamlitMessage {
  type: string;
  args?: Record<string, unknown>;
  theme?: StreamlitTheme;
}

export interface StreamlitTheme {
  primaryColor: string;
  backgroundColor: string;
  secondaryBackgroundColor: string;
  textColor: string;
  font: string;
}

let _readyFired = false;
const _listeners: Array<(msg: StreamlitMessage) => void> = [];

/** Register a handler for Streamlit RENDER messages. */
export function onStreamlitRender(cb: (msg: StreamlitMessage) => void) {
  _listeners.push(cb);
}

/** Tell Streamlit the component is ready to receive data. */
export function sendReady() {
  if (_readyFired) return;
  _readyFired = true;
  window.parent.postMessage(
    { isStreamlitMessage: true, type: "streamlit:componentReady", apiVersion: 1 },
    "*",
  );
}

/** Set the iframe height so Streamlit sizes it correctly. */
export function setFrameHeight(height?: number) {
  const h = height ?? document.documentElement.scrollHeight;
  window.parent.postMessage(
    { isStreamlitMessage: true, type: "streamlit:setFrameHeight", height: h },
    "*",
  );
}

/** Send a value back to Python (triggers a rerun). */
export function setComponentValue(value: unknown) {
  window.parent.postMessage(
    { isStreamlitMessage: true, type: "streamlit:setComponentValue", value },
    "*",
  );
}

// Listen for messages from Streamlit
window.addEventListener("message", (event: MessageEvent) => {
  const msg = event.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "streamlit:render") {
    for (const cb of _listeners) cb(msg);
  }
});
