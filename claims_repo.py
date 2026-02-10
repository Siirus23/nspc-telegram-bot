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

