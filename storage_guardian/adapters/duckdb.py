"""DuckDB adapter placeholder for safe backup flow."""

from __future__ import annotations

from storage_guardian.adapters.base import StoreAdapter


class DuckDBAdapter(StoreAdapter):
    name = "duckdb"
