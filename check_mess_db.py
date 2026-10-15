
import sqlite3

DB_PATH = "instance/hostelhub.db"

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

try:
    tables = db.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
    """).fetchall()

    print("=== EXISTING TABLES ===")

    for table in tables:
        name = table["name"]
        print(f"\nTable: {name}")

        if name.lower() in (
            "mess_menus",
            "mess_menu",
            "mess_routines"
        ):
            columns = db.execute(
                f'PRAGMA table_info("{name}")'
            ).fetchall()

            for column in columns:
                print(
                    f"  {column['name']} ({column['type']})"
                )

finally:
    db.close()
