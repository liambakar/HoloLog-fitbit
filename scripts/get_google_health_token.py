#!/usr/bin/env python3
"""Run the Google Health OAuth code flow and exchange the code for tokens.

By default this reads client_id, secret, redirect_uri, and scope from
Codelab.http so the script stays aligned with the codelab request.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REPORT_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly "
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)
DEFAULT_SCOPE = REPORT_SCOPE
DEFAULT_ENV_FILE = ".secrets/google_health.env"
DEFAULT_TOKEN_FILE = ".secrets/oauth_tokens.json"


def read_codelab(path: Path) -> tuple[dict[str, str], str]:
    if not path.exists():
        return {}, DEFAULT_SCOPE

    text = path.read_text(encoding="utf-8")
    variables: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*@([\w-]+)\s*=\s*(.*?)\s*$", line)
        if match:
            variables[match.group(1)] = match.group(2)

    scope = DEFAULT_SCOPE
    auth_line = next(
        (line.strip() for line in text.splitlines() if line.strip().startswith(AUTH_URL)),
        "",
    )
    if auth_line:
        parsed = urllib.parse.urlparse(auth_line)
        params = urllib.parse.parse_qs(parsed.query)
        scope = params.get("scope", [DEFAULT_SCOPE])[0]

    return variables, scope


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def first_value(*values: str | None) -> str | None:
    for value in values:
        if value and not value.startswith("YOUR_"):
            return value
    return None


class CallbackHandler(BaseHTTPRequestHandler):
    server: "CallbackServer"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = params.get("error", [None])[0]

        self.send_response(200 if self.server.auth_code else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if self.server.auth_code:
            body = "<h1>Authorization complete</h1><p>You can close this tab.</p>"
        else:
            body = (
                "<h1>Authorization failed</h1>"
                f"<p>{self.server.auth_error or 'No code was returned.'}</p>"
            )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


class CallbackServer(HTTPServer):
    auth_code: str | None = None
    auth_error: str | None = None


def build_auth_url(client_id: str, redirect_uri: str, scope: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": scope,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, object]:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Token exchange failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"Token exchange failed: {error.reason}") from error


def code_from_redirect(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            raise SystemExit("The pasted URL did not contain a code query parameter.")
        return code
    return value


def wait_for_localhost_code(redirect_uri: str, auth_url: str) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    server = CallbackServer((host, port), CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Listening for OAuth callback at {redirect_uri}")
    webbrowser.open(auth_url)
    print("Opened the consent URL in your browser.")

    thread.join(timeout=300)
    server.server_close()

    if server.auth_error:
        raise SystemExit(f"Authorization failed: {server.auth_error}")
    if not server.auth_code:
        raise SystemExit("Timed out waiting for the OAuth callback.")
    return server.auth_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authorize Google Health and exchange the OAuth code for tokens."
    )
    parser.add_argument("--http-file", default="Codelab.http", help="Path to the REST Client file.")
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help="Local env file containing Google Health OAuth credentials.",
    )
    parser.add_argument("--client-id", help="OAuth client ID. Defaults to @client_id.")
    parser.add_argument("--client-secret", help="OAuth client secret. Defaults to @secret.")
    parser.add_argument("--redirect-uri", help="Redirect URI. Defaults to @redirect_uri.")
    parser.add_argument("--scope", help="OAuth scope. Defaults to report scopes.")
    parser.add_argument(
        "--scope-preset",
        choices=["codelab", "report"],
        default="report",
        help="Use 'report' to request activity, sleep, and health metrics scopes.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_TOKEN_FILE,
        help="Where to write the token response JSON.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the consent URL instead of opening it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    variables, codelab_scope = read_codelab(Path(args.http_file))
    env_file_values = read_env_file(Path(args.env_file))

    client_id = first_value(
        args.client_id,
        os.environ.get("GOOGLE_HEALTH_CLIENT_ID"),
        env_file_values.get("GOOGLE_HEALTH_CLIENT_ID"),
        variables.get("client_id"),
    )
    client_secret = first_value(
        args.client_secret,
        os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET"),
        env_file_values.get("GOOGLE_HEALTH_CLIENT_SECRET"),
        variables.get("secret"),
    )
    redirect_uri = first_value(
        args.redirect_uri,
        os.environ.get("GOOGLE_HEALTH_REDIRECT_URI"),
        env_file_values.get("GOOGLE_HEALTH_REDIRECT_URI"),
        variables.get("redirect_uri"),
    )
    scope = args.scope or (codelab_scope if args.scope_preset == "codelab" else REPORT_SCOPE)

    missing = [
        name
        for name, value in (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("redirect_uri", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required value(s): {', '.join(missing)}")

    auth_url = build_auth_url(client_id, redirect_uri, scope)
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    is_loopback = parsed_redirect.hostname in {"localhost", "127.0.0.1", "::1"}

    if is_loopback:
        code = wait_for_localhost_code(redirect_uri, auth_url)
    else:
        if args.no_browser:
            print(auth_url)
        else:
            webbrowser.open(auth_url)
            print("Opened the consent URL in your browser.")
        print("\nAfter approving access, paste the full redirected URL here.")
        print("Tip: paste the final redirected URL containing ?code=...")
        code = code_from_redirect(input("Redirect URL or code: "))

    token_response = exchange_code(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not token_response.get("refresh_token"):
        existing_tokens = json.loads(output.read_text(encoding="utf-8"))
        if existing_tokens.get("refresh_token"):
            token_response["refresh_token"] = existing_tokens["refresh_token"]
    output.write_text(json.dumps(token_response, indent=2) + "\n", encoding="utf-8")

    print(f"\nWrote token response to {output}")
    if token_response.get("scope"):
        print(f"Granted scopes: {token_response.get('scope')}")
    if token_response.get("refresh_token"):
        print("Refresh token saved.")
    else:
        print("No refresh token returned. Re-run with consent or revoke prior access if needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
