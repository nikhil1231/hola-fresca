"""Backup and disaster recovery for the local data store.

Two jobs with different aims:

* ``exports`` — the human review decisions (ingredient→product mappings) written
  out as sorted CSV for git. Small, diffable, and the only part of the database
  that cannot be regenerated from ``data/raw/``.
* ``snapshot`` — a consistent whole-database copy for offsite sync. Coarse and
  binary, but it restores everything at once.

Both read the database through plain ``sqlite3`` rather than the ORM, on purpose:
a backup tool must keep working against a database whose schema has drifted from
the current models.
"""
