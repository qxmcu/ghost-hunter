"""
github_service.py
Handles GitHub API interactions: Webhook signature verification, fetching code/context, and posting comments.
Wraps all external network calls in defensive try-except blocks.
"""

import hmac
import hashlib
import logging
import httpx
import asyncio
from app.config import settings

logger = logging.getLogger(__name__)


class GitHubService:
    @staticmethod
    def _get_effective_token(token: str) -> str:
        if not token:
            return token
        cleaned = token.replace("\\n", "\n").strip()
        if cleaned.startswith("-----BEGIN"):
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import padding
                from cryptography.hazmat.primitives import hashes
                import base64
                import json
                import time

                if not settings.GITHUB_APP_ID:
                    raise ValueError("GITHUB_APP_ID not set")

                def base64url_encode(payload: bytes) -> str:
                    return base64.urlsafe_b64encode(payload).rstrip(b'=').decode('utf-8')

                header = {"alg": "RS256", "typ": "JWT"}
                header_json = json.dumps(header, separators=(',', ':')).encode('utf-8')
                header_b64 = base64url_encode(header_json)
                
                now = int(time.time())
                app_id = settings.GITHUB_APP_ID
                payload = {
                    "iat": now - 60,
                    "exp": now + 540,
                    "iss": int(app_id) if (isinstance(app_id, str) and app_id.isdigit()) or isinstance(app_id, int) else app_id
                }
                payload_json = json.dumps(payload, separators=(',', ':')).encode('utf-8')
                payload_b64 = base64url_encode(payload_json)
                
                message = f"{header_b64}.{payload_b64}".encode('utf-8')
                
                private_key = serialization.load_pem_private_key(
                    cleaned.encode('utf-8'),
                    password=None
                )
                signature = private_key.sign(
                    message,
                    padding.PKCS1v15(),
                    hashes.SHA256()
                )
                signature_b64 = base64url_encode(signature)
                return f"{header_b64}.{payload_b64}.{signature_b64}"
            except Exception as e:
                logger.warning(f"Error generating JWT: {e}. Falling back to default dummy token.")
                return "github_pat_placeholder_dummy_token_for_testing_purposes"
        return cleaned

    @staticmethod
    async def _request_with_retry(
        method: str, url: str, headers: dict, json: dict = None, max_retries: int = 5, initial_backoff: float = 1.0
    ) -> httpx.Response:
        # Intercept and translate RSA PEM key in Bearer token to JWT if present
        auth_header = headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]
            effective_token = GitHubService._get_effective_token(raw_token)
            
            # If the effective token is a JWT (contains two dots) and we are making a repo request,
            # we MUST exchange it for an Installation Access Token because JWTs can't access repo resources directly.
            if effective_token and effective_token.count(".") == 2 and "api.github.com/repos/" in url:
                try:
                    parts = url.split("api.github.com/repos/")[1].split("/")
                    if len(parts) >= 2:
                        repo_full_name = f"{parts[0]}/{parts[1]}"
                        jwt_headers = {
                            "Accept": "application/vnd.github.v3+json",
                            "Authorization": f"Bearer {effective_token}",
                            "X-GitHub-Api-Version": "2022-11-28",
                        }
                        async with httpx.AsyncClient() as client:
                            inst_resp = await client.get(
                                f"https://api.github.com/repos/{repo_full_name}/installation",
                                headers=jwt_headers
                            )
                            if inst_resp.status_code == 200:
                                inst_id = inst_resp.json().get("id")
                                token_resp = await client.post(
                                    f"https://api.github.com/app/installations/{inst_id}/access_tokens",
                                    headers=jwt_headers
                                )
                                if token_resp.status_code == 201:
                                    effective_token = token_resp.json().get("token")
                                    logger.info(f"Successfully generated Installation Access Token for {repo_full_name}")
                except Exception as e:
                    logger.warning(f"Failed to generate Installation Access Token: {e}")

            headers["Authorization"] = f"Bearer {effective_token}"

        backoff = initial_backoff
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    if method.upper() == "GET":
                        response = await client.get(url, headers=headers)
                    elif method.upper() == "POST":
                        response = await client.post(url, headers=headers, json=json)
                    else:
                        raise ValueError(f"Unsupported method: {method}")
                    
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    logger.warning(f"GitHub API returned transient status {status} on {method} {url}. Retrying in {backoff}s... (Attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise
            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"GitHub API transient network error: {e} on {method} {url}. Retrying in {backoff}s... (Attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise

    @staticmethod
    def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
        """
        Validates the GitHub Webhook signature to ensure the request is authentically from GitHub
        and hasn't been tampered with.
        """
        if not signature_header:
            return False

        try:
            # GitHub provides the signature as 'sha256=...'
            hash_algorithm, github_signature = signature_header.split("=", 1)

            # Compute our own HMAC hex digest
            mac = hmac.new(
                settings.WEBHOOK_SECRET.encode("utf-8"),
                msg=payload_body,
                digestmod=getattr(hashlib, hash_algorithm, hashlib.sha256),
            )
            expected_signature = mac.hexdigest()

            # Securely compare strings to prevent timing attacks
            return hmac.compare_digest(expected_signature, github_signature)
        except Exception as e:
            logger.error(f"Error validating webhook signature: {e}")
            return False

    @staticmethod
    async def post_issue_comment(
        repo_full_name: str, issue_number: int, markdown_body: str, token: str
    ):
        """
        Posts the reproduction results back to the GitHub issue.
        """
        url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        logger.info(f"Posting comment to {repo_full_name}#{issue_number}")
        try:
            response = await GitHubService._request_with_retry("POST", url, headers=headers, json={"body": markdown_body})
            logger.info("Successfully posted comment.")
        except Exception as e:
            logger.error(f"Error posting comment: {e}")

    @staticmethod
    async def get_issue_comments(repo_full_name: str, issue_number: int, token: str) -> str:
        """
        Fetches all comments for an issue and formats them into a single string conversation history.
        """
        url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments?per_page=100"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = await GitHubService._request_with_retry("GET", url, headers=headers)
            comments = response.json()
            
            history = ""
            for c in comments:
                user = c.get("user", {}).get("login", "unknown")
                body = c.get("body", "")
                history += f"[{user}]: {body}\n\n"
            return history
        except Exception as e:
            logger.error(f"Error fetching comments: {e}")
            return ""

    @staticmethod
    async def get_issue(repo_full_name: str, issue_number: int, token: str) -> dict:
        """
        Fetches the primary issue data for CLI local runs.
        """
        url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = await GitHubService._request_with_retry("GET", url, headers=headers)
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching issue #{issue_number}: {e}")
            return {}

    @staticmethod
    async def get_repo_file(repo_full_name: str, path: str, token: str) -> str:
        """
        Fetches a raw file from the repository's default branch.
        """
        url = f"https://api.github.com/repos/{repo_full_name}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github.v3.raw",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = await GitHubService._request_with_retry("GET", url, headers=headers)
            if response.status_code == 200:
                return response.text
            return ""
        except Exception as e:
            logger.error(f"Error fetching {path}: {e}")
            return ""
