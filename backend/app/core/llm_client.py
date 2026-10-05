"""
Provider-Agnostic LLM Client for AutoKT.
Supports "stub", "gemini", and "azure" providers.
"""

import logging
from typing import Optional
from app.core.config import settings

logger = logging.getLogger("autokt.llm_client")


class LLMClientError(Exception):
    """Base exception for LLM provider errors."""
    pass


def generate_text(
    prompt: str,
    system_instruction: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    Generate text using the configured LLM provider.

    Args:
        prompt: User prompt text.
        system_instruction: Optional system instruction prompt.
        provider: Provider identifier ("stub", "gemini", "azure"). Defaults to settings.LLM_PROVIDER.
        model: Target model name. Defaults to settings.LLM_MODEL.
        api_key: API key. Defaults to settings.LLM_API_KEY.

    Returns:
        Generated text string.

    Raises:
        LLMClientError: If generation fails or provider is invalid.
    """
    prov = (provider or settings.get_effective_llm_provider()).lower().strip()
    if prov in ("", "none", "null"):
        prov = "stub"
    target_model = model or settings.LLM_MODEL or "gpt-5-mini"
    key = api_key if api_key is not None else settings.LLM_API_KEY

    if prov == "stub":
        logger.info("LLM provider set to 'stub'. Returning deterministic template text.")
        system_part = f" [System: {system_instruction[:40]}...]" if system_instruction else ""
        return (
            f"[STUB LLM GENERATION ({target_model})]{system_part}\n"
            f"Summary synthesized from prompt context (length {len(prompt)} chars)."
        )

    if prov == "gemini":
        if not key:
            raise LLMClientError("LLM_API_KEY is required when LLM_PROVIDER is 'gemini'.")

        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=key)
            model_inst = genai.GenerativeModel(
                model_name=target_model,
                system_instruction=system_instruction if system_instruction else None,
            )
            response = model_inst.generate_content(prompt)
            if hasattr(response, "text") and response.text:
                return response.text.strip()
            raise LLMClientError("Gemini returned an empty text response.")
        except Exception as exc:
            if isinstance(exc, LLMClientError):
                raise
            raise LLMClientError(f"Gemini LLM generation failed: {exc}") from exc

    if prov == "azure":
        endpoint = settings.AZURE_OPENAI_ENDPOINT
        deployment = settings.AZURE_OPENAI_DEPLOYMENT or target_model
        api_version = settings.AZURE_OPENAI_API_VERSION or "2024-02-01"

        if not key or not endpoint:
            raise LLMClientError("LLM_API_KEY and AZURE_OPENAI_ENDPOINT are required when LLM_PROVIDER is 'azure'.")

        try:
            from openai import AzureOpenAI  # type: ignore

            client = AzureOpenAI(
                api_key=key,
                api_version=api_version,
                azure_endpoint=endpoint,
            )

            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})

            response = client.chat.completions.create(
                model=deployment,
                messages=messages,
            )
            content = response.choices[0].message.content
            if content:
                return content.strip()
            raise LLMClientError("Azure OpenAI returned an empty response.")
        except Exception as exc:
            if isinstance(exc, LLMClientError):
                raise
            raise LLMClientError(f"Azure OpenAI LLM generation failed: {exc}") from exc

    if prov == "azure_foundry":
        base_url = settings.LLM_BASE_URL
        if not key or not base_url:
            raise LLMClientError("LLM_API_KEY and LLM_BASE_URL are required when LLM_PROVIDER is 'azure_foundry'.")

        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(base_url=base_url, api_key=key)
            combined_input = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt

            try:
                response = client.responses.create(
                    model=target_model,
                    input=combined_input,
                )

                # Azure AI Foundry responses API returns output items: reasoning item + output message item
                if hasattr(response, "output") and response.output:
                    for item in response.output:
                        if getattr(item, "type", "") == "message" or hasattr(item, "content"):
                            cnt = getattr(item, "content", None)
                            if cnt and isinstance(cnt, list):
                                for c in cnt:
                                    if hasattr(c, "text") and c.text:
                                        return c.text.strip()
                                    if isinstance(c, str) and c.strip():
                                        return c.strip()
                            elif cnt and isinstance(cnt, str):
                                return cnt.strip()
                        if hasattr(item, "text") and item.text:
                            return item.text.strip()
                if hasattr(response, "text") and response.text:
                    return response.text.strip()
            except AttributeError:
                # Fallback to chat completions if responses endpoint is unsupported
                messages = []
                if system_instruction:
                    messages.append({"role": "system", "content": system_instruction})
                messages.append({"role": "user", "content": prompt})

                chat_resp = client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                )
                if chat_resp.choices and chat_resp.choices[0].message.content:
                    return chat_resp.choices[0].message.content.strip()

            raise LLMClientError("Azure AI Foundry returned an empty or unparseable response.")
        except Exception as exc:
            if isinstance(exc, LLMClientError):
                raise
            raise LLMClientError(f"Azure AI Foundry LLM generation failed: {exc}") from exc

    raise LLMClientError(f"Unknown LLM_PROVIDER '{prov}'. Must be one of: 'stub', 'gemini', 'azure', 'azure_foundry'.")
