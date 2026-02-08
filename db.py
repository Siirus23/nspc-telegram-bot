import sqlite3

DB_NAME = "cards.db"

# ===========================
# ORDER STATUSES
# ===========================

STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_VERIFYING = "verifying"
STATUS_AWAITING_ADDRESS = "awaiting_address"
STATUS_PACKING_PENDING = "packing_pending"
STATUS_PACKED = "packed"
STATUS_SHIPPED = "shipped"

VALID_STATUSES = {
    STATUS_AWAITING_PAYMENT,
    STATUS_VERIFYING,
    STATUS_AWAITING_ADDRESS,
    STATUS_PACKING_PENDING,
    STATUS_PACKED,
    STATUS_SHIPPED,
}


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS card_listing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_chat_id INTEGER NOT NULL,
                channel_message_id INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                price TEXT NOT NULL,
                initial_qty INTEGER NOT NULL,
                remaining_qty INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_chat_id INTEGER NOT NULL,
                channel_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                claim_order INTEGER,
                claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS user_checkout (
                user_id INTEGER PRIMARY KEY,
                delivery_method TEXT,
                stage TEXT DEFAULT 'idle',
                invoice_no TEXT,
                cards_total REAL DEFAULT 0,
                delivery_fee REAL DEFAULT 0,
                total REAL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                username TEXT,
                delivery_method TEXT NOT NULL,
                cards_total REAL NOT NULL,
                delivery_fee REAL NOT NULL,
                total REAL NOT NULL,
                status TEXT DEFAULT 'pending_payment',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                tracking_number TEXT,
                shipping_proof_file_id TEXT
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                price REAL NOT NULL,
                post_message_id INTEGER,
                qty INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shipping_address (
                order_id INTEGER PRIMARY KEY,
                name TEXT,
                street_name TEXT,
                unit_number TEXT,
                postal_code TEXT,
                phone_number TEXT,
                confirmed INTEGER DEFAULT 0,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                admin_id INTEGER PRIMARY KEY,
                session_type TEXT NOT NULL,
                invoice_no TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_card_lookup
            ON card_listing(channel_chat_id, channel_message_id);

            CREATE INDEX IF NOT EXISTS idx_claims_user
            ON claims(user_id);

            CREATE INDEX IF NOT EXISTS idx_orders_user
            ON orders(user_id);

            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON orders(status);

            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT,
                admin_id INTEGER,
                target_user_id INTEGER,
                card_name TEXT,
                channel_message_id INTEGER,
                quantity INTEGER,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # ---- lightweight migrations ----
        def _column_exists(table: str, column: str) -> bool:
            cur = conn.execute(f"PRAGMA table_info({table})")
            return any(r["name"] == column for r in cur.fetchall())

        if not _column_exists("orders", "shipping_proof_file_id"):
            conn.execute("ALTER TABLE orders ADD COLUMN shipping_proof_file_id TEXT")

        if not _column_exists("orders", "payment_proof_file_id"):
            conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_file_id TEXT")

        if not _column_exists("orders", "payment_proof_type"):
            conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_type TEXT")

        conn.commit()


# =========================
# ADMIN SESSION HELPERS
# =========================

def set_admin_session(admin_id: int, session_type: str, invoice_no: str | None):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO admin_sessions (admin_id, session_type, invoice_no, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(admin_id) DO UPDATE SET
                session_type=excluded.session_type,
                invoice_no=excluded.invoice_no,
                updated_at=CURRENT_TIMESTAMP
            """,
            (admin_id, session_type, invoice_no),
        )
        conn.commit()


def get_admin_session(admin_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def clear_admin_session(admin_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        conn.commit()


def get_orders_pending_packing(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM orders
        WHERE status = ?
        ORDER BY created_at ASC
    """, (STATUS_PACKING_PENDING,))
    return cur.fetchall()

def get_orders_ready_to_ship(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM orders
        WHERE status = ?
        ORDER BY created_at ASC
    """, (STATUS_PACKED,))
    return cur.fetchall()


# =========================
# SHIPPING PROOF HELPERS
# =========================

def set_shipping_proof(invoice_no: str, file_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE orders SET shipping_proof_file_id = ? WHERE invoice_no = ?",
            (file_id, invoice_no),
        )
        conn.commit()


def get_shipping_proof(invoice_no: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT shipping_proof_file_id FROM orders WHERE invoice_no = ?",
            (invoice_no,),
        )
        row = cur.fetchone()
        return row["shipping_proof_file_id"] if row else None


# =========================
# PAYMENT PROOF HELPERS
# =========================

def set_payment_proof(invoice_no: str, file_id: str, proof_type: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE orders SET payment_proof_file_id = ?, payment_proof_type = ? WHERE invoice_no = ?",
            (file_id, proof_type, invoice_no),
        )
        conn.commit()


def get_payment_proof(invoice_no: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT payment_proof_file_id, payment_proof_type FROM orders WHERE invoice_no = ?",
            (invoice_no,),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        return row["payment_proof_file_id"], row["payment_proof_type"]

# =========================
# ORDER STATUS HELPERS
# =========================

def update_order_status(conn, invoice_no: str, new_status: str):
    """
    Single source of truth for updating order status.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid order status: {new_status}")

    conn.execute(
        "UPDATE orders SET status = ? WHERE invoice_no = ?",
        (new_status, invoice_no),
    )
    conn.commit()



def mark_order_packing_pending(conn, invoice_no: str):
    update_order_status(conn, invoice_no, STATUS_PACKING_PENDING)

def mark_order_packed(conn, invoice_no: str):
    update_order_status(conn, invoice_no, STATUS_PACKED)

def mark_order_shipped(conn, invoice_no: str, tracking_number: str, proof_file_id: str | None = None):
    if STATUS_SHIPPED not in VALID_STATUSES:
        raise ValueError("Invalid shipped status")

    conn.execute(
        """
        UPDATE orders
        SET status = ?,
            tracking_number = ?,
            shipping_proof_file_id = ?
        WHERE invoice_no = ?
        """,
        (STATUS_SHIPPED, tracking_number, proof_file_id, invoice_no),
    )
    conn.commit()




