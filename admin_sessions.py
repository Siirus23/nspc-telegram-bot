# admin_sessions.py

from typing import Dict, Optional

# In-memory admin session store
# Keyed by admin_id
_ADMIN_SESSIONS: Dict[int, Dict[str, Optional[str]]] = {}


def set_admin_session(admin_id: int, session_type: str, invoice_no: Optional[str]):
    _ADMIN_SESSIONS[admin_id] = {
        "session_type": session_type,
        "invoice_no": invoice_no,
    }


def get_admin_session(admin_id: int) -> Optional[Dict[str, Optional[str]]]:
    return _ADMIN_SESSIONS.get(admin_id)


def clear_admin_session(admin_id: int):
    _ADMIN_SESSIONS.pop(admin_id, None)
