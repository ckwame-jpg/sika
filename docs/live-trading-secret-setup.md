# Live Trading Secret Setup

This file is for setting up real Kalshi production trading and the OpenAI site analyst without putting secrets in chat.

## What You Need

- Kalshi production API key ID
- Kalshi production private key
- OpenAI API key

Use an OpenAI service account key for the site analyst. A user-owned key works, but a service account is better for backend apps because it is project-scoped and easier to rotate.

## OpenAI Key Choice

In the OpenAI Platform key dialog:

- Owned by: `Service account`
- Name: `sika-site-analyst`
- Project: your billing-enabled project
- Permissions: `Restricted` if Responses/API generation can be enabled, otherwise `All`

Do not use `Read only`; the Responses API creates responses even though the Sika chatbot cannot place trades.

## Kalshi Key Choice

Create a Kalshi production API key named:

```text
sika-live-trading
```

The key needs read and write access:

- Read: balance, positions, orders, fills
- Write: live auto-trading orders

Kalshi shows the private key only once. Save it immediately.

## Easiest Setup

Run this from the repo root:

```bash
bash scripts/setup-live-secrets.sh
```

The script will:

- Create `$HOME/.config/kalshi/kalshi-live.key`
- Save the Kalshi private key there with locked-down file permissions
- Update `apps/api/.env`
- Generate `SIKA_OWNER_ADMIN_TOKEN`
- Turn `AUTO_TRADING_ENABLED=true`
- Set the daily cap to `$10`
- Save the owner token in `apps/api/.owner-token`

When the script asks for the Kalshi private key, paste the full key including:

```text
-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
```

Then type this on its own line:

```text
END_SIKA_KEY
```

## Files Created

```text
$HOME/.config/kalshi/kalshi-live.key
apps/api/.env
apps/api/.owner-token
```

`apps/api/.env` and `apps/api/.owner-token` are ignored by git. The private key is outside the repo.

## After Setup

Start the API and web app, then open Settings or Portfolio. When prompted for owner access, use:

```bash
cat apps/api/.owner-token
```

Do not paste the token into chat. Paste it into the site prompt.

## Safety Check

Before trusting live trading, verify:

- `GET /ops/kalshi/account` shows the real account snapshot
- Auto-trading status shows a `$10.00` daily cap
- Kill switch is visible in Settings
- The latest run either submits at most `$10` total or skips safely
