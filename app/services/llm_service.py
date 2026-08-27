"""
llm_service.py
Interfaces with the LLM to parse the issue text and extract a strict ReproductionContext JSON.
Uses defensive API programming.
"""

import logging
from openai import AsyncOpenAI, OpenAIError, AuthenticationError, RateLimitError
from app.config import settings
from app.schemas import ReproductionContext

logger = logging.getLogger(__name__)


client = None


class FatalLLMException(Exception):
    """Raised for fatal LLM API errors such as authentication failures, rate limits, or missing keys."""
    pass


def _get_client() -> AsyncOpenAI:
    global client
    api_key = settings.LLM_API_KEY
    if not api_key or api_key.strip() in ("", "dummy", "dummy-key", "YOUR_API_KEY"):
        raise FatalLLMException("LLM API key is missing, empty, or default dummy key.")
    
    if client is not None:
        if not hasattr(client, "api_key") or getattr(client, "api_key", None) == api_key:
            return client
    
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=settings.LLM_BASE_URL if settings.LLM_BASE_URL else None
    )
    return client


class LLMService:
    @staticmethod
    async def extract_reproduction_context(
        issue_title: str, issue_body: str, conversation_history: str = "", previous_attempts: str = ""
    ) -> ReproductionContext:
        """
        Uses an LLM with Structured Outputs to parse the issue description.
        """
        system_prompt = (
            "You are an Elite Principal DevOps Engineer. "
            "Your task is to analyze a GitHub issue bug report and determine exactly how to reproduce it in a Docker container. "
            "IMPORTANT Context: The repository is AUTOMATICALLY cloned for you into the `/workspace` directory. "
            "You do NOT need to write files from scratch if they exist in the repository. Just run the installation and execution steps.\n\n"
            "You MUST respond with ONLY a valid JSON object matching this schema: "
            "{'base_image': 'str', 'required_packages': ['str'], 'env_vars': {'str': 'str'}, 'reproduction_commands': ['str'], 'expected_error_keywords': ['str'], 'known_good_commit': 'str or null', 'requires_network': bool}"
        )

        user_prompt = f"Issue Title: {issue_title}\n\nIssue Body:\n{issue_body}"
        if conversation_history:
            user_prompt += f"\n\nConversation History:\n{conversation_history}"
        if previous_attempts:
            user_prompt += f"\n\nPrevious Failed Attempts:\n{previous_attempts}\n\nAnalyze why the previous attempts failed and provide a corrected script."

        logger.info("Sending issue context to LLM for parsing...")
        fallback_ctx = ReproductionContext(
            base_image="ubuntu:22.04",
            required_packages=["curl"],
            env_vars={},
            reproduction_commands=["echo 'LLM Parsing Failed'"],
            expected_error_keywords=[]
        )
        try:
            client = _get_client()
        except FatalLLMException as e:
            logger.error(f"Fatal LLM Configuration Error: {e}")
            raise

        try:
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )

            if not response.choices or not response.choices[0].message.content:
                raise ValueError("LLM returned an empty or invalid response format")
                
            raw_json = response.choices[0].message.content
            
            # CRITICAL FIX: Strip markdown wrappers! 
            # Many open-source models output ```json ... ``` even in JSON object mode.
            raw_json = raw_json.strip()
            if raw_json.startswith("```json"):
                raw_json = raw_json[7:]
            if raw_json.startswith("```"):
                raw_json = raw_json[3:]
            if raw_json.endswith("```"):
                raw_json = raw_json[:-3]
            raw_json = raw_json.strip()
            
            context = ReproductionContext.model_validate_json(raw_json)
            return context

        except (AuthenticationError, RateLimitError) as e:
            logger.error(f"Fatal LLM API Error: {e}")
            raise FatalLLMException(f"Fatal OpenAI error occurred: {e}") from e
        except Exception as e:
            logger.error(f"Error interacting with LLM or parsing output: {e}")
            return fallback_ctx

    @staticmethod
    async def generate_conversational_response(
        issue_title: str, issue_body: str, conversation_history: str, sandbox_logs: str, success: bool, bisect_result: str
    ) -> str:
        system_prompt = (
            "You are a friendly, highly intelligent AI developer assistant named Ghost. "
            "You have just attempted to reproduce a bug reported by a user in a sandboxed Docker environment. "
            "Write a friendly paragraph summarizing what you did, the specific conditions you tested, and the outcome. "
            "If it was successful, explain what triggered the bug. If a Bisect Result is provided, explicitly mention the commit hash that caused the bug! "
            "If it failed, explain what you suspect the problem is, and politely ask the user for more details (e.g., missing config files or steps). "
            "Do NOT use robotic em dashes or overly formal language. Keep it human, casual, and helpful. "
            "If the user is asking a clarifying question in the conversation history like 'wym?', explain your previous actions more clearly. "
            "Always include the raw sandbox logs inside a <details> block at the end of your message."
        )
        
        status = "SUCCESS: The expected error was successfully reproduced." if success else "FAILED: The expected error could not be reproduced or a different error occurred."
        
        user_prompt = (
            f"Issue: {issue_title}\n{issue_body}\n\n"
            f"Conversation History:\n{conversation_history}\n\n"
            f"Reproduction Status: {status}\n"
            f"Git Bisect Result: {bisect_result if bisect_result else 'None'}\n\n"
            f"Sandbox Logs:\n{sandbox_logs[-4000:] if sandbox_logs else 'No output'}\n\n"
            "Write your response comment now."
        )
        
        try:
            client = _get_client()
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = response.choices[0].message.content or ""
            
            if "<details>" not in content:
                content += f"\n\n<details>\n<summary><b>View Sandbox Execution Logs</b></summary>\n\n```text\n{sandbox_logs[-65000:] if sandbox_logs else 'No output generated.'}\n```\n</details>"
            
            # Append hidden signature for infinite loop prevention
            content += "\n<!-- ghost-bot-signature -->"
                
            return content
        except (AuthenticationError, RateLimitError) as e:
            logger.error(f"Fatal LLM API Error: {e}")
            raise FatalLLMException(f"Fatal OpenAI error occurred: {e}") from e
        except FatalLLMException:
            raise
        except Exception as e:
            return f"I tried to reproduce the bug but ran into an internal error. Here are the logs:\n\n<details>\n<summary>Logs</summary>\n\n```text\n{sandbox_logs[-65000:] if sandbox_logs else 'No output'}\n```\n</details>"
