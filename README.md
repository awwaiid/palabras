# Palabra Viva

A phone-first Latin American Spanish trainer: spaced-repetition flashcards, Claude-written
stories and adventures inside your own vocabulary, and a Duolingo word import. It started as a
Claude artifact; [docs/HANDOFF.md](docs/HANDOFF.md) has the original design notes and the remaining
feature plan (ElevenLabs voice, pronunciation practice).

## Pieces

- `index.html` — the whole app, no build step.
- `server.py` — stdlib Python + sqlite. Serves the page, stores each learner's words, cards,
  stories and review log, and proxies Claude so no API key reaches the browser.

The app keeps its state object `S` in memory and, after each change, sends only the items that
changed (`word`, `card`, `story`, `meta`, plus new review-log entries) to `POST api/sync`.
When the page comes back into view it checks `GET api/stamp` and reloads if another device saved since.

## Learners

There are no passwords. Each learner gets a personal login link; opening it once sets a
long-lived cookie on that device. The sign-in screen also accepts a pasted link, for
home-screen web apps that keep their own cookies.

    docker compose exec palabra-viva python3 server.py adduser beth   # prints her link
    docker compose exec palabra-viva python3 server.py link beth      # another link (new device)
    docker compose exec palabra-viva python3 server.py revoke beth    # sign out everywhere
    docker compose exec palabra-viva python3 server.py users
    docker compose exec palabra-viva python3 server.py export beth > beth.json
    docker compose exec -T palabra-viva python3 server.py import beth < beth.json

## Configuration

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Enables stories, adventures, "Explain this form" and the smart import. Without it those features hide themselves. |
| `PV_PUBLIC` | Public URL of the app, used in printed login links (`https://thelackthereof.org/spanish`). |
| `PV_BASE` | Path prefix behind the proxy, used as the cookie path (`/spanish`). |
| `PV_DB` | sqlite file (`/data/palabra.db` in the container). |
| `PV_AI_DAILY` | Claude calls per learner per day (default 200). |

Claude models: `quick` tier (explain, import) uses `claude-haiku-5-5` at low effort; stories and
adventures use `claude-sonnet-5-5` at medium effort with server-side refusal fallback.
Token usage per learner per day is recorded in the `ai_usage` table.

## Local run

    PV_DB=/tmp/pv.db python3 server.py adduser me
    PV_DB=/tmp/pv.db python3 server.py          # http://localhost:8080/#login=…
