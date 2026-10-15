import sqlite3

DB_PATH = "instance/hostelhub.db"

db = sqlite3.connect(DB_PATH)

columns = {
    row[1]
    for row in db.execute(
        "PRAGMA table_info(services)"
    ).fetchall()
}

migrations = {
    "category_id": "ALTER TABLE services ADD COLUMN category_id INTEGER",
    "shop_id": "ALTER TABLE services ADD COLUMN shop_id INTEGER",
    "estimated_minutes": (
        "ALTER TABLE services ADD COLUMN estimated_minutes INTEGER DEFAULT 30"
    ),
}

for column, sql in migrations.items():
    if column not in columns:
        db.execute(sql)
        print(f"Added column: {column}")
    else:
        print(f"Already exists: {column}")

db.commit()
db.close()

print("Services table migration completed.")