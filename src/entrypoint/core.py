import json
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

import functions_framework
import vertexai
from flask import Request, Response
from google.cloud import firestore
from vertexai import agent_engines

from utils.firestore_utils import (
    check_rate_limit,
    create_or_update_document,
    lookup_session_id,
)
from utils.gcp_logging import attach_gcp_logger
from utils.settings import Settings


def _parse_allowed_origins(settings: Settings) -> list[str]:
    return [
        origin.strip()
        for origin in settings.ALLOWED_ORIGINS.split(",")
        if origin.strip()
    ]


def _build_cors_headers(allowed_origins: list[str], origin: str) -> Dict[str, str]:
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
        "Access-Control-Max-Age": "3600",
    }
    if origin and origin in allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def _get_or_create_engine_session(
    agent: Any,
    db: firestore.Client,
    collection: str,
    session_id: str,
    adk_user_id: str,
    org_id: str,
    settings: Settings,
) -> str:
    engine_session_id = lookup_session_id(
        db=db,
        collection=collection,
        doc_id=session_id,
    )
    logging.info(
        msg={
            "event": "Firestore session lookup",
            "payload": {
                "session_id": session_id,
                "engine_session_id": engine_session_id,
            },
        }
    )

    if engine_session_id:
        return engine_session_id

    session = agent.create_session(  # type: ignore
        user_id=adk_user_id,
        state={
            "temperature": settings.TEMPERATURE,
            "top_p": settings.TOP_P,
            "top_k": settings.TOP_K,
            "max_output_tokens": settings.MAX_OUTPUT_TOKENS,
            "org_id": org_id,
        },
    )
    engine_session_id = session["id"]
    logging.info(
        msg={
            "event": "Created new agent engine session",
            "payload": {"engine_session_id": engine_session_id},
        }
    )
    return engine_session_id


def _persist_session_mapping(
    db: firestore.Client,
    collection: str,
    session_id: str,
    engine_session_id: str,
    org_id: str,
    user_id: str,
    metadata: Dict[str, Any],
) -> None:
    create_or_update_document(
        db=db,
        collection=collection,
        doc_id=session_id,
        payload={
            "engine_session_id": engine_session_id,
            "org_id": org_id,
            "user_id": user_id,
            "metadata": metadata,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "expires_at": datetime.now(tz=timezone.utc) + timedelta(days=7),
        },
    )


def _sanitize_message(message: str, max_length: int) -> str:
    """
    Remove control characters (except whitespace), trim length, and strip.
    This helps avoid control-byte abuse and oversized payloads.
    """
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", message)
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def _is_blocked_prompt(message: str, pattern: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, message) is not None
    except re.error:
        logging.warning(
            msg={"event": "widget2agent_invalid_block_pattern", "payload": pattern}
        )
        return False


@functions_framework.http
def widget2agent(request: Request) -> Response:
    """HTTP endpoint for web chat widget → Vertex AI Agent Engine."""
    settings = Settings()
    allowed_origins = _parse_allowed_origins(settings)
    request_origin = request.headers.get("Origin", "")

    def cors_headers(origin: str) -> Dict[str, str]:
        return _build_cors_headers(allowed_origins, origin)

    def json_response(body: dict, status: int) -> Response:
        return Response(
            response=json.dumps(body),
            status=status,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    _ = attach_gcp_logger(
        version=settings.VERSION,
        environment=settings.ENVIRONMENT,
    )

    try:
        if request_origin and request_origin not in allowed_origins:
            return json_response({"error": "Origin not allowed"}, status=403)

        if request.method == "OPTIONS":
            return Response(
                status=204,
                headers=cors_headers(request_origin),
            )

        if request.method != "POST":
            return json_response({"error": "Only POST is allowed"}, status=405)


        data = request.get_json(silent=True) or {}
        logging.info(
            msg={
                "event": "Widget webhook received.",
                "payload": data,
            }
        )

        message: str = _sanitize_message(
            message=data.get("message") or "",
            max_length=settings.MAX_MESSAGE_LENGTH,
        )
        if _is_blocked_prompt(message, settings.BLOCKED_PROMPT_PATTERNS):
            logging.warning(
                msg={
                    "event": "widget2agent_blocked_prompt",
                    "payload": {"origin": request_origin},
                }
            )
            return json_response({"error": "Request rejected"}, status=400)
        if not message:
            return json_response({"error": "Missing 'message'"}, status=400)

        session_id: str = data.get("session_id") or str(uuid4())
        org_id: str = data.get("org_id") or "default_org"
        user_id: str = data.get("user_id") or "anonymous"
        metadata: Dict[str, Any] = data.get("metadata") or {}
        adk_user_id = session_id.replace("-", "")

        # ---- Firestore: rate limit per session
        db = firestore.Client(
            project=settings.GCP_PROJECT_ID,
            database=settings.FIRESTORE_DATABASE,
        )
        allowed, current_count = check_rate_limit(
            db=db,
            collection=settings.RATE_LIMIT_COLLECTION,
            key=session_id,
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
            max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
        )
        if not allowed:
            logging.warning(
                msg={
                    "event": "widget2agent_rate_limited",
                    "payload": {
                        "session_id": session_id,
                        "count": current_count,
                    },
                }
            )
            return json_response(
                {"error": "Rate limit exceeded. Please try again later."}, status=429
            )

        # ---- Initialize Vertex AI
        vertexai.init(
            project=settings.GCP_PROJECT_ID,
            location=settings.AGENT_ENGINE_REGION,
            staging_bucket=settings.AGENT_ENGINE_BUCKET,
        )

        # ---- Get remote agent engine
        agent = agent_engines.get(resource_name=settings.AGENT_ENGINE_RESOURCE_NUM)

        # ---- Firestore: map frontend session -> Agent Engine session
        engine_session_id = _get_or_create_engine_session(
            agent=agent,
            db=db,
            collection=settings.FIRESTORE_COLLECTION,
            session_id=session_id,
            adk_user_id=adk_user_id,
            org_id=org_id,
            settings=settings,
        )

        _persist_session_mapping(
            db=db,
            collection=settings.FIRESTORE_COLLECTION,
            session_id=session_id,
            engine_session_id=engine_session_id,
            org_id=org_id,
            user_id=user_id,
            metadata=metadata,
        )

        # ---- Query the Agent Engine (non-streaming)
        agent_response = ""
        for event in agent.stream_query(  # pyright: ignore
            user_id=adk_user_id, session_id=engine_session_id, message=message
        ):
            if "content" in event:
                if "parts" in event["content"]:
                    parts = event["content"]["parts"]
                    for part in parts:
                        if "text" in part:
                            agent_response = part["text"]

            logging.info(
                msg={
                    "event": "Got agent answer for widget",
                    "payload": {
                        "answer_preview": agent_response[:120],
                    },
                }
            )

        response_body = {
            "session_id": session_id,
            "agent_session_id": engine_session_id,
            "answer": agent_response,
        }

        return Response(
            response=json.dumps(response_body),
            status=200,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    except Exception as e:
        logging.error(
            msg={
                "event": "widget2agent_error",
                "payload": {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
        return Response(
            response=json.dumps(
                {
                    "error": "Internal Error",
                    "detail": "Unexpected error in widget2agent",
                }
            ),
            status=500,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )
