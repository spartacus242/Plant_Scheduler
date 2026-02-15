# components/gantt_sandbox — Custom Streamlit v2 component for drag-and-drop Gantt.
#
# This is a placeholder for the future React-based component.
# The current sandbox (pages/sandbox.py) uses Plotly + Streamlit native
# controls.  When this component is built, it will wrap gantt-task-react
# and communicate via setStateValue / setTriggerValue.
#
# To build:
#   cd frontend && npm install && npm run build
#
# Python usage:
#   from components.gantt_sandbox import gantt_sandbox
#   state = gantt_sandbox(schedule=..., caps=..., demand=...)

_COMPONENT_READY = False


def gantt_sandbox(**kwargs):
    """Placeholder — returns None until React component is built."""
    if not _COMPONENT_READY:
        return None
    # Future: streamlit.components.v2.component(...)
    raise NotImplementedError("React component not yet built")
