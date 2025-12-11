from google.cloud import firestore
from datetime import datetime, timezone, timedelta


def lookup_session_id(
    db: firestore.Client,
    collection: str,
    doc_id: str,
    field: str = "engine_session_id",
) -> str | None:
    """
    Look up and return a field value (default: 'engine_session_id') from a Firestore document.

    Args:
        db (google.cloud.firestore.Client):
            A Firestore client instance used to connect to the Firestore database.
        collection (str):
            The name of the Firestore collection to query.
        doc_id (str):
            The document ID within the collection to retrieve.
        field (str, optional):
            The field key to extract from the document. Defaults to "engine_session_id".

    Returns:
        str | None:
            The value of the specified field if the document exists and contains it,
            otherwise None.
    """
    snap = db.collection(collection).document(doc_id).get()
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get(field)


def create_or_update_document(
    db: firestore.Client,
    collection: str,
    doc_id: str,
    payload: dict,
) -> None:
    """
    Creates a new Firestore document or updates an existing one.

    If the document with the given doc_id does not exist, it will be created.
    If it exists, only the fields in `payload` will be updated (other fields
    in the document remain unchanged, since merge=True is used).

    Args:
        db (firestore.Client): The Firestore client instance.
        collection (str): Name of the Firestore collection.
        doc_id (str): ID of the document to create or update.
        payload (dict): Fields and values to set in the document.

    Returns:
        None
    """
    db.collection(collection).document(doc_id).set(payload, merge=True)


def check_rate_limit(
    db: firestore.Client,
    collection: str,
    key: str,
    window_seconds: int,
    max_requests: int,
) -> tuple[bool, int]:
    """
    Increment and evaluate a rate limit bucket for the given key.

    Returns (allowed, current_count).
    """
    ref = db.collection(collection).document(key)

    @firestore.transactional
    def _tx(transaction: firestore.Transaction) -> tuple[bool, int]:
        snapshot = ref.get(transaction=transaction)
        now = datetime.now(timezone.utc)
        count = 0
        window_start = now

        if snapshot.exists:
            data = snapshot.to_dict() or {}
            window_start = data.get("window_start", now)
            count = data.get("count", 0)

            # Reset window if expired
            if isinstance(window_start, datetime):
                if now - window_start > timedelta(seconds=window_seconds):
                    window_start = now
                    count = 0
            else:
                window_start = now
                count = 0

        count += 1
        allowed = count <= max_requests

        transaction.set(
            ref,
            {
                "count": count,
                "window_start": window_start,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return allowed, count

    transaction = db.transaction()
    return _tx(transaction)
