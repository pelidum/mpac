import hashlib
import json

import jwt as pyjwt
import pytest
from werkzeug.security import check_password_hash

from server.mpac_auth.testdata import generate_fixtures

with open(generate_fixtures.FIXTURES_PATH, encoding="utf-8") as f:
    FIXTURES = json.load(f)


def _python_verdict(token):
    try:
        payload = pyjwt.decode(
            token,
            FIXTURES["jwt_secret"],
            algorithms=["HS256"],
            audience=["mpac-web", "mpac-system"],
        )
    except pyjwt.ExpiredSignatureError:
        return "expired"
    except pyjwt.PyJWTError:
        return "invalid"
    return "ok" if payload.get("sub") else "missing_sub"


@pytest.mark.parametrize("fixture", FIXTURES["passwords"], ids=lambda f: f["method"])
def test_password_hashes_verify_with_werkzeug(fixture):
    assert check_password_hash(fixture["hash"], fixture["password"])
    assert not check_password_hash(fixture["hash"], fixture["password"] + "x")


@pytest.mark.parametrize("fixture", FIXTURES["jwts"], ids=lambda f: f["case"])
def test_jwt_expectations_match_server_auth(fixture):
    assert _python_verdict(fixture["token"]) == fixture["expect"]


@pytest.mark.parametrize("fixture", FIXTURES["api_keys"], ids=lambda f: f["key"])
def test_api_key_hashes_match_server_auth(fixture):
    assert hashlib.sha256(fixture["key"].encode()).hexdigest() == fixture["hash"]


def test_fixtures_cover_every_password_method():
    methods = {fixture["method"] for fixture in FIXTURES["passwords"]}
    assert methods == set(generate_fixtures.PASSWORD_METHODS) | set(
        generate_fixtures.DEFAULT_COST_METHODS
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
