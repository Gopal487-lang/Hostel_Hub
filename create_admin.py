import getpass
import sqlite3

from werkzeug.security import generate_password_hash

from app import app, init_db, get_db


def create_admin():
    print("\n================================")
    print("   HOSTELHUB ADMIN SETUP")
    print("================================")

    full_name = input("Enter admin name: ").strip()
    email = input("Enter admin email: ").strip().lower()
    password = getpass.getpass("Create admin password: ")
    confirm_password = getpass.getpass("Confirm admin password: ")

    if not full_name or not email or not password:
        print("Error: All fields are required.")
        return

    if "@" not in email:
        print("Error: Please enter a valid email.")
        return

    if len(password) < 8:
        print("Error: Password must contain at least 8 characters.")
        return

    if password != confirm_password:
        print("Error: Passwords do not match.")
        return

    with app.app_context():
        init_db()
        db = get_db()

        existing_user = db.execute(
            "SELECT id, role FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if existing_user:
            print(
                f"Error: This email already exists "
                f"with role '{existing_user['role']}'."
            )
            return

        try:
            cursor = db.execute(
                """
                INSERT INTO users (
                    full_name,
                    email,
                    password_hash,
                    role,
                    is_active
                )
                VALUES (?, ?, ?, 'admin', 1)
                """,
                (
                    full_name,
                    email,
                    generate_password_hash(password)
                )
            )

            db.commit()

            print("\nAdmin account created successfully!")
            print("Admin ID:", cursor.lastrowid)
            print("Admin Name:", full_name)
            print("Admin Email:", email)
            print("Role: admin")
            print("\nYou can now log in using the Admin tab.")

        except sqlite3.Error as error:
            db.rollback()
            print("Database error:", error)


if __name__ == "__main__":
    create_admin()