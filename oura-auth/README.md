# oura-auth

One-time Oura OAuth2 bootstrap. Runs the Authorization Code flow on your
machine and writes the resulting token pair, encrypted, into the shared
`oauth_tokens` collection that `oura-sync` reads.

It lives outside `oura-sync` because it is not part of that service: the
service consumes the token, it should not own an interactive flow that runs
on your machine, and this needs to run before it is deployed. (`oura-sync`'s
own `/oauth/authorize` does the same job once it is reachable.)

## Setup

```bash
cd oura-auth
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env
```

**The `.env` lives at the repo root**, shared with oura-sync -- see its
README for why. This script reads whatever `--env-file` you
pass it (default `./.env`), so point it at the shared one explicitly:

```bash
python oura_auth.py connect --env-file ../.env
```

Fill in the root `.env`. Three values matter most:

- `OURA_CLIENT_ID` / `OURA_CLIENT_SECRET` — from
  <https://cloud.ouraring.com/oauth/applications>.
- `OURA_REDIRECT_URI` — must match a redirect URI registered on that
  application **exactly**. For the local bootstrap use a loopback URL such as
  `http://localhost:8000/oauth/callback`; the script serves it itself.
- `TOKEN_ENCRYPTION_KEY` — must be **byte-identical** to the key `oura-sync`
  runs with, or it cannot decrypt what this writes.

Generate the encryption key once and reuse it everywhere:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Usage

```bash
python oura_auth.py connect --env-file ../.env
```

Opens your browser to Oura's consent screen, captures the redirect on
`localhost`, exchanges the code, and writes the encrypted token. It reports
which scopes were actually granted and warns about any you asked for but did
not get — Oura lets the user deselect scopes individually, and a missing one
shows up later as a permission error on the tools that need it.

```bash
python oura_auth.py status
```

Prints the stored token's account, scopes, version and expiry. Never prints
token material.

```bash
python oura_auth.py refresh
```

Rotates the token once by hand. Only for recovering a stuck connection — the
services refresh on their own, under a Redis lock. This command does not take
that lock, so do not run it while they are serving traffic.

If `OURA_REDIRECT_URI` points somewhere this script cannot serve (a real
ingress hostname, say), `connect` falls back to printing the URL and asking you
to paste the redirect back.

## Document schema

The contract between the three programs. Collection `oauth_tokens`, one
document per account, unique on `account_id`:

| Field | Type | Notes |
| --- | --- | --- |
| `account_id` | string | Unique. From `OURA_ACCOUNT_ID`. |
| `access_token` | string | Fernet ciphertext. |
| `refresh_token` | string | Fernet ciphertext. Single-use; rotates on refresh. |
| `expires_at` | date | UTC instant the access token expires. |
| `scopes` | string[] | Scopes Oura actually granted. |
| `token_type` | string | `Bearer`. |
| `version` | int | Incremented on every write. |
| `created_at` | date | Set once. |
| `updated_at` | date | Set on every write. |

Two rules any writer must follow:

1. **Both token halves in one update.** They rotate together; a reader must
   never see a new access token beside a dead refresh token.
2. **Take the Redis lock before refreshing.** Oura refresh tokens are
   single-use. Two processes refreshing from the same stored token means one
   result is discarded and the account can lose its refresh token entirely.
   `oura-sync`'s `TokenService` does this; anything else that refreshes must
   use the same lock key, `oura:lock:refresh:{account_id}`.

## Security

- Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before they
  reach Mongo. Nothing is written in plaintext.
- No command prints an access token, refresh token or authorization code.
- Token-endpoint failures report only Oura's error code, never the submitted
  grant.
- The shared root `.env` is gitignored. Keep it that way.
