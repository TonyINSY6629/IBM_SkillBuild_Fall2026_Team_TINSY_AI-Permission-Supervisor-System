"""
supervisor/app.py — AI Permission Supervisor Service

Local FastAPI process. The Hook Client (hook_client/main.py) calls this.
The Hook Client never touches policy files — all policy loading lives here.

Endpoints:
  GET  /health   → 200 {"status": "ok"}
  POST /decide   → {"action": "allow"|"deny"|"escalate_denied", "reason": "..."}

Policy sync:
  - Loads policy.yaml + taxonomy.yaml from the local policy/ directory at startup.
  - Background thread refreshes them every POLICY_REFRESH_SECONDS seconds.
  - Falls back to the cached copy if the refresh fails.

COS audit sync:
  - Background daemon uploads unsynced audit entries to ais-audit-log COS bucket.
  - Requires COS_API_KEY + COS_INSTANCE_ID environment variables.
  - If not set, sync is skipped silently (local audit log still works).

Run with:
  python -m uvicorn supervisor.app:app --host 127.0.0.1 --port 9001

Or directly:
  python supervisor/app.py
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

# Load .env from the project root if present (never required — env vars can
# also be set in the shell). Must happen before any os.environ.get() calls.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed — env vars must be set in the shell

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from supervisor.audit import AuditLog, audit_log as _default_audit_log
from supervisor.context_store import context_store as _context_store
from supervisor.cos_sync import start_cos_sync
from supervisor.escalation import EscalationManager, escalation_router, set_manager
from supervisor.normalizer import NormalizerError, normalize
from supervisor.policy_engine import PolicyEngine, PolicyEngineError
from supervisor.rule_engine import RuleEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("SUPERVISOR_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("SUPERVISOR_PORT", "9001"))

# Path to the local policy directory (relative to project root or overridden).
_PROJECT_ROOT = Path(__file__).parent.parent
POLICY_DIR: Path = Path(os.environ.get("POLICY_DIR", str(_PROJECT_ROOT / "policy")))

POLICY_REFRESH_SECONDS: float = float(
    os.environ.get("POLICY_REFRESH_SECONDS", "300")  # 5 minutes
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] supervisor: %(message)s",
)
log = logging.getLogger("supervisor")

# ---------------------------------------------------------------------------
# Policy store — loaded at startup, refreshed in background
# ---------------------------------------------------------------------------

class _PolicyStore:
    """
    Thread-safe holder for the active policy and taxonomy dicts.
    Reads are lock-free (Python dict assignment is atomic for GIL purposes);
    writes acquire the lock to prevent partial updates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.policy: dict[str, Any] = {}
        self.taxonomy: dict[str, Any] = {}
        self.policy_version: str = "unknown"
        self.rule_engine: RuleEngine | None = None

    def load(self) -> None:
        """Load (or reload) policy.yaml and taxonomy.yaml from POLICY_DIR."""
        policy_path = POLICY_DIR / "policy.yaml"
        taxonomy_path = POLICY_DIR / "taxonomy.yaml"

        try:
            with policy_path.open("r", encoding="utf-8") as f:
                new_policy = yaml.safe_load(f) or {}
            with taxonomy_path.open("r", encoding="utf-8") as f:
                new_taxonomy = yaml.safe_load(f) or {}

            version = new_policy.get("meta", {}).get("version_tag", "unknown")
            new_engine = RuleEngine(new_policy)

            with self._lock:
                self.policy = new_policy
                self.taxonomy = new_taxonomy
                self.policy_version = version
                self.rule_engine = new_engine

            log.info("Policy loaded: version=%s", version)

        except FileNotFoundError as exc:
            log.error("Policy file not found — keeping cached copy: %s", exc)
        except yaml.YAMLError as exc:
            log.error("Policy YAML parse error — keeping cached copy: %s", exc)
        except Exception as exc:
            log.error("Policy load failed — keeping cached copy: %s", exc)

    def start_background_refresh(self) -> None:
        """Start a daemon thread that reloads policy every POLICY_REFRESH_SECONDS."""
        def _loop() -> None:
            while True:
                time.sleep(POLICY_REFRESH_SECONDS)
                log.info("Background policy refresh triggered.")
                self.load()

        thread = threading.Thread(target=_loop, daemon=True, name="policy-refresh")
        thread.start()
        log.info(
            "Policy background refresh started (interval=%ss).",
            POLICY_REFRESH_SECONDS,
        )


policy_store = _PolicyStore()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="AI Permission Supervisor", version="0.1.0")
app.include_router(escalation_router)

# Let the React pop-up (local dev server or published Lovable URL) call the
# /escalation endpoints. Extra origins: ESCALATION_POPUP_ORIGINS="https://a,https://b".
# NEW: Bug 9 - no longer trusts EVERY *.lovable.app site (anyone's Lovable app).
# Team decision (Nov 13): the pop-up runs locally only (npm run dev), so any
# localhost / 127.0.0.1 port is allowed below; nothing on the internet is.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

_popup_origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    *[o.strip() for o in os.environ.get("ESCALATION_POPUP_ORIGINS", "").split(",") if o.strip()],
]
import inspect as _inspect  # noqa: E402

_pna = (
    {"allow_private_network": True}
    if "allow_private_network" in _inspect.signature(CORSMiddleware.__init__).parameters
    else {}
)
app.add_middleware(
    CORSMiddleware,
    **_pna,
    allow_origins=_popup_origins,
    # NEW: Bug 9 follow-up - accept the local pop-up on any port (npm run dev may use 8081, 5173, ...)
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def _private_network_access(request, call_next):
    # Chrome asks a public https page for permission before it may call 127.0.0.1.
    response = await call_next(request)
    if request.headers.get("access-control-request-private-network"):
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

# Module-level singletons — created after startup loads the policy.
_policy_engine: PolicyEngine | None = None
_escalation_manager: EscalationManager | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _policy_engine, _escalation_manager

    # Escalation Manager
    base_url = f"http://{HOST}:{PORT}"
    _escalation_manager = EscalationManager(base_url=base_url)
    set_manager(_escalation_manager)
    log.info("EscalationManager ready (timeout=%ss).", _escalation_manager._timeout)

    # COS audit sync (background daemon — starts only if credentials are set)
    start_cos_sync()

    # Policy + Rule Engine
    policy_store.load()
    policy_store.start_background_refresh()
    if policy_store.rule_engine:
        _policy_engine = PolicyEngine(
            policy_store.rule_engine,
            _default_audit_log,
            _escalation_manager,
        )
        log.info("PolicyEngine ready.")
    else:
        log.error("Policy failed to load at startup — /decide will deny all calls.")
    log.info("Supervisor service started on %s:%s", HOST, PORT)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    """Liveness check. Returns 200 while the service is running."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "policy_version": policy_store.policy_version,
        },
    )


# ---------------------------------------------------------------------------
# POST /decide
# ---------------------------------------------------------------------------

@app.post("/decide")
async def decide(request: Request) -> JSONResponse:
    """
    Receive a PreToolUse hook payload from the Hook Client and return a verdict.

    Response schema:
      {
        "action":  "allow" | "deny" | "escalate_denied",
        "reason":  "<string>",
        "policy_version": "<string>"
      }
    """
    # --- Parse body ---
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("Could not parse request body: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "action": "deny",
                "reason": "Supervisor could not parse request body.",
                "policy_version": policy_store.policy_version,
            },
        )

    # --- Handle non-PreToolUse hook events ---
    hook_event = payload.get("hook_event_name", "")

    if hook_event == "UserPromptSubmit":
        # Record the prompt in the Context Store for later lookup.
        session_id = payload.get("session_id", "")
        prompt_text = payload.get("prompt", "") or payload.get("message", "")
        if session_id and prompt_text:
            _context_store.record_prompt(session_id=session_id, prompt_text=prompt_text)
            log.info("context_store: recorded prompt for session=%s", session_id)
        return JSONResponse(status_code=200, content={"action": "allow", "reason": ""})

    if hook_event == "Stop":
        session_id = payload.get("session_id", "")
        if session_id:
            _context_store.clear_session(session_id)
        return JSONResponse(status_code=200, content={"action": "allow", "reason": ""})

    if hook_event not in ("PreToolUse", ""):
        # SessionStart, PostToolUse — nothing to do, just allow.
        return JSONResponse(status_code=200, content={"action": "allow", "reason": ""})



    # --- Fail-closed: no policy engine means no decisions ---
    # Refresh the engine in case policy was reloaded since startup.

    
    global _policy_engine  # NEW: Bug 12 - lets us save the rebuilt engine below
    engine = _policy_engine
    if policy_store.rule_engine and (
        engine is None
        or engine._rule_engine is not policy_store.rule_engine
    ):
        engine = PolicyEngine(
            policy_store.rule_engine, _default_audit_log, _escalation_manager
        )
        # NEW: Bug 12 - save it so the next requests reuse it instead of rebuilding every time
        _policy_engine = engine

    if engine is None:
        log.error("PolicyEngine not available — denying tool call.")
        return JSONResponse(
            status_code=200,
            content={
                "action": "deny",
                "reason": "Supervisor policy not loaded — tool call denied.",
                "policy_version": "unknown",
            },
        )

    # --- Normalise payload → PermissionRequest ---
    # workspace_roots: use cwd from payload as the workspace root.
    # Context Store: attach prompt context for the current session.
    cwd = payload.get("cwd", "")
    workspace_roots = [cwd] if cwd else []
    session_id = payload.get("session_id", "")
    prompt_ctx = _context_store.get_prompt(session_id) if session_id else None

    try:
        req = normalize(
            payload,
            workspace_roots=workspace_roots,
            prompt_id=prompt_ctx.prompt_id if prompt_ctx else None,
            prompt_text=prompt_ctx.prompt_text if prompt_ctx else None,
        )
    except NormalizerError as exc:
        log.warning("Normalizer failed — denying: %s", exc)
        return JSONResponse(
            status_code=200,
            content={
                "action": "deny",
                "reason": f"Supervisor could not normalise request: {exc}",
                "policy_version": policy_store.policy_version,
            },
        )

    # --- PolicyEngine: rule eval + audit write + verdict ---
    # engine.decide() blocks up to ESCALATION_TIMEOUT_SECONDS waiting on
    # EscalationManager.request(). Run in a thread pool so the event loop
    # stays free to serve the human's /escalation/{id}/allow or /deny POST.
    try:
        result = await run_in_threadpool(engine.decide, req)
    except PolicyEngineError as exc:
        log.error("PolicyEngine error — denying: %s", exc)
        return JSONResponse(
            status_code=200,
            content={
                "action": "deny",
                "reason": f"Supervisor internal error — tool call denied: {exc}",
                "policy_version": policy_store.policy_version,
            },
        )

    log.info(
        "decide: tool=%s action=%s policy_version=%s",
        payload.get("tool_name", "?"),
        result["action"],
        result["policy_version"],
    )
    return JSONResponse(status_code=200, content=result)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "supervisor.app:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
