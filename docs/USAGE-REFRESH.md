# Usage refresh selectors

Refresh usage is **API first**, then captured `x-ratelimit-*` headers from an exact host+path allowlist of named usage endpoints, then a **read-only** DOM text read. If nothing matches, the row stays `not available`. Nothing is guessed. Headers from any other host or path are ignored.

Update this file and `UsageRefreshLogic.Sites` together when a provider publishes a real usage control.

## Sources

| Feature | API | Header | Page |
| --- | --- | --- | --- |
| SuperGrok | none public | none (no named endpoint) | visible usage text |
| Imagine | none public | none (no named endpoint) | visible usage text |
| X reads | none for Premium in-app reads | none (no named endpoint) | visible usage text |
| X posts | optional `GET https://api.x.com/2/usage/tweets` (developer **project** usage, not Premium) | `x-ratelimit-*` only from `api.x.com` `/2/usage/tweets` | visible usage text |
| Build | hub `GET /api/state` sessions/queue counts | none | none |

X API docs: https://docs.x.com/x-api/usage/introduction and https://developer.twitter.com/en/docs/twitter-api/usage/tweets/api-reference/get-usage-tweets

The X API call is **off** until the user stores their own bearer in Windows Credential Manager (`LPIN/XApiBearer`) or a DPAPI file `x-api-bearer.dpapi` under LocalAppData/HiveBrainComms. Never put a token in the repo, `control.local.json`, or an env dump.

## How to update a DOM selector

1. Open the site signed-in and find a **visible** used/limit label (example: `12 of 100`).
2. Note a stable `data-testid`, `aria-label`, or class that wraps only that text.
3. Add the selector to `UsageRefreshLogic.Sites[id].Selectors`.
4. Keep the per-site regex in that site's parser. Do not share parsers.
5. Add a synthetic HTML fixture under `tests/fixtures/usage/` marked `EXAMPLE ONLY`.
6. Default remains `not available` when the node is missing or the markup changed.

The injected script may only read `innerText` / `textContent` from `querySelectorAll`. No clicks, navigation, fetch, or XHR.

## Interval

`usage-refresh-minutes` in `shell-settings.txt`. `0` (default) is off. Any positive value is raised to 5 minutes. The timer does not run while the window is minimized.
