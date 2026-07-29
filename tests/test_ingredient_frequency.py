from __future__ import annotations

import csv
import sqlite3

from app.analysis.ingredient_frequency import analyze_ingredients


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            is_complete INTEGER NOT NULL,
            curated INTEGER NOT NULL
        );
        CREATE TABLE recipe_ingredients (
            id INTEGER PRIMARY KEY,
            recipe_id INTEGER NOT NULL,
            source_ingredient_id TEXT,
            name TEXT NOT NULL,
            amount REAL,
            unit TEXT
        );
        INSERT INTO recipes VALUES
            (1, 'kept one', 1, 1),
            (2, 'kept two', 1, 1),
            (3, 'uncurated', 1, 0);
        INSERT INTO recipe_ingredients VALUES
            (1, 1, 'garlic', 'Garlic Clove', 1, 'unit(s)'),
            (2, 1, 'garlic', 'Garlic Clove', 1, 'unit(s)'),
            (3, 2, 'garlic', 'Garlic Clove', 2, NULL),
            (4, 2, 'rice', 'Basmati Rice', 150, 'grams'),
            (5, 3, 'rice', 'Basmati Rice', 300, 'grams');
        """
    )
    conn.commit()
    conn.close()


def test_analyze_ingredients_prefers_curated_and_sums_duplicate_recipe_lines(tmp_path):
    db_path = tmp_path / "recipes.db"
    output_path = tmp_path / "ingredient_frequency.csv"
    _make_db(db_path)

    result = analyze_ingredients(db_path, output_path)

    assert result.library_filter == "curated"
    assert result.recipe_count == 2
    assert result.ingredient_line_count == 4
    garlic = result.rows[0]
    assert garlic.name == "Garlic Clove"
    assert garlic.source_ingredient_ids == "garlic"
    assert garlic.recipe_count == 2
    assert garlic.line_count == 3
    assert garlic.median_metric_amount == 10.0
    assert garlic.metric_unit == "g"

    with output_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["name"] == "Garlic Clove"
    assert rows[1]["name"] == "Basmati Rice"


def test_analyze_ingredients_falls_back_to_complete_when_curated_missing(tmp_path):
    db_path = tmp_path / "recipes.db"
    output_path = tmp_path / "ingredient_frequency.csv"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            is_complete INTEGER NOT NULL
        );
        CREATE TABLE recipe_ingredients (
            id INTEGER PRIMARY KEY,
            recipe_id INTEGER NOT NULL,
            source_ingredient_id TEXT,
            name TEXT NOT NULL,
            amount REAL,
            unit TEXT
        );
        INSERT INTO recipes VALUES (1, 'complete', 1), (2, 'stub', 0);
        INSERT INTO recipe_ingredients VALUES
            (1, 1, 'rice', 'Basmati Rice', 150, 'grams'),
            (2, 2, 'rice', 'Basmati Rice', 300, 'grams');
        """
    )
    conn.commit()
    conn.close()

    result = analyze_ingredients(db_path, output_path)

    assert result.library_filter == "complete"
    assert result.recipe_count == 1
    assert result.rows[0].recipe_count == 1
    assert result.rows[0].median_metric_amount == 150.0


def test_analyze_ingredients_can_keep_source_ids_separate(tmp_path):
    db_path = tmp_path / "recipes.db"
    output_path = tmp_path / "ingredient_frequency.csv"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            curated INTEGER NOT NULL
        );
        CREATE TABLE recipe_ingredients (
            id INTEGER PRIMARY KEY,
            recipe_id INTEGER NOT NULL,
            source_ingredient_id TEXT,
            name TEXT NOT NULL,
            amount REAL,
            unit TEXT
        );
        INSERT INTO recipes VALUES (1, 'one', 1), (2, 'two', 1);
        INSERT INTO recipe_ingredients VALUES
            (1, 1, 'honey-a', 'Honey', 15, 'grams'),
            (2, 2, 'honey-b', 'Honey', 15, 'grams');
        """
    )
    conn.commit()
    conn.close()

    by_name = analyze_ingredients(db_path, output_path)
    by_source = analyze_ingredients(db_path, output_path, group_by="source-id")

    assert len(by_name.rows) == 1
    assert by_name.rows[0].recipe_count == 2
    assert len(by_source.rows) == 2
