# Firecrawl local HTTP simulation

The stateless mock models the subset of Firecrawl v2 `/scrape` used by Gatherloom,
based on https://docs.firecrawl.dev/api-reference/endpoint/scrape. It validates
Bearer auth and request options. URL paths select synthetic responses. Unknown
API routes return 501; the mock never fetches a target URL.

Postman CLI 1.70+ is required; no login or real API key is needed. Owner mock
discovery was skipped because the CLI was signed out. Nothing is published.

From the repository root:

```sh
postman simulation run postman/simulations/firecrawl.sim.yaml --port 4019 --output ndjson --no-history
```

In a second terminal:

```sh
cd server
GATHERLOOM_FIRECRAWL_TEST_URL=http://127.0.0.1:4019/firecrawl/v2/ \
GATHERLOOM_TEST_DATABASE_URL=postgresql+psycopg://gatherloom:gatherloom-local@127.0.0.1:54329/gatherloom_test \
uv run --locked python -m pytest tests/test_firecrawl_simulation.py -q
```

Stop with Ctrl+C. Read the `request` events to confirm interception. Tests cover
real HTTP serialization, authentication, success → database → keyword index,
timeouts, JSON and HTML errors, 401/402/403/429/503, empty/oversized text and a
failed target page inside an HTTP 200 API response. These checks establish caller
behavior against this stand-in; a live provider/key check is separate. The full
pytest suite also tests snapshots, semantic indexing, cards, answer citations,
version reads, deletion fencing and concurrent backfill. Never commit run logs.
