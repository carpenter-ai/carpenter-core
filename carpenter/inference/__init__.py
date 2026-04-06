"""Local inference server management for llama.cpp.

Use get_inference_server() to obtain the singleton InferenceServer instance.
"""

from .server import InferenceServer

_instance: InferenceServer | None = None


def get_inference_server() -> InferenceServer:
    """Return the singleton InferenceServer instance."""
    global _instance
    if _instance is None:
        _instance = InferenceServer()
    return _instance


__all__ = ["InferenceServer", "get_inference_server"]
