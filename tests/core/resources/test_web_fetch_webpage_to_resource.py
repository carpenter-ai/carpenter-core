"""Tests for the ``web.fetch_webpage_to_resource`` dispatch tool (Phase B PR B3).

The helper wraps HTTP GET + ``resource.create`` + blob write +
``resource.finalize`` into a single dispatch call.  It produces a raw,
``produced_by_template=NULL`` Resource owned by the caller arc.  On
non-2xx HTTP status it raises and does NOT create a Resource.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import is_trusted, manager as res_manager, resource_trust
from carpenter.tool_backends import web as web_backend


def _make_arc(agent_type: str = "EXECUTOR", integrity: str = "trusted") -> int:
    return arc_manager.create_arc(
        name=f"test-{agent_type.lower()}",
        agent_type=agent_type,
        integrity_level=integrity,
    )


def _mock_response(
    *,
    status_code: int = 200,
    content: bytes = b"<html>hi</html>",
    url: str = "https://example.com/",
    content_type: str = "text/html; charset=utf-8",
    reason_phrase: str = "OK",
) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.content = content
    m.url = url
    m.reason_phrase = reason_phrase
    m.headers = {"content-type": content_type}
    return m


class TestHappyPath:
    def test_fetches_and_creates_resource(self):
        arc_id = _make_arc("EXECUTOR")
        body = b"<html><body>hello world</body></html>"
        mock_resp = _mock_response(content=body, url="https://example.com/page")

        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/page",
                "_caller_arc_id": arc_id,
            })

        assert set(result.keys()) >= {
            "resource_id", "file_path", "byte_size", "content_hash",
            "status_code", "final_url", "truncated", "content_type_declared",
        }
        assert result["status_code"] == 200
        assert result["final_url"] == "https://example.com/page"
        assert result["truncated"] is False
        assert result["byte_size"] == len(body)
        assert result["content_hash"] == hashlib.sha256(body).hexdigest()
        assert result["content_type_declared"] == "text/html; charset=utf-8"

        path = Path(result["file_path"])
        assert path.exists()
        assert path.read_bytes() == body

        row = res_manager.get_resource(result["resource_id"])
        assert row is not None
        assert row["content_type"] == "html"  # default
        assert row["produced_by_arc_id"] == arc_id
        assert row["produced_by_template"] is None
        assert row["byte_size"] == len(body)
        assert row["content_hash"] == hashlib.sha256(body).hexdigest()

        sd = json.loads(row["source_descriptor"])
        assert sd["url"] == "https://example.com/page"
        assert sd["status_code"] == 200
        assert sd["truncated"] is False
        assert "final_url" in sd

    def test_content_type_override(self):
        arc_id = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(
                content=b'{"k":"v"}', content_type="application/json",
            )
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/api",
                "content_type": "json",
                "_caller_arc_id": arc_id,
            })
        row = res_manager.get_resource(result["resource_id"])
        assert row["content_type"] == "json"

    def test_raw_resource_is_untrusted(self):
        arc_id = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response()
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "_caller_arc_id": arc_id,
            })
        row = res_manager.get_resource(result["resource_id"])
        assert resource_trust(row) == "untrusted"
        assert is_trusted(result["resource_id"]) is False

    def test_resource_linked_as_output(self):
        arc_id = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response()
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "_caller_arc_id": arc_id,
            })
        rid = result["resource_id"]
        outputs = res_manager.list_resources_for_arc(arc_id, role="output")
        assert any(r["id"] == rid for r in outputs)
        inputs = res_manager.list_resources_for_arc(arc_id, role="input")
        assert all(r["id"] != rid for r in inputs)


class TestNon2xx:
    def test_404_raises_and_creates_no_resource(self):
        arc_id = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(
                status_code=404, reason_phrase="Not Found", content=b"nope",
            )
            with pytest.raises(ValueError, match="404"):
                web_backend.handle_fetch_webpage_to_resource({
                    "url": "https://example.com/missing",
                    "_caller_arc_id": arc_id,
                })
        outputs = res_manager.list_resources_for_arc(arc_id, role="output")
        assert outputs == []

    def test_500_raises_and_creates_no_resource(self):
        arc_id = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(
                status_code=500, reason_phrase="Internal Server Error",
            )
            with pytest.raises(ValueError, match="500"):
                web_backend.handle_fetch_webpage_to_resource({
                    "url": "https://example.com/boom",
                    "_caller_arc_id": arc_id,
                })
        outputs = res_manager.list_resources_for_arc(arc_id, role="output")
        assert outputs == []


class TestValidation:
    def test_missing_caller_arc_id_raises(self):
        with pytest.raises(ValueError, match="_caller_arc_id"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
            })

    def test_missing_url_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="url"):
            web_backend.handle_fetch_webpage_to_resource({
                "_caller_arc_id": arc_id,
            })

    def test_empty_url_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="url"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "   ",
                "_caller_arc_id": arc_id,
            })

    def test_invalid_scheme_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="HTTP"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "ftp://example.com/",
                "_caller_arc_id": arc_id,
            })

    def test_malformed_url_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="URL"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "not-a-url",
                "_caller_arc_id": arc_id,
            })

    def test_empty_content_type_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="content_type"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "content_type": "",
                "_caller_arc_id": arc_id,
            })

    def test_non_positive_max_bytes_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="max_bytes"):
            web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "max_bytes": 0,
                "_caller_arc_id": arc_id,
            })


class TestTruncation:
    def test_oversize_response_truncated(self):
        arc_id = _make_arc("EXECUTOR")
        big_body = b"X" * 100
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(content=big_body)
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "max_bytes": 10,
                "_caller_arc_id": arc_id,
            })
        assert result["truncated"] is True
        assert result["byte_size"] == 10

        path = Path(result["file_path"])
        assert path.read_bytes() == b"X" * 10

        row = res_manager.get_resource(result["resource_id"])
        assert row["byte_size"] == 10
        sd = json.loads(row["source_descriptor"])
        assert sd["truncated"] is True

    def test_under_max_bytes_not_truncated(self):
        arc_id = _make_arc("EXECUTOR")
        body = b"small body"
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(content=body)
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "max_bytes": 1_000_000,
                "_caller_arc_id": arc_id,
            })
        assert result["truncated"] is False
        assert result["byte_size"] == len(body)


class TestDownstreamReviewerFlow:
    def test_trusted_reviewer_can_link_as_input_and_derive(self):
        """A REVIEWER arc should be able to link the raw Resource as input
        and produce a derived (trusted-pending) Resource — mirroring the
        fetch_web_content pipeline."""
        producer = _make_arc("EXECUTOR")
        with patch("carpenter.tool_backends.web.httpx") as mock_httpx:
            mock_httpx.get.return_value = _mock_response(content=b"raw html")
            result = web_backend.handle_fetch_webpage_to_resource({
                "url": "https://example.com/",
                "_caller_arc_id": producer,
            })
        rid = result["resource_id"]

        reviewer = _make_arc("REVIEWER", integrity="trusted")
        link_id = res_manager.link_arc_resource(
            arc_id=reviewer, resource_id=rid, role="input",
        )
        assert link_id > 0

        derived_id = res_manager.derive_resource(
            content_type="text-summary",
            file_path=None,
            produced_by_arc_id=reviewer,
            produced_by_template="web.fetch_content_v1",
            template_verdict="pending",
        )
        derived = res_manager.get_resource(derived_id)
        assert derived["produced_by_template"] == "web.fetch_content_v1"


class TestDispatchRegistration:
    def test_tool_registered(self):
        from carpenter.api.callbacks import _DISPATCH
        assert "web.fetch_webpage_to_resource" in _DISPATCH
        assert callable(_DISPATCH["web.fetch_webpage_to_resource"])
