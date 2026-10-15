
import sqlite3

db = sqlite3.connect("instance/hostelhub.db")
db.execute("PRAGMA foreign_keys = ON")

try:
    shops = db.execute("""
        SELECT id, name FROM shops
        WHERE LOWER(name) LIKE '%hostelhub sample shop%'
           OR LOWER(name) LIKE '%hostelhub demo store%'
    """).fetchall()

    print("Sample shops found:", len(shops))

    for shop_id, name in shops:
        print(f"Shop ID: {shop_id} | {name}")

    if not shops:
        print("No matching sample shops found.")
    else:
        confirm = input("\nIn sample shops ko delete karna hai? (YES/NO): ")

        if confirm.strip() == "YES":
            ids = [shop[0] for shop in shops]
            placeholders = ",".join("?" for _ in ids)

            db.execute(
                f"DELETE FROM products WHERE shop_id IN ({placeholders})",
                ids
            )

            db.execute(
                f"DELETE FROM shops WHERE id IN ({placeholders})",
                ids
            )

            db.commit()
            print("Deleted shops:", len(ids))
            print("Cleanup completed successfully.")
        else:
            print("Deletion cancelled.")

except sqlite3.Error as e:
    db.rollback()
    print("Database error:", e)

finally:
    db.close()