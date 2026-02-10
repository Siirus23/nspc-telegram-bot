# claims_repo.py
from typing import List, Dict, Optional
from db import get_pool

async def fetch_active_claim_users(channel_id: int) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                user_id,
                COALESCE(username, '') AS username,
                COUNT(*) AS qty,
                MIN(claimed_at) AS earliest
            FROM claims
            WHERE channel_chat_id = $1
              AND status = 'active'
            GROUP BY user_id, username
            ORDER BY earliest ASC
            """,
            channel_id
        )
    return [dict(r) for r in rows]
    
async def fetch_user_claim_groups(
    channel_id: int,
    user_id: int
) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                cl.card_name,
                cl.price AS price_str,
                c.channel_message_id AS post_mid,
                COUNT(*) AS qty,
                MIN(c.claim_order) AS first_order
            FROM claims c
            JOIN card_listing cl
              ON cl.channel_chat_id = c.channel_chat_id
             AND cl.channel_message_id = c.channel_message_id
            WHERE c.channel_chat_id = $1
              AND c.user_id = $2
              AND c.status = 'active'
            GROUP BY cl.card_name, cl.price, c.channel_message_id
            ORDER BY first_order ASC
            """,
            channel_id,
            user_id
        )
    return [dict(r) for r in rows]

async def admin_cancel_claim_group(
    *,
    channel_id: int,
    admin_id: int,
    user_id: int,
    post_mid: int,
    reason: str = "admin_cancel"
) -> Dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():

            # 1️⃣ Count active claims
            qty = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM claims
                WHERE channel_chat_id = $1
                  AND channel_message_id = $2
                  AND user_id = $3
                  AND status = 'active'
                """,
                channel_id, post_mid, user_id
            )

            if not qty or qty <= 0:
                return None

            # 2️⃣ Fetch card listing
            card = await conn.fetchrow(
                """
                SELECT card_name, price, remaining_qty
                FROM card_listing
                WHERE channel_chat_id = $1
                  AND channel_message_id = $2
                FOR UPDATE
                """,
                channel_id, post_mid
            )

            if not card:
                return None

            card_name = card["card_name"]
            price_str = card["price"]
            remaining = int(card["remaining_qty"])

            # 3️⃣ Cancel claims
            await conn.execute(
                """
                UPDATE claims
                SET status = 'cancelled'
                WHERE channel_chat_id = $1
                  AND channel_message_id = $2
                  AND user_id = $3
                  AND status = 'active'
                """,
                channel_id, post_mid, user_id
            )

            # 4️⃣ Restore stock
            await conn.execute(
                """
                UPDATE card_listing
                SET remaining_qty = remaining_qty + $1
                WHERE channel_chat_id = $2
                  AND channel_message_id = $3
                """,
                qty, channel_id, post_mid
            )

            new_remaining = remaining + qty

            # 5️⃣ Admin log
            await conn.execute(
                """
                INSERT INTO admin_logs
                (action_type, admin_id, target_user_id, card_name, channel_message_id, quantity, reason)
                VALUES
                ('cancel_claim', $1, $2, $3, $4, $5, $6)
                """,
                admin_id, user_id, card_name, post_mid, qty, reason
            )

            # 6️⃣ Adjust latest non-shipped order
            ord_row = await conn.fetchrow(
                """
                SELECT id, invoice_no, delivery_fee
                FROM orders
                WHERE user_id = $1
                  AND status IN ('pending_payment', 'payment_received', 'verifying', 'ready_to_ship')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id
            )

            order_cancelled = False
            updated_invoice = None

            if ord_row:
                order_id = ord_row["id"]
                updated_invoice = ord_row["invoice_no"]

                oi = await conn.fetchrow(
                    """
                    SELECT id, qty, price
                    FROM order_items
                    WHERE order_id = $1
                      AND post_message_id = $2
                    """,
                    order_id, post_mid
                )

                if oi:
                    remove_qty = min(qty, int(oi["qty"]))
                    new_qty = int(oi["qty"]) - remove_qty

                    if new_qty <= 0:
                        await conn.execute(
                            "DELETE FROM order_items WHERE id = $1",
                            oi["id"]
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE order_items
                            SET qty = $1
                            WHERE id = $2
                            """,
                            new_qty, oi["id"]
                        )

                    cards_total = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(price * qty), 0)
                        FROM order_items
                        WHERE order_id = $1
                        """,
                        order_id
                    ) or 0

                    delivery_fee = float(ord_row["delivery_fee"] or 0)
                    total = float(cards_total) + delivery_fee

                    if cards_total <= 0:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET status = 'cancelled',
                                cards_total = 0,
                                total = 0
                            WHERE id = $1
                            """,
                            order_id
                        )
                        order_cancelled = True
                    else:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET cards_total = $1,
                                total = $2
                            WHERE id = $3
                            """,
                            cards_total, total, order_id
                        )

            return {
                "card_name": card_name,
                "qty": qty,
                "post_mid": post_mid,
                "new_remaining": new_remaining,
                "invoice_no": updated_invoice,
                "order_cancelled": order_cancelled,
            }

