import os
import sqlite3
import secrets
from pathlib import Path
from functools import wraps
import csv
import io
from flask import Response, request, send_from_directory
from datetime import datetime


from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    g,
    abort,
    Response
)

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    g,
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)
from werkzeug.utils import secure_filename


# =========================================================
# HOSTELHUB CONFIGURATION
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

DATABASE = Path(
    os.environ.get(
        "DATABASE_PATH",
        str(INSTANCE_DIR / "hostelhub.db")
    )
)

DATABASE.parent.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = Path(os.environ.get("UPLOADS_PATH", str(BASE_DIR / "static" / "uploads")))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

@app.route("/service-worker.js")
def pwa_service_worker():
    response = app.send_static_file("service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    # Keep a stable key for local development without requiring shell setup.
    secret_key_path = INSTANCE_DIR / "secret.key"
    try:
        secret_key = secret_key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret_key = secrets.token_hex(32)
        try:
            with secret_key_path.open("x", encoding="utf-8") as secret_file:
                secret_file.write(secret_key)
        except FileExistsError:
            secret_key = secret_key_path.read_text(encoding="utf-8").strip()
app.config["SECRET_KEY"] = secret_key

app.config["DATABASE"] = str(DATABASE)


# =========================================================
# DATABASE CONNECTION
# =========================================================

class _RemoteRow:
    """Provide sqlite3.Row-style name and positional access for Turso rows."""

    def __init__(self, columns, values):
        self._columns = list(columns)
        self._values = tuple(values)
        self._indexes = {name: index for index, name in enumerate(self._columns)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._indexes[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._columns)


class _RemoteCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def _row(self, row):
        if row is None:
            return None
        columns = [column[0] for column in (self._cursor.description or ())]
        return _RemoteRow(columns, row)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [self._row(row) for row in rows]

    def __iter__(self):
        for row in self._cursor:
            yield self._row(row)


class _RemoteConnection:
    """Small sqlite3-compatible adapter around Turso's DB-API driver."""

    def __init__(self, connection):
        self._connection = connection
        self.row_factory = None

    @staticmethod
    def _raise_sqlite_error(error):
        message = str(error)
        if "constraint" in message.lower() or "unique" in message.lower():
            raise sqlite3.IntegrityError(message) from error
        raise sqlite3.OperationalError(message) from error

    def execute(self, sql, parameters=()):
        # Foreign-key enforcement is connection-local in SQLite. Turso's
        # serverless HTTP connection does not expose that PRAGMA; application
        # routes validate related records before writing.
        if sql.strip().lower().startswith("pragma foreign_keys"):
            return _RemoteCursor(_EmptyCursor())
        try:
            return _RemoteCursor(self._connection.execute(sql, parameters))
        except Exception as error:
            self._raise_sqlite_error(error)

    def executemany(self, sql, parameter_rows):
        """Run a statement once for each parameter row (sqlite3-compatible)."""
        cursor = _RemoteCursor(_EmptyCursor())
        for parameters in parameter_rows:
            cursor = self.execute(sql, parameters)
        return cursor

    def executescript(self, script):
        statement = ""
        cursor = _RemoteCursor(_EmptyCursor())
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    cursor = self.execute(statement)
                statement = ""
        if statement.strip():
            cursor = self.execute(statement)
        return cursor

    def commit(self):
        try:
            return self._connection.commit()
        except Exception as error:
            self._raise_sqlite_error(error)

    def rollback(self):
        try:
            return self._connection.rollback()
        except Exception as error:
            self._raise_sqlite_error(error)

    def close(self):
        return self._connection.close()


class _EmptyCursor:
    description = None
    lastrowid = None
    rowcount = -1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())

def get_db():
    if "db" not in g:
        turso_url = os.environ.get("TURSO_DATABASE_URL")
        turso_token = os.environ.get("TURSO_AUTH_TOKEN")
        if turso_url:
            if not turso_token:
                raise RuntimeError("TURSO_AUTH_TOKEN is required with TURSO_DATABASE_URL")
            try:
                import turso_serverless
            except ImportError as error:
                raise RuntimeError("Install turso_serverless to use the hosted database") from error
            remote_connection = turso_serverless.connect(turso_url, auth_token=turso_token)
            g.db = _RemoteConnection(remote_connection)
        else:
            g.db = sqlite3.connect(
                app.config["DATABASE"],
                timeout=30
            )
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 30000")

    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)

    if db is not None:
        db.close()


def save_uploaded_image(upload, folder, prefix):
    """Validate and save an uploaded image in the configured uploads directory."""
    if upload is None or not upload.filename:
        return None

    safe_name = secure_filename(upload.filename)
    extension = Path(safe_name).suffix.lower()
    allowed_signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".webp": (b"RIFF",),
    }
    payload = upload.read(5 * 1024 * 1024 + 1)
    signature_ok = (
        extension in allowed_signatures
        and len(payload) <= 5 * 1024 * 1024
        and any(payload.startswith(signature) for signature in allowed_signatures.get(extension, ()))
        and (extension != ".webp" or payload[8:12] == b"WEBP")
    )
    if not signature_ok:
        raise ValueError("Image must be a valid PNG, JPG or WebP file under 5 MB.")

    if os.environ.get("CLOUDINARY_URL"):
        try:
            import cloudinary
            import cloudinary.uploader
        except ImportError as error:
            raise RuntimeError("Install cloudinary to use hosted image storage") from error

        result = cloudinary.uploader.upload(
            io.BytesIO(payload),
            folder=f"hostelhub/{folder}",
            resource_type="image",
        )
        return result["secure_url"]

    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{extension}"
    image_directory = UPLOADS_DIR / folder
    image_directory.mkdir(parents=True, exist_ok=True)
    (image_directory / filename).write_bytes(payload)
    return f"/uploads/{folder}/{filename}"


def save_product_image(upload):
    return save_uploaded_image(upload, "products", "product")


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():
    db = get_db()

    # -----------------------------------------------------
    # CREATE TABLES
    # -----------------------------------------------------

    db.executescript("""

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student'
                CHECK (
                    role IN (
                        'student',
                        'admin',
                        'warden',
                        'provider'
                    )
                ),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );


        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            icon TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );


        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category_id INTEGER,
            address TEXT,
            opening_hours TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (category_id)
                REFERENCES categories(id)
        );


        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL DEFAULT 0,
            image_url TEXT,
            sizes TEXT NOT NULL DEFAULT '',
            out_of_stock_sizes TEXT NOT NULL DEFAULT '',
            stock_quantity INTEGER NOT NULL DEFAULT 0,
            is_available INTEGER NOT NULL DEFAULT 1,
            is_published INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (shop_id)
                REFERENCES shops(id)
        );

        CREATE TABLE IF NOT EXISTS medical_shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            phone TEXT,
            address TEXT,
            opening_hours TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS medical_medicines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medical_shop_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL DEFAULT 0,
            stock_quantity INTEGER NOT NULL DEFAULT 0,
            image_url TEXT,
            is_available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (medical_shop_id) REFERENCES medical_shops(id)
        );

        CREATE TABLE IF NOT EXISTS medical_doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            specialty TEXT,
            clinic_name TEXT,
            phone TEXT,
            availability TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS medical_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            preferred_at TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',
            admin_notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (doctor_id) REFERENCES medical_doctors(id)
        );

        CREATE TABLE IF NOT EXISTS medical_facilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            facility_type TEXT NOT NULL DEFAULT 'Clinic',
            phone TEXT,
            address TEXT,
            opening_hours TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS medical_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            request_type TEXT NOT NULL DEFAULT 'Medical Help',
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            admin_notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES users(id)
        );


        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category_id INTEGER,
            provider_id INTEGER,
            shop_id INTEGER,
            price REAL NOT NULL DEFAULT 0,
            image_url TEXT,
            estimated_minutes INTEGER NOT NULL DEFAULT 30,
            is_available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (category_id)
                REFERENCES categories(id),

            FOREIGN KEY (provider_id)
                REFERENCES users(id),

            FOREIGN KEY (shop_id)
                REFERENCES shops(id)
        );

        CREATE TABLE IF NOT EXISTS service_bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            preferred_at TEXT,
            details TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            admin_notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id)
        );


        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            total_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Pending',
            delivery_address TEXT,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (student_id)
                REFERENCES users(id)
        );


        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (student_id)
                REFERENCES users(id)
        );


        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_number TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL DEFAULT 1,
            occupied INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Available'
        );


        CREATE TABLE IF NOT EXISTS mess_menus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_type TEXT NOT NULL,
            menu_date TEXT NOT NULL,
            items TEXT NOT NULL
        );


        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,

            UNIQUE (student_id, product_id),

            FOREIGN KEY (student_id)
                REFERENCES users(id),

            FOREIGN KEY (product_id)
                REFERENCES products(id)
        );


        CREATE TABLE IF NOT EXISTS wishlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,

            UNIQUE (student_id, product_id),

            FOREIGN KEY (student_id)
                REFERENCES users(id),

            FOREIGN KEY (product_id)
                REFERENCES products(id)
        );


        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            price REAL NOT NULL DEFAULT 0,

            FOREIGN KEY (order_id)
                REFERENCES orders(id),

            FOREIGN KEY (product_id)
                REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS medical_cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            medicine_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            UNIQUE (student_id, medicine_id),
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (medicine_id) REFERENCES medical_medicines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS medical_wishlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            medicine_id INTEGER NOT NULL,
            UNIQUE (student_id, medicine_id),
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (medicine_id) REFERENCES medical_medicines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS medical_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            medicine_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY (medicine_id) REFERENCES medical_medicines(id)
        );

    """)

    # -----------------------------------------------------
    # SAFE DATABASE MIGRATIONS
    # -----------------------------------------------------

    # Users table migrations

    user_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    }

    if "phone" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN phone TEXT"
        )

    if "student_id" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN student_id TEXT"
        )

    # Categories table migration

    shop_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(shops)").fetchall()
    }
    if "provider_id" not in shop_columns:
        db.execute("ALTER TABLE shops ADD COLUMN provider_id INTEGER REFERENCES users(id)")

    category_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(categories)"
        ).fetchall()
    }

    if "is_active" not in category_columns:
        db.execute("""
            ALTER TABLE categories
            ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
        """)

    # Products table migrations

    product_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(products)"
        ).fetchall()
    }

    if "stock_quantity" not in product_columns:
        db.execute("""
            ALTER TABLE products
            ADD COLUMN stock_quantity INTEGER NOT NULL DEFAULT 0
        """)

    if "is_published" not in product_columns:
        db.execute("""
            ALTER TABLE products
            ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0
        """)

    if "sizes" not in product_columns:
        db.execute("ALTER TABLE products ADD COLUMN sizes TEXT NOT NULL DEFAULT ''")

    if "out_of_stock_sizes" not in product_columns:
        db.execute("ALTER TABLE products ADD COLUMN out_of_stock_sizes TEXT NOT NULL DEFAULT ''")

    # Previously uploaded files used Flask's static URL. They now live in the
    # configurable uploads directory so a Render persistent disk can serve them.
    db.execute("""
        UPDATE products
        SET image_url = REPLACE(image_url, '/static/uploads/', '/uploads/')
        WHERE image_url LIKE '/static/uploads/%'
    """)
    db.execute("""
        UPDATE medical_medicines
        SET image_url = REPLACE(image_url, '/static/uploads/', '/uploads/')
        WHERE image_url LIKE '/static/uploads/%'
    """)

    # Orders table migration

    order_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(orders)"
        ).fetchall()
    }

    if "payment_status" not in order_columns:
        db.execute("""
            ALTER TABLE orders
            ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'Pending'
        """)

    if "payment_reference" not in order_columns:
        db.execute("""
            ALTER TABLE orders
            ADD COLUMN payment_reference TEXT
        """)

    if "order_type" not in order_columns:
        db.execute("""
            ALTER TABLE orders
            ADD COLUMN order_type TEXT NOT NULL DEFAULT 'product'
        """)

    if "estimated_delivery" not in order_columns:
        db.execute("""
            ALTER TABLE orders
            ADD COLUMN estimated_delivery TEXT
        """)

    order_item_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(order_items)").fetchall()
    }
    if "item_name" not in order_item_columns:
        db.execute("""
            ALTER TABLE order_items
            ADD COLUMN item_name TEXT NOT NULL DEFAULT ''
        """)
    if "unit_price" not in order_item_columns:
        db.execute("""
            ALTER TABLE order_items
            ADD COLUMN unit_price REAL NOT NULL DEFAULT 0
        """)
        db.execute("""
            UPDATE order_items
            SET unit_price = price
            WHERE price != 0
        """)
    if "item_name" not in order_item_columns:
        db.execute("""
            UPDATE order_items
            SET item_name = COALESCE(
                (SELECT name FROM products WHERE products.id = order_items.product_id),
                'Product'
            )
        """)

    # Services table migrations

    service_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(services)"
        ).fetchall()
    }

    if "shop_id" not in service_columns:
        db.execute("""
            ALTER TABLE services
            ADD COLUMN shop_id INTEGER REFERENCES shops(id)
        """)

    if "provider_id" not in service_columns:
        db.execute("""
            ALTER TABLE services
            ADD COLUMN provider_id INTEGER REFERENCES users(id)
        """)

    if "estimated_minutes" not in service_columns:
        db.execute("""
            ALTER TABLE services
            ADD COLUMN estimated_minutes INTEGER NOT NULL DEFAULT 30
        """)

    
    # -----------------------------------------------------
    # DEFAULT PRODUCT CATEGORIES
    # -----------------------------------------------------

    categories = [
        
        (
            "Clothing & Tailoring",
            "clothing-tailoring",
            "Stitching, alterations and fitting",
            "👕"
        ),
        (
                    "Daily Essentials",
                    "daily-essentials",
                    "Everyday products and hostel necessities",
                    "🛒"
                ),
        (
            "Electronics & Mobile Repair",
            "electronics-mobile-repair",
            "Mobile repair, electronic accessories and related services",
            "📱"
        ),
        (
            "Food & Snacks",
            "food-snacks",
            "Meals, breakfast, snacks and beverages",
            "🍔"
        ),
        (
            "Laundry",
            "laundry",
            "Washing, ironing and clothes pickup",
            "🧺"
        ),
        (
                    "Medical & Health",
                    "medical-health",
                    "Medical stores, healthcare essentials and health-related services",
                    "🏥"
                ),
        (
            "Printing Services",
            "printing",
            "Printing, photocopy and scanning",
            "🖨️"
        ),
        
        (
            "Other Services",
            "other-services",
            "Discover other nearby services",
            "✨"
        )
    ]

    db.executemany("""
        INSERT OR IGNORE INTO categories
            (name, slug, description, icon)
        VALUES (?, ?, ?, ?)
    """, categories)

    # -----------------------------------------------------
    # DEMO SHOP
    # -----------------------------------------------------

    demo_category = db.execute("""
        SELECT id
        FROM categories
        WHERE slug = ?
    """, ("daily-essentials",)).fetchone()

    if demo_category:

        db.execute("""
            INSERT OR IGNORE INTO shops (
                name,
                description,
                category_id,
                address,
                opening_hours,
                is_verified,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, 1, 1)
        """, (
            "HostelHub Demo Store",
            "Sample store for testing the student marketplace.",
            demo_category["id"],
            "Campus Demo Shop",
            "9:00 AM - 9:00 PM"
        ))

        demo_shop = db.execute("""
            SELECT id
            FROM shops
            WHERE name = ?
        """, ("HostelHub Demo Store",)).fetchone()

        if demo_shop:

            demo_products = [
                (
                    "Notebook",
                    "Useful for classes and daily notes.",
                    50.00
                ),
                (
                    "Blue Ball Pen",
                    "Smooth-writing pen for everyday use.",
                    10.00
                ),
                (
                    "Water Bottle",
                    "Reusable bottle for campus and hostel.",
                    120.00
                ),
                (
                    "Instant Noodles",
                    "Quick snack for hostel evenings.",
                    25.00
                )
            ]

            for name, description, price in demo_products:

                db.execute("""
                    INSERT INTO products (
                        shop_id,
                        name,
                        description,
                        price,
                        stock_quantity,
                        is_available,
                        is_published
                    )
                    SELECT ?, ?, ?, ?, 100, 1, 1
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM products
                        WHERE shop_id = ?
                          AND name = ?
                    )
                """, (
                    demo_shop["id"],
                    name,
                    description,
                    price,
                    demo_shop["id"],
                    name
                ))

            # ==============================================
    # HOSTELHUB ADMIN MODULES - SAFE DB MIGRATIONS
    # ==============================================

    # Add shop connection to services.
    # ==========================================
# SERVICES TABLE - SAFE COLUMN MIGRATION
# ==========================================

    service_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(services)"
        ).fetchall()
    }

    if "category_id" not in service_columns:
        db.execute(
            "ALTER TABLE services ADD COLUMN category_id INTEGER"
        )

    if "shop_id" not in service_columns:
        db.execute(
            "ALTER TABLE services ADD COLUMN shop_id INTEGER"
        )

    if "estimated_minutes" not in service_columns:
        db.execute(
            "ALTER TABLE services ADD COLUMN estimated_minutes INTEGER DEFAULT 30"
        )

    db.commit()

    # Additional room details.
    room_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(rooms)"
        ).fetchall()
    }

    if "room_type" not in room_columns:
        db.execute(
            "ALTER TABLE rooms ADD COLUMN room_type TEXT DEFAULT 'Standard'"
        )

    if "floor" not in room_columns:
        db.execute(
            "ALTER TABLE rooms ADD COLUMN floor TEXT"
        )

    # Complaint priority.
    complaint_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(complaints)"
        ).fetchall()
    }

    if "priority" not in complaint_columns:
        db.execute(
            "ALTER TABLE complaints ADD COLUMN priority TEXT DEFAULT 'Medium'"
        )

    if "subject" not in complaint_columns:
        db.execute("ALTER TABLE complaints ADD COLUMN subject TEXT")

    if "category" not in complaint_columns:
        db.execute("ALTER TABLE complaints ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
    if "warden_reply" not in complaint_columns:
        db.execute("ALTER TABLE complaints ADD COLUMN warden_reply TEXT")

    # Persistent application settings.
    db.execute("""
        CREATE TABLE IF NOT EXISTS hostel_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL UNIQUE,
            setting_value TEXT NOT NULL DEFAULT ''
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS hostel_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_by INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    # Store room allocation records separately.
    db.execute("""
        CREATE TABLE IF NOT EXISTS room_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            allocated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'Active',
            FOREIGN KEY (room_id) REFERENCES rooms(id),
            FOREIGN KEY (student_id) REFERENCES users(id)
        )
    """)

    db.commit()

    # Save all initialization and migration changes.

    db.commit()


# =========================================================
# AUTHENTICATION AND ROLE PROTECTION
# =========================================================

def role_required(*allowed_roles):

    def decorator(view_function):

        @wraps(view_function)
        def wrapped_view(*args, **kwargs):

            if not session.get("user_id"):
                flash(
                    "Please log in first.",
                    "error"
                )

                return redirect(url_for("login"))

            role = session.get("user_role")

            if role not in allowed_roles:
                flash(
                    "You are not authorized to access this page.",
                    "error"
                )

                return redirect(url_for("dashboard"))

            return view_function(*args, **kwargs)

        return wrapped_view

    return decorator


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
def home():

    db = get_db()

    categories = db.execute("""
        SELECT id, name, slug, description, icon
        FROM categories
        WHERE is_active = 1
        ORDER BY id
    """).fetchall()

    products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            c.name AS category_name,
            c.slug AS category_slug,
            s.name AS shop_name
        FROM products AS p
        JOIN shops AS s
            ON s.id = p.shop_id
        LEFT JOIN categories AS c
            ON c.id = s.category_id
        WHERE p.is_available = 1
          AND p.is_published = 1
          AND s.is_active = 1
        ORDER BY p.id DESC
    """).fetchall()

    services = db.execute("""
    SELECT
        sv.id,
        sv.shop_id,
        sv.name,
        sv.description,
        sv.price,
        sv.estimated_minutes,
        sv.is_available,
        s.name AS shop_name,
        s.category_id,
        s.address AS shop_address,
        s.opening_hours,
        c.name AS category_name,
        c.icon AS category_icon
    FROM services AS sv
    JOIN shops AS s ON s.id = sv.shop_id
    LEFT JOIN categories AS c ON c.id = s.category_id
    WHERE sv.is_available = 1
      AND s.is_active = 1
      AND s.is_verified = 1
      AND c.is_active = 1
    ORDER BY sv.id DESC
""").fetchall()
    return render_template(
        "index.html",
        categories=categories,
        products=products,
        services=services
    )


# =========================================================
# STUDENT REGISTRATION
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        full_name = request.form.get(
            "full_name", ""
        ).strip()

        email = request.form.get(
            "email", ""
        ).strip().lower()

        phone = request.form.get(
            "phone", ""
        ).strip()

        student_id = request.form.get(
            "student_id", ""
        ).strip()

        password = request.form.get(
            "password", ""
        )

        confirm_password = request.form.get(
            "confirm_password", ""
        )

        if not full_name or not email or not password:
            flash(
                "Please fill in all required fields.",
                "error"
            )

            return redirect(url_for("register"))

        if (
            "@" not in email
            or "." not in email.rsplit("@", 1)[-1]
        ):
            flash(
                "Please enter a valid email address.",
                "error"
            )

            return redirect(url_for("register"))

        if len(password) < 8:
            flash(
                "Password must contain at least 8 characters.",
                "error"
            )

            return redirect(url_for("register"))

        if password != confirm_password:
            flash(
                "Passwords do not match.",
                "error"
            )

            return redirect(url_for("register"))

        db = get_db()

        existing_user = db.execute("""
            SELECT id
            FROM users
            WHERE email = ?
        """, (email,)).fetchone()

        if existing_user:
            flash(
                "This email is already registered. Please log in.",
                "error"
            )

            return redirect(url_for("login"))

        password_hash = generate_password_hash(password)

        try:

            db.execute("""
                INSERT INTO users (
                    full_name,
                    email,
                    phone,
                    student_id,
                    password_hash,
                    role,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, 'student', 1)
            """, (
                full_name,
                email,
                phone or None,
                student_id or None,
                password_hash
            ))

            db.commit()

            flash(
                "Registration successful! Please log in.",
                "success"
            )

            return redirect(url_for("login"))

        except sqlite3.IntegrityError:

            db.rollback()

            flash(
                "Registration failed. Please check your details.",
                "error"
            )

            return redirect(url_for("register"))

        except sqlite3.Error:

            db.rollback()

            app.logger.exception("Student registration failed")

            flash(
                "A database error occurred. Please try again.",
                "error"
            )

            return redirect(url_for("register"))

    return render_template("register.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get(
            "email", ""
        ).strip().lower()

        password = request.form.get(
            "password", ""
        )

        selected_role = request.form.get(
            "login_role", "user"
        ).strip().lower()

        allowed_tabs = {
            "user": ["student"],
            "warden": ["warden"],
            "admin": ["admin"],
            "shopkeeper": ["provider"]
        }

        if selected_role not in allowed_tabs:

            flash(
                "Invalid login option.",
                "error"
            )

            return redirect(url_for("login"))

        if not email or not password:

            flash(
                "Please enter your email and password.",
                "error"
            )

            return redirect(url_for("login"))

        db = get_db()

        user = db.execute("""
            SELECT
                id,
                full_name,
                email,
                password_hash,
                role,
                is_active
            FROM users
            WHERE email = ?
        """, (email,)).fetchone()

        if user is None or not check_password_hash(
            user["password_hash"],
            password
        ):

            flash(
                "Invalid email or password.",
                "error"
            )

            return redirect(url_for("login"))

        if not user["is_active"]:

            flash(
                "Your account is inactive. Contact the administrator.",
                "error"
            )

            return redirect(url_for("login"))

        if user["role"] not in allowed_tabs[selected_role]:

            flash(
                f"This account cannot sign in through "
                f"the {selected_role.title()} tab.",
                "error"
            )

            return redirect(url_for("login"))

        session.clear()

        session["user_id"] = user["id"]
        session["user_name"] = user["full_name"]
        session["user_role"] = user["role"]

        flash(
            f"Welcome back, {user['full_name']}!",
            "success"
        )

        return redirect(url_for("dashboard"))

    return render_template("login.html")


# =========================================================
# MAIN DASHBOARD ROUTER
# =========================================================

@app.route("/dashboard")
@role_required("admin", "student", "warden", "provider")
def dashboard():

    role = session.get("user_role")

    if role == "admin":
        return redirect(url_for("admin_dashboard"))

    if role == "warden":
        return redirect(url_for("warden_dashboard"))

    if role == "provider":
        return redirect(url_for("provider_dashboard"))

    return redirect(url_for("student_dashboard"))


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin/dashboard")
@role_required("admin")
def admin_dashboard():

    db = get_db()

    total_students = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE role = 'student'
    """).fetchone()["total"]

    total_shops = db.execute("""
        SELECT COUNT(*) AS total
        FROM shops
    """).fetchone()["total"]

    total_products = db.execute("""
        SELECT COUNT(*) AS total
        FROM products
    """).fetchone()["total"]

    total_orders = db.execute("""
        SELECT COUNT(*) AS total
        FROM orders
    """).fetchone()["total"]

    pending_complaints = db.execute("""
        SELECT COUNT(*) AS total
        FROM complaints
        WHERE status = 'Pending'
    """).fetchone()["total"]
    recent_orders = db.execute("""
        SELECT
            o.id,
            o.total_amount,
            o.status,
            o.payment_status,
            o.created_at,
            u.full_name AS student_name
        FROM orders AS o
        JOIN users AS u
            ON u.id = o.student_id
        ORDER BY o.id DESC
        LIMIT 5
    """).fetchall()

    return render_template(
        "admin_dashboard.html",
        total_students=total_students,
        total_shops=total_shops,
        total_products=total_products,
        total_orders=total_orders,
        pending_complaints=pending_complaints,
        recent_orders=recent_orders
    )

# =========================================================
# HOSTELHUB ADMIN MODULES
# =========================================================

import csv
import io
from flask import Response


# ---------------------------------------------------------
# SHARED STATUS OPTIONS
# ---------------------------------------------------------

COMPLAINT_STATUSES = [
    "Pending",
    "In Progress",
    "Resolved",
    "Rejected"
]

COMPLAINT_PRIORITIES = [
    "Low",
    "Medium",
    "High",
    "Urgent"
]


# ---------------------------------------------------------
# ADMIN SERVICES
# ---------------------------------------------------------

@app.route("/admin/services", methods=["GET", "POST"])
@role_required("admin")
def admin_services():
    db = get_db()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        category_id = request.form.get("category_id", "").strip()
        shop_id = request.form.get("shop_id", "").strip()
        provider_id = request.form.get("provider_id", "").strip()
        price = request.form.get("price", "0").strip()
        estimated_minutes = request.form.get(
            "estimated_minutes", "30"
        ).strip()

        if not name or not category_id.isdigit():
            flash("Service name and a service category are required.", "error")
            return redirect(url_for("admin_services"))

        try:
            price = float(price)
            estimated_minutes = int(estimated_minutes)

            if price < 0 or estimated_minutes < 1:
                raise ValueError
        except ValueError:
            flash("Enter a valid price and estimated time.", "error")
            return redirect(url_for("admin_services"))

        category_id = int(category_id) if category_id.isdigit() else None
        shop_id = int(shop_id) if shop_id.isdigit() else None
        provider_id = int(provider_id) if provider_id.isdigit() else None

        category = db.execute(
            "SELECT id FROM categories WHERE id = ? AND is_active = 1", (category_id,)
        ).fetchone()
        if category is None:
            flash("Choose an active service category.", "error")
            return redirect(url_for("admin_services"))

        db.execute("""
            INSERT INTO services (
                name, description, category_id, provider_id, shop_id,
                price, estimated_minutes, is_available
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            name, description, category_id, provider_id, shop_id,
            price, estimated_minutes
        ))

        db.commit()
        flash("Service added successfully.", "success")
        return redirect(url_for("admin_services"))

    services = db.execute("""
        SELECT
            sv.*,
            c.name AS category_name,
            s.name AS shop_name
        FROM services sv
        LEFT JOIN categories c ON c.id = sv.category_id
        LEFT JOIN shops s ON s.id = sv.shop_id
        ORDER BY sv.id DESC
    """).fetchall()

    categories = db.execute("""
        SELECT id, name
        FROM categories
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()

    shops = db.execute("""
        SELECT id, name
        FROM shops
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()

    providers = db.execute("""
        SELECT id, full_name FROM users
        WHERE role = 'provider' AND is_active = 1 ORDER BY full_name
    """).fetchall()

    service_bookings = db.execute("""
        SELECT b.*, sv.name AS service_name, u.full_name AS student_name,
               u.phone AS student_phone
        FROM service_bookings AS b
        JOIN services AS sv ON sv.id = b.service_id
        JOIN users AS u ON u.id = b.student_id
        ORDER BY b.id DESC
    """).fetchall()

    return render_template(
        "admin_module.html",
        module="services",
        title="Services Management",
        services=services,
        categories=categories,
        shops=shops,
        providers=providers,
        service_bookings=service_bookings
    )


@app.route("/admin/service-bookings/<int:booking_id>/update", methods=["POST"])
@role_required("admin")
def update_service_booking(booking_id):
    status = request.form.get("status", "")
    if status not in {
        "Order Placed", "Verified", "Ready for Pickup", "Service Done",
        "Out for Delivery", "Pending", "Confirmed", "In Progress",
        "Completed", "Cancelled",
    }:
        flash("Choose a valid service request status.", "error")
        return redirect(url_for("admin_services"))
    db = get_db()
    db.execute("""
        UPDATE service_bookings SET status = ?, admin_notes = ? WHERE id = ?
    """, (status, request.form.get("admin_notes", "").strip() or None, booking_id))
    db.commit()
    flash("Service request updated.", "success")
    return redirect(url_for("admin_services"))


@app.route(
    "/admin/services/<int:service_id>/toggle",
    methods=["POST"]
)
@role_required("admin")
def toggle_service(service_id):
    db = get_db()

    service = db.execute(
        "SELECT id, is_available FROM services WHERE id = ?",
        (service_id,)
    ).fetchone()

    if service is None:
        flash("Service not found.", "error")
    else:
        new_status = 0 if service["is_available"] else 1

        db.execute(
            "UPDATE services SET is_available = ? WHERE id = ?",
            (new_status, service_id)
        )
        db.commit()
        flash("Service availability updated.", "success")

    return redirect(url_for("admin_services"))


@app.route(
    "/admin/services/<int:service_id>/delete",
    methods=["POST"]
)
@role_required("admin")
def delete_service(service_id):
    db = get_db()

    db.execute(
        "DELETE FROM services WHERE id = ?",
        (service_id,)
    )
    db.commit()

    flash("Service deleted.", "success")
    return redirect(url_for("admin_services"))


# ---------------------------------------------------------
# ADMIN HOSTEL ROOMS
# ---------------------------------------------------------

@app.route("/admin/rooms", methods=["GET", "POST"])
@role_required("admin")
def admin_rooms():
    db = get_db()

    if request.method == "POST":
        room_number = request.form.get(
            "room_number", ""
        ).strip()

        room_type = request.form.get(
            "room_type", "Standard"
        ).strip()

        floor = request.form.get("floor", "").strip()

        try:
            capacity = int(request.form.get("capacity", "1"))
            occupied = int(request.form.get("occupied", "0"))

            if capacity < 1 or occupied < 0 or occupied > capacity:
                raise ValueError
        except ValueError:
            flash(
                "Capacity must be positive and occupancy cannot exceed capacity.",
                "error"
            )
            return redirect(url_for("admin_rooms"))

        if not room_number:
            flash("Room number is required.", "error")
            return redirect(url_for("admin_rooms"))

        status = "Full" if occupied == capacity else "Available"

        try:
            db.execute("""
                INSERT INTO rooms (
                    room_number, capacity, occupied,
                    status, room_type, floor
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                room_number, capacity, occupied,
                status, room_type, floor
            ))

            db.commit()
            flash("Room added successfully.", "success")

        except sqlite3.IntegrityError:
            db.rollback()
            flash("This room number already exists.", "error")

        return redirect(url_for("admin_rooms"))

    rooms = db.execute("""
        SELECT *
        FROM rooms
        ORDER BY room_number
    """).fetchall()

    room_stats = db.execute("""
        SELECT
            COUNT(*) AS total_rooms,
            COALESCE(SUM(capacity), 0) AS total_beds,
            COALESCE(SUM(occupied), 0) AS occupied_beds
        FROM rooms
    """).fetchone()

    return render_template(
        "admin_module.html",
        module="rooms",
        title="Hostel Room Management",
        rooms=rooms,
        room_stats=room_stats
    )


@app.route(
    "/admin/rooms/<int:room_id>/update",
    methods=["POST"]
)
@role_required("admin")
def update_room(room_id):
    db = get_db()

    try:
        capacity = int(request.form.get("capacity", "1"))
        occupied = int(request.form.get("occupied", "0"))

        if capacity < 1 or occupied < 0 or occupied > capacity:
            raise ValueError
    except ValueError:
        flash("Invalid room capacity or occupancy.", "error")
        return redirect(url_for("admin_rooms"))

    room = db.execute(
        "SELECT id FROM rooms WHERE id = ?",
        (room_id,)
    ).fetchone()

    if room is None:
        flash("Room not found.", "error")
        return redirect(url_for("admin_rooms"))

    status = "Full" if occupied == capacity else "Available"

    db.execute("""
        UPDATE rooms
        SET capacity = ?, occupied = ?, status = ?
        WHERE id = ?
    """, (capacity, occupied, status, room_id))

    db.commit()
    flash("Room details updated.", "success")

    return redirect(url_for("admin_rooms"))


@app.route(
    "/admin/rooms/<int:room_id>/delete",
    methods=["POST"]
)
@role_required("admin")
def delete_room(room_id):
    db = get_db()

    allocations = db.execute("""
        SELECT COUNT(*) AS total
        FROM room_allocations
        WHERE room_id = ? AND status = 'Active'
    """, (room_id,)).fetchone()["total"]

    if allocations:
        flash(
            "This room has active allocations and cannot be deleted.",
            "error"
        )
        return redirect(url_for("admin_rooms"))

    db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    db.commit()

    flash("Room deleted.", "success")
    return redirect(url_for("admin_rooms"))



# ---------------------------------------------------------
# ADMIN MESS MANAGEMENT - WEEKLY ROUTINE
# ---------------------------------------------------------

from datetime import date, timedelta

WEEKLY_MEALS = ["Breakfast", "Mid-Meal", "Dinner"]


@app.route("/admin/mess", methods=["GET", "POST"])
@role_required("admin")
def admin_mess():
    db = get_db()

    # Monday to Sunday of the current week
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    week_dates = [
        week_start + timedelta(days=i)
        for i in range(7)
    ]

    if request.method == "POST":
        try:
            for day in week_dates:
                menu_date = day.isoformat()

                for meal_type in WEEKLY_MEALS:
                    field_name = f"{menu_date}_{meal_type}"

                    items = request.form.get(
                        field_name, ""
                    ).strip()

                    existing = db.execute(
                        """
                        SELECT id
                        FROM mess_menus
                        WHERE menu_date = ?
                          AND meal_type = ?
                        """,
                        (menu_date, meal_type)
                    ).fetchone()

                    if existing:
                        # Update existing menu
                        db.execute(
                            """
                            UPDATE mess_menus
                            SET menu_items = ?
                            WHERE menu_date = ?
                              AND meal_type = ?
                            """,
                            (
                                items,
                                menu_date,
                                meal_type
                            )
                        )

                    elif items:
                        # Insert only if menu is not empty
                        db.execute(
                            """
                            INSERT INTO mess_menus
                            (meal_type, menu_date, menu_items)
                            VALUES (?, ?, ?)
                            """,
                            (
                                meal_type,
                                menu_date,
                                items
                            )
                        )

            db.commit()

            flash(
                "Weekly mess routine saved successfully!",
                "success"
            )

        except Exception:
            db.rollback()

            app.logger.exception(
                "Weekly mess routine save failed"
            )

            flash(
                "Menu save nahi hua. Terminal mein error check karein.",
                "error"
            )

        return redirect(url_for("admin_mess"))

    # Fetch current week's saved menus
    saved_rows = db.execute(
        """
        SELECT id, meal_type, menu_date, menu_items
        FROM mess_menus
        WHERE menu_date BETWEEN ? AND ?
        ORDER BY menu_date ASC, id ASC
        """,
        (
            week_dates[0].isoformat(),
            week_dates[-1].isoformat()
        )
    ).fetchall()

    saved_menu = {}

    for row in saved_rows:
        key = (row["menu_date"], row["meal_type"])
        saved_menu[key] = row["menu_items"] or ""

    # Prepare all 7 days and 3 meals
    weekly_menu = {}

    for day in week_dates:
        day_key = day.isoformat()
        weekly_menu[day_key] = {}

        for meal_type in WEEKLY_MEALS:
            weekly_menu[day_key][meal_type] = saved_menu.get(
                (day_key, meal_type),
                ""
            )

    # Keep the existing list for older menu displays
    menus = db.execute(
        """
        SELECT *
        FROM mess_menus
        ORDER BY menu_date DESC, id DESC
        """
    ).fetchall()

    return render_template(
        "admin_module.html",
        module="mess",
        title="Mess Management",
        menus=menus,
        week_dates=week_dates,
        weekly_menu=weekly_menu,
        meals=WEEKLY_MEALS
    )


# ---------------------------------------------------------
# DELETE MESS MENU
# ---------------------------------------------------------

@app.route(
    "/admin/mess/<int:menu_id>/delete",
    methods=["POST"]
)
@role_required("admin")
def delete_mess_menu(menu_id):
    db = get_db()

    existing = db.execute(
        "SELECT id FROM mess_menus WHERE id = ?",
        (menu_id,)
    ).fetchone()

    if existing is None:
        flash("Mess menu not found.", "error")
        return redirect(url_for("admin_mess"))

    try:
        db.execute(
            "DELETE FROM mess_menus WHERE id = ?",
            (menu_id,)
        )

        db.commit()

        flash(
            "Mess menu deleted successfully.",
            "success"
        )

    except sqlite3.Error:
        db.rollback()

        app.logger.exception(
            "Failed to delete mess menu"
        )

        flash(
            "Unable to delete mess menu. Check the terminal.",
            "error"
        )

    return redirect(url_for("admin_mess"))



# ---------------------------------------------------------
# ADMIN COMPLAINTS
# ---------------------------------------------------------

@app.route("/admin/complaints")
@role_required("admin")
def admin_complaints():
    db = get_db()

    selected_status = request.args.get(
        "status", "All"
    ).strip()

    search = request.args.get("search", "").strip()

    conditions = []
    params = []

    if selected_status in COMPLAINT_STATUSES:
        conditions.append("c.status = ?")
        params.append(selected_status)

    if search:
        term = f"%{search}%"
        conditions.append("""
            (
                c.subject LIKE ?
                OR c.description LIKE ?
                OR u.full_name LIKE ?
                OR u.email LIKE ?
            )
        """)
        params.extend([term, term, term, term])

    where_sql = (
        "WHERE " + " AND ".join(conditions)
        if conditions else ""
    )

    complaints = db.execute(f"""
        SELECT
            c.*,
            u.full_name AS student_name,
            u.email AS student_email
        FROM complaints c
        JOIN users u ON u.id = c.student_id
        {where_sql}
        ORDER BY
            CASE c.priority
                WHEN 'Urgent' THEN 1
                WHEN 'High' THEN 2
                WHEN 'Medium' THEN 3
                ELSE 4
            END,
            c.id DESC
    """, params).fetchall()

    return render_template(
        "admin_module.html",
        module="complaints",
        title="User Complaints",
        complaints=complaints,
        statuses=COMPLAINT_STATUSES,
        priorities=COMPLAINT_PRIORITIES,
        selected_status=selected_status,
        search=search
    )


@app.route(
    "/admin/complaints/<int:complaint_id>/update",
    methods=["POST"]
)
@role_required("admin")
def update_complaint(complaint_id):
    db = get_db()

    status = request.form.get("status", "").strip()
    priority = request.form.get("priority", "").strip()

    if (
        status not in COMPLAINT_STATUSES
        or priority not in COMPLAINT_PRIORITIES
    ):
        flash("Invalid complaint status or priority.", "error")
        return redirect(url_for("admin_complaints"))

    db.execute("""
        UPDATE complaints
        SET status = ?, priority = ?
        WHERE id = ?
    """, (status, priority, complaint_id))

    db.commit()
    flash("Complaint updated.", "success")

    return redirect(url_for("admin_complaints"))


# ---------------------------------------------------------
# ADMIN REPORTS
# ---------------------------------------------------------

@app.route("/admin/reports")
@role_required("admin")
def admin_reports():
    db = get_db()

    stats = {
        "students": db.execute("""
            SELECT COUNT(*) FROM users WHERE role = 'student'
        """).fetchone()[0],

        "shops": db.execute("""
            SELECT COUNT(*) FROM shops
        """).fetchone()[0],

        "products": db.execute("""
            SELECT COUNT(*) FROM products
        """).fetchone()[0],

        "services": db.execute("""
            SELECT COUNT(*) FROM services
        """).fetchone()[0],

        "orders": db.execute("""
            SELECT COUNT(*) FROM orders
        """).fetchone()[0],

        "complaints": db.execute("""
            SELECT COUNT(*) FROM complaints
        """).fetchone()[0],

        "pending_complaints": db.execute("""
            SELECT COUNT(*)
            FROM complaints
            WHERE status = 'Pending'
        """).fetchone()[0],

        "rooms": db.execute("""
            SELECT COUNT(*) FROM rooms
        """).fetchone()[0],

        "total_beds": db.execute("""
            SELECT COALESCE(SUM(capacity), 0)
            FROM rooms
        """).fetchone()[0],

        "occupied_beds": db.execute("""
            SELECT COALESCE(SUM(occupied), 0)
            FROM rooms
        """).fetchone()[0],

        "revenue": db.execute("""
            SELECT COALESCE(SUM(total_amount), 0)
            FROM orders
            WHERE status = 'Delivered'
        """).fetchone()[0]
    }

    return render_template(
        "admin_module.html",
        module="reports",
        title="Reports & Analytics",
        stats=stats
    )


@app.route("/admin/reports/export")
@role_required("admin")
def export_admin_report():
    db = get_db()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Report", "Value"])

    report_queries = [
        ("Registered Students",
         "SELECT COUNT(*) FROM users WHERE role='student'"),
        ("Shops",
         "SELECT COUNT(*) FROM shops"),
        ("Products",
         "SELECT COUNT(*) FROM products"),
        ("Services",
         "SELECT COUNT(*) FROM services"),
        ("Orders",
         "SELECT COUNT(*) FROM orders"),
        ("Complaints",
         "SELECT COUNT(*) FROM complaints"),
        ("Pending Complaints",
         "SELECT COUNT(*) FROM complaints WHERE status='Pending'"),
        ("Hostel Rooms",
         "SELECT COUNT(*) FROM rooms"),
        ("Total Beds",
         "SELECT COALESCE(SUM(capacity),0) FROM rooms"),
        ("Occupied Beds",
         "SELECT COALESCE(SUM(occupied),0) FROM rooms"),
        ("Delivered Order Revenue",
         "SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE status='Delivered'")
    ]

    for label, query in report_queries:
        writer.writerow([label, db.execute(query).fetchone()[0]])

    response = Response(
        output.getvalue(),
        mimetype="text/csv"
    )

    response.headers["Content-Disposition"] = (
        "attachment; filename=hostelhub_admin_report.csv"
    )

    return response


# ---------------------------------------------------------
# ADMIN SETTINGS
# ---------------------------------------------------------

@app.route("/admin/settings", methods=["GET", "POST"])
@role_required("admin")
def admin_settings():
    db = get_db()

    if request.method == "POST":
        upi_id = request.form.get("upi_id", "").strip()
        if upi_id and (
            "@" not in upi_id
            or len(upi_id) > 255
            or any(character.isspace() for character in upi_id)
            or not all(character.isalnum() or character in ".-_@" for character in upi_id)
        ):
            flash("Enter a valid UPI ID, for example name@bank.", "error")
            return redirect(url_for("admin_settings"))

        payment_qr = request.files.get("payment_qr")
        qr_filename = None
        if payment_qr and payment_qr.filename:
            safe_name = secure_filename(payment_qr.filename)
            extension = Path(safe_name).suffix.lower()
            allowed_signatures = {
                ".png": (b"\x89PNG\r\n\x1a\n",),
                ".jpg": (b"\xff\xd8\xff",),
                ".jpeg": (b"\xff\xd8\xff",),
                ".webp": (b"RIFF",),
            }
            payload = payment_qr.read(5 * 1024 * 1024 + 1)
            signature_ok = (
                extension in allowed_signatures
                and len(payload) <= 5 * 1024 * 1024
                and any(payload.startswith(signature) for signature in allowed_signatures.get(extension, ()))
                and (extension != ".webp" or payload[8:12] == b"WEBP")
            )
            if not signature_ok:
                flash("QR image must be a valid PNG, JPG or WebP file under 5 MB.", "error")
                return redirect(url_for("admin_settings"))

            qr_filename = f"upi_qr_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{extension}"
            qr_directory = UPLOADS_DIR / "payments"
            qr_directory.mkdir(parents=True, exist_ok=True)
            (qr_directory / qr_filename).write_bytes(payload)

        settings = {
            "hostel_name": request.form.get("hostel_name", "").strip(),
            "hostel_address": request.form.get("hostel_address", "").strip(),
            "contact_email": request.form.get("contact_email", "").strip(),
            "contact_phone": request.form.get("contact_phone", "").strip(),
            "mess_notice": request.form.get("mess_notice", "").strip(),
            "upi_id": upi_id,
        }
        if qr_filename:
            settings["payment_qr"] = qr_filename
        elif request.form.get("remove_payment_qr") == "1":
            settings["payment_qr"] = ""

        for key, value in settings.items():
            db.execute("""
                INSERT INTO hostel_settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key)
                DO UPDATE SET setting_value = excluded.setting_value
            """, (key, value))

        db.commit()
        flash("Hostel settings saved.", "success")
        return redirect(url_for("admin_settings"))

    rows = db.execute("""
        SELECT setting_key, setting_value
        FROM hostel_settings
    """).fetchall()

    settings = {
        row["setting_key"]: row["setting_value"]
        for row in rows
    }

    return render_template(
        "admin_module.html",
        module="settings",
        title="HostelHub Settings",
        settings=settings
    )

@app.route("/admin/payment-settings", methods=["GET", "POST"])
@role_required("admin")
def admin_payment_settings():
    db = get_db()
    if request.method == "POST":
        upi_id = request.form.get("upi_id", "").strip()
        if upi_id and (
            "@" not in upi_id
            or len(upi_id) > 255
            or any(character.isspace() for character in upi_id)
            or not all(character.isalnum() or character in ".-_@" for character in upi_id)
        ):
            flash("Enter a valid UPI ID, for example name@bank.", "error")
            return redirect(url_for("admin_payment_settings"))

        payment_qr = request.files.get("payment_qr")
        qr_filename = None
        if payment_qr and payment_qr.filename:
            safe_name = secure_filename(payment_qr.filename)
            extension = Path(safe_name).suffix.lower()
            signatures = {
                ".png": (b"\x89PNG\r\n\x1a\n",),
                ".jpg": (b"\xff\xd8\xff",),
                ".jpeg": (b"\xff\xd8\xff",),
                ".webp": (b"RIFF",),
            }
            payload = payment_qr.read(5 * 1024 * 1024 + 1)
            valid_image = (
                extension in signatures
                and len(payload) <= 5 * 1024 * 1024
                and any(payload.startswith(sig) for sig in signatures.get(extension, ()))
                and (extension != ".webp" or payload[8:12] == b"WEBP")
            )
            if not valid_image:
                flash("QR image must be a valid PNG, JPG or WebP file under 5 MB.", "error")
                return redirect(url_for("admin_payment_settings"))

            qr_filename = f"upi_qr_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{extension}"
            qr_directory = UPLOADS_DIR / "payments"
            qr_directory.mkdir(parents=True, exist_ok=True)
            (qr_directory / qr_filename).write_bytes(payload)

        settings_to_save = {"upi_id": upi_id}
        if qr_filename:
            settings_to_save["payment_qr"] = qr_filename
        elif request.form.get("remove_payment_qr") == "1":
            settings_to_save["payment_qr"] = ""

        for key, value in settings_to_save.items():
            db.execute("""
                INSERT INTO hostel_settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key)
                DO UPDATE SET setting_value = excluded.setting_value
            """, (key, value))
        db.commit()
        flash("Payment details saved. Students can now use these details to pay.", "success")
        return redirect(url_for("admin_payment_settings"))

    rows = db.execute("""
        SELECT setting_key, setting_value FROM hostel_settings
        WHERE setting_key IN ('upi_id', 'payment_qr', 'hostel_name')
    """).fetchall()
    settings = {row["setting_key"]: row["setting_value"] for row in rows}
    return render_template("admin_payment_settings.html", settings=settings)


# ==================================================

# ADMIN - ORDER MANAGEMENT

# ==================================================

ORDER_STATUSES = [
"Pending",
"Confirmed",
"Processing",
"Ready for Pickup",
"Out for Delivery",
"Delivered",
"Cancelled",
"Refund Requested",
"Refunded"
]

@app.route("/admin/orders")
@role_required("admin")
def admin_orders():
    db = get_db()


    selected_status = request.args.get("status", "All").strip()
    search = request.args.get("search", "").strip()

    conditions = []
    params = []

    if selected_status in ORDER_STATUSES:
        conditions.append("o.status = ?")
        params.append(selected_status)

    if search:
        conditions.append("""
            (
                CAST(o.id AS TEXT) LIKE ?
                OR u.full_name LIKE ?
                OR u.email LIKE ?
            )
        """)

        term = f"%{search}%"
        params.extend([term, term, term])

    where_sql = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    orders = db.execute(f"""
        SELECT
            o.id,
            o.student_id,
            o.total_amount,
            o.status,
            o.payment_status,
            o.payment_reference,
            o.delivery_address,
            o.created_at,
            u.full_name AS student_name,
            u.email AS student_email,
            (
                SELECT GROUP_CONCAT(order_lines.item_label, ', ')
                FROM (
                    SELECT p.name || ' x' || oi.quantity AS item_label
                    FROM order_items AS oi
                    JOIN products AS p ON p.id = oi.product_id
                    WHERE oi.order_id = o.id
                    UNION ALL
                    SELECT moi.item_name || ' x' || moi.quantity AS item_label
                    FROM medical_order_items AS moi
                    WHERE moi.order_id = o.id
                ) AS order_lines
            ) AS item_summary
        FROM orders AS o
        JOIN users AS u
            ON u.id = o.student_id
        {where_sql}
        ORDER BY o.id DESC
    """, params).fetchall()

    counts = db.execute("""
        SELECT
            COUNT(*) AS all_orders,

            SUM(
                CASE
                    WHEN status = 'Pending'
                    THEN 1 ELSE 0
                END
            ) AS pending,

            SUM(
                CASE
                    WHEN status IN (
                        'Processing',
                        'Confirmed',
                        'Ready for Pickup',
                        'Out for Delivery'
                    )
                    THEN 1 ELSE 0
                END
            ) AS active,

            SUM(
                CASE
                    WHEN status = 'Delivered'
                    THEN 1 ELSE 0
                END
            ) AS completed,

            SUM(
                CASE
                    WHEN status IN (
                        'Cancelled',
                        'Refund Requested',
                        'Refunded'
                    )
                    THEN 1 ELSE 0
                END
            ) AS cancelled

        FROM orders
    """).fetchone()

    return render_template(
        "order_management.html",
        orders=orders,
        counts=counts,
        statuses=ORDER_STATUSES,
        payment_statuses=["Pending", "Verification Pending", "Verified", "Rejected", "Failed", "Refunded", "Paid"],
        selected_status=selected_status,
        search=search
    )


# ==================================================

# ADMIN - UPDATE ORDER STATUS

# ==================================================

@app.route("/admin/orders/<int:order_id>/update", methods=["POST"])
@role_required("admin")
def update_order_status(order_id):
    db = get_db()

    new_status = request.form.get("status", "").strip()

    if new_status not in ORDER_STATUSES:
        flash("Invalid order status.", "error")
        return redirect(url_for("admin_orders"))

    order = db.execute(
        "SELECT id, status FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))

    if order["status"] == "Delivered":
        flash(
            "Delivered orders cannot be changed from this page.",
            "error"
        )
        return redirect(url_for("admin_orders"))

    db.execute(
        "UPDATE orders SET status = ? WHERE id = ?",
        (new_status, order_id)
    )

    db.commit()

    flash(
        f"Order #{order_id} status updated to {new_status}.",
        "success"
    )

    return redirect(url_for("admin_orders"))

    @role_required("admin")
    def update_order_status(order_id):
        db = get_db()


    new_status = request.form.get(
        "status", ""
    ).strip()

    if new_status not in ORDER_STATUSES:
        flash("Invalid order status.", "error")
        return redirect(url_for("admin_orders"))

    order = db.execute("""
        SELECT id, status
        FROM orders
        WHERE id = ?
    """, (order_id,)).fetchone()

    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))

    if order["status"] == "Delivered":
        flash(
            "Delivered orders cannot be changed from this page.",
            "error"
        )
        return redirect(url_for("admin_orders"))

    db.execute("""
        UPDATE orders
        SET status = ?
        WHERE id = ?
    """, (new_status, order_id))

    db.commit()

    flash(
        f"Order #{order_id} status updated to {new_status}.",
        "success"
    )

    return redirect(url_for("admin_orders"))

# ==================================================

# ADMIN - ORDER REPORT CSV

# ==================================================

@app.route("/my-orders/<int:order_id>/payment/submit", methods=["POST"])
@role_required("student")
def submit_order_payment(order_id):
    db = get_db()
    order = db.execute("""
        SELECT id, payment_status
        FROM orders
        WHERE id = ? AND student_id = ?
    """, (order_id, session["user_id"])).fetchone()
    if order is None:
        return render_template("404.html"), 404

    current_status = (order["payment_status"] or "Pending").strip().lower()
    if current_status in {"verified", "paid", "success", "successful", "completed"}:
        flash("This order's payment has already been verified.", "success")
        return redirect(url_for("order_payment_page", order_id=order_id))
    if current_status == "verification pending":
        flash("Your payment is already waiting for admin verification.", "info")
        return redirect(url_for("order_payment_page", order_id=order_id))

    payment_reference = request.form.get("payment_reference", "").strip()
    if len(payment_reference) > 120:
        flash("Transaction reference must be 120 characters or fewer.", "error")
        return redirect(url_for("order_payment_page", order_id=order_id))

    db.execute("""
        UPDATE orders
        SET payment_status = 'Verification Pending', payment_reference = ?
        WHERE id = ? AND student_id = ?
    """, (payment_reference or None, order_id, session["user_id"]))
    db.commit()
    flash("Payment submitted. Please wait while the admin verifies it.", "success")
    return redirect(url_for("order_payment_page", order_id=order_id))


@app.route("/admin/orders/<int:order_id>/payment/verify", methods=["POST"])
@role_required("admin")
def verify_order_payment(order_id):
    db = get_db()
    order = db.execute(
        "SELECT id, payment_status FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))
    if (order["payment_status"] or "").strip().lower() != "verification pending":
        flash("This order is not waiting for payment verification.", "error")
        return redirect(url_for("admin_orders"))

    db.execute(
        "UPDATE orders SET payment_status = 'Verified' WHERE id = ?",
        (order_id,)
    )
    db.commit()
    flash(f"Payment for order #{order_id} verified.", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/<int:order_id>/payment", methods=["POST"])
@role_required("admin")
def update_order_payment_status(order_id):
    db = get_db()
    payment_status = request.form.get("payment_status", "").strip()
    allowed_statuses = {"Pending", "Verification Pending", "Verified", "Rejected", "Paid", "Failed", "Refunded"}
    if payment_status not in allowed_statuses:
        flash("Invalid payment status.", "error")
        return redirect(url_for("admin_orders"))

    order = db.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        flash("Order not found.", "error")
        return redirect(url_for("admin_orders"))

    db.execute("UPDATE orders SET payment_status = ? WHERE id = ?", (payment_status, order_id))
    db.commit()
    flash(f"Order #{order_id} payment status updated to {payment_status}.", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/export")
@role_required("admin")
def export_orders():
    db = get_db()


    orders = db.execute("""
        SELECT
            o.id,
            u.full_name,
            u.email,
            o.total_amount,
            o.status,
            o.payment_status,
            o.delivery_address,
            o.created_at
        FROM orders AS o
        JOIN users AS u
            ON u.id = o.student_id
        ORDER BY o.id DESC
    """).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Order ID",
        "Student Name",
        "Email",
        "Total Amount",
        "Order Status",
        "Payment Status",
        "Delivery Address",
        "Created At"
    ])

    for order in orders:
        writer.writerow([
            order["id"],
            order["full_name"],
            order["email"],
            order["total_amount"],
            order["status"],
            order["payment_status"],
            order["delivery_address"] or "",
            order["created_at"]
        ])

    response = Response(
        output.getvalue(),
        mimetype="text/csv"
    )

    response.headers["Content-Disposition"] = (
        "attachment; filename=hostelhub_orders.csv"
    )

    return response



# =========================================================
# WARDEN DASHBOARD
# =========================================================

@app.route("/warden/dashboard")
@app.route("/warden/dashboard/<page>")
@role_required("warden")
def warden_dashboard(page="overview"):

    valid_pages = {"overview", "complaints", "rooms", "students", "mess", "notices"}
    if page not in valid_pages:
        abort(404)
    requested_service = request.args.get("service", "").strip()

    db = get_db()

    total_rooms = db.execute("""
        SELECT COUNT(*) AS total
        FROM rooms
    """).fetchone()["total"]

    available_rooms = db.execute("""
        SELECT COUNT(*) AS total
        FROM rooms
        WHERE status = 'Available'
          AND occupied < capacity
    """).fetchone()["total"]

    total_students = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE role = 'student'
    """).fetchone()["total"]

    pending_complaints = db.execute("""
        SELECT COUNT(*) AS total
        FROM complaints
        WHERE status = 'Pending'
    """).fetchone()["total"]

    urgent_requests = db.execute("""
        SELECT COUNT(*) AS total
        FROM complaints
        WHERE priority = 'Urgent' AND status NOT IN ('Resolved', 'Rejected')
    """).fetchone()["total"]

    complaints = db.execute("""
        SELECT
            c.id,
            c.subject,
            c.description,
            c.status,
            c.priority,
            c.category,
            c.warden_reply,
            c.created_at,
            u.full_name AS student_name,
            u.phone AS student_phone,
            (SELECT r.room_number FROM room_allocations ra
             JOIN rooms r ON r.id = ra.room_id
             WHERE ra.student_id = u.id AND ra.status = 'Active'
             ORDER BY ra.id DESC LIMIT 1) AS student_room
        FROM complaints AS c
        JOIN users AS u
            ON u.id = c.student_id
        ORDER BY CASE c.status WHEN 'Pending' THEN 0 WHEN 'In Progress' THEN 1 ELSE 2 END,
                 c.id DESC
    """).fetchall()

    rooms = db.execute("""
        SELECT r.*,
               (SELECT COUNT(*) FROM room_allocations ra
                WHERE ra.room_id = r.id AND ra.status = 'Active') AS assigned_students
        FROM rooms AS r
        ORDER BY r.room_number
    """).fetchall()
    students = db.execute("""
        SELECT u.id, u.full_name, u.email, u.phone, u.student_id,
               (SELECT r.room_number FROM room_allocations ra
                JOIN rooms r ON r.id = ra.room_id
                WHERE ra.student_id = u.id AND ra.status = 'Active'
                ORDER BY ra.id DESC LIMIT 1) AS room_number,
               (SELECT r.id FROM room_allocations ra
                JOIN rooms r ON r.id = ra.room_id
                WHERE ra.student_id = u.id AND ra.status = 'Active'
                ORDER BY ra.id DESC LIMIT 1) AS room_id
        FROM users u
        WHERE u.role = 'student'
        ORDER BY u.full_name
    """).fetchall()
    assignable_rooms = db.execute("""
        SELECT id, room_number, capacity, occupied
        FROM rooms
        WHERE occupied < capacity
        ORDER BY room_number
    """).fetchall()
    notices = db.execute("""
        SELECT n.*, u.full_name AS author_name
        FROM hostel_notices n
        LEFT JOIN users u ON u.id = n.created_by
        WHERE n.is_active = 1
        ORDER BY n.id DESC
    """).fetchall()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    menu_columns = {row["name"] for row in db.execute("PRAGMA table_info(mess_menus)").fetchall()}
    menu_value_column = "menu_items" if "menu_items" in menu_columns else "items"
    mess_rows = db.execute(f"""
        SELECT menu_date, meal_type, {menu_value_column} AS meal_items
        FROM mess_menus
        WHERE menu_date BETWEEN ? AND ?
        ORDER BY menu_date, meal_type
    """, (week_start.isoformat(), week_end.isoformat())).fetchall()
    mess_menu = {(row["menu_date"], row["meal_type"]): row["meal_items"] for row in mess_rows}

    return render_template(
        "warden_dashboard.html",
        active_page=page,
        requested_service=requested_service,
        total_rooms=total_rooms,
        available_rooms=available_rooms,
        total_students=total_students,
        pending_complaints=pending_complaints,
        urgent_requests=urgent_requests,
        complaints=complaints,
        complaint_priorities=COMPLAINT_PRIORITIES,
        rooms=rooms,
        students=students,
        notices=notices,
        complaint_statuses=COMPLAINT_STATUSES,
        assignable_rooms=assignable_rooms,
        mess_menu=mess_menu,
        week_start=week_start,
        week_end=week_end,
        week_dates=[week_start + timedelta(days=i) for i in range(7)],
        meals=WEEKLY_MEALS
    )


@app.route("/warden/students/<int:student_id>")
@role_required("warden")
def warden_student_detail(student_id):
    db = get_db()
    student = db.execute(
        "SELECT id, full_name, email, phone, student_id, created_at FROM users WHERE id = ? AND role = 'student'",
        (student_id,)
    ).fetchone()
    if student is None:
        abort(404)

    current_room = db.execute("""
        SELECT r.id, r.room_number, r.room_type, r.floor, r.capacity, r.occupied
        FROM room_allocations ra
        JOIN rooms r ON r.id = ra.room_id
        WHERE ra.student_id = ? AND ra.status = 'Active'
        ORDER BY ra.id DESC LIMIT 1
    """, (student_id,)).fetchone()
    student_complaints = db.execute("""
        SELECT id, subject, description, status, category, warden_reply, created_at
        FROM complaints
        WHERE student_id = ?
        ORDER BY id DESC
    """, (student_id,)).fetchall()
    assignable_rooms = db.execute("""
        SELECT id, room_number, capacity, occupied
        FROM rooms
        WHERE occupied < capacity
        ORDER BY room_number
    """).fetchall()

    return render_template(
        "warden_dashboard.html",
        active_page="student_detail",
        student=student,
        current_room=current_room,
        student_complaints=student_complaints,
        assignable_rooms=assignable_rooms,
        requested_service="",
        complaint_statuses=COMPLAINT_STATUSES,
        complaint_priorities=COMPLAINT_PRIORITIES,
        complaints=[],
        rooms=[],
        students=[],
        notices=[],
        total_rooms=0,
        available_rooms=0,
        total_students=0,
        pending_complaints=0,
        urgent_requests=0,
        mess_menu={},
        week_start=date.today(),
        week_end=date.today(),
        week_dates=[],
        meals=WEEKLY_MEALS
    )


@app.route("/warden/complaints/<int:complaint_id>/update", methods=["POST"])
@role_required("warden")
def warden_update_complaint(complaint_id):
    status = request.form.get("status", "").strip()
    priority = request.form.get("priority", "Medium").strip()
    reply = request.form.get("warden_reply", "").strip()[:2000]
    if status not in COMPLAINT_STATUSES or priority not in COMPLAINT_PRIORITIES:
        flash("Choose a valid request status and priority.", "error")
        return redirect(url_for("warden_dashboard", page="complaints"))
    db = get_db()
    updated = db.execute("UPDATE complaints SET status = ?, priority = ?, warden_reply = ? WHERE id = ?",
                         (status, priority, reply or None, complaint_id))
    db.commit()
    flash("Request updated." if updated.rowcount else "Request not found.",
          "success" if updated.rowcount else "error")
    return redirect(url_for("warden_dashboard", page="complaints"))


@app.route("/warden/rooms/add", methods=["POST"])
@role_required("warden")
def warden_add_room():
    room_number = request.form.get("room_number", "").strip()
    room_type = request.form.get("room_type", "Standard").strip() or "Standard"
    floor = request.form.get("floor", "").strip() or None
    try:
        capacity = int(request.form.get("capacity", ""))
        if not room_number or len(room_number) > 30 or capacity < 1 or capacity > 100:
            raise ValueError
    except ValueError:
        flash("Enter a room number (up to 30 characters) and a capacity from 1 to 100 beds.", "error")
        return redirect(url_for("warden_dashboard", page="rooms"))

    db = get_db()
    try:
        db.execute("""
            INSERT INTO rooms (room_number, capacity, occupied, status, room_type, floor)
            VALUES (?, ?, 0, 'Available', ?, ?)
        """, (room_number, capacity, room_type, floor))
        db.commit()
        flash(f"Room {room_number} added successfully.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("That room number already exists. Use a unique room number.", "error")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Warden room creation failed")
        flash("Could not add the room. Please try again.", "error")
    return redirect(url_for("warden_dashboard", page="rooms"))


@app.route("/warden/mess/update", methods=["POST"])
@role_required("warden")
def warden_update_mess():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_dates = [week_start + timedelta(days=offset) for offset in range(7)]
    db = get_db()
    columns = {row["name"] for row in db.execute("PRAGMA table_info(mess_menus)").fetchall()}
    value_column = "menu_items" if "menu_items" in columns else "items"

    try:
        updates = []
        for day in week_dates:
            menu_date = day.isoformat()
            for meal in WEEKLY_MEALS:
                items = request.form.get(f"{menu_date}_{meal}", "").strip()
                if len(items) > 500:
                    raise ValueError("Each meal menu must be 500 characters or fewer.")
                updates.append((menu_date, meal, items))

        for menu_date, meal, items in updates:
            existing = db.execute(
                "SELECT id FROM mess_menus WHERE menu_date = ? AND meal_type = ?",
                (menu_date, meal)
            ).fetchone()
            if existing:
                db.execute(
                    f"UPDATE mess_menus SET {value_column} = ? WHERE menu_date = ? AND meal_type = ?",
                    (items, menu_date, meal)
                )
            elif items:
                db.execute(
                    f"INSERT INTO mess_menus (meal_type, menu_date, {value_column}) VALUES (?, ?, ?)",
                    (meal, menu_date, items)
                )
        db.commit()
        flash("Mess schedule updated. Students can now see the latest menu.", "success")
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Warden mess schedule update failed")
        flash("Could not update the mess schedule. Please try again.", "error")
    return redirect(url_for("warden_dashboard", page="mess"))


@app.route("/warden/rooms/allocate", methods=["POST"])
@role_required("warden")
def warden_allocate_room():
    db = get_db()
    try:
        student_id = int(request.form.get("student_id", ""))
        room_id = int(request.form.get("room_id", ""))
        student = db.execute("SELECT id FROM users WHERE id = ? AND role = 'student'", (student_id,)).fetchone()
        room = db.execute("SELECT id, room_number, capacity, occupied FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if student is None or room is None:
            raise ValueError("Choose a valid student and room.")
        current = db.execute("""
            SELECT ra.id, ra.room_id FROM room_allocations ra
            WHERE ra.student_id = ? AND ra.status = 'Active'
            ORDER BY ra.id DESC LIMIT 1
        """, (student_id,)).fetchone()
        if current and current["room_id"] == room_id:
            flash("This student is already assigned to that room.", "info")
            return redirect(url_for("warden_dashboard", page="students"))
        if room["occupied"] >= room["capacity"]:
            raise ValueError("That room has no beds available.")
        if current:
            db.execute("UPDATE room_allocations SET status = 'Released' WHERE id = ?", (current["id"],))
            db.execute("UPDATE rooms SET occupied = MAX(occupied - 1, 0), status = CASE WHEN occupied - 1 >= capacity THEN 'Full' ELSE 'Available' END WHERE id = ?", (current["room_id"],))
        db.execute("INSERT INTO room_allocations (room_id, student_id) VALUES (?, ?)", (room_id, student_id))
        db.execute("UPDATE rooms SET occupied = occupied + 1, status = CASE WHEN occupied + 1 >= capacity THEN 'Full' ELSE 'Available' END WHERE id = ?", (room_id,))
        db.commit()
        flash(f"Student assigned to room {room['room_number']}.", "success")
    except (TypeError, ValueError) as error:
        db.rollback()
        flash(str(error) or "Choose a valid student and room.", "error")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Warden room assignment failed")
        flash("Could not save the room assignment.", "error")
    return redirect(url_for("warden_dashboard", page="students"))


@app.route("/warden/rooms/release/<int:student_id>", methods=["POST"])
@role_required("warden")
def warden_release_room(student_id):
    db = get_db()
    allocation = db.execute("""
        SELECT ra.id, ra.room_id, r.room_number
        FROM room_allocations ra JOIN rooms r ON r.id = ra.room_id
        WHERE ra.student_id = ? AND ra.status = 'Active'
        ORDER BY ra.id DESC LIMIT 1
    """, (student_id,)).fetchone()
    if allocation:
        db.execute("UPDATE room_allocations SET status = 'Released' WHERE id = ?", (allocation["id"],))
        db.execute("UPDATE rooms SET occupied = MAX(occupied - 1, 0), status = CASE WHEN occupied - 1 >= capacity THEN 'Full' ELSE 'Available' END WHERE id = ?", (allocation["room_id"],))
        db.commit()
        flash(f"Room {allocation['room_number']} released for the student.", "success")
    else:
        flash("This student has no active room assignment.", "error")
    return redirect(url_for("warden_dashboard", page="students"))


@app.route("/warden/notices", methods=["POST"])
@role_required("warden")
def warden_create_notice():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    if not title or not body or len(title) > 120 or len(body) > 2000:
        flash("Notice title and message are required (maximum 120 and 2000 characters).", "error")
        return redirect(url_for("warden_dashboard", page="notices"))
    db = get_db()
    db.execute("INSERT INTO hostel_notices (title, body, created_by) VALUES (?, ?, ?)",
               (title, body, session["user_id"]))
    db.commit()
    flash("Notice published for students.", "success")
    return redirect(url_for("warden_dashboard", page="notices"))


@app.route("/warden/notices/<int:notice_id>/delete", methods=["POST"])
@role_required("warden")
def warden_delete_notice(notice_id):
    db = get_db()
    db.execute("UPDATE hostel_notices SET is_active = 0 WHERE id = ?", (notice_id,))
    db.commit()
    flash("Notice removed.", "success")
    return redirect(url_for("warden_dashboard", page="notices"))


# =========================================================
# PROVIDER DASHBOARD
# =========================================================

@app.route("/provider/services/<int:service_id>/toggle", methods=["POST"])
@role_required("provider")
def toggle_provider_service(service_id):
    db = get_db()
    service = db.execute("""
        SELECT id, is_available
        FROM services
        WHERE id = ? AND provider_id = ?
    """, (service_id, session["user_id"])).fetchone()

    if service is None:
        flash("Service not found in your account.", "error")
        return redirect(url_for("provider_services"))

    db.execute(
        "UPDATE services SET is_available = ? WHERE id = ? AND provider_id = ?",
        (0 if service["is_available"] else 1, service_id, session["user_id"])
    )
    db.commit()
    flash("Service availability updated.", "success")
    return redirect(url_for("provider_services"))


@app.route("/provider/dashboard")
@role_required("provider")
def provider_dashboard():

    db = get_db()

    shop = db.execute("""
        SELECT id, name, category_id, address
        FROM shops
        WHERE provider_id = ? AND is_active = 1
        ORDER BY id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()

    services = db.execute("""
        SELECT
            id,
            name,
            description,
            price,
            estimated_minutes,
            is_available
        FROM services
        WHERE provider_id = ?
        ORDER BY id DESC
    """, (session["user_id"],)).fetchall()

    products = []
    if shop:
        products = db.execute("""
            SELECT p.*, c.name AS category_name
            FROM products p
            LEFT JOIN shops s ON s.id = p.shop_id
            LEFT JOIN categories c ON c.id = s.category_id
            WHERE p.shop_id = ?
            ORDER BY p.id DESC
        """, (shop["id"],)).fetchall()

    return render_template(
        "provider_dashboard.html",
        services=services,
        shop=shop,
        products=products,
        active_page="overview",
        page_heading="Overview"
    )


@app.route("/provider/products")
@role_required("provider")
def provider_products():
    db = get_db()
    shop = db.execute("""
        SELECT id, name FROM shops
        WHERE provider_id = ? AND is_active = 1
        ORDER BY id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()
    products = []
    if shop:
        products = db.execute("""
            SELECT p.*, c.name AS category_name
            FROM products p
            LEFT JOIN shops s ON s.id = p.shop_id
            LEFT JOIN categories c ON c.id = s.category_id
            WHERE p.shop_id = ?
            ORDER BY p.id DESC
        """, (shop["id"],)).fetchall()
    return render_template("provider_products.html", shop=shop, products=products,
        active_page="products", page_heading="My Products")


@app.route("/provider/services")
@role_required("provider")
def provider_services():
    db = get_db()
    services = db.execute("""
        SELECT id, name, description, price, estimated_minutes, is_available
        FROM services WHERE provider_id = ? ORDER BY id DESC
    """, (session["user_id"],)).fetchall()
    return render_template("provider_services.html", services=services,
        active_page="services", page_heading="My Services")


@app.route("/provider/products/add", methods=["GET", "POST"])
@role_required("provider")
def provider_add_product():
    db = get_db()
    shop = db.execute("""
        SELECT s.id, s.name, s.category_id, s.address, c.slug AS category_slug
        FROM shops s
        LEFT JOIN categories c ON c.id = s.category_id
        WHERE s.provider_id = ? AND s.is_active = 1
        ORDER BY s.id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()
    if not shop:
        flash("Your account is not linked to an active shop. Ask the administrator to assign your shop.", "error")
        return redirect(url_for("provider_dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price_text = request.form.get("price", "").strip()
        stock_text = request.form.get("stock_quantity", "0").strip()
        sizes_text = request.form.get("sizes", "").strip()
        out_text = request.form.get("out_of_stock_sizes", "").strip()
        image_url = request.form.get("image_url", "").strip()
        if not name or not price_text:
            flash("Product name and price are required.", "error")
            return redirect(url_for("provider_add_product"))
        try:
            price = float(price_text)
            stock = int(stock_text)
            if price < 0 or stock < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Enter a valid price and stock quantity.", "error")
            return redirect(url_for("provider_add_product"))
        try:
            image_url = save_product_image(request.files.get("product_image")) or image_url or None
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("provider_add_product"))
        clothing = shop["category_slug"] == "clothing-tailoring"
        sizes = list(dict.fromkeys(v.strip().upper() for v in sizes_text.split(",") if v.strip())) if clothing else []
        out_sizes = list(dict.fromkeys(v.strip().upper() for v in out_text.split(",") if v.strip())) if clothing else []
        sizes = [v for v in sizes if v not in out_sizes]
        try:
            db.execute("""
                INSERT INTO products (shop_id, name, description, price, image_url, sizes,
                    out_of_stock_sizes, stock_quantity, is_available, is_published)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (shop["id"], name, description, price, image_url,
                  ", ".join(sizes), ", ".join(out_sizes), stock, 1 if stock > 0 else 0))
            db.commit()
            flash("Product submitted. It will appear to students after admin approval.", "success")
            return redirect(url_for("provider_products"))
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("Shopkeeper product submission failed")
            flash("Could not save the product. Please try again.", "error")

    return render_template("provider_add_product.html", shop=shop,
        active_page="add_product", page_heading="Add Product")


# =========================================================
# STUDENT DASHBOARD
# =========================================================

@app.route("/student/complaints", methods=["POST"])
@role_required("student")
def submit_student_complaint():
    db = get_db()
    category = request.form.get("category", "").strip()
    subject = request.form.get("subject", "").strip()
    description = request.form.get("description", "").strip()

    if not category or not subject or not description:
        flash("Please complete all complaint fields.", "error")
        return redirect(url_for("student_dashboard"))

    try:
        db.execute("""
            INSERT INTO complaints (student_id, category, subject, description)
            VALUES (?, ?, ?, ?)
        """, (session["user_id"], category, subject, description))
        db.commit()
        flash("Complaint submitted successfully.", "success")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("STUDENT COMPLAINT SUBMISSION FAILED")
        flash("Could not submit your complaint. Please try again.", "error")

    return redirect(url_for("student_dashboard"))


@app.route("/student/dashboard")
@role_required("student")
def student_dashboard():

    db = get_db()

    products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            c.name AS category_name,
            s.name AS shop_name
        FROM products AS p
        JOIN shops AS s
            ON p.shop_id = s.id
        LEFT JOIN categories AS c
            ON s.category_id = c.id
        WHERE p.is_available = 1
          AND p.is_published = 1
          AND s.is_active = 1
        ORDER BY p.id DESC
    """).fetchall()

    cart_result = db.execute("""
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM cart_items
        WHERE student_id = ?
    """, (session["user_id"],)).fetchone()

    cart_count = cart_result["total"]

    wishlist_rows = db.execute("""
        SELECT product_id
        FROM wishlist_items
        WHERE student_id = ?
    """, (session["user_id"],)).fetchall()

    wishlist_ids = [
        row["product_id"]
        for row in wishlist_rows
    ]

    wishlist_products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            p.is_available,
            p.is_published,
            s.name AS shop_name,
            s.is_active AS shop_active
        FROM wishlist_items AS wi
        JOIN products AS p ON p.id = wi.product_id
        LEFT JOIN shops AS s ON s.id = p.shop_id
        WHERE wi.student_id = ?
        ORDER BY wi.id DESC
    """, (session["user_id"],)).fetchall()

    wishlist_medicines = db.execute("""
        SELECT m.id, m.name, m.description, m.price, m.image_url,
               m.is_available, m.stock_quantity,
               s.name AS shop_name, s.is_active AS shop_active
        FROM medical_wishlist_items AS wi
        JOIN medical_medicines AS m ON m.id = wi.medicine_id
        LEFT JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE wi.student_id = ?
        ORDER BY wi.id DESC
    """, (session["user_id"],)).fetchall()

    # Fetch student's cart products
    cart_products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            p.stock_quantity,
            s.name AS shop_name,
            ci.quantity,
            (p.price * ci.quantity) AS item_total
        FROM cart_items AS ci
        JOIN products AS p ON ci.product_id = p.id
        JOIN shops AS s ON p.shop_id = s.id
        WHERE ci.student_id = ?
        ORDER BY ci.id DESC
    """, (session["user_id"],)).fetchall()

    cart_total = sum(
        item["item_total"] for item in cart_products
    )

    student_room = db.execute("""
        SELECT r.room_number, r.room_type, r.floor
        FROM room_allocations ra
        JOIN rooms r ON r.id = ra.room_id
        WHERE ra.student_id = ? AND ra.status = 'Active'
        ORDER BY ra.id DESC LIMIT 1
    """, (session["user_id"],)).fetchone()

    complaints = db.execute("""
        SELECT id, category, subject, description, status, warden_reply, created_at
        FROM complaints
        WHERE student_id = ?
        ORDER BY id DESC
    """, (session["user_id"],)).fetchall()

    billing_orders = db.execute("""
        SELECT id, total_amount, status, payment_status, payment_reference, created_at
        FROM orders
        WHERE student_id = ?
        ORDER BY id DESC
    """, (session["user_id"],)).fetchall()
    paid_total = sum(
        float(order["total_amount"] or 0)
        for order in billing_orders
        if (order["payment_status"] or "").strip().lower() in {"paid", "success", "successful", "completed", "verified"}
    )
    pending_total = sum(
        float(order["total_amount"] or 0)
        for order in billing_orders
        if (order["payment_status"] or "").strip().lower() in {"pending", "unpaid", "due", "verification pending", "rejected", "failed"}
    )
    payment_settings_rows = db.execute("""
        SELECT setting_key, setting_value
        FROM hostel_settings
        WHERE setting_key IN ('upi_id', 'payment_qr', 'hostel_name')
    """).fetchall()
    payment_settings = {row["setting_key"]: row["setting_value"] for row in payment_settings_rows}
    hostel_notices = db.execute("""
        SELECT id, title, body, created_at
        FROM hostel_notices
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()
    today = date.today()
    student_week_start = today - timedelta(days=today.weekday())
    student_week_end = student_week_start + timedelta(days=6)
    mess_columns = {row["name"] for row in db.execute("PRAGMA table_info(mess_menus)").fetchall()}
    mess_value_column = "menu_items" if "menu_items" in mess_columns else "items"
    student_mess_rows = db.execute(f"""
        SELECT menu_date, meal_type, {mess_value_column} AS meal_items
        FROM mess_menus
        WHERE menu_date BETWEEN ? AND ?
        ORDER BY menu_date, meal_type
    """, (student_week_start.isoformat(), student_week_end.isoformat())).fetchall()
    student_mess_menu = {(row["menu_date"], row["meal_type"]): row["meal_items"] for row in student_mess_rows}

    return render_template(
        "student_dashboard.html",
        products=products,
        cart_count=cart_count,
        wishlist_ids=wishlist_ids,
        wishlist_products=wishlist_products,
        wishlist_medicines=wishlist_medicines,
        cart_products=cart_products,
        cart_total=cart_total,
        student_room=student_room,
        complaints=complaints,
        billing_orders=billing_orders,
        paid_total=paid_total,
        pending_total=pending_total,
        payment_settings=payment_settings,
        hostel_notices=hostel_notices,
        mess_menu=student_mess_menu,
        mess_week_start=student_week_start,
        mess_week_end=student_week_end,
        mess_week_dates=[student_week_start + timedelta(days=i) for i in range(7)],
        meals=WEEKLY_MEALS
    )


# ==========================================
# HOSTELHUB - MY CART PAGE
# ==========================================

@app.route("/cart")
@role_required("student")
def my_cart():
    db = get_db()
    student_id = session["user_id"]

    cart_products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            ci.quantity,
            s.name AS shop_name,
            (p.price * ci.quantity) AS item_total
        FROM cart_items AS ci
        JOIN products AS p ON ci.product_id = p.id
        JOIN shops AS s ON p.shop_id = s.id
        WHERE ci.student_id = ?
        ORDER BY ci.id DESC
    """, (student_id,)).fetchall()

    cart_total = sum(
        item["item_total"] for item in cart_products
    )

    return render_template(
        "cart.html",
        cart_products=cart_products,
        cart_total=cart_total
    )

@app.route("/checkout")
@role_required("student")
def checkout():
    db = get_db()
    student_id = session["user_id"]
    buy_now_product_id = request.args.get("buy_now", "").strip()

    if buy_now_product_id:
        if not buy_now_product_id.isdigit():
            flash("That product could not be selected.", "danger")
            return redirect(url_for("marketplace"))
        cart_products = db.execute("""
            SELECT p.id AS product_id, p.name, p.price, p.stock_quantity,
                   p.is_available, p.is_published, 1 AS quantity, s.is_active
            FROM products AS p
            JOIN shops AS s ON p.shop_id = s.id
            WHERE p.id = ?
        """, (int(buy_now_product_id),)).fetchall()
    else:
        cart_products = db.execute("""
            SELECT
                p.id AS product_id,
                p.name,
                p.price,
                p.stock_quantity,
                p.is_available,
                p.is_published,
                ci.quantity,
                s.is_active
            FROM cart_items AS ci
            JOIN products AS p ON ci.product_id = p.id
            JOIN shops AS s ON p.shop_id = s.id
            WHERE ci.student_id = ?
        """, (student_id,)).fetchall()

    if not cart_products:
        flash("Your cart is empty or the selected product is unavailable.", "warning")
        return redirect(url_for("my_cart" if not buy_now_product_id else "marketplace"))

    for item in cart_products:
        if (
            not item["is_available"]
            or not item["is_published"]
            or not item["is_active"]
        ):
            flash(f'{item["name"]} is no longer available.', "danger")
            return redirect(url_for("my_cart"))

        if item["quantity"] <= 0:
            flash(f'Invalid quantity for {item["name"]}.', "danger")
            return redirect(url_for("my_cart"))

        if item["quantity"] > item["stock_quantity"]:
            flash(f'Insufficient stock for {item["name"]}.', "danger")
            return redirect(url_for("my_cart"))

    cart_total = sum(
        item["price"] * item["quantity"]
        for item in cart_products
    )

    return render_template(
        "checkout.html",
        cart_products=cart_products,
        cart_total=cart_total,
        buy_now_product_id=buy_now_product_id if buy_now_product_id.isdigit() else ""
    )

# ==========================================
# HOSTELHUB - PLACE ORDER
# ==========================================

@app.route("/cart/order", methods=["POST"])
@role_required("student")
def place_cart_order():
    db = get_db()
    student_id = session["user_id"]

    try:
        # Buy Now checks out exactly one product and leaves the student's cart untouched.
        buy_now_product_id = request.form.get("buy_now_product_id", "").strip()
        if buy_now_product_id:
            if not buy_now_product_id.isdigit():
                flash("That product could not be selected.", "danger")
                return redirect(url_for("marketplace"))
            cart_products = db.execute("""
                SELECT p.id AS product_id, 1 AS quantity, p.name, p.price,
                       p.stock_quantity, p.is_available, p.is_published, s.is_active
                FROM products AS p
                JOIN shops AS s ON p.shop_id = s.id
                WHERE p.id = ?
            """, (int(buy_now_product_id),)).fetchall()
        else:
            cart_products = db.execute("""
                SELECT
                    ci.product_id,
                    ci.quantity,
                    p.name,
                    p.price,
                    p.stock_quantity,
                    p.is_available,
                    p.is_published,
                    s.is_active
                FROM cart_items AS ci
                JOIN products AS p ON ci.product_id = p.id
                JOIN shops AS s ON p.shop_id = s.id
                WHERE ci.student_id = ?
            """, (student_id,)).fetchall()

        # Check that the selected product or cart has purchasable items.
        if not cart_products:
            flash("Your cart is empty or the selected product is unavailable.", "warning")
            return redirect(url_for("my_cart" if not buy_now_product_id else "marketplace"))

        # 3. Validate product availability and stock
        for item in cart_products:
            if (
                not item["is_available"]
                or not item["is_published"]
                or not item["is_active"]
            ):
                flash(
                    f"{item['name']} is no longer available.",
                    "danger"
                )
                return redirect(url_for("my_cart"))

            if item["quantity"] <= 0:
                flash(
                    f"Invalid quantity for {item['name']}.",
                    "danger"
                )
                return redirect(url_for("my_cart"))

            if item["quantity"] > item["stock_quantity"]:
                flash(
                    f"Insufficient stock for {item['name']}.",
                    "danger"
                )
                return redirect(url_for("my_cart"))

        # 4. Calculate cart total
        cart_total = sum(
            item["price"] * item["quantity"]
            for item in cart_products
        )

        # 5. Check orders table columns
        order_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(orders)"
            ).fetchall()
        }

        required_columns = {
            "student_id",
            "total_amount",
            "order_type"
        }

        if not required_columns.issubset(order_columns):
            raise Exception(
                "Required columns are missing from the orders table."
            )

        # 6. Create order
        cursor = db.execute("""
            INSERT INTO orders (
                student_id,
                total_amount,
                order_type
            )
            VALUES (?, ?, ?)
        """, (
            student_id,
            cart_total,
            "product"
        ))

        order_id = cursor.lastrowid

        # 7. Save order items and update stock
        for item in cart_products:
            db.execute("""
                INSERT INTO order_items (
                    order_id,
                    product_id,
                    item_name,
                    quantity,
                    unit_price
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                order_id,
                item["product_id"],
                item["name"],
                item["quantity"],
                item["price"]
            ))

            # Update product stock
            db.execute("""
                UPDATE products
                SET stock_quantity = stock_quantity - ?
                WHERE id = ?
            """, (
                item["quantity"],
                item["product_id"]
            ))
        # A Buy Now order is independent; keep all cart contents unchanged.
        if not buy_now_product_id:
            db.execute("""
                DELETE FROM cart_items
                WHERE student_id = ?
            """, (student_id,))

        # Commit transaction.
        db.commit()

        flash(
            f"Order placed successfully! Order ID: {order_id}",
            "success"
        )

        return redirect(url_for("my_orders" if buy_now_product_id else "my_cart"))

    except Exception as e:
        db.rollback()

        app.logger.exception("HOSTELHUB ORDER ERROR")

        flash(
            f"Order failed: {e}",
            "danger"
        )

        return redirect(url_for("my_cart"))



# ---------------------------------------------------------
# STUDENT MY ORDERS
# ---------------------------------------------------------

@app.route("/my-orders")
@role_required("student")
def my_orders():
    db = get_db()
    student_id = session["user_id"]

    orders = db.execute("""
        SELECT
            id,
            order_type,
            status,
            total_amount,
            delivery_address,
            estimated_delivery,
            created_at,
            payment_status
        FROM orders
        WHERE student_id = ?
        ORDER BY id DESC
    """, (student_id,)).fetchall()

    print("MY ORDERS DEBUG")
    print("Logged-in user ID:", student_id)
    print("Orders fetched:", [dict(order) for order in orders])

    order_items = {}

    for order in orders:
        if (order["order_type"] or "").strip().lower() == "medical":
            items = db.execute("""
                SELECT item_name, quantity, unit_price, unit_price AS price
                FROM medical_order_items WHERE order_id = ?
            """, (order["id"],)).fetchall()
        else:
            items = db.execute("""
                SELECT item_name, quantity, unit_price, price
                FROM order_items WHERE order_id = ?
            """, (order["id"],)).fetchall()
        order_items[order["id"]] = items

    payment_settings_rows = db.execute("""
        SELECT setting_key, setting_value
        FROM hostel_settings
        WHERE setting_key IN ('upi_id', 'payment_qr', 'hostel_name')
    """).fetchall()
    payment_settings = {row["setting_key"]: row["setting_value"] for row in payment_settings_rows}

    return render_template(
        "my_orders.html",
        orders=orders,
        order_items=order_items,
        payment_settings=payment_settings
    )



@app.route("/my-orders/<int:order_id>/payment")
@role_required("student")
def order_payment_page(order_id):
    db = get_db()
    order = db.execute("""
        SELECT id, order_type, status, total_amount, payment_status, payment_reference, created_at
        FROM orders
        WHERE id = ? AND student_id = ?
    """, (order_id, session["user_id"])).fetchone()
    if order is None:
        return render_template("404.html"), 404

    rows = db.execute("""
        SELECT setting_key, setting_value FROM hostel_settings
        WHERE setting_key IN ('upi_id', 'payment_qr', 'hostel_name')
    """).fetchall()
    payment_settings = {row["setting_key"]: row["setting_value"] for row in rows}
    return render_template(
        "order_payment.html",
        order=order,
        payment_settings=payment_settings,
    )


# =========================================================
# HOSTELHUB MARKETPLACE
# =========================================================

@app.route("/services")
@role_required("student")
def student_services():
    return redirect(url_for("student_services_page", category="printing"))


@app.route("/services/<category>")
@role_required("student")
def student_services_page(category):
    service_categories = {
        "printing": "Print & Xerox",
        "laundry": "Laundry",
        "electronics-mobile-repair": "Repairs",
        "clothing-tailoring": "Tailoring",
        "requests": "My Service Requests",
    }
    if category not in service_categories:
        return render_template("404.html"), 404
    db = get_db()
    services = []
    if category != "requests":
        services = db.execute("""
            SELECT sv.id, sv.name, sv.description, sv.price, sv.estimated_minutes,
                   c.name AS category_name, c.slug AS category_slug,
                   s.name AS shop_name, s.address AS shop_address,
                   u.full_name AS provider_name
            FROM services AS sv
            JOIN categories AS c ON c.id = sv.category_id
            LEFT JOIN shops AS s ON s.id = sv.shop_id
            LEFT JOIN users AS u ON u.id = sv.provider_id
            WHERE sv.is_available = 1 AND c.slug = ?
              AND (s.id IS NULL OR s.is_active = 1)
            ORDER BY sv.id DESC
        """, (category,)).fetchall()
    bookings = db.execute("""
        SELECT b.*, sv.name AS service_name, c.slug AS category_slug
        FROM service_bookings AS b
        JOIN services AS sv ON sv.id = b.service_id
        LEFT JOIN categories AS c ON c.id = sv.category_id
        WHERE b.student_id = ?
        ORDER BY b.id DESC
    """, (session["user_id"],)).fetchall()
    return render_template(
        "services.html",
        category=category,
        page_title=service_categories[category],
        service_categories=service_categories,
        services=services,
        bookings=bookings,
    )


@app.route("/services/requests/<int:booking_id>/track")
@role_required("student")
def track_service_booking(booking_id):
    db = get_db()
    booking = db.execute("""
        SELECT b.*, sv.name AS service_name, c.slug AS category_slug,
               s.name AS shop_name, u.full_name AS provider_name
        FROM service_bookings AS b
        JOIN services AS sv ON sv.id = b.service_id
        LEFT JOIN categories AS c ON c.id = sv.category_id
        LEFT JOIN shops AS s ON s.id = sv.shop_id
        LEFT JOIN users AS u ON u.id = sv.provider_id
        WHERE b.id = ? AND b.student_id = ?
    """, (booking_id, session["user_id"])).fetchone()
    if booking is None:
        return render_template("404.html"), 404

    status_steps = [
        "Order Placed",
        "Verified",
        "Ready for Pickup",
        "Service Done",
        "Out for Delivery",
    ]
    status_indexes = {
        "pending": 0,
        "order placed": 0,
        "confirmed": 1,
        "verified": 1,
        "in progress": 2,
        "ready for pickup": 2,
        "service done": 3,
        "completed": 3,
        "out for delivery": 4,
    }
    status_key = (booking["status"] or "Pending").strip().lower()
    tracking_index = status_indexes.get(status_key, 0)
    return render_template(
        "service_tracking.html",
        booking=booking,
        status_steps=status_steps,
        tracking_index=tracking_index,
        is_cancelled=status_key == "cancelled",
    )


@app.route("/services/book/<int:service_id>", methods=["POST"])
@role_required("student")
def book_service(service_id):
    db = get_db()
    service = db.execute("""
        SELECT sv.id, c.slug AS category_slug
        FROM services AS sv JOIN categories AS c ON c.id = sv.category_id
        WHERE sv.id = ? AND sv.is_available = 1
    """, (service_id,)).fetchone()
    details = request.form.get("details", "").strip()
    preferred_at = request.form.get("preferred_at", "").strip()
    if service is None or not details:
        flash("Service is unavailable or request details are missing.", "error")
        return redirect(url_for("student_services"))
    if preferred_at:
        try:
            if datetime.fromisoformat(preferred_at) <= datetime.now():
                raise ValueError
        except ValueError:
            flash("Choose a future preferred time.", "error")
            return redirect(url_for("student_services_page", category=service["category_slug"]))
    try:
        cursor = db.execute("""
            INSERT INTO service_bookings (service_id, student_id, preferred_at, details)
            VALUES (?, ?, ?, ?)
        """, (service_id, session["user_id"], preferred_at or None, details))
        db.commit()
        flash("Service request placed. Track each update on this page.", "success")
        return redirect(url_for("track_service_booking", booking_id=cursor.lastrowid))
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Service request failed")
        flash("Could not send the service request.", "error")
    return redirect(url_for("student_services_page", category=service["category_slug"]))


@app.route("/medical")
@role_required("student")
def student_medical():
    return redirect(url_for("student_medical_page", section="shops"))


@app.route("/medical/<section>")
@role_required("student")
def student_medical_page(section):
    allowed_sections = {
        "shops", "medicines", "facilities", "appointments",
        "help", "requests", "emergency",
    }
    if section not in allowed_sections:
        return render_template("404.html"), 404
    db = get_db()
    medical_shops = db.execute("""
        SELECT * FROM medical_shops
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()
    medicines = db.execute("""
        SELECT m.*, s.name AS shop_name, s.phone AS shop_phone,
               s.address AS shop_address
        FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE m.is_available = 1 AND m.stock_quantity > 0 AND s.is_active = 1
        ORDER BY m.name
    """).fetchall()
    medical_wishlist_rows = db.execute(
        "SELECT medicine_id FROM medical_wishlist_items WHERE student_id = ?",
        (session["user_id"],),
    ).fetchall()
    medical_wishlist_ids = [row["medicine_id"] for row in medical_wishlist_rows]
    doctors = db.execute("""
        SELECT * FROM medical_doctors
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()
    facilities = db.execute("SELECT * FROM medical_facilities WHERE is_active = 1 ORDER BY name").fetchall()
    appointments = db.execute("""
        SELECT a.*, d.name AS doctor_name, d.clinic_name
        FROM medical_appointments AS a
        JOIN medical_doctors AS d ON d.id = a.doctor_id
        WHERE a.student_id = ?
        ORDER BY a.id DESC
    """, (session["user_id"],)).fetchall()
    medical_requests = db.execute(
        "SELECT * FROM medical_requests WHERE student_id = ? ORDER BY id DESC",
        (session["user_id"],),
    ).fetchall()
    medical_cart_count = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM medical_cart_items WHERE student_id = ?",
        (session["user_id"],),
    ).fetchone()[0]
    return render_template(
        "medical.html",
        page=section,
        medical_shops=medical_shops,
        medicines=medicines,
        doctors=doctors,
        facilities=facilities,
        appointments=appointments,
        medical_requests=medical_requests,
        medical_cart_count=medical_cart_count,
        medical_wishlist_ids=medical_wishlist_ids,
    )


@app.route("/medical/medicine/<int:medicine_id>")
@role_required("student")
def medical_medicine_details(medicine_id):
    db = get_db()
    medicine = db.execute("""
        SELECT m.*, s.name AS shop_name, s.phone AS shop_phone,
               s.address AS shop_address
        FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE m.id = ? AND m.is_available = 1 AND s.is_active = 1
    """, (medicine_id,)).fetchone()
    if medicine is None:
        return render_template("404.html"), 404
    is_wishlisted = db.execute("""
        SELECT 1 FROM medical_wishlist_items
        WHERE student_id = ? AND medicine_id = ?
    """, (session["user_id"], medicine_id)).fetchone() is not None
    return render_template("medical_medicine_details.html", medicine=medicine,
                           is_wishlisted=is_wishlisted)


@app.route("/medical/wishlist/toggle/<int:medicine_id>", methods=["POST"])
@role_required("student")
def toggle_medical_wishlist(medicine_id):
    db = get_db()
    available = db.execute("""
        SELECT m.id FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE m.id = ? AND m.is_available = 1 AND s.is_active = 1
    """, (medicine_id,)).fetchone()
    if available is None:
        return {"success": False, "message": "Medicine is not available."}, 404
    student_id = session["user_id"]
    existing = db.execute("""
        SELECT id FROM medical_wishlist_items
        WHERE student_id = ? AND medicine_id = ?
    """, (student_id, medicine_id)).fetchone()
    if existing:
        db.execute("DELETE FROM medical_wishlist_items WHERE id = ?", (existing["id"],))
        action, message = "removed", "Removed from wishlist."
    else:
        db.execute("INSERT INTO medical_wishlist_items (student_id, medicine_id) VALUES (?, ?)",
                   (student_id, medicine_id))
        action, message = "added", "Added to wishlist."
    db.commit()
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        flash(message, "success")
        return redirect(url_for("student_dashboard", _anchor="section-wishlist"))
    return {"success": True, "action": action, "message": message}


@app.route("/medical/cart/add/<int:medicine_id>", methods=["POST"])
@role_required("student")
def add_medicine_to_cart(medicine_id):
    db = get_db()
    student_id = session["user_id"]
    medicine = db.execute("""
        SELECT m.id, m.stock_quantity
        FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE m.id = ? AND m.is_available = 1 AND m.stock_quantity > 0
          AND s.is_active = 1
    """, (medicine_id,)).fetchone()
    if medicine is None:
        flash("This medicine is unavailable or out of stock.", "error")
        return redirect(url_for("student_medical_page", section="medicines"))

    current = db.execute("""
        SELECT quantity FROM medical_cart_items
        WHERE student_id = ? AND medicine_id = ?
    """, (student_id, medicine_id)).fetchone()
    if current and current["quantity"] >= medicine["stock_quantity"]:
        flash("Your medical cart has reached the available stock limit.", "error")
        return redirect(url_for("medical_cart"))
    if current:
        db.execute("""
            UPDATE medical_cart_items SET quantity = quantity + 1
            WHERE student_id = ? AND medicine_id = ?
        """, (student_id, medicine_id))
    else:
        db.execute("""
            INSERT INTO medical_cart_items (student_id, medicine_id, quantity)
            VALUES (?, ?, 1)
        """, (student_id, medicine_id))
    db.commit()
    flash("Medicine added to your medical cart.", "success")
    return redirect(url_for("medical_cart"))


@app.route("/medical/cart")
@role_required("student")
def medical_cart():
    db = get_db()
    cart_items = db.execute("""
        SELECT ci.medicine_id, ci.quantity, m.name, m.price, m.stock_quantity,
               m.image_url, s.name AS shop_name, (m.price * ci.quantity) AS item_total
        FROM medical_cart_items AS ci
        JOIN medical_medicines AS m ON m.id = ci.medicine_id
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        WHERE ci.student_id = ?
        ORDER BY ci.id DESC
    """, (session["user_id"],)).fetchall()
    cart_total = sum(item["item_total"] for item in cart_items)
    return render_template("medical_cart.html", cart_items=cart_items, cart_total=cart_total)


@app.route("/medical/cart/<int:medicine_id>/update", methods=["POST"])
@role_required("student")
def update_medical_cart_item(medicine_id):
    db = get_db()
    try:
        quantity = int(request.form.get("quantity", ""))
        if quantity < 1:
            raise ValueError
    except (ValueError, TypeError):
        flash("Enter a quantity of at least one.", "error")
        return redirect(url_for("medical_cart"))
    medicine = db.execute("""
        SELECT m.stock_quantity FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id
        JOIN medical_cart_items AS ci ON ci.medicine_id = m.id
        WHERE m.id = ? AND ci.student_id = ? AND m.is_available = 1 AND s.is_active = 1
    """, (medicine_id, session["user_id"])).fetchone()
    if medicine is None or quantity > medicine["stock_quantity"]:
        flash("Requested quantity is unavailable.", "error")
        return redirect(url_for("medical_cart"))
    db.execute("""
        UPDATE medical_cart_items SET quantity = ?
        WHERE student_id = ? AND medicine_id = ?
    """, (quantity, session["user_id"], medicine_id))
    db.commit()
    flash("Medical cart updated.", "success")
    return redirect(url_for("medical_cart"))


@app.route("/medical/cart/<int:medicine_id>/remove", methods=["POST"])
@role_required("student")
def remove_medical_cart_item(medicine_id):
    db = get_db()
    db.execute("DELETE FROM medical_cart_items WHERE student_id = ? AND medicine_id = ?", (session["user_id"], medicine_id))
    db.commit()
    flash("Medicine removed from your medical cart.", "success")
    return redirect(url_for("medical_cart"))


@app.route("/medical/checkout")
@role_required("student")
def medical_checkout():
    db = get_db()
    student_id = session["user_id"]
    buy_now_id = request.args.get("buy_now", "").strip()
    if buy_now_id:
        if not buy_now_id.isdigit():
            flash("Medicine could not be selected.", "error")
            return redirect(url_for("student_medical_page", section="medicines"))
        items = db.execute("""
            SELECT m.id AS medicine_id, m.name, m.price, m.stock_quantity,
                   1 AS quantity, s.is_active
            FROM medical_medicines AS m
            JOIN medical_shops AS s ON s.id = m.medical_shop_id
            WHERE m.id = ? AND m.is_available = 1
        """, (int(buy_now_id),)).fetchall()
    else:
        items = db.execute("""
            SELECT m.id AS medicine_id, m.name, m.price, m.stock_quantity,
                   ci.quantity, s.is_active
            FROM medical_cart_items AS ci
            JOIN medical_medicines AS m ON m.id = ci.medicine_id
            JOIN medical_shops AS s ON s.id = m.medical_shop_id
            WHERE ci.student_id = ? AND m.is_available = 1
        """, (student_id,)).fetchall()
    if not items:
        flash("Your medical cart is empty or the selected medicine is unavailable.", "warning")
        return redirect(url_for("student_medical_page", section="medicines"))
    for item in items:
        if not item["is_active"] or item["stock_quantity"] < item["quantity"]:
            flash(f"{item['name']} is out of stock or unavailable.", "error")
            return redirect(url_for("medical_cart" if not buy_now_id else "student_medical_page", **({} if not buy_now_id else {"section": "medicines"})))
    total = sum(item["price"] * item["quantity"] for item in items)
    return render_template("medical_checkout.html", cart_items=items, cart_total=total, buy_now_medicine_id=buy_now_id)


@app.route("/medical/order", methods=["POST"])
@role_required("student")
def place_medical_order():
    db = get_db()
    student_id = session["user_id"]
    buy_now_id = request.form.get("buy_now_medicine_id", "").strip()
    delivery_address = request.form.get("delivery_address", "").strip()
    phone = request.form.get("phone", "").strip()
    if not delivery_address or not phone:
        flash("Delivery address and phone are required.", "error")
        return redirect(url_for("medical_checkout", **({"buy_now": buy_now_id} if buy_now_id else {})))
    try:
        if buy_now_id:
            if not buy_now_id.isdigit():
                raise ValueError("Invalid medicine selection.")
            items = db.execute("""
                SELECT m.id AS medicine_id, m.name, m.price, m.stock_quantity,
                       1 AS quantity, s.is_active
                FROM medical_medicines AS m
                JOIN medical_shops AS s ON s.id = m.medical_shop_id
                WHERE m.id = ? AND m.is_available = 1
            """, (int(buy_now_id),)).fetchall()
        else:
            items = db.execute("""
                SELECT m.id AS medicine_id, m.name, m.price, m.stock_quantity,
                       ci.quantity, s.is_active
                FROM medical_cart_items AS ci
                JOIN medical_medicines AS m ON m.id = ci.medicine_id
                JOIN medical_shops AS s ON s.id = m.medical_shop_id
                WHERE ci.student_id = ? AND m.is_available = 1
            """, (student_id,)).fetchall()
        if not items:
            raise ValueError("No available medicines were selected.")
        for item in items:
            if not item["is_active"] or item["quantity"] < 1 or item["quantity"] > item["stock_quantity"]:
                raise ValueError(f"{item['name']} is unavailable or has insufficient stock.")

        total = sum(item["price"] * item["quantity"] for item in items)
        cursor = db.execute("""
            INSERT INTO orders (student_id, total_amount, order_type, delivery_address)
            VALUES (?, ?, 'medical', ?)
        """, (student_id, total, delivery_address))
        order_id = cursor.lastrowid
        for item in items:
            db.execute("""
                INSERT INTO medical_order_items (order_id, medicine_id, item_name, quantity, unit_price)
                VALUES (?, ?, ?, ?, ?)
            """, (order_id, item["medicine_id"], item["name"], item["quantity"], item["price"]))
            db.execute("""
                UPDATE medical_medicines SET stock_quantity = stock_quantity - ?,
                    is_available = CASE WHEN stock_quantity - ? > 0 THEN 1 ELSE 0 END
                WHERE id = ?
            """, (item["quantity"], item["quantity"], item["medicine_id"]))
        if not buy_now_id:
            db.execute("DELETE FROM medical_cart_items WHERE student_id = ?", (student_id,))
        db.commit()
        flash(f"Medicine order placed successfully! Order ID: {order_id}", "success")
        return redirect(url_for("my_orders"))
    except (sqlite3.Error, ValueError) as error:
        db.rollback()
        app.logger.exception("Medical order placement failed")
        flash(str(error) or "Could not place the medicine order.", "error")
        return redirect(url_for("medical_cart" if not buy_now_id else "student_medical_page", **({} if not buy_now_id else {"section": "medicines"})))


@app.route("/medical/appointments", methods=["POST"])
@role_required("student")
def request_medical_appointment():
    db = get_db()
    doctor_id = request.form.get("doctor_id", "").strip()
    preferred_at = request.form.get("preferred_at", "").strip()
    reason = request.form.get("reason", "").strip()
    try:
        doctor_id = int(doctor_id)
        doctor = db.execute(
            "SELECT id FROM medical_doctors WHERE id = ? AND is_active = 1",
            (doctor_id,),
        ).fetchone()
        requested_time = datetime.fromisoformat(preferred_at)
        if not doctor or requested_time <= datetime.now():
            raise ValueError("Choose an available doctor and a future appointment time.")
        db.execute("""
            INSERT INTO medical_appointments (student_id, doctor_id, preferred_at, reason)
            VALUES (?, ?, ?, ?)
        """, (session["user_id"], doctor_id, preferred_at, reason or None))
        db.commit()
        flash("Appointment request sent. You can track its status below.", "success")
    except (ValueError, TypeError) as error:
        db.rollback()
        flash(str(error) or "Choose an available doctor and future appointment time.", "error")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Medical appointment request failed")
        flash("Could not submit the appointment request.", "error")
    return redirect(url_for("student_medical_page", section="appointments"))


@app.route("/medical/requests", methods=["POST"])
@role_required("student")
def submit_medical_request():
    db = get_db()
    request_type = request.form.get("request_type", "Medical Help").strip()
    description = request.form.get("description", "").strip()
    if request_type not in {"Medical Help", "Emergency Assistance"} or not description:
        flash("Choose a request type and describe what help you need.", "error")
        return redirect(url_for("student_medical_page", section="help"))
    try:
        db.execute("""
            INSERT INTO medical_requests (student_id, request_type, description)
            VALUES (?, ?, ?)
        """, (session["user_id"], request_type, description))
        db.commit()
        flash("Your medical request was sent to the admin.", "success")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Medical help request failed")
        flash("Could not send the medical request. Please try again.", "error")
    return redirect(url_for("student_medical_page", section="requests"))


@app.route("/admin/medical", methods=["GET", "POST"])
@role_required("admin")
def admin_medical_management():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "add_shop":
                name = request.form.get("name", "").strip()
                if not name:
                    raise ValueError("Medical shop name is required.")
                db.execute("""
                    INSERT INTO medical_shops (name, description, phone, address, opening_hours)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    name,
                    request.form.get("description", "").strip() or None,
                    request.form.get("phone", "").strip() or None,
                    request.form.get("address", "").strip() or None,
                    request.form.get("opening_hours", "").strip() or None,
                ))
                flash("Medical shop added.", "success")
            elif action == "add_medicine":
                shop_id = int(request.form.get("medical_shop_id", ""))
                name = request.form.get("name", "").strip()
                price = float(request.form.get("price", ""))
                stock = int(request.form.get("stock_quantity", ""))
                shop = db.execute(
                    "SELECT id FROM medical_shops WHERE id = ? AND is_active = 1", (shop_id,)
                ).fetchone()
                if not shop or not name or price < 0 or stock < 0:
                    raise ValueError("Enter a valid medicine, shop, price and stock quantity.")
                image_url = save_uploaded_image(
                    request.files.get("medicine_image"), "medical", "medicine"
                )
                db.execute("""
                    INSERT INTO medical_medicines
                        (medical_shop_id, name, description, price, stock_quantity, image_url, is_available)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    shop_id, name,
                    request.form.get("description", "").strip() or None,
                    price, stock,
                    image_url,
                    1 if stock > 0 else 0,
                ))
                flash("Medicine added to the student medical page.", "success")
            elif action == "update_medicine":
                medicine_id = int(request.form.get("medicine_id", ""))
                price = float(request.form.get("price", ""))
                stock = int(request.form.get("stock_quantity", ""))
                if price < 0 or stock < 0:
                    raise ValueError("Price and stock cannot be negative.")
                db.execute("""
                    UPDATE medical_medicines
                    SET price = ?, stock_quantity = ?,
                        is_available = CASE WHEN ? > 0 THEN 1 ELSE 0 END
                    WHERE id = ?
                """, (price, stock, stock, medicine_id))
                flash("Medicine price and stock updated.", "success")
            elif action == "add_doctor":
                name = request.form.get("name", "").strip()
                if not name:
                    raise ValueError("Doctor name is required.")
                db.execute("""
                    INSERT INTO medical_doctors (name, specialty, clinic_name, phone, availability)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    name,
                    request.form.get("specialty", "").strip() or None,
                    request.form.get("clinic_name", "").strip() or None,
                    request.form.get("phone", "").strip() or None,
                    request.form.get("availability", "").strip() or None,
                ))
                flash("Doctor added to the appointment directory.", "success")
            elif action == "add_facility":
                name = request.form.get("name", "").strip()
                facility_type = request.form.get("facility_type", "Clinic").strip()
                if not name or facility_type not in {"Hospital", "Clinic", "Emergency Centre"}:
                    raise ValueError("Enter a facility name and valid facility type.")
                db.execute("""
                    INSERT INTO medical_facilities
                        (name, facility_type, phone, address, opening_hours)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    name, facility_type,
                    request.form.get("phone", "").strip() or None,
                    request.form.get("address", "").strip() or None,
                    request.form.get("opening_hours", "").strip() or None,
                ))
                flash("Hospital or clinic added to the student directory.", "success")
            elif action == "appointment_status":
                appointment_id = int(request.form.get("appointment_id", ""))
                status = request.form.get("status", "")
                if status not in {"Pending", "Confirmed", "Completed", "Cancelled"}:
                    raise ValueError("Choose a valid appointment status.")
                db.execute("""
                    UPDATE medical_appointments SET status = ?, admin_notes = ? WHERE id = ?
                """, (
                    status,
                    request.form.get("admin_notes", "").strip() or None,
                    appointment_id,
                ))
                flash("Appointment status updated.", "success")
            elif action == "medical_request_status":
                request_id = int(request.form.get("request_id", ""))
                status = request.form.get("status", "")
                if status not in {"Pending", "In Progress", "Resolved", "Cancelled"}:
                    raise ValueError("Choose a valid medical request status.")
                db.execute("""
                    UPDATE medical_requests SET status = ?, admin_notes = ? WHERE id = ?
                """, (
                    status,
                    request.form.get("admin_notes", "").strip() or None,
                    request_id,
                ))
                flash("Medical help request updated.", "success")
            elif action in {"toggle_shop", "toggle_doctor", "toggle_facility", "toggle_medicine"}:
                record_id = int(request.form.get("record_id", ""))
                if action == "toggle_medicine":
                    db.execute("UPDATE medical_medicines SET is_available = CASE is_available WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (record_id,))
                else:
                    table = {
                        "toggle_shop": "medical_shops",
                        "toggle_doctor": "medical_doctors",
                        "toggle_facility": "medical_facilities",
                    }[action]
                    db.execute(f"UPDATE {table} SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (record_id,))
                flash("Medical listing status updated.", "success")
            else:
                raise ValueError("Unknown medical management action.")
            db.commit()
        except (ValueError, TypeError) as error:
            db.rollback()
            flash(str(error) or "Please check the submitted details.", "error")
        except sqlite3.Error:
            db.rollback()
            app.logger.exception("Medical management update failed")
            flash("Could not save the medical management update.", "error")
        return redirect(url_for("admin_medical_management"))

    medical_shops = db.execute("SELECT * FROM medical_shops ORDER BY name").fetchall()
    medicines = db.execute("""
        SELECT m.*, s.name AS shop_name FROM medical_medicines AS m
        JOIN medical_shops AS s ON s.id = m.medical_shop_id ORDER BY m.id DESC
    """).fetchall()
    doctors = db.execute("SELECT * FROM medical_doctors ORDER BY name").fetchall()
    facilities = db.execute("SELECT * FROM medical_facilities ORDER BY name").fetchall()
    appointments = db.execute("""
        SELECT a.*, d.name AS doctor_name, d.clinic_name, u.full_name AS student_name,
               u.phone AS student_phone
        FROM medical_appointments AS a
        JOIN medical_doctors AS d ON d.id = a.doctor_id
        JOIN users AS u ON u.id = a.student_id
        ORDER BY a.id DESC
    """).fetchall()
    medical_requests = db.execute("""
        SELECT r.*, u.full_name AS student_name, u.phone AS student_phone
        FROM medical_requests AS r
        JOIN users AS u ON u.id = r.student_id
        ORDER BY r.id DESC
    """).fetchall()
    return render_template(
        "admin_medical_management.html",
        medical_shops=medical_shops,
        medicines=medicines,
        doctors=doctors,
        facilities=facilities,
        appointments=appointments,
        medical_requests=medical_requests,
    )

@app.route("/marketplace")
@role_required("student")
def marketplace():

    db = get_db()
    category_filter = request.args.get("category", "").strip().lower()
    if category_filter not in {"clothing-tailoring", "food-snacks"}:
        category_filter = ""

    products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            p.sizes,
            p.out_of_stock_sizes,
            p.stock_quantity,
            c.name AS category_name,
            c.slug AS category_slug,
            s.name AS shop_name
        FROM products AS p
        JOIN shops AS s ON p.shop_id = s.id
        LEFT JOIN categories AS c ON s.category_id = c.id
        WHERE p.is_available = 1
          AND p.is_published = 1
          AND s.is_active = 1
          AND (? = '' OR lower(c.slug) = ?)
        ORDER BY p.id DESC
    """, (category_filter, category_filter)).fetchall()

    wishlist_rows = db.execute("""
        SELECT product_id
        FROM wishlist_items
        WHERE student_id = ?
    """, (session["user_id"],)).fetchall()

    wishlist_ids = [
        row["product_id"] for row in wishlist_rows
    ]

    return render_template(
        "marketplace.html",
        products=products,
        wishlist_ids=wishlist_ids,
        category_filter=category_filter
    )



# =========================================================
# PRODUCT DETAILS
# =========================================================

@app.route("/product/<int:product_id>")
def product_details(product_id):

    db = get_db()

    product = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            p.sizes,
            p.out_of_stock_sizes,
            p.stock_quantity,
            p.is_available,
            p.is_published,
            s.name AS shop_name,
            s.address AS shop_address,
            c.name AS category_name,
            c.slug AS category_slug
        FROM products AS p
        JOIN shops AS s
            ON p.shop_id = s.id
        LEFT JOIN categories AS c
            ON s.category_id = c.id
        WHERE p.id = ?
          AND s.is_active = 1
          AND p.is_available = 1
          AND p.is_published = 1
    """, (product_id,)).fetchone()

    if product is None:
        return render_template("404.html"), 404

    is_wishlisted = False
    if session.get("user_id") and (session.get("user_role") or session.get("role")) == "student":
        is_wishlisted = db.execute("""
            SELECT 1 FROM wishlist_items
            WHERE student_id = ? AND product_id = ?
        """, (session["user_id"], product_id)).fetchone() is not None

    return render_template(
        "product_details.html",
        product=product,
        is_wishlisted=is_wishlisted
    )


# =========================================================
# ADMIN PRODUCT MANAGEMENT
# =========================================================

@app.route("/admin/products")
@role_required("admin")
def admin_products():

    db = get_db()
    category_filter = request.args.get("category", "all").strip().lower()
    if category_filter not in {"food-snacks", "clothing-tailoring"}:
        category_filter = "all"

    products = db.execute("""
        SELECT
            p.id,
            p.name,
            p.description,
            p.price,
            p.image_url,
            p.sizes,
            p.out_of_stock_sizes,
            p.stock_quantity,
            p.is_available,
            p.is_published,
            p.created_at,
            s.name AS shop_name,
            s.address AS shop_address,
            c.name AS category_name,
            c.slug AS category_slug
        FROM products AS p
        LEFT JOIN shops AS s
            ON s.id = p.shop_id
        LEFT JOIN categories AS c
            ON c.id = s.category_id
        WHERE (? = 'all' OR lower(c.slug) = ?)
        ORDER BY p.id DESC
    """, (category_filter, category_filter)).fetchall()

    return render_template(
        "product_management.html",
        products=products,
        category_filter=category_filter
    )


# =========================================================
# ADD PRODUCT
# =========================================================

@app.route("/admin/products/add", methods=["GET", "POST"])
@role_required("admin")
def add_product():

    db = get_db()
    category_filter = request.form.get("category_filter", request.args.get("category", "")).strip().lower()
    if category_filter not in {"food-snacks", "clothing-tailoring"}:
        category_filter = ""

    shops = db.execute("""
        SELECT s.id, s.name, s.address, c.name AS category_name, c.slug AS category_slug
        FROM shops AS s
        LEFT JOIN categories AS c ON c.id = s.category_id
        WHERE s.is_active = 1
          AND (? = '' OR lower(c.slug) = ?)
        ORDER BY s.name ASC
    """, (category_filter, category_filter)).fetchall()

    if request.method == "POST":

        name = request.form.get(
            "name", ""
        ).strip()

        description = request.form.get(
            "description", ""
        ).strip()

        image_url = request.form.get("image_url", "").strip()

        shop_id_text = request.form.get(
            "shop_id", ""
        ).strip()

        price_text = request.form.get(
            "price", ""
        ).strip()

        stock_text = request.form.get(
            "stock_quantity", "0"
        ).strip()

        size_text = request.form.get("sizes", "").strip()
        out_of_stock_size_text = request.form.get("out_of_stock_sizes", "").strip()

        if not name or not shop_id_text or not price_text:

            flash(
                "Product name, shop and price are required.",
                "error"
            )

            return redirect(url_for("add_product"))

        try:

            price = float(price_text)
            stock_quantity = int(stock_text)
            shop_id = int(shop_id_text)

            if price < 0 or stock_quantity < 0:
                raise ValueError

        except (ValueError, TypeError):

            flash(
                "Enter a valid price and stock quantity.",
                "error"
            )

            return redirect(url_for("add_product"))

        shop = db.execute("""
            SELECT s.id, c.slug AS category_slug
            FROM shops AS s
            LEFT JOIN categories AS c ON c.id = s.category_id
            WHERE s.id = ?
              AND s.is_active = 1
              AND (? = '' OR lower(c.slug) = ?)
        """, (shop_id, category_filter, category_filter)).fetchone()

        if shop is None:

            flash(
                "Please select a valid active shop.",
                "error"
            )

            return redirect(url_for("add_product"))

        try:
            image_url = save_product_image(request.files.get("product_image")) or image_url or None
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("add_product"))

        available_size_values = list(dict.fromkeys(size.strip().upper() for size in size_text.split(",") if size.strip())) if shop["category_slug"] == "clothing-tailoring" else []
        out_of_stock_size_values = list(dict.fromkeys(size.strip().upper() for size in out_of_stock_size_text.split(",") if size.strip())) if shop["category_slug"] == "clothing-tailoring" else []
        available_size_values = [size for size in available_size_values if size not in out_of_stock_size_values]

        try:

            db.execute("""
                INSERT INTO products (
                    shop_id,
                    name,
                    description,
                    price,
                    image_url,
                    sizes,
                    out_of_stock_sizes,
                    stock_quantity,
                    is_available,
                    is_published
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                shop_id,
                name,
                description,
                price,
                image_url or None,
                ", ".join(available_size_values),
                ", ".join(out_of_stock_size_values),
                stock_quantity,
                1 if stock_quantity > 0 else 0
            ))

            db.commit()

            flash(
                "Product added successfully. Publish it when ready.",
                "success"
            )

            return redirect(url_for("admin_products"))

        except sqlite3.Error:

            db.rollback()

            app.logger.exception("Adding product failed")

            flash(
                "Could not save the product. Please try again.",
                "error"
            )

    return render_template(
        "add_product.html",
        shops=shops,
        product=None,
        page_title=("Add Food Product" if category_filter == "food-snacks" else "Add Clothing Product" if category_filter == "clothing-tailoring" else "Add New Product"),
        category_filter=category_filter
    )


# =========================================================
# EDIT PRODUCT
# =========================================================

@app.route(
    "/admin/products/<int:product_id>/edit",
    methods=["GET", "POST"]
)
@role_required("admin")
def edit_product(product_id):

    db = get_db()

    product = db.execute("""
        SELECT p.*, c.slug AS shop_category_slug
        FROM products AS p
        LEFT JOIN shops AS s ON s.id = p.shop_id
        LEFT JOIN categories AS c ON c.id = s.category_id
        WHERE p.id = ?
    """, (product_id,)).fetchone()

    if product is None:

        flash(
            "Product not found.",
            "error"
        )

        return redirect(url_for("admin_products"))

    shops = db.execute("""
        SELECT s.id, s.name, s.address, c.slug AS category_slug
        FROM shops AS s
        LEFT JOIN categories AS c ON c.id = s.category_id
        WHERE s.is_active = 1
        ORDER BY s.name
    """).fetchall()

    if request.method == "POST":

        name = request.form.get(
            "name", ""
        ).strip()

        description = request.form.get(
            "description", ""
        ).strip()

        image_url = request.form.get("image_url", "").strip()

        shop_id_text = request.form.get(
            "shop_id", ""
        ).strip()

        price_text = request.form.get(
            "price", ""
        ).strip()

        stock_text = request.form.get(
            "stock_quantity", "0"
        ).strip()

        size_text = request.form.get("sizes", "").strip()
        out_of_stock_size_text = request.form.get("out_of_stock_sizes", "").strip()

        try:

            price = float(price_text)
            stock_quantity = int(stock_text)
            shop_id = int(shop_id_text)

            if (
                not name
                or price < 0
                or stock_quantity < 0
            ):
                raise ValueError

        except (ValueError, TypeError):

            flash(
                "Please enter valid product details.",
                "error"
            )

            return redirect(
                url_for(
                    "edit_product",
                    product_id=product_id
                )
            )

        shop = db.execute("""
            SELECT s.id, c.slug AS category_slug
            FROM shops AS s
            LEFT JOIN categories AS c ON c.id = s.category_id
            WHERE s.id = ? AND s.is_active = 1
        """, (shop_id,)).fetchone()

        if shop is None:

            flash(
                "Please select a valid active shop.",
                "error"
            )

            return redirect(
                url_for(
                    "edit_product",
                    product_id=product_id
                )
            )

        try:
            image_url = save_product_image(request.files.get("product_image")) or image_url or product["image_url"]
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("edit_product", product_id=product_id))

        available_size_values = list(dict.fromkeys(size.strip().upper() for size in size_text.split(",") if size.strip())) if shop["category_slug"] == "clothing-tailoring" else []
        out_of_stock_size_values = list(dict.fromkeys(size.strip().upper() for size in out_of_stock_size_text.split(",") if size.strip())) if shop["category_slug"] == "clothing-tailoring" else []
        available_size_values = [size for size in available_size_values if size not in out_of_stock_size_values]

        try:

            db.execute("""
                UPDATE products
                SET
                    shop_id = ?,
                    name = ?,
                    description = ?,
                    price = ?,
                    image_url = ?,
                    sizes = ?,
                    out_of_stock_sizes = ?,
                    stock_quantity = ?,
                    is_available = CASE
                        WHEN ? > 0 THEN 1
                        ELSE 0
                    END
                WHERE id = ?
            """, (
                shop_id,
                name,
                description,
                price,
                image_url or None,
                ", ".join(available_size_values),
                ", ".join(out_of_stock_size_values),
                stock_quantity,
                stock_quantity,
                product_id
            ))

            db.commit()

            flash(
                "Product updated successfully.",
                "success"
            )

            return redirect(url_for("admin_products"))

        except sqlite3.Error:

            db.rollback()

            app.logger.exception("Updating product failed")

            flash(
                "Could not update the product.",
                "error"
            )

    return render_template(
        "add_product.html",
        shops=shops,
        product=product,
        page_title="Edit Product"
    )


# =========================================================
# PUBLISH / UNPUBLISH PRODUCT
# =========================================================

@app.route(
    "/admin/products/<int:product_id>/toggle-publish",
    methods=["POST"]
)
@role_required("admin")
def toggle_product_publish(product_id):

    db = get_db()

    product = db.execute("""
        SELECT id, is_published, is_available
        FROM products
        WHERE id = ?
    """, (product_id,)).fetchone()

    if product is None:

        flash(
            "Product not found.",
            "error"
        )

    else:

        if not product["is_published"] and not product["is_available"]:

            flash(
                "This product has no available stock. "
                "Update its stock before publishing.",
                "error"
            )

            return redirect(url_for("admin_products"))

        new_status = (
            0 if product["is_published"] else 1
        )

        try:

            db.execute("""
                UPDATE products
                SET is_published = ?
                WHERE id = ?
            """, (
                new_status,
                product_id
            ))

            db.commit()

            flash(
                "Product published successfully."
                if new_status
                else "Product unpublished successfully.",
                "success"
            )

        except sqlite3.Error:

            db.rollback()

            flash(
                "Could not update the product status.",
                "error"
            )

    return redirect(url_for("admin_products"))

# =========================================================
# HOSTELHUB - ADD TO CART
# =========================================================

@app.route("/cart/add/<int:product_id>", methods=["POST"])
@role_required("student")
def add_to_cart(product_id):
    db = get_db()
    student_id = session["user_id"]

    product = db.execute("""
        SELECT id, stock_quantity
        FROM products
        WHERE id = ?
          AND is_available = 1
          AND is_published = 1
    """, (product_id,)).fetchone()

    if product is None:
        return {
            "success": False,
            "message": "Product is not available."
        }, 404

    existing = db.execute("""
        SELECT quantity
        FROM cart_items
        WHERE student_id = ? AND product_id = ?
    """, (student_id, product_id)).fetchone()

    if existing:
        if existing["quantity"] >= product["stock_quantity"]:
            return {
                "success": False,
                "message": "Available stock limit reached."
            }, 400

        db.execute("""
            UPDATE cart_items
            SET quantity = quantity + 1
            WHERE student_id = ? AND product_id = ?
        """, (student_id, product_id))
    else:
        if product["stock_quantity"] < 1:
            return {
                "success": False,
                "message": "Product is out of stock."
            }, 400

        db.execute("""
            INSERT INTO cart_items (student_id, product_id, quantity)
            VALUES (?, ?, 1)
        """, (student_id, product_id))

    db.commit()

    cart_result = db.execute("""
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM cart_items
        WHERE student_id = ?
    """, (student_id,)).fetchone()

    return {
        "success": True,
        "message": "Product added to cart!",
        "cart_count": cart_result["total"]
    }


# =========================================================
# HOSTELHUB - WISHLIST TOGGLE
# =========================================================

@app.route("/wishlist/remove/<int:product_id>", methods=["POST"])
@role_required("student")
def remove_wishlist_item(product_id):
    db = get_db()
    db.execute("""
        DELETE FROM wishlist_items
        WHERE student_id = ? AND product_id = ?
    """, (session["user_id"], product_id))
    db.commit()
    flash("Removed from wishlist.", "success")
    return redirect(url_for("student_dashboard", _anchor="section-wishlist"))


@app.route("/wishlist/toggle/<int:product_id>", methods=["POST"])
@role_required("student")
def toggle_wishlist(product_id):
    db = get_db()
    student_id = session["user_id"]

    product = db.execute("""
        SELECT p.id
        FROM products AS p
        JOIN shops AS s ON p.shop_id = s.id
        WHERE p.id = ?
          AND p.is_available = 1
          AND p.is_published = 1
          AND s.is_active = 1
    """, (product_id,)).fetchone()

    if product is None:
        return {
            "success": False,
            "message": "Product is not available."
        }, 404

    existing = db.execute("""
        SELECT id
        FROM wishlist_items
        WHERE student_id = ? AND product_id = ?
    """, (student_id, product_id)).fetchone()

    if existing:
        db.execute("""
            DELETE FROM wishlist_items
            WHERE student_id = ? AND product_id = ?
        """, (student_id, product_id))

        action = "removed"
        message = "Removed from wishlist."
    else:
        db.execute("""
            INSERT INTO wishlist_items (student_id, product_id)
            VALUES (?, ?)
        """, (student_id, product_id))

        action = "added"
        message = "Added to wishlist!"

    db.commit()

    return {
        "success": True,
        "action": action,
        "message": message
    }


# =========================================================
# DELETE PRODUCT
# =========================================================

@app.route(
    "/admin/products/<int:product_id>/delete",
    methods=["POST"]
)
@role_required("admin")
def delete_product(product_id):

    db = get_db()

    product = db.execute("""
        SELECT id
        FROM products
        WHERE id = ?
    """, (product_id,)).fetchone()

    if product is None:

        flash(
            "Product not found.",
            "error"
        )

        return redirect(url_for("admin_products"))

    references = db.execute("""
        SELECT
            (
                SELECT COUNT(*)
                FROM order_items
                WHERE product_id = ?
            ) AS orders,

            (
                SELECT COUNT(*)
                FROM cart_items
                WHERE product_id = ?
            ) AS carts,

            (
                SELECT COUNT(*)
                FROM wishlist_items
                WHERE product_id = ?
            ) AS wishlists
    """, (
        product_id,
        product_id,
        product_id
    )).fetchone()

    try:

        if (
            references["orders"]
            or references["carts"]
            or references["wishlists"]
        ):

            db.execute("""
                UPDATE products
                SET
                    is_published = 0,
                    is_available = 0
                WHERE id = ?
            """, (product_id,))

            db.commit()

            flash(
                "This product has existing references. "
                "It has been unpublished and made unavailable.",
                "success"
            )

        else:

            db.execute("""
                DELETE FROM products
                WHERE id = ?
            """, (product_id,))

            db.commit()

            flash(
                "Product deleted successfully.",
                "success"
            )

    except sqlite3.Error:

        db.rollback()

        app.logger.exception("Deleting product failed")

        flash(
            "Could not delete the product.",
            "error"
        )

    return redirect(url_for("admin_products"))




# =========================================================
# ADMIN - SHOP MANAGEMENT
# =========================================================

@app.route("/admin/shops")
@app.route("/admin/shop-management")
@role_required("admin")
def admin_shops():
    db = get_db()

    # Fetch all shops with category names and product counts
    shops = db.execute("""
        SELECT
            s.*,
            c.name AS category_name,
            (
                SELECT COUNT(*)
                FROM products p
                WHERE p.shop_id = s.id
            ) AS total_products
        FROM shops s
        LEFT JOIN categories c
            ON c.id = s.category_id
        ORDER BY s.id DESC
    """).fetchall()

    # Fetch active categories in the desired order
    
    categories = db.execute("""
        SELECT
            id,
            name,
            icon
        FROM categories
        WHERE is_active = 1
        ORDER BY
            CASE name
                WHEN 'Clothing & Tailoring' THEN 1
                WHEN 'Daily Essentials' THEN 2
                WHEN 'Electronics & Mobile Repair' THEN 3
                WHEN 'Food & Snacks' THEN 4
                WHEN 'Laundry' THEN 5
                WHEN 'Medical & Health' THEN 6
                WHEN 'Printing Services' THEN 7
                WHEN 'Other Services' THEN 8
                
            END,
            name
    """).fetchall()

    providers = db.execute("SELECT id, full_name FROM users WHERE role = 'provider' AND is_active = 1 ORDER BY full_name").fetchall()

    return render_template(
        "shop_management.html",
        shops=shops,
        categories=categories,
        providers=providers
    )

    



# =========================================================
# ADMIN - USER MANAGEMENT
# =========================================================

@app.route("/admin/users/create", methods=["POST"])
@role_required("admin")
def admin_create_user():
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    role = request.form.get("role", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    allowed_roles = {"admin", "warden", "provider"}

    if not full_name or len(full_name) > 120 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Enter a name and valid email address.", "error")
        return redirect(url_for("admin_users", _anchor="add-staff"))
    if role not in allowed_roles:
        flash("Choose Admin, Warden or Shopkeeper.", "error")
        return redirect(url_for("admin_users", _anchor="add-staff"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_users", _anchor="add-staff"))
    if password != confirm_password:
        flash("Passwords do not match.", "error")
        return redirect(url_for("admin_users", _anchor="add-staff"))

    db = get_db()
    try:
        db.execute("""
            INSERT INTO users (full_name, email, phone, password_hash, role, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (full_name, email, phone or None, generate_password_hash(password), role))
        db.commit()
        role_label = "Shopkeeper" if role == "provider" else role.title()
        flash(f"{role_label} account created. They can now sign in with the selected role.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("An account with this email already exists.", "error")
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Admin staff account creation failed")
        flash("Could not create the account. Please try again.", "error")
    return redirect(url_for("admin_users", _anchor="add-staff"))

@app.route("/admin/users")
@app.route("/admin/user-management")
@role_required("admin")
def admin_users():

    db = get_db()

    search = request.args.get("search", "").strip()
    selected_role = request.args.get("role", "").strip().lower()

    allowed_roles = {
        "",
        "student",
        "admin",
        "warden",
        "provider"
    }

    if selected_role not in allowed_roles:
        selected_role = ""
        flash("Invalid role filter.", "error")

    query = """
        SELECT
            id,
            full_name,
            email,
            phone,
            student_id,
            role,
            is_active,
            created_at
        FROM users
        WHERE 1 = 1
    """

    params = []

    if search:
        query += """
            AND (
                full_name LIKE ?
                OR email LIKE ?
                OR phone LIKE ?
                OR student_id LIKE ?
            )
        """

        term = f"%{search}%"
        params.extend([term, term, term, term])

    if selected_role:
        query += " AND role = ?"
        params.append(selected_role)

    query += " ORDER BY id DESC"

    users = db.execute(
        query,
        params
    ).fetchall()

    total_users = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
    """).fetchone()["total"]

    active_users = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE is_active = 1
    """).fetchone()["total"]

    total_students = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE role = 'student'
    """).fetchone()["total"]

    return render_template(
        "user_management.html",
        users=users,
        search=search,
        selected_role=selected_role,
        total_users=total_users,
        active_users=active_users,
        total_students=total_students
    )



@app.route("/admin/users/download")
@role_required("admin")
def admin_download_users():
    db = get_db()

    search = request.args.get("search", "").strip()
    selected_role = request.args.get("role", "").strip().lower()

    query = """
        SELECT id, full_name, email, phone, student_id,
               role, is_active, created_at
        FROM users
        WHERE 1 = 1
    """
    params = []

    if search:
        query += """
            AND (
                full_name LIKE ? OR email LIKE ?
                OR phone LIKE ? OR student_id LIKE ?
            )
        """
        term = f"%{search}%"
        params.extend([term, term, term, term])

    if selected_role in {"student", "admin", "warden", "provider"}:
        query += " AND role = ?"
        params.append(selected_role)

    query += " ORDER BY id DESC"

    users = db.execute(query, params).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "Full Name", "Email", "Phone",
        "Student ID", "Role", "Status", "Created At"
    ])

    for user in users:
        writer.writerow([
            user["id"],
            user["full_name"],
            user["email"],
            user["phone"],
            user["student_id"],
            user["role"],
            "Active" if user["is_active"] else "Inactive",
            user["created_at"]
        ])

    filename = f"hostelhub_users_{datetime.now():%Y%m%d_%H%M%S}.csv"

    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )



# =========================================================
# ADMIN - ACTIVATE / DEACTIVATE USER
# =========================================================

@app.route(
    "/admin/users/<int:user_id>/toggle",
    methods=["POST"]
)
@role_required("admin")
def toggle_user(user_id):

    db = get_db()

    # Prevent admin from deactivating their own account.
    if user_id == session.get("user_id"):

        flash(
            "You cannot deactivate your own account.",
            "error"
        )

        return redirect(url_for("admin_users"))

    user = db.execute("""
        SELECT id, role, is_active
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    if user is None:

        flash("User not found.", "error")

        return redirect(url_for("admin_users"))

    new_status = 0 if user["is_active"] else 1

    try:

        db.execute("""
            UPDATE users
            SET is_active = ?
            WHERE id = ?
        """, (new_status, user_id))

        db.commit()

        flash(
            "User activated successfully."
            if new_status
            else "User deactivated successfully.",
            "success"
        )

    except sqlite3.Error:

        db.rollback()

        flash(
            "Unable to update user status.",
            "error"
        )

    return redirect(url_for("admin_users"))





# =========================================================
# ADMIN - ACTIVATE / DEACTIVATE SHOP
# =========================================================

@app.route(
    "/admin/shops/<int:shop_id>/toggle",
    methods=["POST"]
)
@role_required("admin")
def toggle_shop(shop_id):

    db = get_db()

    shop = db.execute("""
        SELECT *
        FROM shops
        WHERE id = ?
    """, (shop_id,)).fetchone()

    if shop is None:
        flash("Shop not found.", "error")
        return redirect(url_for("admin_shops"))

    try:

        # Support the common is_active field.
        current_status = shop["is_active"]
        new_status = 0 if current_status else 1

        db.execute("""
            UPDATE shops
            SET is_active = ?
            WHERE id = ?
        """, (new_status, shop_id))

        db.commit()

        flash(
            "Shop activated successfully."
            if new_status
            else "Shop deactivated successfully.",
            "success"
        )

    except (sqlite3.Error, IndexError):

        db.rollback()

        flash(
            "Could not update shop status. Check whether the shops table has an is_active column.",
            "error"
        )

    return redirect(url_for("admin_shops"))



# =========================================================
# ADMIN - DELETE SHOP
# =========================================================

@app.route("/admin/shops/<int:shop_id>/delete", methods=["POST"])
@role_required("admin")
def delete_shop(shop_id):
    db = get_db()

    shop = db.execute(
        "SELECT id, name FROM shops WHERE id = ?",
        (shop_id,)
    ).fetchone()

    if not shop:
        flash("Shop not found.", "error")
        return redirect(url_for("admin_shops"))

    try:
        # Check whether this shop has associated services
        services = db.execute(
            "SELECT COUNT(*) FROM services WHERE shop_id = ?",
            (shop_id,)
        ).fetchone()[0]

        if services > 0:
            flash(
                "This shop has associated services. Remove those services first.",
                "error"
            )
            return redirect(url_for("admin_shops"))

        db.execute(
            "DELETE FROM shops WHERE id = ?",
            (shop_id,)
        )
        db.commit()

        flash(f"Shop '{shop['name']}' deleted successfully.", "success")

    except sqlite3.Error:
        db.rollback()
        app.logger.exception("DELETE SHOP ERROR")
        flash("Unable to delete shop. Check the terminal for details.", "error")

    return redirect(url_for("admin_shops"))


# =========================================================
# ADMIN - VERIFY SHOP
# =========================================================

@app.route(
    "/admin/shops/<int:shop_id>/verify",
    methods=["POST"]
)
@role_required("admin")
def verify_shop(shop_id):

    db = get_db()

    shop = db.execute("""
        SELECT *
        FROM shops
        WHERE id = ?
    """, (shop_id,)).fetchone()

    if shop is None:
        flash("Shop not found.", "error")
        return redirect(url_for("admin_shops"))

    
    try:
        db.execute("""
            UPDATE shops
            SET is_verified = 1
            WHERE id = ?
        """, (shop_id,))
        db.commit()
        flash("Shop verified successfully.", "success")

    except sqlite3.Error:
        db.rollback()
        app.logger.exception("SHOP VERIFICATION ERROR")
        flash("Could not verify shop.", "error")

    return redirect(url_for("admin_shops"))


# =========================================================
# ADMIN - ADD SHOP
# =========================================================

@app.route("/admin/shops/add", methods=["POST"])
@role_required("admin")
def add_shop():
    db = get_db()

    shop_name = request.form.get("shop_name", "").strip()
    category_id = request.form.get("category_id", "").strip()
    address = request.form.get("address", "").strip()
    opening_hours = request.form.get("opening_hours", "").strip()
    description = request.form.get("description", "").strip()
    provider_id = request.form.get("provider_id", "").strip()

    if not shop_name:
        flash("Shop name is required.", "error")
        return redirect(url_for("admin_shops"))

    try:
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(shops)"
            ).fetchall()
        }

        if "name" not in columns:
            raise sqlite3.OperationalError(
                "The shops table does not contain a name column."
            )

        insert_columns = ["name"]
        values = [shop_name]

        if category_id:
            category = db.execute(
                """
                SELECT id FROM categories
                WHERE id = ? AND is_active = 1
                """,
                (category_id,)
            ).fetchone()

            if not category:
                flash("Please select a valid shop category.", "error")
                return redirect(url_for("admin_shops"))

            insert_columns.append("category_id")
            values.append(int(category_id))

        for column, value in [
            ("address", address),
            ("opening_hours", opening_hours),
            ("description", description),
        ]:
            if column in columns:
                insert_columns.append(column)
                values.append(value)

        if "provider_id" in columns and provider_id:
            provider = db.execute("SELECT id FROM users WHERE id = ? AND role = 'provider' AND is_active = 1", (provider_id,)).fetchone()
            if not provider:
                flash("Please select a valid active shopkeeper account.", "error")
                return redirect(url_for("admin_shops"))
            insert_columns.append("provider_id")
            values.append(int(provider_id))

        column_sql = ", ".join(insert_columns)
        placeholders = ", ".join("?" for _ in values)

        db.execute(
            f"INSERT INTO shops ({column_sql}) VALUES ({placeholders})",
            values
        )

        db.commit()
        flash("Shop added successfully!", "success")

    except (sqlite3.Error, ValueError):
        db.rollback()
        app.logger.exception("ADD SHOP ERROR")
        flash(
            "Unable to add shop. Check the VS Code terminal.",
            "error"
        )

    return redirect(url_for("admin_shops"))


# =========================================================
# ADMIN - EDIT SHOP
# =========================================================

@app.route("/admin/shops/<int:shop_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_shop(shop_id):
    db = get_db()

    shop = db.execute(
        "SELECT * FROM shops WHERE id = ?",
        (shop_id,)
    ).fetchone()

    if not shop:
        flash("Shop not found.", "error")
        return redirect(url_for("admin_shops"))

    categories = db.execute("""
        SELECT id, name, icon
        FROM categories
        WHERE is_active = 1
        ORDER BY CASE name
            WHEN 'Daily Essentials' THEN 1
            WHEN 'Clothing & Tailoring' THEN 2
            WHEN 'Electronics & Mobile Repair' THEN 3
            WHEN 'Food & Snacks' THEN 4
            WHEN 'Laundry' THEN 5
            WHEN 'Printing Services' THEN 6
            WHEN 'Medical & Health' THEN 7
            WHEN 'Other Services' THEN 8
            ELSE 9
        END, name
    """).fetchall()

    providers = db.execute("SELECT id, full_name FROM users WHERE role = 'provider' AND is_active = 1 ORDER BY full_name").fetchall()

    if request.method == "POST":
        shop_name = request.form.get("shop_name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        address = request.form.get("address", "").strip()
        opening_hours = request.form.get("opening_hours", "").strip()
        description = request.form.get("description", "").strip()

        if not shop_name or not category_id:
            flash(
                "Shop name and category are required.",
                "error"
            )
            return render_template(
                "edit_shop.html",
                shop=shop,
                categories=categories,
                providers=providers
            )

        try:
            category = db.execute(
                """
                SELECT id FROM categories
                WHERE id = ? AND is_active = 1
                """,
                (category_id,)
            ).fetchone()

            if not category:
                flash("Please select a valid category.", "error")
                return render_template(
                    "edit_shop.html",
                    shop=shop,
                    categories=categories,
                    providers=providers
                )

            columns = {
                row["name"]
                for row in db.execute(
                    "PRAGMA table_info(shops)"
                ).fetchall()
            }

            update_fields = [
                ("name", shop_name),
                ("category_id", int(category_id)),
                ("address", address),
                ("opening_hours", opening_hours),
                ("description", description),
                ("provider_id", int(request.form.get("provider_id")) if request.form.get("provider_id", "").isdigit() else None),
            ]

            available_fields = [
                (column, value)
                for column, value in update_fields
                if column in columns
            ]

            if not available_fields:
                raise sqlite3.OperationalError(
                    "No editable shop columns were found."
                )

            set_clause = ", ".join(
                f"{column} = ?"
                for column, value in available_fields
            )

            values = [
                value
                for column, value in available_fields
            ]
            values.append(shop_id)

            db.execute(
                f"UPDATE shops SET {set_clause} WHERE id = ?",
                values
            )

            db.commit()
            flash("Shop updated successfully!", "success")

            return redirect(url_for("admin_shops"))

        except (sqlite3.Error, ValueError):
            db.rollback()
            app.logger.exception("EDIT SHOP ERROR")
            flash(
                "Unable to update shop. Check the VS Code terminal.",
                "error"
            )

    return render_template(
        "edit_shop.html",
        shop=shop,
        categories=categories,
        providers=providers
    )

# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout", methods=["GET", "POST"])
def logout():

    session.clear()

    flash(
        "You have been logged out successfully.",
        "success"
    )

    return redirect(url_for("login"))


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/uploads/<folder>/<path:filename>")
def uploaded_file(folder, filename):
    if folder not in {"products", "medical", "payments"}:
        abort(404)
    return send_from_directory(UPLOADS_DIR / folder, filename)

@app.route("/health")
def health():

    return {
        "status": "healthy",
        "application": "HostelHub"
    }


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def page_not_found(error):

    try:
        return render_template("404.html"), 404

    except Exception:
        return "404 - Page not found", 404


# =========================================================
# START APPLICATION
# =========================================================

# Initialize the schema for both local starts and production WSGI servers.
with app.app_context():
    init_db()

if __name__ == "__main__":

    print("\n" + "=" * 45)
    print("       HOSTELHUB IS STARTING")
    print("=" * 45)
    print("Local URL: http://127.0.0.1:5000")
    print("Database:", app.config["DATABASE"])
    print("=" * 45 + "\n")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
