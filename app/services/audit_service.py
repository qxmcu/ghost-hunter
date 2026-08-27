import json
import hashlib
import time
from pathlib import Path
import logging
import threading

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = Path.home() / ".ghost" / "audit.log"
AUDIT_LOCK = threading.Lock()

class AuditService:
    @staticmethod
    def _get_last_hash() -> str:
        if not AUDIT_LOG_PATH.exists():
            return "GENESIS"
        try:
            with open(AUDIT_LOG_PATH, "r") as f:
                lines = f.readlines()
                if not lines:
                    return "GENESIS"
                last_line = lines[-1].strip()
                if not last_line:
                    return "GENESIS"
                last_entry = json.loads(last_line)
                return last_entry.get("hash", "GENESIS")
        except Exception:
            return "GENESIS"

    @staticmethod
    def log_event(event_type: str, data: dict):
        with AUDIT_LOCK:
            try:
                AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                prev_hash = AuditService._get_last_hash()
                
                payload = {
                    "timestamp": time.time(),
                    "event": event_type,
                    "data": data,
                    "prev_hash": prev_hash
                }
                
                # Compute hash of the payload deterministically
                payload_str = json.dumps(payload, sort_keys=True)
                current_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
                
                payload["hash"] = current_hash
                
                with open(AUDIT_LOG_PATH, "a") as f:
                    f.write(json.dumps(payload) + "\n")
            except Exception as e:
                logger.error(f"Failed to write to audit log: {e}")

    @staticmethod
    def verify_chain() -> bool:
        """
        Recalculates the hash chain from the beginning.
        Returns True if the chain is intact, False if it has been tampered with.
        """
        with AUDIT_LOCK:
            if not AUDIT_LOG_PATH.exists():
                return True # Empty is technically valid
                
            expected_prev = "GENESIS"
            line_num = 0
            try:
                with open(AUDIT_LOG_PATH, "r") as f:
                    for line in f:
                        line_num += 1
                        line = line.strip()
                        if not line:
                            continue
                            
                        entry = json.loads(line)
                        stored_hash = entry.pop("hash", None)
                        
                        if entry.get("prev_hash") != expected_prev:
                            logger.error(f"Tamper detected at line {line_num}: prev_hash mismatch.")
                            return False
                            
                        payload_str = json.dumps(entry, sort_keys=True)
                        calculated_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
                        
                        if calculated_hash != stored_hash:
                            logger.error(f"Tamper detected at line {line_num}: hash mismatch.")
                            return False
                            
                        expected_prev = stored_hash
                        
                return True
            except Exception as e:
                logger.error(f"Error during audit verification: {e}")
                return False
