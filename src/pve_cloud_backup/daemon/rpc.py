from enum import Enum

class Command(Enum):
  ARCHIVE = 1
  IMAGE_META = 2
  STACK_META = 3
  LIST_BACKUPS = 4
  LIST_BACKUP_DETAILS = 5
  RESTORE_PROCEDURE = 6
