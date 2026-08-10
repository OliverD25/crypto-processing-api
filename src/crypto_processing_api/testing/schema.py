"""A normalized, comparable description of the database schema.

Built from `pg_catalog` rather than from `pg_dump`, for one reason: the
round-trip test compares two dumps for equality, and `pg_dump` output is not
stable enough to compare. It carries a server version banner, its object order
varies, and it prints `SET` preambles that change between releases. A test that
flakes on day one gets deleted by week two.

Every query below is explicitly ordered, so the same schema always produces the
same text. What it captures is exactly what this schema is full of and what a
careless `downgrade` forgets: enum values, triggers, partial indexes, CHECK
constraints.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Excluded from the fingerprint: its single row is the migration pointer, and
#: it differs by construction between "at head" and "at base".
VERSION_TABLE = "alembic_version"

_ENUMS = """
SELECT t.typname, e.enumlabel
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public'
ORDER BY t.typname, e.enumsortorder
"""

_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable,
       COALESCE(column_default, ''), COALESCE(character_maximum_length::text, ''),
       COALESCE(numeric_precision::text, '')
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, column_name
"""

_CONSTRAINTS = """
SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace n ON n.oid = rel.relnamespace
WHERE n.nspname = 'public'
ORDER BY rel.relname, con.conname
"""

_INDEXES = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname
"""

_TRIGGERS = """
SELECT rel.relname, tg.tgname, pg_get_triggerdef(tg.oid)
FROM pg_trigger tg
JOIN pg_class rel ON rel.oid = tg.tgrelid
JOIN pg_namespace n ON n.oid = rel.relnamespace
WHERE n.nspname = 'public' AND NOT tg.tgisinternal
ORDER BY rel.relname, tg.tgname
"""

_FUNCTIONS = """
SELECT p.proname, pg_get_functiondef(p.oid)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY p.proname
"""

_TABLES = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""


def schema_fingerprint(connection: Connection) -> str:
    """A deterministic text rendering of the public schema.

    Two databases with the same fingerprint have the same tables, columns,
    constraints, indexes, enum values, triggers and functions.
    """
    lines: list[str] = []

    for section, query, prefix in (
        ("enums", _ENUMS, "enum"),
        ("columns", _COLUMNS, "column"),
        ("constraints", _CONSTRAINTS, "constraint"),
        ("indexes", _INDEXES, "index"),
        ("triggers", _TRIGGERS, "trigger"),
        ("functions", _FUNCTIONS, "function"),
    ):
        lines.append(f"# {section}")
        for row in connection.execute(text(query)):
            values = [str(value).strip() for value in row]
            if values[0] == VERSION_TABLE:
                continue
            lines.append(f"{prefix}: " + " | ".join(values))

    return "\n".join(lines) + "\n"


def public_tables(connection: Connection) -> list[str]:
    return [row[0] for row in connection.execute(text(_TABLES))]
