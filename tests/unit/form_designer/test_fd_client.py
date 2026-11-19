"""Unit tests for fd client."""

import httpx
import pytest

from imednet.errors import ApiError, ClientError
from imednet.form_designer.client import FormDesignerClient
from imednet.form_designer.models import Layout, Page


@pytest.fixture
def mock_layout():
    """Helper function to mock layout."""
    return Layout(pages=[Page(entities=[])])


def test_save_form_success(mock_layout, respx_mock):
    """Test that save form success."""
    base_url = "https://test.imednet.com"
    client = FormDesignerClient(base_url, "fake_sessid")

    respx_mock.post(
        f"{base_url}/app/formdez/formdez_save.php",
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    resp = client.save_form("csrf", 1, 1, 1, mock_layout)
    # response.text is returned
    assert '{"success": true}' in resp or "success" in resp

    assert respx_mock.calls.call_count == 1
    req = respx_mock.calls.last.request
    assert "PHPSESSID=fake_sessid" in req.headers["Cookie"]
    assert req.headers["X-Requested-With"] == "XMLHttpRequest"


def test_save_form_server_error(mock_layout, respx_mock):
    """Test that save form server error."""
    base_url = "https://test.imednet.com"
    client = FormDesignerClient(base_url, "fake_sessid")

    respx_mock.post(
        f"{base_url}/app/formdez/formdez_save.php",
    ).mock(return_value=httpx.Response(200, json={"error": "Concurrency Error"}))

    with pytest.raises(ApiError, match="Server Error: Concurrency Error"):
        client.save_form("csrf", 1, 1, 1, mock_layout)


def test_save_form_http_error(mock_layout, respx_mock):
    """Test that save form http error sends a single POST on HTTP 500."""
    base_url = "https://test.imednet.com"
    with FormDesignerClient(base_url, "fake_sessid") as client:
        respx_mock.post(f"{base_url}/app/formdez/formdez_save.php").mock(
            return_value=httpx.Response(500)
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.save_form("csrf", 1, 1, 1, mock_layout)

        assert respx_mock.calls.call_count == 1
    assert client.session.is_closed


def test_save_form_invalid_json_fallback(mock_layout, respx_mock):
    """Simulate a legacy PHP endpoint returning HTML instead of JSON.

    The client should catch json.JSONDecodeError and raise ApiError instead of returning text.
    """
    base_url = "https://test.imednet.com"
    client = FormDesignerClient(base_url, "fake_sessid")

    html_response = "<html><body>Error</body></html>"
    respx_mock.post(
        f"{base_url}/app/formdez/formdez_save.php",
    ).mock(return_value=httpx.Response(200, text=html_response))

    with pytest.raises(ApiError) as exc_info:
        client.save_form("csrf", 1, 1, 1, mock_layout)

    assert str(exc_info.value.response) == html_response
    assert exc_info.value.status_code == 200


@pytest.mark.parametrize(
    ("csrf", "form_id", "comm_id", "rev", "expected_error"),
    [
        ("", 1, 1, 1, "CSRF Key cannot be empty."),
        ("   ", 1, 1, 1, "CSRF Key cannot be empty."),
        ("csrf", 0, 1, 1, "Invalid form_id: 0. Must be a positive integer."),
        ("csrf", -1, 1, 1, "Invalid form_id: -1. Must be a positive integer."),
        ("csrf", 1, 0, 1, "Invalid community_id: 0. Must be a positive integer."),
        ("csrf", 1, -1, 1, "Invalid community_id: -1. Must be a positive integer."),
        ("csrf", 1, 1, -1, "Invalid revision: -1. Must be non-negative."),
    ],
)
def test_save_form_validation_sad_paths(mock_layout, csrf, form_id, comm_id, rev, expected_error):
    """Test that save form validation sad paths."""
    base_url = "https://test.imednet.com"
    client = FormDesignerClient(base_url, "fake_sessid")

    with pytest.raises(ClientError, match=expected_error):
        client.save_form(csrf, form_id, comm_id, rev, mock_layout)
    client.close()


def test_form_designer_client_context_manager_closes_session_on_exit_and_error():
    """FormDesignerClient supports with blocks and closes session on normal exit and error."""
    base_url = "https://test.imednet.com"
    with FormDesignerClient(base_url, "fake_sessid") as client:
        assert not client.session.is_closed
    assert client.session.is_closed

    with pytest.raises(RuntimeError, match="boom"):
        with FormDesignerClient(base_url, "fake_sessid") as err_client:
            assert not err_client.session.is_closed
            raise RuntimeError("boom")
    assert err_client.session.is_closed


def test_save_form_retries_http_429_and_does_not_retry_403_or_network_error(
    mock_layout, respx_mock, monkeypatch
):
    """save_form retries HTTP 429 responses and sends only one POST on HTTP 403 or network error."""
    base_url = "https://test.imednet.com"
    endpoint = f"{base_url}/app/formdez/formdez_save.php"

    # Avoid sleeping during retry unit test
    monkeypatch.setattr("tenacity.nap.sleep", lambda _s: None)

    # 1. HTTP 429 twice -> HTTP 200 succeeds on attempt 3
    route = respx_mock.post(endpoint)
    route.side_effect = [
        httpx.Response(429, text="rate limited"),
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, json={"success": True}),
    ]
    with FormDesignerClient(base_url, "fake_sessid") as client:
        resp = client.save_form("csrf", 1, 1, 1, mock_layout)
        assert "success" in resp
    assert respx_mock.calls.call_count == 3
    assert client.session.is_closed

    # 2. HTTP 403 -> raises HTTPStatusError after 1 attempt
    respx_mock.calls.clear()
    route.side_effect = None
    route.mock(return_value=httpx.Response(403, text="forbidden"))
    with FormDesignerClient(base_url, "fake_sessid") as client_403:
        with pytest.raises(httpx.HTTPStatusError) as exc_403:
            client_403.save_form("csrf", 1, 1, 1, mock_layout)
        assert exc_403.value.response.status_code == 403
    assert respx_mock.calls.call_count == 1
    assert client_403.session.is_closed

    # 3. Network error on mutating POST -> raises RequestError after 1 attempt
    respx_mock.calls.clear()
    route.side_effect = httpx.ConnectError("connection dropped")
    with FormDesignerClient(base_url, "fake_sessid") as client_net:
        with pytest.raises(httpx.ConnectError):
            client_net.save_form("csrf", 1, 1, 1, mock_layout)
    assert respx_mock.calls.call_count == 1
    assert client_net.session.is_closed
