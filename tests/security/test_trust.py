"""Tests for the conversation trust taint tracking."""

from carpenter.db import get_db
from carpenter.security import trust


def _create_conversation():
    """Helper to create a conversation and return its ID."""
    db = get_db()
    db.execute("INSERT INTO conversations (title) VALUES ('test')")
    db.commit()
    row = db.execute("SELECT last_insert_rowid()").fetchone()
    conv_id = row[0]
    db.close()
    return conv_id


# --- check_code_for_taint ---

def test_check_code_no_taint():
    """Clean code returns None."""
    code = """
from carpenter_tools.act import files
files.write("/tmp/test.txt", "hello")
"""
    assert trust.check_code_for_taint(code) is None


def test_check_code_web_import_taints():
    """Code with 'from carpenter_tools.act import web' detected."""
    code = """
from carpenter_tools.act import web
result = web.get("https://example.com")
"""
    result = trust.check_code_for_taint(code)
    assert result is not None
    assert "web" in result


def test_check_code_web_submodule_import():
    """Code with 'from carpenter_tools.act.web import get' detected."""
    code = """
from carpenter_tools.act.web import get
result = get("https://example.com")
"""
    result = trust.check_code_for_taint(code)
    assert result is not None
    assert "web" in result


def test_check_code_web_direct_import():
    """Code with 'import carpenter_tools.act.web' detected."""
    code = """
import carpenter_tools.act.web
result = carpenter_tools.act.web.get("https://example.com")
"""
    result = trust.check_code_for_taint(code)
    assert result is not None
    assert "web" in result


def test_check_code_mixed_imports():
    """Code with both safe and unsafe imports detects the unsafe one."""
    code = """
from carpenter_tools.act import files
from carpenter_tools.act import web
files.write("/tmp/test.txt", "hello")
result = web.get("https://example.com")
"""
    result = trust.check_code_for_taint(code)
    assert result is not None


def test_check_code_read_tools_safe():
    """Read tools don't cause taint."""
    code = """
from carpenter_tools.read import files
from carpenter_tools.read import state
content = files.read("/tmp/test.txt")
"""
    assert trust.check_code_for_taint(code) is None


# --- Direct networking module detection ---

def test_check_code_import_httpx():
    """Direct 'import httpx' is tainted."""
    code = 'import httpx\nresponse = httpx.get("https://example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "httpx"


def test_check_code_from_httpx_import():
    """'from httpx import Client' is tainted."""
    code = 'from httpx import Client\n'
    result = trust.check_code_for_taint(code)
    assert result == "httpx"


def test_check_code_import_requests():
    """Direct 'import requests' is tainted."""
    code = 'import requests\nrequests.get("https://example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "requests"


def test_check_code_import_socket():
    """Direct 'import socket' is tainted."""
    code = 'import socket\ns = socket.socket()\n'
    result = trust.check_code_for_taint(code)
    assert result == "socket"


def test_check_code_import_urllib_request():
    """'import urllib.request' is tainted."""
    code = 'import urllib.request\nurllib.request.urlopen("https://example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "urllib.request"


def test_check_code_from_urllib_import_request():
    """'from urllib import request' is tainted."""
    code = 'from urllib import request\nrequest.urlopen("https://example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "urllib.request"


def test_check_code_import_http_client():
    """'import http.client' is tainted."""
    code = 'import http.client\nconn = http.client.HTTPSConnection("example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "http.client"


def test_check_code_from_http_import_client():
    """'from http import client' is tainted."""
    code = 'from http import client\nconn = client.HTTPSConnection("example.com")\n'
    result = trust.check_code_for_taint(code)
    assert result == "http.client"


def test_check_code_import_aiohttp():
    """'import aiohttp' is tainted."""
    code = 'import aiohttp\n'
    result = trust.check_code_for_taint(code)
    assert result == "aiohttp"


def test_check_code_safe_stdlib_not_tainted():
    """Non-networking stdlib imports don't cause taint."""
    code = """
import os
import json
import pathlib
from collections import defaultdict
result = json.dumps({"key": "value"})
"""
    assert trust.check_code_for_taint(code) is None


# --- record_taint / is_conversation_tainted ---

def test_record_and_check_taint():
    """Record taint → is_tainted returns True."""
    conv_id = _create_conversation()
    assert not trust.is_conversation_tainted(conv_id)

    trust.record_taint(conv_id, "carpenter_tools.act.web")
    assert trust.is_conversation_tainted(conv_id)


def test_untainted_conversation():
    """Fresh conversation → is_tainted returns False."""
    conv_id = _create_conversation()
    assert not trust.is_conversation_tainted(conv_id)


def test_get_taint_sources():
    """get_taint_sources returns list of source tools."""
    conv_id = _create_conversation()
    trust.record_taint(conv_id, "carpenter_tools.act.web")
    trust.record_taint(conv_id, "carpenter_tools.act.web")  # duplicate

    sources = trust.get_taint_sources(conv_id)
    assert sources == ["carpenter_tools.act.web"]


def test_get_taint_sources_empty():
    """Untainted conversation has no sources."""
    conv_id = _create_conversation()
    assert trust.get_taint_sources(conv_id) == []


def test_taint_isolation():
    """Taint on one conversation doesn't affect another."""
    conv1 = _create_conversation()
    conv2 = _create_conversation()

    trust.record_taint(conv1, "carpenter_tools.act.web")

    assert trust.is_conversation_tainted(conv1)
    assert not trust.is_conversation_tainted(conv2)
