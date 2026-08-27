"""
main.py
FastAPI Server Entrypoint for Ghost Hunter.
Initializes endpoints and orchestrates the services for incoming webhooks.
"""

import logging
import sqlite3
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.schemas import WebhookPayload
from app.config import settings
from app.services.github_service import GitHubService
from app.services.llm_service import LLMService, FatalLLMException
from app.services.sandbox_service import SandboxService, FatalSandboxException
from app.services.db_service import DBService
from app.services.config_service import ConfigService
from app.services.hook_service import HookService
from app.services.audit_service import AuditService

from collections import defaultdict
import time
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Very generous Rate Limiting state (e.g. 150 requests per minute)
RATE_LIMIT_DB = defaultdict(list)
MAX_REQUESTS = 150
TIME_WINDOW = 60 # seconds



DB_LOCK = threading.Lock()
DEDUP_LOCK = threading.Lock()
DB_PATH = Path.home() / ".ghost" / "ghost.db"

def run_db_with_retry(func, *args, **kwargs):
    retries = 5
    delay = 0.05
    for attempt in range(retries):
        conn = None
        try:
            with DB_LOCK:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(DB_PATH, timeout=30.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_webhooks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        delivery_id TEXT UNIQUE
                    )
                """)
                res = func(conn, *args, **kwargs)
                return res
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            logger.warning(f"Database operation failed on attempt {attempt+1}: {e}")
            raise
        except Exception as e:
            logger.error(f"Database error: {e}", exc_info=True)
            raise
        finally:
            if conn:
                conn.close()

def init_dedup_db():
    run_db_with_retry(lambda conn: None)

class SQLiteWebhookSet:
    def __init__(self):
        init_dedup_db()

    def __len__(self) -> int:
        def _get_len(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM processed_webhooks")
            res = cursor.fetchone()
            return res[0] if res else 0
        return run_db_with_retry(_get_len)

    def __contains__(self, item: str) -> bool:
        if not item:
            return False
        def _contains(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_webhooks WHERE delivery_id = ?", (item,))
            return cursor.fetchone() is not None
        return run_db_with_retry(_contains)

    def add(self, item: str):
        if not item:
            return
        def _add(conn):
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO processed_webhooks (delivery_id) VALUES (?)", (item,))
            conn.commit()
        run_db_with_retry(_add)

    def discard(self, item: str):
        if not item:
            return
        def _discard(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_webhooks WHERE delivery_id = ?", (item,))
            conn.commit()
        run_db_with_retry(_discard)

    def clear(self):
        def _clear(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_webhooks")
            conn.commit()
        run_db_with_retry(_clear)


class SQLiteWebhookList:
    def __init__(self):
        init_dedup_db()

    def append(self, item: str):
        if not item:
            return
        def _append(conn):
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO processed_webhooks (delivery_id) VALUES (?)", (item,))
            conn.commit()
        run_db_with_retry(_append)

    def __len__(self) -> int:
        def _get_len(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM processed_webhooks")
            res = cursor.fetchone()
            return res[0] if res else 0
        return run_db_with_retry(_get_len)

    def pop(self, index: int = 0):
        def _pop(conn):
            cursor = conn.cursor()
            if index == 0:
                cursor.execute("SELECT id, delivery_id FROM processed_webhooks ORDER BY id ASC LIMIT 1")
            else:
                cursor.execute("SELECT id, delivery_id FROM processed_webhooks ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                row_id, delivery_id = row
                cursor.execute("DELETE FROM processed_webhooks WHERE id = ?", (row_id,))
                conn.commit()
                return delivery_id
            return None
        return run_db_with_retry(_pop)

    def clear(self):
        def _clear(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_webhooks")
            conn.commit()
        run_db_with_retry(_clear)

# Thread-safe and process-safe webhook deduplication state
PROCESSED_WEBHOOK_IDS = SQLiteWebhookSet()
PROCESSED_WEBHOOK_IDS_FIFO = SQLiteWebhookList()

app = FastAPI(title="Ghost Hunter Webhook Receiver")

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Only rate limit the webhook endpoint
    if request.url.path == "/webhook":
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        
        # Clean up old entries
        RATE_LIMIT_DB[client_ip] = [t for t in RATE_LIMIT_DB[client_ip] if now - t < TIME_WINDOW]
        
        if len(RATE_LIMIT_DB[client_ip]) >= MAX_REQUESTS:
            logger.warning(f"Rate limit exceeded for IP {client_ip}")
            return JSONResponse(status_code=429, content={"detail": "Too Many Requests. Please slow down."})
            
        RATE_LIMIT_DB[client_ip].append(now)
        
    return await call_next(request)


def get_github_token() -> str:
    if settings.GITHUB_PRIVATE_KEY:
        return settings.get_clean_private_key()
    return "github_pat_placeholder_dummy_token_for_testing_purposes"


async def process_issue(payload: WebhookPayload):
    """
    Background task that orchestrates the entire workflow:
    LLM Parsing -> Sandbox Reproduction -> GitHub Commenting
    Now features an Agentic Retry Loop!
    """
    issue = payload.issue
    repo = payload.repository

    issue_title = issue.get("title", "")
    issue_body = issue.get("body", "")
    issue_number = issue.get("number")
    repo_full_name = repo.get("full_name")

    if not isinstance(repo_full_name, str) or not isinstance(issue_number, int):
        return

    logger.info(f"Processing issue #{issue_number} in {repo_full_name}")

    gh = GitHubService()
    token = get_github_token()
    
    try:
        # Fetch and parse repo-level configuration
        yaml_content = await gh.get_repo_file(repo_full_name, "ghost.yml", token)
        repo_config = ConfigService.parse_ghost_yml(yaml_content)
        
        conversation_history = await gh.get_issue_comments(repo_full_name, issue_number, token)
        
        sandbox = SandboxService()
        
        max_retries = repo_config.get("max_retries", 3)
        previous_attempts = ""
        sandbox_result = {"logs": "No execution occurred", "expected_found": False, "bisect_result": ""}
        
        HookService.trigger_pre_run(repo_full_name, issue_number)
        
        for attempt in range(max_retries):
            logger.info(f"--- Reproduction Attempt {attempt + 1}/{max_retries} ---")
            
            try:
                repro_context = await LLMService.extract_reproduction_context(
                    issue_title, issue_body, conversation_history, previous_attempts
                )
            except FatalLLMException as e:
                logger.error(f"Fatal LLM error: {e}. Aborting retry loop.")
                raise
            
            # Check against Prompt Injection allowlists before Docker pull
            try:
                ConfigService.enforce_security(repo_config, repro_context)
            except ValueError as e:
                logger.warning(str(e))
                sandbox_result["logs"] = str(e)
                break
            
            # Pass the repo full name and config to the sandbox
            try:
                sandbox_result = sandbox.run_reproduction(repro_context, repo_full_name, repo_config)
            except FatalSandboxException as e:
                logger.error(f"Fatal Sandbox exception: {e}. Aborting retry loop.")
                sandbox_result = {"logs": str(e), "expected_found": False, "bisect_result": ""}
                raise
            
            if sandbox_result["expected_found"]:
                logger.info("Successfully reproduced error! Breaking loop.")
                break
                
            logger.info("Failed to reproduce expected error. Appending logs for next try.")
            previous_attempts += f"\n\nAttempt {attempt + 1} Logs:\n{sandbox_result['logs'][-2000:]}"

        # Generate friendly conversational response, passing in the bisect result
        markdown_comment = await LLMService.generate_conversational_response(
            issue_title, 
            issue_body, 
            conversation_history, 
            sandbox_result["logs"], 
            sandbox_result["expected_found"],
            sandbox_result.get("bisect_result", "")
        )

        await gh.post_issue_comment(repo_full_name, issue_number, markdown_comment, token)
        
        # Save to SQLite History
        db = DBService()
        db.log_reproduction(
            repo_full_name, 
            issue_number, 
            sandbox_result["expected_found"], 
            sandbox_result.get("bisect_result", ""), 
            sandbox_result["logs"]
        )
        
        AuditService.log_event("REPRODUCTION_ATTEMPT", {
            "repo": repo_full_name,
            "issue_number": issue_number,
            "success": sandbox_result["expected_found"],
            "bisect": sandbox_result.get("bisect_result", ""),
            "is_local_run": False
        })
        
        HookService.trigger_post_run(
            repo_full_name, 
            issue_number, 
            sandbox_result["expected_found"], 
            sandbox_result.get("bisect_result", "")
        )
    except Exception as e:
        logger.error(f"Unhandled exception in process_issue: {e}", exc_info=True)
        # Attempt to post an error comment back to the issue
        try:
            error_comment = f"Ghost: An internal error occurred while processing this issue. Error: {str(e)}"
            await gh.post_issue_comment(repo_full_name, issue_number, error_comment, token)
        except Exception as comment_err:
            logger.error(f"Failed to post error comment: {comment_err}")
            
        # Log the failure to the DB/Audit logs
        try:
            db = DBService()
            db.log_reproduction(
                repo_full_name, 
                issue_number, 
                False, 
                "", 
                f"Error: {str(e)}"
            )
        except Exception as db_err:
            logger.error(f"Failed to log error to DB: {db_err}")
            
        try:
            AuditService.log_event("REPRODUCTION_FAILED", {
                "repo": repo_full_name,
                "issue_number": issue_number,
                "success": False,
                "error": str(e)
            })
        except Exception as audit_err:
            logger.error(f"Failed to log error to Audit: {audit_err}")


@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    
    logger.info(f"DEBUG: ALL HEADERS RECEIVED: {dict(request.headers)}")
    logger.info(f"DEBUG: Received signature header: {signature}")
    logger.info(f"DEBUG: My loaded WEBHOOK_SECRET is: {settings.WEBHOOK_SECRET}")

    if not GitHubService.verify_webhook_signature(body, signature):
        logger.error("DEBUG: verify_webhook_signature returned False!")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Webhook deduplication using X-GitHub-Delivery header
    delivery_id = request.headers.get("x-github-delivery")
    if delivery_id:
        delivery_id = delivery_id.strip()

    if not delivery_id:
        logger.warning("Missing X-GitHub-Delivery header in webhook request.")
    else:
        with DEDUP_LOCK:
            if delivery_id in PROCESSED_WEBHOOK_IDS:
                logger.info(f"Duplicate webhook detected (X-GitHub-Delivery: {delivery_id}). Ignoring.")
                return {"status": "ignored", "reason": "Duplicate webhook request"}
            
            # Maintain sliding window of up to 1000 IDs
            PROCESSED_WEBHOOK_IDS.add(delivery_id)
            PROCESSED_WEBHOOK_IDS_FIFO.append(delivery_id)
            if len(PROCESSED_WEBHOOK_IDS_FIFO) > 1000:
                oldest_id = PROCESSED_WEBHOOK_IDS_FIFO.pop(0)
                if oldest_id:
                    PROCESSED_WEBHOOK_IDS.discard(oldest_id)

    try:
        data = await request.json()
        payload = WebhookPayload(**data)
    except Exception as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")

    if payload.action not in ["opened", "reopened", "created"]:
        return {"status": "ignored", "reason": "Not an opened issue or created comment"}
        
    if not payload.issue or not payload.repository:
        return {"status": "ignored", "reason": "Payload does not contain issue or repository data"}

    trigger_keyword = "bot/reproduce"
    issue_body = payload.issue.get("body") or ""
    comment_body = payload.comment.get("body") if payload.comment else ""
    comment_body = comment_body or ""
    
    # CRITICAL FIX: Allow the bot to answer follow-up questions!
    # If the original issue has the trigger, we should listen to the conversation thread.
    is_issue_trigger = payload.action in ["opened", "reopened"] and trigger_keyword in issue_body.lower()
    is_comment_trigger = payload.action == "created" and trigger_keyword in comment_body.lower()
    is_thread_reply = payload.action == "created" and trigger_keyword in issue_body.lower()
    
    if not (is_issue_trigger or is_comment_trigger or is_thread_reply):
        return {"status": "ignored", "reason": "No explicit trigger keyword in the event or thread"}

    # Ignore comments that are from the bot itself (prevent infinite loops)
    # We check for our hidden signature instead of username, since PATs use the user's username
    if payload.action == "created" and "<!-- ghost-bot-signature -->" in comment_body:
        return {"status": "ignored", "reason": "Ignoring bot's own comment"}

    background_tasks.add_task(process_issue, payload)

    return {"status": "accepted", "message": "Reproduction started in background"}


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "ghost-hunter"}
