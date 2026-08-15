# Floorwatch — Auth & Configuration Setup Guide

Step-by-step instructions for configuring authentication and every variable introduced in the security-hardening pass (`SECURITY_REVIEW.md`). Follow this in order — later steps depend on earlier ones.

---

## 1. Prerequisites

- Python environment for both services set up (`pip install -r requirements.txt` in each of `services/floorwatch-rules-engine/` and `services/floorwatch-intelligence/`)
- A terminal in the repo root (`C:\Product\CCTV Agent`)

---

## 2. Create your config files from the templates

```powershell
Copy-Item config\deployment.env.template config\deployment.env
Copy-Item config\secrets.env.template config\secrets.env
```

- **`deployment.env`** — operational settings (timers, retention window, which notification channel to use). No secrets. Safe to share with your ops team.
- **`secrets.env`** — API keys/tokens/connection strings. **Never commit this file** (it's already `.gitignore`'d, but see step 8).

Both services automatically load `config/deployment.env` then `config/secrets.env` on startup if present — you don't need to `export` anything manually for local/dev use. **A real environment variable set by your process manager always overrides these files**, so this is safe to layer under a real production setup later without conflict.

---

## 3. Set up `FLOORWATCH_AUTH_SECRET` (do this separately — read why)

This is the single most sensitive value in the system: whoever has it can forge a valid login for any supervisor account. It is **deliberately not** in `secrets.env.template` — don't add it there.

**Simplest option (fine for a pilot):** do nothing. Leave it unset. On first run, it self-generates and saves to `services/.floorwatch_auth_secret` (already `.gitignore`'d). Both services read the same file, so they agree on it automatically.

**Better option for a real deployment:** generate one yourself and set it as a real environment variable (not in any file in this repo):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the output, then set it via whatever your process manager uses (see step 7) as `FLOORWATCH_AUTH_SECRET`. Setting it this way means it never touches disk inside this repo at all.

---

## 4. Fill in `secrets.env`

Open `config/secrets.env` and fill in only what you actually need right now — everything has a safe fallback if left blank.

| Variable | Needed for | Where to get it |
|---|---|---|
| `FLOORWATCH_TWILIO_ACCOUNT_SID` | SMS notifications | Twilio Console → Account Info |
| `FLOORWATCH_TWILIO_AUTH_TOKEN` | SMS notifications | Twilio Console → Account Info |
| `FLOORWATCH_TWILIO_FROM_NUMBER` | SMS notifications | A phone number you've purchased in Twilio |
| `FLOORWATCH_FCM_CREDENTIALS_PATH` | Push notifications (alternative to Twilio) | Firebase Console → Project Settings → Service Accounts → Generate new private key (path to the downloaded JSON file, store it outside version control) |
| `VOYAGE_API_KEY` | Real semantic search | https://dash.voyageai.com |
| `ANTHROPIC_API_KEY` | Chat assistant (`/api/chat`) — only if `FLOORWATCH_LLM_PROVIDER=anthropic` (the default) | https://console.anthropic.com |
| `FLOORWATCH_LLM_API_KEY` | Chat assistant — only if `FLOORWATCH_LLM_PROVIDER=openai` or `=gemini` | Whichever vendor's console you're using — see below |
| `FLOORWATCH_POSTGRES_DSN` | Real vector store instead of sqlite fallback | Your Postgres instance, format `postgresql://user:password@host/dbname` |

If you leave a value blank, that feature falls back gracefully (e.g. no Twilio → notifications stay in shadow-mode-style log-only via `NoOpSender`; no Voyage key → TF-IDF lexical search instead of real embeddings; no Postgres DSN → sqlite).

**Using a different AI model for the chat assistant?** The default is Claude via `ANTHROPIC_API_KEY`, but any vendor works — set these three in `deployment.env` (not secrets — see `config/deployment.env.template`):

| `FLOORWATCH_LLM_PROVIDER` | `FLOORWATCH_LLM_MODEL` example | `FLOORWATCH_LLM_BASE_URL` |
|---|---|---|
| `anthropic` (default) | *(uses `FLOORWATCH_ANTHROPIC_MODEL`)* | — |
| `openai` | `gpt-4o` | leave unset — real ChatGPT |
| `openai` | `moonshot-v1-8k` | `https://api.moonshot.ai/v1` (Kimi) |
| `openai` | whatever your host calls it | your Groq/Together/Ollama/vLLM endpoint (Llama, DeepSeek, etc.) |
| `gemini` | `gemini-2.0-flash` | — |

Then set `FLOORWATCH_LLM_API_KEY` in `secrets.env` instead of `ANTHROPIC_API_KEY`, and install the matching SDK (`pip install openai` or `pip install google-genai` — see `services/floorwatch-intelligence/requirements.txt`). See that service's `app/llm.py` module docstring for why one `openai` adapter covers ChatGPT and several other vendors.

---

## 5. Set operational values in `deployment.env`

Open `config/deployment.env` and adjust the ones you actually want to change from the defaults — every line has a comment. The ones most worth a deliberate decision:

```ini
FLOORWATCH_NOTIFY_CHANNEL=none        # change to "twilio" or "fcm" once step 4's credentials are filled in
FLOORWATCH_EMBEDDING_PROVIDER=tfidf   # change to "voyage" once VOYAGE_API_KEY is set
FLOORWATCH_RETENTION_DAYS=90          # how many days of history to keep before retention.py prunes it
FLOORWATCH_CORS_ALLOWED_ORIGINS=...   # set to your real dashboard URL(s) before this leaves localhost
FLOORWATCH_SHADOW_MODE=true           # leave true until go_live_checklist.py genuinely passes
```

---

## 6. Lock down file permissions

Both services warn at startup if `config/secrets.env` (or the auth-secret file) looks broadly readable. Fix it directly:

**Windows:**
```powershell
icacls "config\secrets.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)"
icacls "services\.floorwatch_auth_secret" /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

**Linux/macOS:**
```bash
chmod 600 config/secrets.env services/.floorwatch_auth_secret
```

---

## 7. Create your first user account

Authentication requires at least one real account — nothing ships pre-created (a checked-in default credential would be a checked-in secret).

Accounts are admin-managed now, not one-CLI-command-per-person — see the "Manage Users" screen on the dashboard (admin role only). But that screen needs an admin to already exist to use it, so the very first account still has to be bootstrapped outside the UI, one of two ways:

```bash
cd services/floorwatch-rules-engine

# Option A — run against the app directly (writes to whichever store
# config.py is set up for: Postgres if FLOORWATCH_POSTGRES_DSN is set,
# else the local users.json)
python create_user.py alice --role admin
python create_user.py --list                     # confirm the account exists

# Option B — managing Postgres directly instead? Generate the SQL locally
# (the password is hashed on your machine, never printed in plaintext)
# and paste the output into your Postgres client / Railway's query console:
python generate_admin_sql.py alice --role admin
```

Either way you'll be prompted for a password interactively (recommended). For non-interactive setups (e.g. a container entrypoint), use `--password "..."` instead.

Once `alice` exists, log in as her, open **Manage Users** in the dashboard's top bar, and create everyone else from there:

- **`admin`** — everything supervisor can do, plus create/deactivate accounts and reset passwords.
- **`supervisor`** — full access: approve/reassign zones, assign/complete tasks, confirm/dismiss flags.
- **`viewer`** — read-only: can see the dashboard live but can't act on anything.

Admin-created accounts get a temporary password the admin sets (or generates) and shares with the new person directly — no email system exists in this codebase — and that person is required to set their own password on first login.

By default accounts are stored in a local `users.json` file, which does **not** survive a Railway redeploy without a mounted volume — set `FLOORWATCH_POSTGRES_DSN` (§4 above) to store accounts in Postgres instead, reusing the same instance `floorwatch-intelligence` uses if you have one. See `RAILWAY_DEPLOYMENT.md` §3a for the full writeup, including `migrate_users_to_postgres.py` if you already have accounts in `users.json` from before switching.

---

## 8. Verify nothing secret is about to be committed

Before your first commit (or any commit after touching config):

```bash
python tools/check_no_secrets.py
```

Should print `No secret-shaped content found.` If it flags something, review it before committing — don't override it without understanding why it fired.

---

## 9. Start both services

```bash
# Terminal 1
cd services/floorwatch-rules-engine/app
uvicorn main:app --host 127.0.0.1 --port 8080

# Terminal 2
cd services/floorwatch-intelligence/app
uvicorn main:app --host 127.0.0.1 --port 8090
```

Watch the startup logs — you should see the notification channel, shadow-mode status, and (if permissions weren't fixed in step 6) any lingering permission warnings.

---

## 10. Log in and verify

Open `dashboard/floorwatch_demo.html` — it shows a login screen automatically. Log in with the account from step 7. Once logged in it connects the WebSocket and starts showing live zone/task state.

To verify from the command line instead:

```bash
curl -X POST http://127.0.0.1:8080/api/login -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"<your password>"}'
```

You should get back `{"token": "...", "username": "alice", "role": "supervisor", "expires_in": 43200}`. Confirm an unauthenticated request is correctly rejected:

```bash
curl -i http://127.0.0.1:8080/api/state
# expect: 401
```

---

## Reference: every variable from this hardening pass

| Variable | File | Default | Sensitive? |
|---|---|---|---|
| `FLOORWATCH_AUTH_SECRET` | set directly, not in a file | auto-generated | **Yes — handle separately, see step 3** |
| `FLOORWATCH_TOKEN_TTL_SECONDS` | deployment.env | 43200 (12h) | No |
| `FLOORWATCH_CORS_ALLOWED_ORIGINS` | deployment.env | localhost only | No |
| `FLOORWATCH_DOCS_ENABLED` | deployment.env | false | No |
| `FLOORWATCH_CHAT_RATE_LIMIT_PER_MINUTE` | deployment.env | 10 | No |
| `FLOORWATCH_RETENTION_DAYS` | deployment.env | 90 | No |
| `FLOORWATCH_CONSENT_CONFIRMED` | deployment.env | false | No (legal attestation, not a secret) |
| `FLOORWATCH_NOTIFY_CHANNEL` | deployment.env | none | No |
| `FLOORWATCH_TWILIO_ACCOUNT_SID/_AUTH_TOKEN/_FROM_NUMBER` | secrets.env | empty | **Yes** |
| `FLOORWATCH_FCM_CREDENTIALS_PATH` | secrets.env | empty | **Yes** (the file it points to is also secret) |
| `FLOORWATCH_EMBEDDING_PROVIDER` | deployment.env | tfidf | No |
| `VOYAGE_API_KEY` | secrets.env | empty | **Yes** |
| `ANTHROPIC_API_KEY` | secrets.env | empty | **Yes** |
| `FLOORWATCH_POSTGRES_DSN` | secrets.env | empty | **Yes** |

Full explanation of *why* the split exists: `config/README.md`.
