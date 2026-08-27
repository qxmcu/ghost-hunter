"""
schemas.py
Pydantic models defining strict structures for webhook payloads and LLM outputs.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    """
    Schema for the incoming GitHub Issue Webhook.
    """
    action: str
    issue: Optional[Dict[str, Any]] = None
    repository: Optional[Dict[str, Any]] = None
    comment: Optional[Dict[str, Any]] = None
    installation: Optional[Dict[str, Any]] = None


class ReproductionContext(BaseModel):
    """
    Strict schema mapped to LLM Structured Outputs (JsonSchemaMode).
    """
    base_image: str = Field(
        description="The precise Docker base image to use (e.g., 'python:3.11-slim', 'openjdk:17')."
    )
    required_packages: List[str] = Field(
        description="System packages to install via apt-get or apk (e.g., 'build-essential')."
    )
    env_vars: Dict[str, str] = Field(
        description="Environment variables needed during reproduction."
    )
    reproduction_commands: List[str] = Field(
        description="Shell commands to execute sequentially to reproduce the bug. The target repository is already cloned automatically into the /workspace directory, so do NOT clone it yourself."
    )
    expected_error_keywords: List[str] = Field(
        description="Keywords or exception names expected in stderr if the crash is successfully reproduced."
    )
    known_good_commit: Optional[str] = Field(
        description="A git commit hash or tag where the bug did NOT exist. Infer from the issue text (e.g., 'worked in v1.0'). If entirely unknown, leave null.",
        default=None
    )
    requires_network: bool = Field(
        description="Set to True ONLY if the runtime reproduction commands explicitly require internet access (e.g. testing a live API call). Otherwise, False to run in an air-gapped container.",
        default=False
    )
