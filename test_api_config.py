import httpx
import pytest
from api_config import normalize_key, fingerprint, auth_error
from check_api import check


@pytest.mark.parametrize("raw", [" test-key \n", '"test-key"', "Bearer test-key", "'Bearer test-key'"])
def test_pasted_key_normalization(raw):
    assert normalize_key(raw) == "test-key"


def test_fingerprint_does_not_reveal_key():
    assert len(fingerprint("test-key")) == 12
    assert "test-key" not in fingerprint("test-key")


@pytest.mark.parametrize("code", [200, 401, 402])
def test_auth_check_without_generating_chat(code, capsys):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/models"
        assert request.headers["authorization"] == "Bearer private-test-token"
        return httpx.Response(code, json={"data":[{"id":"deepseek-flash"}], "error":{"message":"private-test-token"}})
    result = check("private-test-token", "https://api.deepseek.com", "deepseek-flash", transport=httpx.MockTransport(handler))
    output = capsys.readouterr().out
    assert "private-test-token" not in output
    assert result == (0 if code == 200 else 1)


def test_401_message_does_not_blame_balance():
    message = auth_error(401)
    assert "401" in message and "余额" not in message
