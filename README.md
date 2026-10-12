# Mehar Super Agent v2 — Fixed Prototype

This package is a **hardened Streamlit prototype**, not a production-ready marketplace, payment gateway, webhook server, or installable PWA.

## Features implemented
- Single-admin login configured via environment variables or Streamlit secrets; no built-in default password.
- Local SQLite lead storage with parameterized queries, short-lived connections, WAL, busy timeout, constraints, and indexes.
- Rolling 60-second SEO rate limit recorded atomically in SQLite.
- Optional Groq SEO generation with timeout, bounded SDK retries, output limits, and guarded error messages.
- Clear demo-only payment UI; it does not verify or approve Easypaisa payments.
- Webhook/PWA limitations documented instead of presenting placeholders as working integrations.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set secrets before starting. Use a unique strong password; do not commit secrets to GitHub.

Linux/macOS:
```bash
export ADMIN_USER='your-admin-user'
export ADMIN_PASS='use-a-long-unique-password'
export GROQ_API_KEY='your-groq-api-key'
streamlit run app.py
```

Windows PowerShell:
```powershell
$env:ADMIN_USER='your-admin-user'
$env:ADMIN_PASS='use-a-long-unique-password'
$env:GROQ_API_KEY='your-groq-api-key'
streamlit run app.py
```

Alternatively, create `.streamlit/secrets.toml` locally (never commit it):
```toml
ADMIN_USER = "your-admin-user"
ADMIN_PASS = "use-a-long-unique-password"
GROQ_API_KEY = "your-groq-api-key"
```

## Database

By default the app creates `leads.db` in its working directory. Set `MEHAR_DB_PATH` to choose another path. Confirm persistent disk/volume and backups before deployment; ephemeral hosting may erase local files on restart or redeploy. This is a small local prototype database, not a substitute for production database operations.

## Important limitations
- **Authentication:** one shared admin credential, not multi-user account management, MFA, password reset, audit-grade session management, or production authorization.
- **Rate limiting:** per username and shared SQLite database. It is not a distributed limiter for multi-instance deployments.
- **Groq:** requires a valid API key, supported model, internet access, and review of provider data handling. The product description is sent to the provider when the user clicks Generate SEO.
- **Easypaisa:** no live API integration. No payment status is verified.
- **Webhooks:** no webhook endpoint is exposed by this Streamlit app.
- **PWA:** no installable PWA is delivered by this package. Streamlit does not automatically serve the generated files as a PWA; use a proper frontend/static hosting architecture and HTTPS.
- **Deployment:** run behind HTTPS and suitable access controls. Do not expose a private admin tool publicly without a deployment-specific security review.

## Validation performed
Python syntax compilation and ZIP integrity checks were run on the packaged source. Live Streamlit/browser tests, Groq API calls, mobile-device tests, penetration tests, and live Easypaisa/WhatsApp integrations were **not** performed.

## v2.1 changes (code review of v2 FIXED)
- Login throttle: 5 attempts / 5 minutes, global (stops password guessing; an attacker can also temporarily lock the admin out).
- Login uses a form (Enter / phone keyboard "Go" submits).
- Rate-limit cleanup and counting are now scoped per action (v2 would have let one action's cleanup delete another's rows once a second action was added).
- Phone validation (7-15 digits). Stale SEO result no longer shown under a new error.
- `.streamlit/config.toml` forces the dark theme, so labels stay readable on light-mode phones.
- Not re-tested live: Streamlit UI, Groq, mobile. Logic tested with a script (see below).
- Known: refreshing the page logs you out (session-only auth). `use_container_width` may warn on newer Streamlit; check on your version.

## Mehar Store Builder (added in this build)

The authenticated dashboard includes an independent store-building workspace:
- Create multiple stores with a unique URL-friendly slug and niche.
- Add, edit, and delete products with SKU uniqueness, price, stock, description, image URL, and active/inactive state.
- Save theme and primary-color settings in SQLite and preview the selected style.
- Create manual orders against available stock; stock is reserved transactionally, and cancelling an order restores the reserved quantity once.
- Generate and download a standalone HTML catalog for upload to a hosting provider.

**Important limits:** This is a local prototype, not a hosted ecommerce platform. No Wicofly API is used or required. The downloaded HTML is a static catalog only; it does not accept orders or payments. Public publishing, checkout, shipping integrations, and production multi-user permissions require separate implementation and hosting. Use a persistent `MEHAR_DB_PATH` on the deployment platform and arrange backups before real business use.
