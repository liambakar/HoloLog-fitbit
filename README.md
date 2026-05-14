# HoloLog-fitbit

## Retrieve Google Health OAuth Tokens

Create a local secrets file from the example and fill in your OAuth values:

```sh
mkdir -p .secrets
cp .env.example .secrets/google_health.env
```

`.secrets/` is ignored by git. Keep OAuth client secrets, access tokens, and
refresh tokens there rather than in committed files.

Run the helper script from the repository root:

```sh
python3 scripts/get_google_health_token.py
```

The script reads `client_id`, `secret`, and `redirect_uri` from
`.secrets/google_health.env`, environment variables, or `Codelab.http`, opens
the Google consent URL with the report scopes, extracts the returned
authorization code, exchanges it for tokens, and writes the response to
`.secrets/oauth_tokens.json`.

With the current
`redirect_uri = https://liambakar.github.io/projects/hololog.html`, Google will
redirect you to that URL with `?code=...`. Paste the full redirected URL into
the script prompt; the script extracts the code automatically, so you do not
need to copy it into `code=`.

For a fully automatic callback, add a loopback redirect URI such as
`http://127.0.0.1:8080/callback` to the OAuth client in Google Cloud, then run:

```sh
python3 scripts/get_google_health_token.py --redirect-uri http://127.0.0.1:8080/callback
```

After consent, confirm the script prints all three granted scopes: activity and
fitness, sleep, and health metrics. If health metrics is missing, the heart-rate
endpoints will return `MISSING_OAUTH_SCOPE`.

## Create a Fitbit / Google Health Report

After `.secrets/oauth_tokens.json` exists, run:

```sh
python3 scripts/create_fitbit_report.py
```

By default this creates `outputs/fitbit_report.md` for the last 7 days. You can
choose a range and save raw API responses for debugging:

```sh
python3 scripts/create_fitbit_report.py --start 2026-05-01 --end 2026-05-14 --output outputs/may-report.md --raw-output outputs/may-raw.json
```

The report script refreshes the access token automatically when
`.secrets/oauth_tokens.json` contains a `refresh_token`.
