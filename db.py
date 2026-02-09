import asyncpg
import os
import json




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
            Json(data or {}),
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

