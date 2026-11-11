"""
teacher/dspy_config.py — Configure DSPy to use IBM watsonx.ai as its LM backend.

Uses ibm-watsonx-ai SDK directly via a custom DSPy LM wrapper, bypassing
LiteLLM's watsonx_text provider which has an empty-input bug in v1.108.

Usage:
  from teacher.dspy_config import configure_dspy, TEACHER_MODEL_ID
  configure_dspy()   # call once before any dspy.Predict / dspy.ChainOfThought

Environment variables (load from .env or shell):
  WATSONX_API_KEY     — IBM Cloud IAM API key
  WATSONX_PROJECT_ID  — watsonx.ai project UUID (Manage → General)
  WATSONX_URL         — regional endpoint, default: https://us-south.ml.cloud.ibm.com

Available models on the hackathon WML instance (confirmed 2026-11-10):
  meta-llama/llama-3-3-70b-instruct   — primary teacher (best quality)
  meta-llama/llama-3-1-8b             — secondary teacher (cross-check)
  ibm/granite-4-h-small               — IBM Granite 4.0 small
  mistral-large-2512                  — Mistral large
"""

from __future__ import annotations

import os
from typing import Any

import dspy

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WATSONX_URL: str = os.environ.get(
    "WATSONX_URL", "https://us-south.ml.cloud.ibm.com"
)
WATSONX_API_KEY: str | None = os.environ.get("WATSONX_API_KEY")
WATSONX_PROJECT_ID: str | None = os.environ.get("WATSONX_PROJECT_ID")

# Primary teacher — best instruction-following available on the hackathon instance.
TEACHER_MODEL_ID: str = "meta-llama/llama-3-3-70b-instruct"

# Secondary teacher — used for ensemble cross-check (faster, cheaper).
TEACHER_MODEL_ID_SMALL: str = "meta-llama/llama-3-1-8b"

# Generation params for labeling (low temperature = consistent labels).
_GEN_PARAMS: dict = {
    "max_tokens": 300,       # chat endpoint uses max_tokens (not max_new_tokens)
    "temperature": 0.0,
    "top_p": 1.0,
    "repetition_penalty": 1.05,
}


# ---------------------------------------------------------------------------
# Suppress harmless SDK warnings (token cap notification, API version notes).
import warnings as _warnings
_warnings.filterwarnings("ignore", message="Parameters.*not recognized")
try:
    from ibm_watsonx_ai.wml_resource import WatsonxAPIWarning as _W
    _warnings.filterwarnings("ignore", category=_W)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Custom DSPy LM backed by ibm-watsonx-ai SDK
# (LiteLLM watsonx_text has an empty-input bug in v1.108 — bypassed here)
# ---------------------------------------------------------------------------

class WatsonxLM(dspy.LM):
    """
    DSPy LM subclass that calls ibm-watsonx-ai ModelInference directly.
    Converts DSPy's messages list to a flat prompt string for the
    /text/generation endpoint.
    """

    def __init__(self, model_id: str, api_key: str, url: str, project_id: str,
                 **gen_params) -> None:
        # Initialise parent with a dummy model string (not used for actual calls).
        super().__init__(model=f"watsonx_text/{model_id}", cache=False)
        self._model_id = model_id
        self._gen_params = gen_params

        from ibm_watsonx_ai import Credentials
        from ibm_watsonx_ai.foundation_models import ModelInference
        creds = Credentials(url=url, api_key=api_key)
        self._inference = ModelInference(
            model_id=model_id,
            credentials=creds,
            project_id=project_id,
            params=gen_params,
        )

    def __call__(self, prompt: str | None = None,
                 messages: list | None = None, **kwargs) -> list[str]:
        # Use chat endpoint (non-deprecated) with a messages list.
        # Build messages from prompt string if needed.
        if messages is None:
            messages = [{"role": "user", "content": prompt or ""}]
        elif not messages:
            return [""]

        # Filter out empty messages.
        messages = [m for m in messages if m.get("content", "").strip()]
        if not messages:
            return [""]

        result = self._inference.chat(messages=messages)
        # chat() returns a dict: result["choices"][0]["message"]["content"]
        try:
            return [result["choices"][0]["message"]["content"]]
        except (KeyError, IndexError, TypeError):
            return [str(result)]


# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------

def configure_dspy(model_id: str = TEACHER_MODEL_ID) -> WatsonxLM:
    """
    Configure DSPy to use the given watsonx.ai model as its LM.

    Returns the configured WatsonxLM instance (also set as dspy.settings.lm).
    Raises ValueError if required env vars are missing.
    """
    if not WATSONX_API_KEY:
        raise ValueError(
            "WATSONX_API_KEY is not set. "
            "Add it to your .env file or set it in the shell."
        )
    if not WATSONX_PROJECT_ID:
        raise ValueError(
            "WATSONX_PROJECT_ID is not set. "
            "Find it at dataplatform.ibm.com -> project -> Manage -> General."
        )

    lm = WatsonxLM(
        model_id=model_id,
        api_key=WATSONX_API_KEY,
        url=WATSONX_URL,
        project_id=WATSONX_PROJECT_ID,
        **_GEN_PARAMS,
    )
    dspy.configure(lm=lm)
    return lm
