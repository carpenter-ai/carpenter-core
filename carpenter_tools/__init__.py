"""Executor-side tool package for Carpenter.

Tools are partitioned into two subpackages:
- read/  — Safe, read-only, local-only tools. Available for direct agentic use.
- act/   — Action tools requiring reviewed Python code. NOT directly available.

Use `from carpenter_tools.read import files` or `from carpenter_tools.act import files`.
"""
from . import read, act
