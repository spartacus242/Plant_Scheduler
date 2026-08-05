# helpers/labels.py -- Display names for things stored on disk under older names.
#
# Domain correction: AZAP is the customer / corporate DEMAND PLAN (SKU + kg +
# week). It is NOT a schedule -- it never assigns lines, sequence or equipment.
# The plant's production planner builds the actual line schedule, and that is
# what lives in data/calendar_blocks.csv.
#
# Older builds mislabelled calendar_blocks.csv as the "AZAP schedule" and saved
# a version at slug 'azap_baseline' named "AZAP Baseline". Those files stay on
# disk and must keep loading; this module only fixes how they are LABELLED.

from __future__ import annotations

CURRENT_SCHEDULE = "Current schedule"
CURRENT_SCHEDULE_LONG = "Current schedule (the plant's own line schedule)"
DEMAND_PLAN = "Demand plan (AZAP)"

# Legacy slug -> friendly display name. Directories are never renamed.
LEGACY_VERSION_NAMES = {
    "azap_baseline": "Current schedule (imported)",
}

# Legacy stored labels -> display text.
_LEGACY_LABELS = {
    "azap": CURRENT_SCHEDULE,
    "azap baseline": "Current schedule (imported)",
    "azap-baseline": "Current schedule (imported)",
    "azap_baseline": "Current schedule (imported)",
    "azap schedule": CURRENT_SCHEDULE,
    "official azap / current": "Current schedule",
}

AZAP_NOTE = (
    "AZAP is the customer / corporate **demand plan** -- which SKUs, how many kg, "
    "which week. It does not schedule lines. The line schedule below is the "
    "plant's own."
)


def display_name(slug: str, stored_name: str | None = None) -> str:
    """Friendly name for a saved version, remapping legacy AZAP wording."""
    key = str(slug or "").strip().lower()
    if key in LEGACY_VERSION_NAMES:
        return LEGACY_VERSION_NAMES[key]
    name = str(stored_name or slug or "").strip()
    return display_label(name) if name else str(slug)


def display_label(label: str | None) -> str:
    """Remap a stored week/version label for display. Unknown labels pass through."""
    text = str(label or "").strip()
    hit = _LEGACY_LABELS.get(text.lower())
    if hit:
        return hit
    if text.lower().startswith("azap-"):
        # Legacy default week label 'AZAP-2026-02-15'
        return "Week-" + text[5:]
    return text
