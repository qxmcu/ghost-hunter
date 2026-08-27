import sqlite3
from pathlib import Path
import logging
import threading
import time

logger = logging.getLogger(__name__)
DB_PATH = Path.home() / ".ghost" / "ghost.db"
DB_LOCK = threading.Lock()

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
                    CREATE TABLE IF NOT EXISTS reproductions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        repo TEXT,
                        issue_number INTEGER,
                        success BOOLEAN,
                        bisect_result TEXT,
                        logs TEXT
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

class DBService:
    @property
    def db_path(self):
        return DB_PATH

    def __init__(self):
        self._init_db()

    def _init_db(self):
        try:
            run_db_with_retry(lambda conn: None)
        except Exception as e:
            logger.error(f"Failed to initialize SQLite DB: {e}")

    def log_reproduction(self, repo: str, issue_number: int, success: bool, bisect_result: str, logs: str):
        def _log(conn):
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO reproductions (repo, issue_number, success, bisect_result, logs)
                VALUES (?, ?, ?, ?, ?)
            """, (repo, issue_number, success, bisect_result, logs))
            conn.commit()
        try:
            run_db_with_retry(_log)
        except Exception as e:
            logger.error(f"Failed to log reproduction to DB: {e}")

    def get_history(self, issue_number: int, limit: int = 5):
        def _history(conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, repo, success, bisect_result, logs
                FROM reproductions
                WHERE issue_number = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (issue_number, limit))
            return [dict(row) for row in cursor.fetchall()]
        try:
            return run_db_with_retry(_history)
        except Exception as e:
            logger.error(f"Failed to fetch history from DB: {e}")
            return []

    def get_stats(self):
        def _stats(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM reproductions")
            total = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM reproductions WHERE success = 1")
            successes = cursor.fetchone()[0]
            
            return {
                "total_runs": total,
                "success_rate": round((successes / total * 100) if total > 0 else 0, 2),
            }
        try:
            return run_db_with_retry(_stats)
        except Exception as e:
            logger.error(f"Failed to fetch stats from DB: {e}")
            return {"total_runs": 0, "success_rate": 0}
