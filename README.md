# Insider Buy Alerts

Posts to Discord when a company's CEO, CFO, president, chair, director or 10% owner **buys their own company's stock on the open market** (SEC Form 4, code P) for at least $100,000.

- Source: SEC EDGAR latest-filings feed (free, official). Filings appear within 2 business days of the trade.
- Ignores sales, stock awards, option exercises and gifts.
- 🔥 flags when 2+ insiders bought the same stock within 30 days.
- 🗓️ flags trades made under a pre-scheduled 10b5-1 plan (less meaningful).

**Research only — not investment advice.**

## Setup
- Secret `DISCORD_WEBHOOK_URL` — the #insider-buys webhook
- Secret or variable `SEC_CONTACT` — your e-mail (the SEC requires one in the request header)
- Optional variable `MIN_VALUE` (default 100000)

Actions → Insider buys → Run workflow: `probe` (check the feed), `sample` (test message), `run`.
