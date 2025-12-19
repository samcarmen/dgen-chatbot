import html
import json
import logging
import re
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Tuple
from uuid import uuid4

import functions_framework
import jwt
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
    adk_user_id: str


def _sanitize_session_id(raw_id: str | None) -> str:
    """Return a safe session ID or generate one if the input is invalid.

    Args:
        raw_id: Client-supplied session identifier.

    Returns:
        A validated session ID consisting of allowed characters, or a UUID4 string.
    """
    if raw_id and len(raw_id) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", raw_id):
        return raw_id
    return str(uuid4())


def _issue_session_token(
    session_id: str,
    user_id: str,
    origin: str,
    signing_key: str,
    ttl_seconds: int,
    issuer: str,
    audience: str,
) -> str:
    """Create a signed JWT for the session.

    Args:
        session_id: Server-issued session identifier.
        user_id: Server-issued user identifier.
        origin: Request origin to bind, if provided.
        signing_key: Secret key for HMAC signing.
        ttl_seconds: Token lifetime in seconds.
        issuer: Token issuer claim.
        audience: Token audience claim.

    Returns:
        A compact JWT string.
    """
    now = datetime.now(tz=timezone.utc)
    payload: Dict[str, Any] = {
        "sid": session_id,
        "uid": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "iss": issuer,
        "aud": audience,
    }
    if origin:
        payload["origin"] = origin
    return jwt.encode(payload, signing_key, algorithm="HS256")


def _validate_session_token(
    token: str, origin: str, signing_key: str, issuer: str, audience: str
) -> tuple[str, str, int] | None:
    """Validate a session JWT and return the embedded session and user IDs.

    Args:
        token: Compact JWT from the client.
        origin: Origin to verify against the token (if present).
        signing_key: Secret key for HMAC verification.
        issuer: Expected issuer claim.
        audience: Expected audience claim.

    Returns:
        Tuple of session ID, user ID, and expiry timestamp if valid; otherwise None.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            options={"require": ["sid", "exp", "iat", "iss", "aud"]},
            issuer=issuer,
            audience=audience,
        )
    except jwt.InvalidTokenError:
        return None

    sid = payload.get("sid")
    uid = payload.get("uid")
    exp = payload.get("exp")
    if not isinstance(sid, str) or not isinstance(uid, str) or not isinstance(exp, int):
        return None
    token_sid = _sanitize_session_id(sid)
    token_uid = _sanitize_session_id(uid)

    token_origin = payload.get("origin")
    if token_origin and token_origin != origin:
        return None

    return token_sid, token_uid, exp


def _resolve_session(
    context: WidgetRequestContext,
    request: Request,
    request_origin: str,
    settings: Settings,
) -> tuple[WidgetRequestContext, str]:
    """Resolve or issue a session and token for the request.

    Args:
        context: Parsed request context from payload.
        request: Incoming HTTP request.
        request_origin: Origin header value.
        settings: Application settings containing signing config.

    Returns:
        Tuple of updated context (with server session and user IDs) and the JWT token.
    """
    token = request.headers.get("X-Session-Token", "")
    token_claims = _validate_session_token(
        token=token,
        origin=request_origin,
        signing_key=settings.SESSION_SIGNING_KEY,
        issuer=settings.SESSION_TOKEN_ISSUER,
        audience=settings.SESSION_TOKEN_AUDIENCE,
    )

    if token_claims:
        token_sid, token_uid, token_exp = token_claims
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        should_refresh = (
            token_exp - now_ts < settings.SESSION_TOKEN_REFRESH_THRESHOLD_SECONDS
        )
        new_token = token
        if should_refresh:
            new_token = _issue_session_token(
                session_id=token_sid,
                user_id=token_uid,
                origin=request_origin,
                signing_key=settings.SESSION_SIGNING_KEY,
                ttl_seconds=settings.SESSION_TOKEN_TTL_SECONDS,
                issuer=settings.SESSION_TOKEN_ISSUER,
                audience=settings.SESSION_TOKEN_AUDIENCE,
            )
        return (
            replace(
                context,
                session_id=token_sid,
                user_id=token_uid,
                adk_user_id=token_sid.replace("-", ""),
            ),
            new_token,
        )

    new_session_id = str(uuid4())
    new_user_id = str(uuid4())
    new_token = _issue_session_token(
        session_id=new_session_id,
        user_id=new_user_id,
        origin=request_origin,
        signing_key=settings.SESSION_SIGNING_KEY,
        ttl_seconds=settings.SESSION_TOKEN_TTL_SECONDS,
        issuer=settings.SESSION_TOKEN_ISSUER,
        audience=settings.SESSION_TOKEN_AUDIENCE,
    )
    updated_context = replace(
        context,
        session_id=new_session_id,
        user_id=new_user_id,
        adk_user_id=new_session_id.replace("-", ""),
    )
    return updated_context, new_token


def _parse_allowed_origins(settings: Settings) -> list[str]:
    """Split and sanitize the allowed origins from settings.

    Args:
        settings: Application settings containing allowed origins string.

    Returns:
        A list of non-empty, stripped origins.
    """
    return [
        origin.strip()
        for origin in settings.ALLOWED_ORIGINS.split(",")
        if origin.strip()
    ]


def _build_cors_headers(allowed_origins: list[str], origin: str) -> Dict[str, str]:
    """Build CORS headers for the request origin.

    Args:
        allowed_origins: List of allowed origins.
        origin: Origin header value from the request.

    Returns:
        A dictionary of CORS headers to attach to the response.
    """
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key, X-Session-Token",
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
    """Fetch or create an agent engine session for a frontend session.

    Args:
        agent: Agent engine client.
        db: Firestore client.
        collection: Firestore collection name.
        session_id: Frontend session identifier.
        adk_user_id: Agent user ID derived from session.
        settings: Application settings.

    Returns:
        The agent engine session ID.
    """
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
) -> None:
    """Persist the mapping of frontend session to engine session in Firestore.

    Args:
        db: Firestore client.
        collection: Firestore collection name.
        session_id: Frontend session identifier.
        engine_session_id: Agent engine session identifier.
        user_id: User identifier.
    """
    create_or_update_document(
        db=db,
        collection=collection,
        doc_id=session_id,
        payload={
            "engine_session_id": engine_session_id,
            "user_id": user_id,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "expires_at": datetime.now(tz=timezone.utc) + timedelta(days=7),
        },
    )


def _sanitize_message(message: str, max_length: int) -> str:
    """Remove control characters, trim length, escape HTML, and strip whitespace.

    Args:
        message: Incoming message text.
        max_length: Maximum allowed length.

    Returns:
        A sanitized message string.
    """
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", message)
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    cleaned = html.escape(cleaned, quote=False)
    return cleaned


def _is_blocked_prompt(message: str, pattern: str) -> bool:
    """Check if a message matches the blocked prompt regex.

    Args:
        message: Message content to evaluate.
        pattern: Regex pattern of blocked prompts.

    Returns:
        True if the message is blocked; otherwise False.
    """
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
    """Validate Origin and HTTP method and return an early response if invalid.

    Args:
        request: Incoming HTTP request.
        request_origin: Origin header value.
        allowed_origins: List of allowed origins.
        cors_headers: Function to build CORS headers.

    Returns:
        A Response if the request is invalid; otherwise None.
    """
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
    """Parse and validate the incoming widget payload.

    Args:
        request: Incoming HTTP request.
        settings: Application settings.
        request_origin: Origin header value.
        cors_headers: Function to build CORS headers.

    Returns:
        Tuple of context (or None) and an error response (or None).
    """
    data = request.get_json(silent=True) or {}
    logging.info(msg={"event": "widget_request_received"})

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

    context = WidgetRequestContext(
        message=message,
        session_id="",
        user_id="",
        adk_user_id="",
    )
    return context, None


def _enforce_rate_limit(
    db: firestore.Client,
    settings: Settings,
    session_id: str,
    cors_headers: Callable[[str], Dict[str, str]],
    request_origin: str,
    request: Request,
) -> Response | None:
    """Enforce per-session + IP rate limiting and return 429 if exceeded.

    Args:
        db: Firestore client.
        settings: Application settings.
        session_id: Session identifier.
        cors_headers: Function to build CORS headers.
        request_origin: Origin header value.
        request: Incoming HTTP request.

    Returns:
        A 429 response if limited; otherwise None.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")
    rate_limit_key = f"{session_id}:{client_ip}"
    allowed, current_count = check_rate_limit(
        db=db,
        collection=settings.RATE_LIMIT_COLLECTION,
        key=rate_limit_key,
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
    """Stream agent response and return the latest text chunk.

    Args:
        agent: Agent engine client.
        adk_user_id: Agent user ID.
        engine_session_id: Agent engine session identifier.
        message: User message to send.

        Returns:
            The latest text content from the streamed response.
    """
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
    """Handle widget POSTs to query the Vertex AI Agent Engine.

    Args:
        request: Flask request containing the widget message and session info.

    Returns:
        A Flask Response with the agent answer, session identifiers, or an error.
    """
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
        if not context:
            logging.error(
                msg={
                    "event": "widget_request_failed",
                    "payload": {"error": "Missing parsed request context"},
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
        context, session_token = _resolve_session(
            context=context,
            request=request,
            request_origin=request_origin,
            settings=settings,
        )

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
            request=request,
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
        )

        # ---- Query Agent Engine
        agent_response = _extract_agent_answer(
            agent=agent,
            adk_user_id=context.adk_user_id,
            engine_session_id=engine_session_id,
            message=context.message,
        )

        logging.info(msg={"event": "widget_agent_answer_received"})

        response_body = {
            "session_id": context.session_id,
            "session_token": session_token,
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
        error_payload = {"error": str(e)}
        if settings.ENVIRONMENT.lower() == "dev":
            error_payload["traceback"] = traceback.format_exc()

        logging.error(
            msg={
                "event": "widget_request_failed",
                "payload": error_payload,
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
