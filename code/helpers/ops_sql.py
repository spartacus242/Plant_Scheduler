# code/helpers/ops_sql.py — optional live SQL connection to VIF (NPA).
#
# Pulls current-MO progress (manprg) and the CIP schedule (tblCIPSchedule)
# directly from SQL Server so Flowstate can refresh without re-exporting
# files. FILE IMPORT IS THE FALLBACK: if pyodbc is missing, the DSN is not
# configured, or the server is unreachable, every function returns None and
# the app silently uses the file importers instead.
#
# The manprg query is the user's own (current MO per line = latest start with
# Qty_made_Cas > 0).

from __future__ import annotations

from dataclasses import dataclass

MANPRG_CURRENT_SQL = """
SELECT [MO Completion %], [MO Start DateTime], [Line], [Designation], [Left_Cas]
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY [Line] ORDER BY [MO Start DateTime] DESC) AS rn
  FROM (
    SELECT
      CAST([Qty_made_Cas] AS float)/CAST([Fct_qty_Cas] AS float) AS [MO Completion %],
      CAST([Start_date] AS datetime)+CAST([Start_time] AS datetime) AS [MO Start DateTime],
      REPLACE([Line],'LMH-','') AS [Line],
      [Designation],
      [Left_Cas]
    FROM [NPA].[dbo].[manprg]
    WHERE [Qty_made_Cas] > 0
  ) AS queryholder
) AS a
WHERE rn = 1
"""

CIP_SQL = """
SELECT [LineEquipment], [PreviousCIP], [MaxHoursBetweenCIP], [ScheduledCIP], [Notes]
FROM [NPA].[dbo].[tblCIPSchedule]
"""


@dataclass
class SqlConfig:
    dsn: str = ""          # e.g. "DRIVER={ODBC Driver 17 for SQL Server};SERVER=...;DATABASE=NPA;Trusted_Connection=yes;"
    enabled: bool = False


def _connect(dsn: str):
    import pyodbc  # local import: optional dependency
    return pyodbc.connect(dsn, timeout=5)


def available(cfg: SqlConfig) -> bool:
    """True only if pyodbc imports, SQL is enabled, and a DSN is set."""
    if not cfg.enabled or not cfg.dsn.strip():
        return False
    try:
        import pyodbc  # noqa: F401
        return True
    except Exception:
        return False


def fetch_current_mo(cfg: SqlConfig):
    """List of dicts (line, completion_pct, start_dt, designation, left_cas)
    or None when SQL is unavailable/fails."""
    if not available(cfg):
        return None
    try:
        with _connect(cfg.dsn) as con:
            cur = con.cursor()
            cur.execute(MANPRG_CURRENT_SQL)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return None


def fetch_cip_schedule(cfg: SqlConfig):
    """List of dicts (LineEquipment, PreviousCIP, ...) or None on failure."""
    if not available(cfg):
        return None
    try:
        with _connect(cfg.dsn) as con:
            cur = con.cursor()
            cur.execute(CIP_SQL)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return None
