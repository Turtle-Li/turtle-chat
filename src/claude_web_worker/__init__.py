"""Turtle's isolated Claude Web adapter.

The package deliberately keeps Claude browser authentication separate from
the ChatGPT/gpt4free runtime.  It only talks directly to ``claude.ai`` and
publishes no model until a route has passed a real verification request.
"""

from .app import create_app

__all__ = ["create_app"]
