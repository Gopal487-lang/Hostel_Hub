import sqlite3
from pathlib import Path

db_path = Path("instance/hostelhub.db").resolve()

print("DATABASE PATH:", db_path)
print("DATABASE EXISTS:", db_path.exists())

db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("\nALL ORDERS:")
orders = db.execute("""
    SELECT id, student_id, order_type, status, total_amount
    FROM orders
    ORDER BY id DESC
""").fetchall()

for order in orders:
    print(dict(order))

print("\nORDER ITEMS:")
items = db.execute("""
    SELECT order_id, item_name, quantity, unit_price, price
    FROM order_items
    ORDER BY order_id DESC
""").fetchall()

for item in items:
    print(dict(item))

db.close()