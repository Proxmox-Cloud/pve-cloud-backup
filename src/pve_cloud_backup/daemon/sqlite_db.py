"""
SQLite database module to replace TinyDB.
Provides similar query functionality with relational database backend.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SQLiteDB:
    """
    A simple SQLite wrapper that provides document-like storage similar to TinyDB.
    Each "table" stores JSON documents with automatic schema management.
    """

    def __init__(self, db_path: str):
        """
        Initialize SQLite database.
        
        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database tables if they don't exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Create a generic documents table if it doesn't exist
            # We'll use table names based on the db_path filename
            table_name = self._get_table_name()
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS {table_name} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            logger.debug(f"Initialized table '{table_name}' in {self.db_path}")

    def _get_table_name(self) -> str:
        """Get table name from database path."""
        filename = os.path.basename(self.db_path)
        # Convert filename to valid table name (remove extensions, replace special chars)
        table_name = filename.replace('.json', '').replace('-', '_').replace('.', '_')
        return f"documents_{table_name}"

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def insert(self, document: Dict[str, Any]) -> int:
        """
        Insert a document into the database.
        
        Args:
            document: Dictionary to insert
            
        Returns:
            The ID of the inserted document
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            data_json = json.dumps(document)
            cursor.execute(
                f'INSERT INTO {table_name} (data) VALUES (?)',
                (data_json,)
            )
            conn.commit()
            doc_id = cursor.lastrowid
            logger.debug(f"Inserted document with ID {doc_id}")
            return doc_id

    def get(self, condition_func) -> Optional[Dict[str, Any]]:
        """
        Get a single document matching the condition.
        
        Args:
            condition_func: A callable that takes a document and returns True if it matches
            
        Returns:
            The matching document or None
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            cursor.execute(f'SELECT data FROM {table_name} ORDER BY id DESC')
            
            for row in cursor.fetchall():
                doc = json.loads(row['data'])
                if condition_func(doc):
                    logger.debug(f"Found matching document: {doc}")
                    return doc
            
            logger.debug("No matching document found")
            return None

    def search(self, condition_func) -> List[Dict[str, Any]]:
        """
        Search for all documents matching the condition.
        
        Args:
            condition_func: A callable that takes a document and returns True if it matches
            
        Returns:
            List of matching documents
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            cursor.execute(f'SELECT data FROM {table_name}')
            
            results = []
            for row in cursor.fetchall():
                doc = json.loads(row['data'])
                if condition_func(doc):
                    results.append(doc)
            
            logger.debug(f"Found {len(results)} matching documents")
            return results

    def all(self) -> List[Dict[str, Any]]:
        """
        Get all documents from the database.
        
        Returns:
            List of all documents
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            cursor.execute(f'SELECT data FROM {table_name}')
            
            results = []
            for row in cursor.fetchall():
                results.append(json.loads(row['data']))
            
            logger.debug(f"Retrieved {len(results)} documents")
            return results

    def remove(self, condition_func) -> int:
        """
        Remove documents matching the condition.
        
        Args:
            condition_func: A callable that takes a document and returns True if it should be removed
            
        Returns:
            Number of documents removed
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            
            # Get all documents first to check condition
            cursor.execute(f'SELECT id, data FROM {table_name}')
            ids_to_remove = []
            for row in cursor.fetchall():
                doc = json.loads(row['data'])
                if condition_func(doc):
                    ids_to_remove.append(row['id'])
            
            # Delete matching documents
            if ids_to_remove:
                placeholders = ','.join('?' * len(ids_to_remove))
                cursor.execute(
                    f'DELETE FROM {table_name} WHERE id IN ({placeholders})',
                    ids_to_remove
                )
                conn.commit()
                logger.debug(f"Removed {len(ids_to_remove)} documents")
                return len(ids_to_remove)
            
            return 0

    def count(self, condition_func=None) -> int:
        """
        Count documents in the database.
        
        Args:
            condition_func: Optional callable to filter documents
            
        Returns:
            Number of documents
        """
        import json
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            
            if condition_func is None:
                cursor.execute(f'SELECT COUNT(*) FROM {table_name}')
                return cursor.fetchone()[0]
            
            # Count with filter
            cursor.execute(f'SELECT data FROM {table_name}')
            count = 0
            for row in cursor.fetchall():
                doc = json.loads(row['data'])
                if condition_func(doc):
                    count += 1
            
            return count

    def clear(self):
        """Remove all documents from the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            table_name = self._get_table_name()
            cursor.execute(f'DELETE FROM {table_name}')
            conn.commit()
            logger.debug("Cleared all documents")


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
        return QueryMatcher(self.field_name, 'eq', value)
    
    def __ne__(self, value):
        """Not equal condition."""
        return QueryMatcher(self.field_name, 'ne', value)
    
    def __lt__(self, value):
        """Less than condition."""
        return QueryMatcher(self.field_name, 'lt', value)
    
    def __le__(self, value):
        """Less than or equal condition."""
        return QueryMatcher(self.field_name, 'le', value)
    
    def __gt__(self, value):
        """Greater than condition."""
        return QueryMatcher(self.field_name, 'gt', value)
    
    def __ge__(self, value):
        """Greater than or equal condition."""
        return QueryMatcher(self.field_name, 'ge', value)
    
    def __contains__(self, value):
        """Contains condition (for strings/lists)."""
        return QueryMatcher(self.field_name, 'contains', value)


class QueryMatcher:
    """
    Matcher that evaluates documents against conditions.
    Supports chaining with & (and) and | (or) operators.
    """
    
    def __init__(self, field_name=None, operator=None, value=None, 
                 left=None, right=None, logic_op=None):
        self.field_name = field_name
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.logic_op = logic_op  # 'and' or 'or'
    
    def __and__(self, other):
        """Combine with AND."""
        return QueryMatcher(left=self, right=other, logic_op='and')
    
    def __or__(self, other):
        """Combine with OR."""
        return QueryMatcher(left=self, right=other, logic_op='or')
    
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
            
            if self.logic_op == 'and':
                return left_result and right_result
            elif self.logic_op == 'or':
                return left_result or right_result
        
        # Handle simple field conditions
        if self.field_name and self.operator:
            # Support nested fields with dot notation
            value = document
            for field in self.field_name.split('.'):
                if isinstance(value, dict):
                    value = value.get(field)
                else:
                    return False
            
            # Apply operator
            if self.operator == 'eq':
                return value == self.value
            elif self.operator == 'ne':
                return value != self.value
            elif self.operator == 'lt':
                return value < self.value
            elif self.operator == 'le':
                return value <= self.value
            elif self.operator == 'gt':
                return value > self.value
            elif self.operator == 'ge':
                return value >= self.value
            elif self.operator == 'contains':
                return self.value in value
        
        return True
