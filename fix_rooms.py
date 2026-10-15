
import sqlite3

DB_PATH = "instance/hostelhub.db"

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

try:
    # Check existing rooms table columns
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(rooms)").fetchall()
    }

    if not columns:
        print("ERROR: rooms table nahi mili. Database path check karo.")

    else:
        print("Existing rooms columns:", ", ".join(sorted(columns)))

        # Add only missing columns
        migrations = {
            "capacity": "ALTER TABLE rooms ADD COLUMN capacity INTEGER NOT NULL DEFAULT 4",
            "occupied": "ALTER TABLE rooms ADD COLUMN occupied INTEGER NOT NULL DEFAULT 0",
            "status": "ALTER TABLE rooms ADD COLUMN status TEXT NOT NULL DEFAULT 'Available'",
            "room_type": "ALTER TABLE rooms ADD COLUMN room_type TEXT DEFAULT 'Standard'",
            "floor": "ALTER TABLE rooms ADD COLUMN floor INTEGER DEFAULT 1",
        }

        for column, sql in migrations.items():
            if column not in columns:
                db.execute(sql)
                print(f"Added column: {column}")
            else:
                print(f"Already exists: {column}")

        db.commit()
        print("\nRooms table migration completed successfully!")

finally:
    db.close()
