"""
SQLite database wrapper for replacing TinyDB.
Provides similar API to TinyDB for easy migration.
"""

import sqlite3
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("bdd.db")


class SQLiteQuery:
    """
    Simple query builder to mimic TinyDB's Query API.
    Usage: Meta = SQLiteQuery(); db.get((Meta.image_name == "test") & (Meta.timestamp == "123"))
    """

    def __init__(self):
        self._conditions = []

    def __eq__(self, other: Any) -> "SQLiteQuery":
        """Create equality condition: Meta.field == value"""
        cond = {"type": "eq", "field": self._get_field_name(), "value": other}
        return SQLiteQueryWithCondition(self._conditions + [cond])

    def _get_field_name(self) -> str:
        """Extract field name from the calling context."""
        import inspect

        frame = inspect.currentframe()
        try:
            # Look at the calling frame to get the variable name
            caller_frame = frame.f_back
            if caller_frame:
                # Try to find which variable we're being compared to
                for var_name, var_value in caller_frame.f_locals.items():
                    if var_value is self:
                        return var_name
        finally:
            del frame
        return "unknown"


class SQLiteQueryWithCondition:
    """Query with conditions attached."""

    def __init__(self, conditions: List[Dict]):
        self._conditions = conditions

    def __and__(self, other: "SQLiteQueryWithCondition") -> "SQLiteQueryWithCondition":
        """Combine conditions with AND: (Meta.field == value) & (Meta.other == value2)"""
        return SQLiteQueryWithCondition(self._conditions + other._conditions)

    def get_conditions(self) -> List[Dict]:
        """Return the conditions list."""
        return self._conditions


class SQLiteDB:
    """
    SQLite database wrapper that mimics TinyDB's API.
    Supports simple key-value storage with JSON-serialized data.
    """

    def __init__(self, db_path: str, use_hdd_optimization: bool = False):
        """
        Initialize SQLite database.

        Args:
            db_path: Path to the SQLite database file
            use_hdd_optimization: If True, optimize for HDD with synchronous=OFF and wal mode disabled
        """
        self.db_path = db_path
        self.use_hdd_optimization = use_hdd_optimization
        self._init_db()

    def _init_db(self):
        """Initialize the database and create tables if needed."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Create a generic table for storing JSON documents
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Apply HDD optimizations if requested
            if self.use_hdd_optimization:
                # Disable WAL mode for HDD (reduces write amplification)
                cursor.execute("PRAGMA journal_mode=DELETE")
                # Lower synchronous for better HDD performance (risk of data loss on crash)
                cursor.execute("PRAGMA synchronous=OFF")
                # Disable freelist cleanup
                cursor.execute("PRAGMA auto_vacuum=NONE")
                logger.info("Applied HDD optimizations to SQLite database")
            else:
                # Default optimizations for SSD
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA cache_size=10000")
                cursor.execute("PRAGMA temp_store=MEMORY")

            conn.commit()
        finally:
            conn.close()

    def insert(self, document: Dict[str, Any]) -> int:
        """
        Insert a document into the database.

        Args:
            document: Dictionary to insert

        Returns:
            The row ID of the inserted document
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            # Store the entire document as JSON
            json_data = json.dumps(document)
            cursor.execute("INSERT INTO documents (data) VALUES (?)", (json_data,))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get(self, condition: Optional[SQLiteQueryWithCondition] = None) -> Optional[Dict[str, Any]]:
        """
        Get a single document matching the condition.

        Args:
            condition: Query condition (optional)

        Returns:
            The first matching document or None
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT data FROM documents WHERE 1=1"
            params = []

            if condition:
                for cond in condition.get_conditions():
                    if cond["type"] == "eq":
                        field = cond["field"]
                        value = cond["value"]
                        # Check if the JSON document contains this field with this value
                        query += f" AND JSON_EXTRACT(data, '$.{field}') = ?"
                        # Don't use json.dumps for simple values - JSON_EXTRACT returns the raw value
                        params.append(value)

            cursor.execute(query, params)
            row = cursor.fetchone()

            if row:
                return json.loads(row["data"])
            return None
        finally:
            conn.close()

    def search(self, condition: SQLiteQueryWithCondition) -> List[Dict[str, Any]]:
        """
        Search for all documents matching the condition.

        Args:
            condition: Query condition

        Returns:
            List of matching documents
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT data FROM documents WHERE 1=1"
            params = []

            for cond in condition.get_conditions():
                if cond["type"] == "eq":
                    field = cond["field"]
                    value = cond["value"]
                    query += f" AND JSON_EXTRACT(data, '$.{field}') = ?"
                    params.append(value)

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [json.loads(row["data"]) for row in rows]
        finally:
            conn.close()

    def all(self) -> List[Dict[str, Any]]:
        """
        Get all documents from the database.

        Returns:
            List of all documents
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT data FROM documents")
            rows = cursor.fetchall()

            return [json.loads(row["data"]) for row in rows]
        finally:
            conn.close()

    def remove(self, condition: SQLiteQueryWithCondition) -> int:
        """
        Remove documents matching the condition.

        Args:
            condition: Query condition

        Returns:
            Number of documents removed
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            query = "DELETE FROM documents WHERE 1=1"
            params = []

            for cond in condition.get_conditions():
                if cond["type"] == "eq":
                    field = cond["field"]
                    value = cond["value"]
                    query += f" AND JSON_EXTRACT(data, '$.{field}') = ?"
                    params.append(value)

            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def close(self):
        """Close the database connection (cleanup)."""
        # SQLite connections are opened per operation, so no explicit close needed
        pass


def get_db(db_path: str, use_hdd_optimization: bool = False) -> SQLiteDB:
    """
    Factory function to create a SQLiteDB instance.

    Args:
        db_path: Path to the database file
        use_hdd_optimization: If True, optimize for HDD storage

    Returns:
        SQLiteDB instance
    """
    return SQLiteDB(db_path, use_hdd_optimization)
