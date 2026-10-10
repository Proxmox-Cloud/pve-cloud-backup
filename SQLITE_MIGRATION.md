# SQLite Database Migration

This document describes the migration from TinyDB to SQLite for the pve-cloud-backup project.

## Overview

TinyDB has been replaced with SQLite for improved performance, better concurrency support, and reduced dependencies. SQLite is included in Python's standard library, eliminating the need for external database dependencies.

## Features

### HDD Optimization

The SQLite implementation includes optional HDD optimization that can be enabled via environment variables:

- Set `ENV_HDD=1` or `HDD_OPTIMIZED=1` to enable HDD-friendly settings
- When enabled, the database uses:
  - Larger cache size (64MB vs 2MB)
  - Disabled synchronous writes for better performance
  - DELETE journal mode instead of WAL
  - Memory-based temporary storage

### Database Files

The migration changes database file extensions from `.json` to `.sqlite`:

- `volume-meta-db.json` → `volume-meta-db.sqlite`
- `ns-secret-db.json` → `ns-secret-db.sqlite`

## Usage

### Environment Variables

```bash
# For HDD optimization
export ENV_HDD=1

# Or alternatively
export HDD_OPTIMIZED=1
```

### API Compatibility

The SQLite wrapper maintains compatibility with the TinyDB API:

```python
from pve_cloud_backup.daemon.db_sqlite import Query, get_db

# Get database connection
db = get_db("/path/to/database.sqlite")

# Insert document
db.insert({"image_name": "my-image", "timestamp": "123456"})

# Query with compound conditions
Meta = Query()
result = db.get((Meta.image_name == "my-image") & (Meta.timestamp == "123456"))
```

## Performance Considerations

### SSD (Default)
- WAL journal mode for better concurrency
- Full synchronous writes for data integrity
- 2MB cache size

### HDD (Optimized)
- DELETE journal mode
- Disabled synchronous writes (use with caution)
- 64MB cache size for reduced disk I/O
- Memory-based temporary storage

## Migration Notes

1. **Existing Data**: Old TinyDB JSON files will not be automatically migrated. If you need to preserve existing data, you'll need to manually migrate it or allow the system to create new SQLite databases.

2. **Backward Compatibility**: The system will create new `.sqlite` files alongside existing `.json` files. Once you've verified the new system works correctly, you can remove the old `.json` files.

3. **Concurrency**: SQLite with the existing async locks provides better concurrent access control than TinyDB.

## Testing

To test the new database implementation:

```bash
# Test with default (SSD) settings
python -m pve_cloud_backup.daemon.bdd

# Test with HDD optimization
ENV_HDD=1 python -m pve_cloud_backup.daemon.bdd
```

## Benefits

1. **No External Dependencies**: SQLite is part of Python's standard library
2. **Better Performance**: Especially for concurrent read/write operations
3. **HDD Optimization**: Optional tuning for mechanical hard drives
4. **Data Integrity**: Better transaction support and crash recovery
5. **Query Performance**: Indexed queries on JSON fields
