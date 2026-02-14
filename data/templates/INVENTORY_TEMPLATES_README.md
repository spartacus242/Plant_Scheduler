# Inventory Check Data Templates

Use these templates to enable inventory validation in the Flowstate scheduler.

## Files

| File | Description |
|------|-------------|
| `BOM_by_SKU.csv` | Bill of Materials: material consumption per SKU unit |
| `OnHand_Inventory.csv` | Current on-hand inventory by material |
| `Inbound_Inventory.csv` | Inbound shipments with arrival time |
| `Material_Master.csv` | Material metadata (optional) |

## Column Details

### BOM_by_SKU.csv
| Column | Required | Description |
|--------|----------|-------------|
| sku | Yes | Finished goods SKU (matches DemandPlan) |
| material_id | Yes | Raw material or component ID |
| qty_per_unit | Yes | Units of material consumed per 1 unit of SKU produced |
| material_description | No | Human-readable material name |

### OnHand_Inventory.csv
| Column | Required | Description |
|--------|----------|-------------|
| material_id | Yes | Raw material ID (matches BOM) |
| quantity | Yes | On-hand quantity |
| location | No | Warehouse or bin |
| uom | No | Unit of measure (EA, KG, etc.) |
| as_of_date | No | Snapshot date |

### Inbound_Inventory.csv
| Column | Required | Description |
|--------|----------|-------------|
| material_id | Yes | Raw material ID |
| quantity | Yes | Inbound quantity |
| arrival_hour | Yes* | Hours from planning anchor (2026-02-15 00:00) when shipment arrives |
| arrival_date | Yes* | Alternative: YYYY-MM-DD datetime for arrival |
| shipment_id | No | PO or shipment reference |
| notes | No | Free text |

*Use either `arrival_hour` or `arrival_date`; `arrival_hour` takes precedence if both present.

### Material_Master.csv (optional)
| Column | Description |
|--------|-------------|
| material_id | Material identifier |
| material_name | Display name |
| uom | Unit of measure |
| safety_stock_min | Minimum stock target |
| lead_time_days | Typical replenishment lead time |

## Validation Logic

- **PLAN**: On-hand + inbound (arriving before order start) ≥ material needed → schedule as-is
- **FLAG**: On-hand insufficient and no inbound arrives before stockout → adjust demand or accept lower fill %

## Setup

1. Copy template files from `data/templates/` to `data/`
2. Fill in your SKUs, materials, and quantities
3. Run scheduler; inventory check runs in Gantt viewer and flags orders as needed
