import os


def _require_env_int(name: str) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    try:
        return int(val)
    except ValueError as e:
        raise RuntimeError(
            f"Environment variable {name} must be an integer, got: {val!r}"
        ) from e


def _require_env_str(name: str) -> str:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


BOT_TOKEN = _require_env_str("BOT_TOKEN")
ADMIN_ID = _require_env_int("ADMIN_ID")
CHANNEL_ID = _require_env_int("CHANNEL_ID")

# Optional (only for display / channel links, etc.)
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
