import base64
import hashlib
import json
import os
import sys

import jwt as pyjwt
from werkzeug.security import generate_password_hash

FIXTURES_PATH = "server/mpac_auth/testdata/auth_fixtures.json"
JWT_SECRET = "mpac-fixture-secret"
FAR_FUTURE = 4_102_444_800
PAST = 1_000_000_000
PASSWORDS = ("correct horse battery staple", "pässwörd ✓ 🔐", "")
PASSWORD_METHODS = ("scrypt", "pbkdf2:sha256:1000", "pbkdf2:sha512:1000")
DEFAULT_COST_METHODS = ("pbkdf2", "pbkdf2:sha512")
API_KEYS = ("sk_ci_test_key_for_integration_tests", "mpac-ключ-🔑")


def _password_fixture(method, password):
    return {
        "method": method,
        "password": password,
        "hash": generate_password_hash(password, method=method),
    }


def _password_fixtures():
    fixtures = [
        _password_fixture(method, password)
        for method in PASSWORD_METHODS
        for password in PASSWORDS
    ]
    fixtures.extend(
        _password_fixture(method, PASSWORDS[0]) for method in DEFAULT_COST_METHODS
    )
    return fixtures


def _unsigned_token(payload):
    def encode(part):
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}."


def _hs512_token(payload):
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS512")


def _server_verdict(token):
    try:
        payload = pyjwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=["mpac-web", "mpac-system"],
        )
    except pyjwt.ExpiredSignatureError:
        return "expired"
    except pyjwt.PyJWTError:
        return "invalid"
    return "ok" if payload.get("sub") else "missing_sub"


def _jwt_fixtures():
    web = {"sub": "user@example.com", "aud": "mpac-web", "exp": FAR_FUTURE}
    system = {"sub": "system@mpac", "aud": "mpac-system", "exp": FAR_FUTURE}
    cases = [
        ("web", pyjwt.encode(web | {"iat": PAST, "name": "U"}, JWT_SECRET), "ok"),
        ("system", pyjwt.encode(system | {"iat": PAST}, JWT_SECRET), "ok"),
        (
            "aud_list",
            pyjwt.encode(web | {"aud": ["other", "mpac-web"]}, JWT_SECRET),
            "ok",
        ),
        ("no_exp", pyjwt.encode({"sub": "a@b.c", "aud": "mpac-web"}, JWT_SECRET), "ok"),
        ("expired", pyjwt.encode(web | {"exp": PAST}, JWT_SECRET), "expired"),
        ("wrong_aud", pyjwt.encode(web | {"aud": "other"}, JWT_SECRET), "invalid"),
        (
            "no_aud",
            pyjwt.encode({"sub": "a@b.c", "exp": FAR_FUTURE}, JWT_SECRET),
            "invalid",
        ),
        ("wrong_secret", pyjwt.encode(web, "not-the-secret"), "invalid"),
        (
            "future_nbf",
            pyjwt.encode(web | {"nbf": FAR_FUTURE - 1}, JWT_SECRET),
            "invalid",
        ),
        (
            "future_iat",
            pyjwt.encode(web | {"iat": FAR_FUTURE - 1}, JWT_SECRET),
            "invalid",
        ),
        ("alg_none", _unsigned_token(web), "invalid"),
        ("hs512", _hs512_token(web), "invalid"),
        (
            "missing_sub",
            pyjwt.encode({"aud": "mpac-web", "exp": FAR_FUTURE}, JWT_SECRET),
            "missing_sub",
        ),
        ("garbage", "not.a.jwt", "invalid"),
    ]
    return [
        {"case": case, "token": token, "expect": expect}
        for case, token, expect in cases
    ]


def generate():
    return {
        "jwt_secret": JWT_SECRET,
        "passwords": _password_fixtures(),
        "jwts": _jwt_fixtures(),
        "api_keys": [
            {"key": key, "hash": hashlib.sha256(key.encode()).hexdigest()}
            for key in API_KEYS
        ],
    }


def render(fixtures):
    return json.dumps(fixtures, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    output = render(generate())
    if not workspace:
        sys.stdout.write(output)
        return
    with open(os.path.join(workspace, FIXTURES_PATH), "w", encoding="utf-8") as f:
        f.write(output)


if __name__ == "__main__":
    main()
