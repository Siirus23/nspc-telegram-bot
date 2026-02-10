# checkout_store.py

from typing import Dict, Any

# In-memory checkout state
# Keyed by Telegram user_id
_CHECKOUT_STORE: Dict[int, Dict[str, Any]] = {}


async def get_checkout(user_id: int) -> Dict[str, Any] | None:
    """
    Retrieve checkout state for a user.
    """
    return _CHECKOUT_STORE.get(user_id)


async def upsert_checkout(user_id: int, **data):
    """
    Create or update checkout state for a user.
    """
    current = _CHECKOUT_STORE.get(user_id, {})
    current.update(data)
    _CHECKOUT_STORE[user_id] = current


async def clear_checkout(user_id: int):
    """
    Remove checkout state (after order completion or cancellation).
    """
    _CHECKOUT_STORE.pop(user_id, None)
