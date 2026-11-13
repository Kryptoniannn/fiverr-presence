# fiverr-presence

Self-hosted, open-source, **zero-dependency, zero-paid-API** tool that reports the
**publicly displayed Online / Offline status** of any publicly accessible Fiverr
seller profile. Give it a profile URL or a username — it tells you what Fiverr
shows to any visitor.

## How it works (no hacks, no login, no scraping of private data)

Fiverr's own website lights up the green "Online" badge on every seller profile
by calling one unauthenticated public endpoint:

```
GET https://www.fiverr.com/users/<username>/is_online
→ {"is_online": true}
```

This tool performs exactly that request — the same one your browser makes when
you open a profile — with normal browser headers. It reads only the presence
flag Fiverr publishes publicly.

## Install

Requires Python 3.9+ and **nothing else** (standard library only — no pip).

```bash
git clone <this-repo> fiverr-presence
cd fiverr-presence
```

## Usage

### Single check (URL or username — all accepted)

```bash
python3 -m fiverr_presence https://www.fiverr.com/some_seller
python3 -m fiverr_presence some_seller
python3 -m fiverr_presence "www.fiverr.com/some_seller?source=search"
```

Output (coloured):

```
● ONLINE      some_seller
```

JSON: `python3 -m fiverr_presence some_seller --json`

```json
{
  "username": "some_seller",
  "status": "online",
  "is_online": true,
  "profile_url": "https://www.fiverr.com/some_seller",
  "checked_at": "2026-11-13T12:00:00Z",
  "http_code": 200,
  "error": null,
  "source": "fiverr.com/users/<username>/is_online",
  "cached": false
}
```

### Batch (one target per line, `#` comments allowed)

```bash
python3 -m fiverr_presence --batch sellers.txt --json -o report.json
python3 -m fiverr_presence --batch sellers.txt --csv  -o report.csv
```

Requests are sequential with a 1.5 s default delay (`--delay` to change) so you
stay polite to Fiverr's servers.

### Watch mode (monitor one seller)

```bash
python3 -m fiverr_presence some_seller --watch 300
```

### Self-hosted HTTP API + dashboard

```bash
python3 -m fiverr_presence --serve --host 127.0.0.1 --port 8399
```

* `GET /` — mini dashboard (type a username, see the dot)
* `GET /api/v1/status/<username>` — single JSON result
* `GET /api/v1/status?user=a,b,c` — up to 50 usernames at once
* `GET /healthz` — liveness

Example:

```bash
curl 'http://127.0.0.1:8399/api/v1/status/valeriiaty'
```

## Library use

```python
from fiverr_presence import FiverrPresenceChecker, CheckerConfig

checker = FiverrPresenceChecker(CheckerConfig(cache_ttl=60, rate_limit=1.0))
result = checker.check("https://www.fiverr.com/some_seller")
print(result.status)  # "online" | "offline" | "unknown"
```

## Behaviour details

| Concern | Default | Flag |
|---|---|---|
| Retries on 429 / transient 5xx / network errors | 3, exponential backoff | `--retries` |
| Per-request timeout | 15 s | `--timeout` |
| Min interval between outbound requests | 1.0 s (single) / 1.5 s (batch) | `--delay` |
| Disk cache TTL (`~/.cache/fiverr_presence/cache.json`) | 60 s | `--cache-ttl 0` disables |
| Exit code when status is `unknown` | `2` | — |

Status values: `online` (Fiverr says `{"is_online": true}`), `offline`
(`{"is_online": false}`), `unknown` (network problem, Fiverr error page,
or username not found — the `error` field explains which).

> **Note:** Fiverr's presence endpoint answers `{"is_online": false}` for
> nonexistent usernames too — it does not distinguish "offline" from "no such
> seller". Treat "offline" as "not currently shown as online".

## Troubleshooting

**HTTP 403 with body `{"BDAction":"Ignore ABR"}`** — this is Fiverr's bot
manager. Empirically, the presence endpoint rejects requests that carry an
**`Accept-Language` header** (verified by A/B test: identical requests with and
without it → 403 vs 200). This tool therefore never sends `Accept-Language`.
If you still get 403s, you are sending requests too fast: raise `--delay`,
lower your polling frequency, and let the built-in backoff work. 403 verdicts
clear on their own after a short cool-down.

## Legal & fair use

* Reads **only** the public presence flag Fiverr displays to every visitor.
  No authentication, no cookies, no private or personal data beyond the username.
* Requests are rate-limited and cached on purpose. Do not remove the delays —
  hammering the endpoint is the fastest way to get your IP blocked, and it is
  against the spirit of the tool.
* Automated access may be subject to Fiverr's Terms of Service. Use it for
  your own legitimate purposes (e.g. tracking your own competitors' public
  availability, integration into your own dashboards) at a reasonable rate.
* Not affiliated with or endorsed by Fiverr.

## License

MIT — see `LICENSE`.
