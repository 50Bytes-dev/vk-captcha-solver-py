import json
import random
from typing import Any, Optional


def safe_json_parse(data: str) -> Optional[Any]:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None


def get_random_number(min_val: int, max_val: int) -> int:
    """Returns a random integer between min and max (inclusive)."""
    return random.randint(min_val, max_val)
