import json
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Tuple
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


@dataclass
class WidgetRequestContext:
    message: str
    session_id: str
    user_id: str
    metadata: Dict[str, Any]
    adk_user_id: str


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
    settings: Settings,
) -> str:
    engine_session_id = lookup_session_id(
        db=db,
        collection=collection,
        doc_id=session_id,
    )
    logging.info(
        msg={
            "event": "firestore_session_lookup",
            "payload": {
                "session_id": session_id,
                "engine_session_id": engine_session_id,
            },
        }
    )

    if engine_session_id:
        return engine_session_id

    session = agent.create_session(  # type: ignore
        user_id=adk_user_id
    )
    engine_session_id = session["id"]
    logging.info(
        msg={
            "event": "agent_engine_session_created",
            "payload": {"engine_session_id": engine_session_id},
        }
    )
    return engine_session_id


def _persist_session_mapping(
    db: firestore.Client,
    collection: str,
    session_id: str,
    engine_session_id: str,
    user_id: str,
    metadata: Dict[str, Any],
) -> None:
    create_or_update_document(
        db=db,
        collection=collection,
        doc_id=session_id,
        payload={
            "engine_session_id": engine_session_id,
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
            msg={
                "event": "widget_prompt_block_pattern_invalid",
                "payload": pattern,
            }
        )
        return False


def _validate_origin_and_method(
    request: Request,
    request_origin: str,
    allowed_origins: list[str],
    cors_headers: Callable[[str], Dict[str, str]],
) -> Response | None:
    if not request_origin:
        return Response(
            response=json.dumps({"error": "Origin required"}),
            status=403,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    if request_origin not in allowed_origins:
        return Response(
            response=json.dumps({"error": "Origin not allowed"}),
            status=403,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    if request.method == "OPTIONS":
        return Response(
            status=204,
            headers=cors_headers(request_origin),
        )

    if request.method != "POST":
        return Response(
            response=json.dumps({"error": "Only POST is allowed"}),
            status=405,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    return None


def _parse_request_payload(
    request: Request,
    settings: Settings,
    request_origin: str,
    cors_headers: Callable[[str], Dict[str, str]],
) -> Tuple[WidgetRequestContext | None, Response | None]:
    data = request.get_json(silent=True) or {}
    logging.info(
        msg={
            "event": "widget_request_received",
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
                "event": "widget_prompt_blocked",
                "payload": {"origin": request_origin},
            }
        )
        return None, Response(
            response=json.dumps({"error": "Request rejected"}),
            status=400,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )
    if not message:
        return None, Response(
            response=json.dumps({"error": "Missing 'message'"}),
            status=400,
            content_type="application/json",
            headers=cors_headers(request_origin),
        )

    session_id: str = data.get("session_id") or str(uuid4())
    context = WidgetRequestContext(
        message=message,
        session_id=session_id,
        user_id=data.get("user_id") or "anonymous",
        metadata=data.get("metadata") or {},
        adk_user_id=session_id.replace("-", ""),
    )
    return context, None


def _enforce_rate_limit(
    db: firestore.Client,
    settings: Settings,
    session_id: str,
    cors_headers: Callable[[str], Dict[str, str]],
    request_origin: str,
) -> Response | None:
    allowed, current_count = check_rate_limit(
        db=db,
        collection=settings.RATE_LIMIT_COLLECTION,
        key=session_id,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
    )
    if allowed:
        return None

    logging.warning(
        msg={
            "event": "widget_rate_limit_exceeded",
            "payload": {
                "session_id": session_id,
                "count": current_count,
            },
        }
    )
    return Response(
        response=json.dumps({"error": "Rate limit exceeded. Please try again later."}),
        status=429,
        content_type="application/json",
        headers=cors_headers(request_origin),
    )


def _extract_agent_answer(
    agent: Any, adk_user_id: str, engine_session_id: str, message: str
) -> str:
    agent_response = ""
    for event in agent.stream_query(  # pyright: ignore
        user_id=adk_user_id, session_id=engine_session_id, message=message
    ):
        content = event.get("content", {})
        for part in content.get("parts", []):
            text = part.get("text")
            if text:
                agent_response = text
    return agent_response


@functions_framework.http
def widget2agent(request: Request) -> Response:
    """HTTP endpoint for web chat widget → Vertex AI Agent Engine."""
    settings = Settings()
    allowed_origins = _parse_allowed_origins(settings)
    request_origin = request.headers.get("Origin", "")

    def cors_headers(origin: str) -> Dict[str, str]:
        return _build_cors_headers(allowed_origins, origin)

    _ = attach_gcp_logger(
        version=settings.VERSION,
        environment=settings.ENVIRONMENT,
    )

    try:
        preflight_response = _validate_origin_and_method(
            request=request,
            request_origin=request_origin,
            allowed_origins=allowed_origins,
            cors_headers=cors_headers,
        )
        if preflight_response:
            return preflight_response

        context, payload_error_response = _parse_request_payload(
            request=request,
            settings=settings,
            request_origin=request_origin,
            cors_headers=cors_headers,
        )
        if payload_error_response:
            return payload_error_response
        assert context  # for type checkers

        # ---- Firestore: rate limit per session
        db = firestore.Client(
            project=settings.GCP_PROJECT_ID,
            database=settings.FIRESTORE_DATABASE,
        )
        rate_limit_response = _enforce_rate_limit(
            db=db,
            settings=settings,
            session_id=context.session_id,
            cors_headers=cors_headers,
            request_origin=request_origin,
        )
        if rate_limit_response:
            return rate_limit_response

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
            session_id=context.session_id,
            adk_user_id=context.adk_user_id,
            settings=settings,
        )

        _persist_session_mapping(
            db=db,
            collection=settings.FIRESTORE_COLLECTION,
            session_id=context.session_id,
            engine_session_id=engine_session_id,
            user_id=context.user_id,
            metadata=context.metadata,
        )

        # ---- Query Agent Engine
        agent_response = _extract_agent_answer(
            agent=agent,
            adk_user_id=context.adk_user_id,
            engine_session_id=engine_session_id,
            message=context.message,
        )

        logging.info(
            msg={
                "event": "widget_agent_answer_received",
                "payload": {
                    "answer_preview": agent_response[:120],
                },
            }
        )

        response_body = {
            "session_id": context.session_id,
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
                "event": "widget_request_failed",
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
