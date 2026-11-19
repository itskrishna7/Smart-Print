"""Compatibility entry point for the PostgreSQL schema migration."""

from migrate_postgres import main


if __name__ == "__main__":
    raise SystemExit(main())
