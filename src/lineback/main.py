import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .handlers import process_tool_call
from .notify import Notifier, build_notifier, dispatch_notifications, send_unsent
from .vapi import extract_tool_calls, is_authorized, parse_call_context, tool_result

load_dotenv()
if not logging.getLogger().handlers:  # uvicorn leaves the root logger unconfigured
    logging.basicConfig(
        level=os.getenv("LINEBACK_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
logger = logging.getLogger("lineback")


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    notifier = notifier or build_notifier(settings)
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        if not settings.webhook_secret and not settings.allow_insecure:
            logger.error(
                "LINEBACK_WEBHOOK_SECRET is not set: /webhook will answer 503 to every request. "
                "Set a secret (and add the same Bearer credential in Vapi), or set "
                "LINEBACK_ALLOW_INSECURE=1 for local development only."
            )
        elif not settings.webhook_secret:
            logger.warning("Running WITHOUT webhook authentication (LINEBACK_ALLOW_INSECURE=1).")
        if not notifier.delivers:
            logger.warning(
                "Owner notifications are NOT configured (set LINEBACK_SMTP_* and LINEBACK_OWNER_EMAIL). "
                "Booking requests are saved to the database but nobody is told about them."
            )
        retry = asyncio.create_task(
            asyncio.to_thread(send_unsent, db, notifier, settings.business_name)
        )
        try:
            yield
        finally:
            try:
                await retry
            except Exception:
                logger.exception("Startup notification retry failed")

    app = FastAPI(title="lineback", lifespan=lifespan)

    @app.get("/health")
    def health():
        try:
            with db.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    @app.post("/webhook")
    async def vapi_webhook(request: Request, background: BackgroundTasks):
        if settings.webhook_secret:
            if not is_authorized(request.headers, settings.webhook_secret):
                raise HTTPException(
                    status_code=401,
                    detail="unauthorized",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        elif not settings.allow_insecure:
            raise HTTPException(status_code=503, detail="webhook secret not configured")

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")
        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="body is not valid JSON")
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="expected a Vapi message envelope")

        message_type = message.get("type")
        if message_type == "tool-calls":
            ctx = parse_call_context(message)
            results = []
            booking_ids: list[int] = []
            for call in extract_tool_calls(message):
                outcome = await run_in_threadpool(process_tool_call, db, ctx, call)
                results.append(tool_result(call, outcome.result))
                if outcome.booking_id is not None:
                    booking_ids.append(outcome.booking_id)
                logger.info(
                    "tool=%s tool_call_id=%s call_id=%s replayed=%s",
                    call.name, call.id, ctx.call_id, outcome.replayed,
                )
            if booking_ids:
                background.add_task(
                    dispatch_notifications, db, notifier, settings.business_name, booking_ids
                )
            return {"results": results}

        if message_type == "end-of-call-report":
            logger.info(
                "end-of-call-report call_id=%s duration=%s",
                parse_call_context(message).call_id, message.get("durationSeconds"),
            )
        return {"received": True}

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        logger.exception("Unhandled error in webhook")
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


app = create_app()
