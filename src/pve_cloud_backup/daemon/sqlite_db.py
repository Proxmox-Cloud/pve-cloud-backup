"""
SQLite database module to replace TinyDB.
Provides similar query functionality with relational database backend.
Uses a single database file with separate tables for different data types.
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class UnifiedSQLiteDB:
    """
    A unified SQLite database with separate tables for different data types.
    Replaces multiple TinyDB files with a single SQLite database.
    """

    def __init__(self, db_path: str):
        """
        Initialize SQLite database.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        # Ensure parent directory exists
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.row_factory = sqlite3.Row  # Enable row factory for dict-like access
        try:
            cursor = conn.cursor()

            # Create volume_metas table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS volume_metas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    image_name TEXT,
                    timestamp TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Create index for faster queries
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_volume_metas_image_timestamp 
                ON volume_metas(image_name, timestamp)
            """
            )

            # Create ns_secrets table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ns_secrets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    timestamp TEXT,
                    stack TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Create index for faster queries
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ns_secrets_timestamp_stack 
                ON ns_secrets(timestamp, stack)
            """
            )

            conn.commit()
            logger.debug(f"Initialized database at {self.db_path}")
        finally:
            conn.close()

    def insert_volume_meta(self, document: Dict[str, Any]) -> int:
        """
        Insert a volume metadata document.

        Args:
            document: Dictionary to insert

        Returns:
            The ID of the inserted document
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            data_json = json.dumps(document)

            # Extract indexed fields
            image_name = document.get("image_name")
            timestamp = document.get("timestamp")

            cursor.execute(
                "INSERT INTO volume_metas (data, image_name, timestamp) VALUES (?, ?, ?)",
                (data_json, image_name, timestamp),
            )
            conn.commit()
            doc_id = cursor.lastrowid
            logger.debug(f"Inserted volume meta with ID {doc_id}")
            return doc_id
        finally:
            conn.close()

    def insert_ns_secret(self, document: Dict[str, Any]) -> int:
        """
        Insert a namespace secret document.

        Args:
            document: Dictionary to insert

        Returns:
            The ID of the inserted document
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            data_json = json.dumps(document)

            # Extract indexed fields
            timestamp = document.get("timestamp")
            stack = document.get("stack")

            cursor.execute(
                "INSERT INTO ns_secrets (data, timestamp, stack) VALUES (?, ?, ?)",
                (data_json, timestamp, stack),
            )
            conn.commit()
            doc_id = cursor.lastrowid
            logger.debug(f"Inserted ns secret with ID {doc_id}")
            return doc_id
        finally:
            conn.close()

    def get_volume_meta(self, condition_func) -> Optional[Dict[str, Any]]:
        """
        Get a single volume meta document matching the condition.

        Args:
            condition_func: A callable that takes a document and returns True if it matches

        Returns:
            The matching document or None
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM volume_metas ORDER BY id DESC")

            for row in cursor.fetchall():
                doc = json.loads(row["data"])
                if condition_func(doc):
                    logger.debug(f"Found matching volume meta: {doc}")
                    return doc

            logger.debug("No matching volume meta found")
            return None
        finally:
            conn.close()

    def get_ns_secret(self, condition_func) -> Optional[Dict[str, Any]]:
        """
        Get a single ns secret document matching the condition.

        Args:
            condition_func: A callable that takes a document and returns True if it matches

        Returns:
            The matching document or None
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM ns_secrets ORDER BY id DESC")

            for row in cursor.fetchall():
                doc = json.loads(row["data"])
                if condition_func(doc):
                    logger.debug(f"Found matching ns secret: {doc}")
                    return doc

            logger.debug("No matching ns secret found")
            return None
        finally:
            conn.close()

    def search_volume_metas(self, condition_func) -> List[Dict[str, Any]]:
        """
        Search for all volume meta documents matching the condition.

        Args:
            condition_func: A callable that takes a document and returns True if it matches

        Returns:
            List of matching documents
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM volume_metas")

            results = []
            for row in cursor.fetchall():
                doc = json.loads(row["data"])
                if condition_func(doc):
                    results.append(doc)

            logger.debug(f"Found {len(results)} matching volume metas")
            return results
        finally:
            conn.close()

    def search_ns_secrets(self, condition_func) -> List[Dict[str, Any]]:
        """
        Search for all ns secret documents matching the condition.

        Args:
            condition_func: A callable that takes a document and returns True if it matches

        Returns:
            List of matching documents
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM ns_secrets")

            results = []
            for row in cursor.fetchall():
                doc = json.loads(row["data"])
                if condition_func(doc):
                    results.append(doc)

            logger.debug(f"Found {len(results)} matching ns secrets")
            return results
        finally:
            conn.close()

    def all_volume_metas(self) -> List[Dict[str, Any]]:
        """
        Get all volume meta documents.

        Returns:
            List of all volume meta documents
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM volume_metas")

            results = []
            for row in cursor.fetchall():
                results.append(json.loads(row["data"]))

            logger.debug(f"Retrieved {len(results)} volume metas")
            return results
        finally:
            conn.close()

    def all_ns_secrets(self) -> List[Dict[str, Any]]:
        """
        Get all ns secret documents.

        Returns:
            List of all ns secret documents
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM ns_secrets")

            results = []
            for row in cursor.fetchall():
                results.append(json.loads(row["data"]))

            logger.debug(f"Retrieved {len(results)} ns secrets")
            return results
        finally:
            conn.close()


# Query class to match TinyDB's Query API
class Query:
    """
    Query class that mimics TinyDB's Query functionality.
    Allows building conditions like: Query().field_name == 'value'
    """

    def __init__(self):
        self._conditions = []

    def __getattr__(self, name):
        """Create a condition for the given field."""
        return QueryCondition(name, self._conditions)

    def _add_condition(self, field, operator, value, conditions_list):
        """Add a condition to the query."""
        conditions_list.append((field, operator, value))
        return self

    def __call__(self):
        """Return the conditions for evaluation."""
        return self._conditions


class QueryCondition:
    """Represents a single query condition."""

    def __init__(self, field_name, conditions_list):
        self.field_name = field_name
        self.conditions_list = conditions_list

    def __eq__(self, value):
        """Equality condition."""
        return QueryMatcher(self.field_name, "eq", value)

    def __ne__(self, value):
        """Not equal condition."""
        return QueryMatcher(self.field_name, "ne", value)

    def __lt__(self, value):
        """Less than condition."""
        return QueryMatcher(self.field_name, "lt", value)

    def __le__(self, value):
        """Less than or equal condition."""
        return QueryMatcher(self.field_name, "le", value)

    def __gt__(self, value):
        """Greater than condition."""
        return QueryMatcher(self.field_name, "gt", value)

    def __ge__(self, value):
        """Greater than or equal condition."""
        return QueryMatcher(self.field_name, "ge", value)

    def __contains__(self, value):
        """Contains condition (for strings/lists)."""
        return QueryMatcher(self.field_name, "contains", value)


class QueryMatcher:
    """
    Matcher that evaluates documents against conditions.
    Supports chaining with & (and) and | (or) operators.
    """

    def __init__(
        self,
        field_name=None,
        operator=None,
        value=None,
        left=None,
        right=None,
        logic_op=None,
    ):
        self.field_name = field_name
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.logic_op = logic_op  # 'and' or 'or'

    def __and__(self, other):
        """Combine with AND."""
        return QueryMatcher(left=self, right=other, logic_op="and")

    def __or__(self, other):
        """Combine with OR."""
        return QueryMatcher(left=self, right=other, logic_op="or")

    def __call__(self, document: Dict[str, Any]) -> bool:
        """
        Evaluate the query against a document.

        Args:
            document: The document to test

        Returns:
            True if the document matches the query
        """
        # Handle combined queries (AND/OR)
        if self.logic_op:
            left_result = self.left(document)
            right_result = self.right(document)

            if self.logic_op == "and":
                return left_result and right_result
            elif self.logic_op == "or":
                return left_result or right_result

        # Handle simple field conditions
        if self.field_name and self.operator:
            # Support nested fields with dot notation
            value = document
            for field in self.field_name.split("."):
                if isinstance(value, dict):
                    value = value.get(field)
                else:
                    return False

            # Apply operator
            if self.operator == "eq":
                return value == self.value
            elif self.operator == "ne":
                return value != self.value
            elif self.operator == "lt":
                return value < self.value
            elif self.operator == "le":
                return value <= self.value
            elif self.operator == "gt":
                return value > self.value
            elif self.operator == "ge":
                return value >= self.value
            elif self.operator == "contains":
                return self.value in value

        return True
