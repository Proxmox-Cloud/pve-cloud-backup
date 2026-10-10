# TinyDB to SQLite Migration Summary

## Overview
This document summarizes the migration from TinyDB to SQLite for the pve-cloud-backup project.

## Changes Made

### 1. New SQLite Wrapper Module (`src/pve_cloud_backup/daemon/db.py`)
Created a new SQLite database wrapper that mimics TinyDB's API for easy migration:

- **SQLiteDB class**: Main database class with methods:
  - `insert(document)`: Insert a JSON document
  - `get(condition)`: Get a single document matching conditions
  - `search(condition)`: Search for all documents matching conditions
  - `all()`: Get all documents
  - `remove(condition)`: Remove documents matching conditions

- **SQLiteQueryWithCondition class**: Query builder for compound conditions
  - Supports AND operations with `&` operator
  - Field-based equality queries

- **HDD Optimization**: Optional optimization via `USE_HDD_OPTIMIZATION` environment variable
  - For HDD: Disables WAL mode, sets synchronous=OFF
  - For SSD (default): Uses WAL mode, synchronous=NORMAL, increased cache

### 2. Updated Files

#### `src/pve_cloud_backup/daemon/funcs.py`
- Replaced `from tinydb import Query, TinyDB` with SQLite imports
- Updated `get_volume_metas()` function to use SQLite
- Changed database file from `.json` to `.sqlite`
- Added HDD optimization check via environment variable

#### `src/pve_cloud_backup/daemon/bdd.py`
- Replaced TinyDB imports with SQLite imports
- Updated all database operations:
  - `Command.VOLUME_META`: Insert operation
  - `Command.LIST_BACKUP_DETAILS`: Query operation
  - `Command.INIT_RESTORE_PROCEDURE`: Query operation
- Changed database files from `.json` to `.sqlite`
- Added HDD optimization checks

#### `requirements.txt`
- Removed `tinydb==4.8.2` dependency
- SQLite is part of Python's standard library (no additional dependency needed)

### 3. Database File Changes
- Old: `volume-meta-db.json`, `ns-secret-db.json`
- New: `volume-meta-db.sqlite`, `ns-secret-db.sqlite`

## Usage

### Basic Usage
```python
from pve_cloud_backup.daemon.db import get_db, SQLiteQueryWithCondition

# Initialize database (automatically creates if doesn't exist)
db = get_db("/path/to/database.sqlite", use_hdd_optimization=False)

# Insert a document
db.insert({"image_name": "nginx", "timestamp": "123456", "data": "metadata"})

# Query with conditions
condition = SQLiteQueryWithCondition([
    {"type": "eq", "field": "image_name", "value": "nginx"},
    {"type": "eq", "field": "timestamp", "value": "123456"}
])
result = db.get(condition)
```

### HDD Optimization
Enable HDD optimization by setting the environment variable:
```bash
export USE_HDD_OPTIMIZATION=true
```

This will:
- Disable WAL mode (reduces write amplification on HDD)
- Set synchronous=OFF (better performance, risk of data loss on crash)
- Disable auto-vacuum

For SSD storage (default), the database uses:
- WAL mode for better concurrent access
- synchronous=NORMAL for safety
- Increased cache size (10000 pages)
- Temp storage in memory

## Benefits

1. **Better Performance**: SQLite provides better performance for concurrent access
2. **No External Dependency**: SQLite is built into Python (no pip install needed)
3. **HDD Optimization**: Optional tuning for HDD environments
4. **ACID Compliance**: Full transaction support
5. **Better Query Capabilities**: SQL queries with JSON support
6. **Scalability**: Better handling of larger datasets

## Migration Notes

- Existing JSON database files will not be automatically migrated
- New databases will be created with `.sqlite` extension
- If you need to migrate existing data, you'll need to:
  1. Read the old JSON files
  2. Insert the data into the new SQLite database

## Testing

A test suite is available at `test_sqlite_wrapper.py` to verify the SQLite wrapper functionality:

```bash
python test_sqlite_wrapper.py
```

## Backward Compatibility

This is a breaking change for existing deployments:
- Database file format changes from JSON to SQLite
- File names change from `.json` to `.sqlite`
- No automatic migration provided

Plan accordingly for production deployments to ensure data migration if needed.
