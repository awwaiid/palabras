# Working on Palabra Viva

Notes for coding agents (and people) changing this repo. The README covers what the app is for;
this covers how it is built and what must not break.

## Product rules

These come from the owner and are the point of the app. Don't trade them away for a feature.

- No streaks, leagues, social pressure, guilt notifications, or inflated progress. Stats show
  only real numbers: cards due, new words available, solid words (review interval of 21+ days),
  and the 30-day review recall rate.
- Latin American Spanish: `ustedes`, never `vosotros`; carro, celular, computadora, jugo. Every
Claude prompt includes the `LATAM` instruction.
- Fluent Forever ideas: gender colors on nouns, a personal "connection" note on every card,
pronunciation first.
- Phone first. Check every change at about 390px wide, in light and dark mode. Colors are
tokens on `:root` with a dark set under `prefers-color-scheme` and `[data-theme]`; Literata
for reading, Atkinson Hyperlegible for UI, violet accent. Keep the design as it is.

## Layout

- `index.html` is the whole app: markup, CSS and one `<script>`. No build step, no framework.
- `server.py` is stdlib Python (`http.server`, `sqlite3`, `urllib`) plus the `anthropic` SDK.
`python3 server.py --help`-style usage is in its docstring; the CLI manages learners.
- `Dockerfile` builds the image; data lives in `/data` (`palabra.db` and the `tts/` audio cache).
- `docs/HANDOFF.md` is the original handoff from when this was a Claude artifact. Tasks 1–4 are
done. Task 5 (pronunciation practice with ElevenLabs speech-to-text) and task 6 (vocabulary
corrections) are not.

## The app's state

All learner data is one object, `S`: `words` (keyed by `keyOf(es)`, which drops the article),
`cards` (two per word, `<key>|r` Spanish → English and `<key>|p` English → Spanish; the `p` card
unlocks when the `r` card reaches review), `log` (review history), `newDay`, `settings`
(including `voice` and `speed`), `stories`, and `adv` (the current adventure).

Code mutates `S` directly and calls `save()`. `save()` debounces into `flush()`, which diffs `S`
against the last synced copy (`itemsOf`/`changes`) and posts only changed items to `api/sync`.
That means new state needs no sync code as long as it lives inside `S`. Never store learner data
in `localStorage`; it is only used for the last open tab. Never put API keys in `S`, since it is
what Copy backup exports.

## Server API

All under the app's base path; everything except `/`, `/health` and `api/login` needs the
`pv_token` cookie.

| Route | Purpose |
|---|---|
| `GET /` | The app. Sends its own `Permissions-Policy` (see below). |
| `POST api/login`, `api/logout` | Exchange a login-link token for the cookie, or clear it. |
| `GET api/me` | Name, and whether Claude (`ai`) and ElevenLabs (`tts`) are configured. |
| `GET api/state`, `GET api/stamp` | Load `S`; check whether another device saved since. |
| `POST api/sync`, `POST api/restore` | Save changed items; replace everything from a backup. |
| `POST api/ai` | Claude, streamed as JSON lines: `{"t": text}`… then `{"done": stop_reason}` or `{"error": code}`. |
| `GET api/voices`, `GET api/tts` | Voice list; an mp3 for `t` (text), `v` (voice id), `s` (speed). |

Tables: `users`, `tokens` (sha256 of each login token), `items` (one row per word, card, story
or meta key, holding its JSON), `log`, and `ai_usage` (Claude calls, tokens and ElevenLabs
characters per learner per day).

## Claude and ElevenLabs

- Keys live only in the server's environment. The browser never sees them.
- In the app, `AI(prompt, {signal, onText, modelTier})` resolves to `{text}` and `AI.json` parses
tolerantly; errors reject with `{code}` objects that `errCopy()` turns into words. The adapter
prefers `window.claude.use("sample")` when it exists, so the file still works as a Claude artifact.
- `modelTier: "quick"` (explain, import) uses `claude-haiku-5-5` at low effort; everything else
uses `claude-sonnet-5-5` at medium effort with server-side refusal fallback. Models live in
`MODELS` in `server.py`.
- Every playback goes through `speak(text)`, which resolves `true` when the clip finished and
`false` when something interrupted it. It uses ElevenLabs when a voice is set and falls back to
the device's speech voice. The server generates each voice/model/speed/text clip once, keeps it
under `/data/tts/`, and marks it immutable, so a word costs credits once across all learners.
- ElevenLabs library voices need a paid plan or prepaid credits; free accounts get `402`. The
server stops asking for a refused voice for 10 minutes.
- The production proxy (BunkerWeb) adds `Permissions-Policy: autoplay=()` to every response,
which leaves `audio.play()` silently pending in Chrome. The page sends its own
`Permissions-Policy: autoplay=(self), microphone=(self)` to override it. Keep that header.

## Running and testing

```sh
python3 -m venv .venv && .venv/bin/pip install anthropic
PV_DB=/tmp/pv.db .venv/bin/python server.py adduser me     # prints a login link
PV_DB=/tmp/pv.db .venv/bin/python server.py     # http://localhost:8080/#login=…
```

Put keys in `.env` (gitignored) and load them with `set -a; . ./.env; set +a` when you need
Claude or voices. There is no test suite; check changes in a real browser at phone width. A
Playwright pass that signs in with a link, reviews a few cards, reloads, and restores a large
backup covers the storage path. Restoring an 800-word backup should still work, and the Repaso
counts should still be right afterwards.
