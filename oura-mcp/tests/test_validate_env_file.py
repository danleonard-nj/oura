"""The env-file validator that guards `kubectl create secret --from-env-file`.

This script (and the shared Secret it validates) live at the repo root, not
under oura-mcp -- oura-mcp and oura-sync run in the same namespace and share
one Secret (see ../../push-secret.bat), so there is one validator, not two.
This test file is the only copy; oura-sync does not have its own.

Each case here mirrors something kubectl actually does: it keeps quotes, it
keeps trailing whitespace, and it has no idea which keys belong in a Secret
rather than a ConfigMap. All three fail silently in the cluster, which is why
they are worth a test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "scripts" / "validate_env_file.py"
_spec = importlib.util.spec_from_file_location("validate_env_file", _MODULE_PATH)
validate_env_file = importlib.util.module_from_spec(_spec)
sys.modules["validate_env_file"] = validate_env_file
_spec.loader.exec_module(validate_env_file)

COMPLETE = """\
OURA_CLIENT_ID=cid
OURA_CLIENT_SECRET=csecret
OURA_REDIRECT_URI=https://oura.test/oauth/callback
MCP_API_KEY=key
TOKEN_ENCRYPTION_KEY=fernet
MONGODB_URI=mongodb://mongo:27017
REDIS_URL=redis://redis:6379/0
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".env.prod"
    path.write_text(content, encoding="utf-8")
    return path


def run(tmp_path: Path, content: str) -> int:
    return validate_env_file.main(["prog", str(write(tmp_path, content))])


def problems_for(tmp_path: Path, content: str) -> list[str]:
    _, problems = validate_env_file.parse(write(tmp_path, content))
    return problems


def test_a_complete_file_passes(tmp_path):
    assert run(tmp_path, COMPLETE) == 0


def test_comments_and_blank_lines_are_ignored(tmp_path):
    content = "# a comment\n\n" + COMPLETE + "\n# trailing comment\n"

    assert run(tmp_path, content) == 0


def test_optional_keys_are_allowed(tmp_path):
    content = COMPLETE + "OURA_WEBHOOK_VERIFICATION_TOKEN=tok\nMCP_OWNER_PASSWORD=pw\n"

    assert run(tmp_path, content) == 0


def test_a_quoted_value_is_rejected(tmp_path):
    """kubectl keeps the quotes, so a quoted Fernet key fails at startup."""
    problems = problems_for(tmp_path, 'TOKEN_ENCRYPTION_KEY="abc"\n')

    assert any("quoted" in p for p in problems)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_both_quote_styles_are_rejected(tmp_path, quote):
    problems = problems_for(tmp_path, f"MCP_API_KEY={quote}abc{quote}\n")

    assert any("quoted" in p for p in problems)


def test_trailing_whitespace_is_rejected(tmp_path):
    """A trailing space on the API key means every request 401s."""
    problems = problems_for(tmp_path, "MCP_API_KEY=abc   \n")

    assert any("trailing whitespace" in p for p in problems)


def test_a_value_containing_equals_is_fine(tmp_path):
    """Base64 and connection strings both carry `=`."""
    values, problems = validate_env_file.parse(
        write(tmp_path, "TOKEN_ENCRYPTION_KEY=YWJjZA==\n")
    )

    assert values["TOKEN_ENCRYPTION_KEY"] == "YWJjZA=="
    assert problems == []


def test_a_url_with_credentials_survives_parsing(tmp_path):
    values, problems = validate_env_file.parse(
        write(tmp_path, "MONGODB_URI=mongodb://user:p@ss@host:27017/?a=b\n")
    )

    assert values["MONGODB_URI"] == "mongodb://user:p@ss@host:27017/?a=b"
    assert problems == []


def test_a_missing_required_key_is_reported(tmp_path):
    content = COMPLETE.replace("REDIS_URL=redis://redis:6379/0\n", "")

    assert run(tmp_path, content) == 1


def test_an_empty_required_key_is_reported(tmp_path):
    content = COMPLETE.replace("MCP_API_KEY=key", "MCP_API_KEY=")

    assert run(tmp_path, content) == 1


def test_a_configmap_key_only_warns(tmp_path):
    """Overriding the chart is legitimate, so it is a note rather than a fault.

    Pushing a whole .env is a reasonable way to deploy. The validator's job is
    to catch values that break the deployment, not to police which layer a
    setting lives in.
    """
    content = COMPLETE + "LOG_LEVEL=DEBUG\nPORT=8000\n"

    assert run(tmp_path, content) == 0


def test_configmap_keys_fail_under_strict(tmp_path):
    content = COMPLETE + "LOG_LEVEL=DEBUG\n"

    assert validate_env_file.main(["prog", str(write(tmp_path, content)), "--strict"]) == 1


def test_the_override_is_reported(tmp_path, capsys):
    run(tmp_path, COMPLETE + "LOG_LEVEL=DEBUG\n")

    assert "LOG_LEVEL" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PUBLIC_HOST", "localhost:8000"),
        ("OURA_REDIRECT_URI", "http://localhost:8000/oauth/callback"),
        ("MONGODB_URI", "mongodb://127.0.0.1:27017"),
        ("REDIS_URL", "redis://localhost:6379/0"),
    ],
)
def test_a_localhost_value_is_rejected(tmp_path, key, value):
    """These four are what actually break a cluster deployment."""
    lines = [ln for ln in COMPLETE.splitlines() if not ln.startswith(f"{key}=")]
    content = "\n".join(lines) + f"\n{key}={value}\n"

    assert run(tmp_path, content) == 1


def test_localhost_is_allowed_for_a_local_target(tmp_path):
    content = COMPLETE + "PUBLIC_HOST=localhost:8000\nPUBLIC_SCHEME=http\n"
    path = write(tmp_path, content)

    assert validate_env_file.main(["prog", str(path)]) == 1
    assert validate_env_file.main(["prog", str(path), "--allow-local"]) == 0


def test_http_scheme_against_a_remote_host_is_rejected(tmp_path):
    """OAuth metadata would advertise http and clients would send tokens in the clear."""
    content = COMPLETE + "PUBLIC_HOST=oura.example.com\nPUBLIC_SCHEME=http\n"

    assert run(tmp_path, content) == 1


def test_https_against_a_remote_host_is_fine(tmp_path):
    content = COMPLETE + "PUBLIC_HOST=oura.example.com\nPUBLIC_SCHEME=https\n"

    assert run(tmp_path, content) == 0


def test_a_missing_file_exits_two(tmp_path):
    assert validate_env_file.main(["prog", str(tmp_path / "absent.env")]) == 2


def test_no_value_is_ever_printed(tmp_path, capsys):
    run(tmp_path, COMPLETE + "MCP_OWNER_PASSWORD=super-secret-value\n")

    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out + captured.err
    assert "MCP_OWNER_PASSWORD" in captured.out


def test_the_committed_template_parses_and_documents_every_required_key(tmp_path):
    """`.env.example` is the single template; it must stay in step with REQUIRED."""
    example = _REPO_ROOT / ".env.example"
    values, problems = validate_env_file.parse(example)

    assert problems == [], problems
    for key in validate_env_file.REQUIRED + validate_env_file.OPTIONAL:
        assert key in values, f"{key} is missing from .env.example"


def test_a_duplicate_key_is_reported(tmp_path):
    """Last assignment wins silently, so the earlier one is invisible."""
    content = COMPLETE + "OURA_CLIENT_ID=a-second-value\n"

    problems = problems_for(tmp_path, content)

    assert any("set again" in problem for problem in problems)
    assert run(tmp_path, content) == 1


def test_a_repeated_key_with_the_same_value_is_still_reported(tmp_path):
    """Even harmless duplication hides which line is in effect."""
    content = COMPLETE + "REDIS_URL=redis://redis:6379/0\n"

    assert run(tmp_path, content) == 1


def test_the_duplicate_message_names_both_lines(tmp_path):
    problems = problems_for(tmp_path, "A=1\nB=2\nA=3\n")

    assert any("line 3" in p and "line 1" in p for p in problems)
