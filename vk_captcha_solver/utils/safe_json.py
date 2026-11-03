import json
from typing import Any, Optional


def safe_json_parse(data: str) -> Optional[Any]:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
