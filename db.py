import asyncpg
import os
import json


from datetime import timedelta
from contextlib import asynccontextmanager


@asynccontextmanager
async def get_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

DATABASE_URL = os.getenv("DATABASE_URL")

_pool = None

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

async def init_db():
    """
    Connects the bot to Supabase.
    Runs once when the bot starts.
    """
    global _pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=3,
            ssl="require",
            timeout=30,
        )

async def get_pool():
    """
    Allows other parts of the bot to use the database.
    """
    if _pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pool

# ===========================
# STALE CLAIMS MGMT
# ===========================

async def get_stale_claims_for_user(user_id: int, hours: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT *
            FROM claims
            WHERE user_id = $1
              AND status = 'active'
              AND claimed_at < (now() - make_interval(hours => $2))
            """,
            user_id,
            hours
        )



async def cancel_all_claims_for_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Restore stock
            rows = await conn.fetch(
                """
                SELECT channel_chat_id, channel_message_id, COUNT(*) AS qty
                FROM claims
                WHERE user_id = $1 AND status = 'active'
                GROUP BY channel_chat_id, channel_message_id
                """,
                user_id
            )

            for r in rows:
                await conn.execute(
                    """
                    UPDATE card_listing
                    SET remaining_qty = remaining_qty + $1
                    WHERE channel_chat_id = $2
                      AND channel_message_id = $3
                    """,
                    r["qty"],
                    r["channel_chat_id"],
                    r["channel_message_id"]
                )

            # Cancel claims
            await conn.execute(
                """
                UPDATE claims
                SET status = 'cancelled'
                WHERE user_id = $1 AND status = 'active'
                """,
                user_id
            )
async def get_user_claims_summary(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                cl.card_name,
                cl.price,
                COUNT(*) AS qty
            FROM claims c
            JOIN card_listing cl
              ON c.channel_chat_id = cl.channel_chat_id
             AND c.channel_message_id = cl.channel_message_id
            WHERE c.user_id = $1
              AND c.status = 'active'
            GROUP BY cl.card_name, cl.price
            ORDER BY cl.card_name
            """,
            user_id
        )





# ===========================
# ORDER QUERY HELPERS
# ===========================

async def get_orders_by_status(status: str):
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid order status: {status}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT *
            FROM orders
            WHERE status = $1
            ORDER BY created_at ASC
            """,
            status
        )

# ===========================
# ORDER STATUS HELPERS
# ===========================

async def update_order_status(invoice_no: str, new_status: str):
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid order status: {new_status}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE orders
            SET status = $1
            WHERE invoice_no = $2
            """,
            new_status,
            invoice_no
        )

async def mark_order_shipped(
    order_id: int,
    tracking: str,
    file_id: str | None = None
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE orders
            SET status = $1,
                tracking_number = $2,
                shipping_proof_file_id = $3
            WHERE id = $4
            """,
            STATUS_SHIPPED,
            tracking,
            file_id,
            order_id
        )

# ===========================
# ORDER STATE SHORTCUTS
# ===========================

async def mark_order_packing_pending(invoice_no: str):
    await update_order_status(invoice_no, STATUS_PACKING_PENDING)


async def mark_order_packed(invoice_no: str):
    await update_order_status(invoice_no, STATUS_PACKED)

# ===========================
# SHIPPING SESSIONS
# ===========================

async def create_shipping_session(admin_id: int, order_id: int):
    """
    Starts a shipping session for an order.
    Called when admin clicks 'Mark as Shipped'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO shipping_sessions (admin_id, order_id, step)
            VALUES ($1, $2, 'awaiting_photo')
            """,
            admin_id,
            order_id
        )


async def get_active_shipping_session(order_id: int):
    """
    Gets the current (not completed) shipping session for an order.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM shipping_sessions
            WHERE order_id = $1
              AND step != 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            order_id
        )


async def update_shipping_session(
    order_id: int,
    *,
    step: str | None = None,
    photo_file_id: str | None = None,
    detected_tracking: str | None = None
):
    """
    Updates the shipping session step or stored data.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        fields = []
        values = []
        idx = 1

        if step is not None:
            fields.append(f"step = ${idx}")
            values.append(step)
            idx += 1

        if photo_file_id is not None:
            fields.append(f"photo_file_id = ${idx}")
            values.append(photo_file_id)
            idx += 1

        if detected_tracking is not None:
            fields.append(f"detected_tracking = ${idx}")
            values.append(detected_tracking)
            idx += 1

        if not fields:
            return

        values.append(order_id)

        await conn.execute(
            f"""
            UPDATE shipping_sessions
            SET {', '.join(fields)}, updated_at = now()
            WHERE order_id = ${idx}
            """,
            *values
        )


async def complete_shipping_session(order_id: int):
    """
    Marks a shipping session as completed.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE shipping_sessions
            SET step = 'completed', updated_at = now()
            WHERE order_id = $1
            """,
            order_id
        )

async def get_active_shipping_session_by_admin(admin_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT *
            FROM shipping_sessions
            WHERE admin_id = $1
              AND step = 'awaiting_photo'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            admin_id
        )

# ===========================
# BUYER PANEL HELPERS
# ===========================

async def get_orders_by_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                invoice_no,
                status,
                tracking_number,
                created_at
            FROM orders
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id
        )
async def get_latest_order_by_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                invoice_no,
                status,
                tracking_number,
                shipping_proof_file_id
            FROM orders
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id
        )

async def get_active_claims_by_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT
                cl.card_name,
                cl.price,
                c.channel_message_id
            FROM claims c
            JOIN card_listing cl
              ON c.channel_chat_id = cl.channel_chat_id
             AND c.channel_message_id = cl.channel_message_id
            WHERE c.user_id = $1
              AND c.status = 'active'
            ORDER BY c.claim_order ASC
            """,
            user_id
        )

# ===========================
# BOT SESSION HELPERS
# ===========================

async def set_session(
    user_id: int,
    role: str,
    session_type: str,
    data: dict | None = None,
):
    """
    Create or replace a session for a user.
    One active session per user (enforced by PRIMARY KEY).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_sessions (user_id, role, session_type, data, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                role = EXCLUDED.role,
                session_type = EXCLUDED.session_type,
                data = EXCLUDED.data,
                updated_at = NOW()
            """,
            user_id,
            role,
            session_type,
            json.dumps(data or {}),
        )


async def get_session(user_id: int):
    """
    Fetch the active session for a user.
    Returns None if no session exists.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT user_id, role, session_type, data
            FROM bot_sessions
            WHERE user_id = $1
            """,
            user_id,
        )


async def clear_session(user_id: int):
    """
    Clear the active session for a user.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM bot_sessions WHERE user_id = $1",
            user_id,
        )
# ===========================
# ADMIN CSV SESSIONS
# ===========================

async def start_csv_photo_session(admin_id: int):
    await set_session(
        user_id=admin_id,
        role="admin",
        session_type="awaiting_card_photos",
        data={},
    )


async def get_csv_photo_session(admin_id: int):
    sess = await get_session(admin_id)
    if not sess:
        return None
    if sess["role"] != "admin":
        return None
    if sess["session_type"] != "awaiting_card_photos":
        return None
    return sess


# ===========================
# ADMIN CSV / CARD LISTING HELPERS
# ===========================

async def clear_card_listings():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM card_listing")


async def insert_card_listing(
    card_name: str,
    price: str,
    qty: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO card_listing (
                channel_chat_id,
                channel_message_id,
                card_name,
                price,
                initial_qty,
                remaining_qty
            )
            VALUES (0, 0, $1, $2, $3, $3)
            """,
            card_name,
            price,
            qty,
        )


async def get_next_unposted_card():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, card_name, price, remaining_qty
            FROM card_listing
            WHERE channel_message_id = 0
            ORDER BY id ASC
            LIMIT 1
            """
        )


async def mark_card_posted(
    card_id: int,
    channel_chat_id: int,
    channel_message_id: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE card_listing
            SET channel_chat_id = $1,
                channel_message_id = $2
            WHERE id = $3
            """,
            channel_chat_id,
            channel_message_id,
            card_id,
        )


async def count_unposted_cards() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS c
            FROM card_listing
            WHERE channel_message_id = 0
            """
        )
        return int(row["c"] or 0)

# ===========================
# CLAIMS — CARD LOOKUP
# ===========================

async def get_card_by_post(channel_chat_id: int, channel_message_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, card_name, price, remaining_qty
            FROM card_listing
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
            """,
            channel_chat_id,
            channel_message_id,
        )

# ===========================
# CLAIMS — COUNT HELPERS
# ===========================

async def count_active_claims_for_card(
    channel_chat_id: int,
    channel_message_id: int,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS c
            FROM claims
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
              AND status = 'active'
            """,
            channel_chat_id,
            channel_message_id,
        )
        return int(row["c"] or 0)


async def user_has_active_claim(
    channel_chat_id: int,
    channel_message_id: int,
    user_id: int,
) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM claims
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
              AND user_id = $3
              AND status = 'active'
            LIMIT 1
            """,
            channel_chat_id,
            channel_message_id,
            user_id,
        )
        return row is not None

# ===========================
# CLAIMS — CREATE / REVIVE
# ===========================

async def get_latest_cancelled_claim_id(
    channel_chat_id: int,
    channel_message_id: int,
    user_id: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id
            FROM claims
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
              AND user_id = $3
              AND status = 'cancelled'
            ORDER BY id DESC
            LIMIT 1
            """,
            channel_chat_id,
            channel_message_id,
            user_id,
        )
        return row["id"] if row else None


async def revive_cancelled_claim(
    claim_id: int,
    username: str | None,
    claim_order: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE claims
            SET status = 'active',
                username = $1,
                claim_order = $2,
                claimed_at = NOW()
            WHERE id = $3
            """,
            username,
            claim_order,
            claim_id,
        )


async def create_claim(
    channel_chat_id: int,
    channel_message_id: int,
    user_id: int,
    username: str | None,
    claim_order: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO claims (
                channel_chat_id,
                channel_message_id,
                user_id,
                username,
                claim_order
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            channel_chat_id,
            channel_message_id,
            user_id,
            username,
            claim_order,
        )

# ===========================
# CLAIMS — CANCEL
# ===========================

async def get_active_claims_for_user(
    channel_chat_id: int,
    channel_message_id: int,
    user_id: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, claimed_at
            FROM claims
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
              AND user_id = $3
              AND status = 'active'
            """,
            channel_chat_id,
            channel_message_id,
            user_id,
        )


async def cancel_claims_for_user(
    channel_chat_id: int,
    channel_message_id: int,
    user_id: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE claims
            SET status = 'cancelled'
            WHERE channel_chat_id = $1
              AND channel_message_id = $2
              AND user_id = $3
              AND status = 'active'
            """,
            channel_chat_id,
            channel_message_id,
            user_id,
        )

# ===========================
# CLAIMS — AVAILABILITY
# ===========================

async def update_card_remaining(
    channel_chat_id: int,
    channel_message_id: int,
    delta: int,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE card_listing
            SET remaining_qty = remaining_qty + $1
            WHERE channel_chat_id = $2
              AND channel_message_id = $3
            """,
            delta,
            channel_chat_id,
            channel_message_id,
        )

# ===========================
# CHECKOUT HELPERS
# ===========================

async def get_checkout_by_invoice(invoice_no: str):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT user_id, delivery_method
            FROM checkout
            WHERE invoice_no = $1
            """,
            invoice_no
        )

# =========================
# CHECKOUT SESSION HELPERS
# =========================

async def upsert_checkout(user_id: int, **fields):
    cols = []
    vals = []
    idx = 2

    for k, v in fields.items():
        cols.append(f"{k} = ${idx}")
        vals.append(v)
        idx += 1

    set_clause = ", ".join(cols)

    query = f"""
    insert into checkout (user_id)
    values ($1)
    on conflict (user_id)
    do update set
        {set_clause},
        updated_at = now()
    """

    async with get_db() as conn:
        await conn.execute(query, user_id, *vals)

async def get_checkout(user_id: int):
    async with get_db() as conn:
        row = await conn.fetchrow(
            "select * from checkout where user_id = $1",
            user_id
        )
        return dict(row) if row else None


# =========================
# PAYMENT PROOF
# =========================

async def set_payment_proof(invoice_no: str, file_id: str, file_type: str):
    async with get_db() as conn:
        await conn.execute(
            """
            update checkout
            set
                payment_proof_file_id = $1,
                payment_proof_type = $2,
                updated_at = now()
            where invoice_no = $3
            """,
            file_id,
            file_type,
            invoice_no
        )

