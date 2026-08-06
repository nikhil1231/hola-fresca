"""DDL for catalogue-aware planner cache revisions.

The planner reads only five catalogue tables.  SQLite keeps personal state in
the same file, so the file's mtime cannot say whether a write matters to the
planner.  These triggers maintain two cheap, process-independent generations
that can.
"""
from __future__ import annotations

RECIPE_TABLES = ("recipes", "recipe_ingredients")
INGREDIENT_TABLES = ("ingredient_mappings", "ingredient_mapping_products", "products")


def trigger_name(table: str, operation: str) -> str:
    return f"trg_planner_cache_{table}_{operation.lower()}"


def create_trigger_statements() -> tuple[str, ...]:
    statements: list[str] = [
        """
        INSERT OR IGNORE INTO planner_cache_state
            (id, recipe_revision, ingredient_revision)
        VALUES (1, 1, 1)
        """
    ]
    for column, tables in (
        ("recipe_revision", RECIPE_TABLES),
        ("ingredient_revision", INGREDIENT_TABLES),
    ):
        for table in tables:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                statements.append(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name(table, operation)}
                    AFTER {operation} ON {table}
                    BEGIN
                        UPDATE planner_cache_state
                        SET {column} = {column} + 1
                        WHERE id = 1;
                    END
                    """
                )
    return tuple(statements)


def drop_trigger_statements() -> tuple[str, ...]:
    return tuple(
        f"DROP TRIGGER IF EXISTS {trigger_name(table, operation)}"
        for table in (*RECIPE_TABLES, *INGREDIENT_TABLES)
        for operation in ("INSERT", "UPDATE", "DELETE")
    )
