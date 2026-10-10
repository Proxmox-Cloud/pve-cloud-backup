"""
SQLite database wrapper for replacing TinyDB in pve-cloud-backup.

Provides a compatible interface for storing and querying backup metadata.
Includes optional HDD optimization via environment variable.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("bdd.db")


class SQLiteDB:
    """
    SQLite database wrapper that provides a TinyDB-compatible interface.

    Supports:
    - Insert operations
    - Query operations with field matching
    - Compound queries with AND conditions

    HDD Optimization:
    - Set ENV_HDD=1 or HDD_OPTIMIZED=1 to enable HDD-friendly settings
    - Increases cache size and disables synchronous writes for better performance
    """

    def __init__(self, db_path: str, hdd_optimized: Optional[bool] = None):
        """
        Initialize SQLite database connection.

        Args:
            db_path: Path to the SQLite database file
            hdd_optimized: If True, enable HDD optimization settings.
                          If None, auto-detect from environment variables.
        """
        self.db_path = db_path
        self._ensure_db_dir()

        # Auto-detect HDD optimization from environment
        if hdd_optimized is None:
            hdd_optimized = self._detect_hdd_mode()

        self.hdd_optimized = hdd_optimized
        self._init_connection()

        if self.hdd_optimized:
            logger.info(f"SQLite initialized with HDD optimization: {db_path}")
        else:
            logger.info(f"SQLite initialized in standard mode: {db_path}")

    def _detect_hdd_mode(self) -> bool:
        """Detect if HDD optimization should be enabled."""
        return os.getenv("ENV_HDD", "").lower() in ("1", "true", "yes") or os.getenv(
            "HDD_OPTIMIZED", ""
        ).lower() in ("1", "true", "yes")

    def _ensure_db_dir(self):
        """Ensure the directory for the database file exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            Path(db_dir).mkdir(parents=True, exist_ok=True)

    def _init_connection(self):
        """Initialize database connection with optional HDD optimizations."""
        self.conn = sqlite3.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row

        cursor = self.conn.cursor()

        if self.hdd_optimized:
            # HDD optimization settings
            cursor.execute("PRAGMA cache_size = -64000")  # 64MB cache
            cursor.execute("PRAGMA synchronous = OFF")
            cursor.execute("PRAGMA journal_mode = DELETE")
            cursor.execute("PRAGMA temp_store = MEMORY")
            logger.debug("Applied HDD optimization settings")
        else:
            # Standard settings for SSD
            cursor.execute("PRAGMA cache_size = -2000")  # 2MB cache
            cursor.execute("PRAGMA synchronous = FULL")
            cursor.execute("PRAGMA journal_mode = WAL")

        self.conn.commit()

    def _get_table_name(self) -> str:
        """Extract table name from database path."""
        # Convert filename like 'volume-meta-db.json' to 'volume_meta_db'
        base_name = os.path.basename(self.db_path)
        table_name = os.path.splitext(base_name)[0].replace("-", "_").replace(".", "_")
        return table_name

    def _init_table(self):
        """Initialize the database table schema."""
        table_name = self._get_table_name()
        cursor = self.conn.cursor()

        # Create table with JSON column for flexible schema (like TinyDB)
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Create indexes for common query patterns
        cursor.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_data 
            ON {table_name}((json_extract(data, '$.image_name')))
        """
        )
        cursor.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_timestamp 
            ON {table_name}((json_extract(data, '$.timestamp')))
        """
        )
        cursor.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_stack 
            ON {table_name}((json_extract(data, '$.stack')))
        """
        )

        self.conn.commit()

    def insert(self, document: Dict[str, Any]) -> int:
        """
        Insert a document into the database.

        Args:
            document: Dictionary to insert

        Returns:
            The ID of the inserted document
        """
        self._init_table()

        cursor = self.conn.cursor()
        data_json = json.dumps(document)

        cursor.execute(
            f"INSERT INTO {self._get_table_name()} (data) VALUES (?)", (data_json,)
        )

        self.conn.commit()
        return cursor.lastrowid

    def get(self, query_conditions: "Query") -> Optional[Dict[str, Any]]:
        """
        Get a single document matching the query conditions.

        Args:
            query_conditions: Query object with field conditions

        Returns:
            First matching document or None
        """
        self._init_table()

        cursor = self.conn.cursor()

        # Build WHERE clause from query conditions
        where_clauses = []
        params = []

        for condition in query_conditions.conditions:
            field = condition.field
            value = condition.value
            operator = condition.operator

            if operator == "==":
                where_clauses.append(f"json_extract(data, '$.{field}') = ?")
                # Handle different types
                if isinstance(value, str):
                    params.append(value)
                elif isinstance(value, (int, float)):
                    params.append(value)
                else:
                    params.append(str(value))

        where_clause = " AND ".join(where_clauses)

        cursor.execute(
            f"SELECT data FROM {self._get_table_name()} WHERE {where_clause} LIMIT 1",
            params,
        )

        row = cursor.fetchone()
        if row:
            return json.loads(row["data"])
        return None

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


class Query:
    """
    Query builder compatible with TinyDB's Query interface.

    Supports field comparisons with == operator and AND combinations.

    Example usage:
        Meta = Query()
        query = (Meta.image_name == "my-image") & (Meta.timestamp == "123456")
    """

    def __init__(self):
        self.conditions = []
        self._current_field = None

    def __getattr__(self, field_name: str) -> "Query":
        """
        Create a new Query instance for a field.

        Args:
            field_name: The field to query on

        Returns:
            A new Query instance configured for this field
        """
        new_query = Query()
        new_query._current_field = field_name
        return new_query

    def __eq__(self, value: Any) -> "Query":
        """
        Create an equality condition.

        Args:
            value: The value to compare against

        Returns:
            Self with the condition added
        """
        if self._current_field is not None:
            self.conditions.append(QueryCondition(self._current_field, value, "=="))
        return self

    def __and__(self, other: "Query") -> "Query":
        """
        Combine queries with AND.

        Args:
            other: Another Query instance

        Returns:
            New Query with combined conditions
        """
        combined = Query()
        combined.conditions = self.conditions + other.conditions
        return combined


class QueryCondition:
    """Represents a single query condition."""

    def __init__(self, field: str, value: Any, operator: str):
        self.field = field
        self.value = value
        self.operator = operator


# Global cache for database connections to avoid repeated initialization
_db_connections: Dict[str, SQLiteDB] = {}


def get_db(db_path: str, hdd_optimized: Optional[bool] = None) -> SQLiteDB:
    """
    Get or create a database connection.

    Args:
        db_path: Path to the database file
        hdd_optimized: HDD optimization flag

    Returns:
        SQLiteDB instance
    """
    if db_path not in _db_connections:
        _db_connections[db_path] = SQLiteDB(db_path, hdd_optimized)

    return _db_connections[db_path]


def close_all_dbs():
    """Close all cached database connections."""
    for db in _db_connections.values():
        db.close()
    _db_connections.clear()
