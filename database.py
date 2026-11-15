import sqlite3
from datetime import datetime, timedelta

DB_NAME = "kitchensync.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        station TEXT NOT NULL,
        price_paise INTEGER NOT NULL,
        prep_time_mins INTEGER NOT NULL,
        allergens TEXT DEFAULT 'None',
        is_available INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS restaurant_tables (
        id INTEGER PRIMARY KEY,
        table_number TEXT UNIQUE NOT NULL,
        capacity INTEGER NOT NULL,
        status TEXT DEFAULT 'AVAILABLE'
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_id INTEGER REFERENCES restaurant_tables(id),
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'ACTIVE',
        guest_count INTEGER DEFAULT 2
    );

    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER REFERENCES orders(id),
        menu_item_id INTEGER REFERENCES menu_items(id),
        quantity INTEGER NOT NULL,
        unit_price_paise INTEGER NOT NULL,
        notes TEXT DEFAULT '',
        status TEXT DEFAULT 'QUEUED',
        course TEXT DEFAULT 'Course 1'
    );
    """)

    if cursor.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0] == 0:
        menu_seed = [
            ("Paneer Tikka", "Starters", "Tandoor", 24000, 12, "Dairy"),
            ("Spicy Tuna Roll", "Starters", "Cold Prep", 32000, 10, "Fish, Soy"),
            ("Chicken Tikka Masala", "Mains", "Grill", 36000, 15, "Dairy, Nuts"),
            ("Veg Hakka Noodles", "Mains", "Wok", 22000, 8, "Gluten, Soy"),
            ("Butter Naan", "Mains", "Tandoor", 6000, 5, "Gluten, Dairy"),
            ("Mango Lassi", "Drinks", "Bar", 14000, 3, "Dairy"),
        ]
        cursor.executemany(
            "INSERT INTO menu_items (name, category, station, price_paise, prep_time_mins, allergens) VALUES (?, ?, ?, ?, ?, ?)",
            menu_seed
        )

    if cursor.execute("SELECT COUNT(*) FROM restaurant_tables").fetchone()[0] == 0:
        tables_seed = [
            (1, "T1", 2, "AVAILABLE"),
            (2, "T2", 4, "AVAILABLE"),
            (3, "T3", 4, "OCCUPIED"),
            (4, "T4", 6, "AVAILABLE"),
        ]
        cursor.executemany(
            "INSERT INTO restaurant_tables (id, table_number, capacity, status) VALUES (?, ?, ?, ?)",
            tables_seed
        )

        past_time = (datetime.utcnow() - timedelta(minutes=25)).isoformat()
        cursor.execute("INSERT INTO orders (table_id, created_at, status, guest_count) VALUES (3, ?, 'ACTIVE', 3)", (past_time,))
        order_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price_paise, notes, status, course) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (order_id, 1, 2, 24000, "Extra spicy", "PREPARING", "Course 1: Starters"),
                (order_id, 3, 2, 36000, "Medium spice", "QUEUED", "Course 2: Mains")
            ]
        )

    conn.commit()
    conn.close()