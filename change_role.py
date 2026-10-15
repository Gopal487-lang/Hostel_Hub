import sqlite3

connection = sqlite3.connect("instance/hostelhub.db")

connection.execute(
"UPDATE users SET role = ? WHERE id = ?",
("admin", 1)
)

connection.commit()

user = connection.execute(
"SELECT id, full_name, role FROM users WHERE id = ?",
(1,)
).fetchone()

connection.close()

print("Updated account:", user)
