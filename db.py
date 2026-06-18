"""Database helpers for TrainerPro."""
import sqlite3

DB_PATH = 'trainer_app.db'


def get_db():
    """Return a SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn