from enum import Enum


class Command(Enum):
    ARCHIVE = 1
    VOLUME_META = 2
    NAMESPACE_SECRETS = 3
    LIST_BACKUPS = 4
    LIST_BACKUP_DETAILS = 5
    RESTORE_PROCEDURE = 6
