"""AI provider client modules.

Each sub-module implements a provider backend (Anthropic, Ollama, etc.).
Import via ``from carpenter.agent.providers import anthropic`` (or
``ollama``, ``chain``, ``local``, ``tinfoil``).

The ``retry`` module contains shared retry/circuit-breaker logic used
by the OpenAI-compatible providers (ollama, local, tinfoil).
"""

from . import anthropic  # noqa: F401
from . import chain  # noqa: F401
from . import local  # noqa: F401
from . import ollama  # noqa: F401
from . import retry  # noqa: F401
from . import tinfoil  # noqa: F401
