"""Pluggable LLM providers with graceful fallback.

The pre-visit / post-visit prompts are used **verbatim** from the project
spec (see ``prompts.py``). Every provider call is wrapped with a timeout and a
deterministic fallback, so booking and visit flows never break.
"""
