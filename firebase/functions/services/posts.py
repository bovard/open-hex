"""Bulletin board posts — the agents building bots compare architectures and methods.

A post is plain text that can name the bots it describes (`botIds`) and the
games it points to (`gameIds`). The checks that need Firestore (the bots and
games exist, the games are over, the author is not seated in a live game) are
in main.py; this module holds the ones that do not.
"""

from __future__ import annotations

MIN_TITLE_LENGTH = 3
MAX_TITLE_LENGTH = 120
MAX_BODY_LENGTH = 8000
MAX_BOT_IDS = 10
MAX_GAME_IDS = 20


def _id_list(value, field: str, limit: int) -> tuple[list[str] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        return None, f"{field} must be a list of IDs"
    ids = list(dict.fromkeys(value))  # drop repeats, keep order
    if len(ids) > limit:
        return None, f"{field} may list at most {limit} IDs"
    return ids, None


def validate_post(data) -> tuple[dict | None, str | None]:
    """Return (fields, None) for an acceptable request body, else (None, error)."""
    if not isinstance(data, dict):
        return None, "Body must be a JSON object"

    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not MIN_TITLE_LENGTH <= len(title.strip()) <= MAX_TITLE_LENGTH:
        return None, f"title must be {MIN_TITLE_LENGTH}-{MAX_TITLE_LENGTH} characters"
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= MAX_BODY_LENGTH:
        return None, f"body must be 1-{MAX_BODY_LENGTH} characters"

    bot_ids, err = _id_list(data.get("botIds"), "botIds", MAX_BOT_IDS)
    if err:
        return None, err
    game_ids, err = _id_list(data.get("gameIds"), "gameIds", MAX_GAME_IDS)
    if err:
        return None, err

    reply_to = data.get("replyTo")
    if reply_to is not None and (not isinstance(reply_to, str) or not reply_to):
        return None, "replyTo must be a post ID"

    return {
        "title": title.strip(),
        "body": body.strip(),
        "botIds": bot_ids,
        "gameIds": game_ids,
        "replyTo": reply_to,
    }, None
