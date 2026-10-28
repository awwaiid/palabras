# Palabras: handoff for self-hosting

Palabras is a phone-first Latin American Spanish trainer. It was built as a Claude artifact; this package moves it to Brock's own website and adds ElevenLabs voice and pronunciation practice.

## What's here

- `index.html`: the whole app in one file, with no build step. It's the artifact version wrapped as a standalone document. Fonts come from Google Fonts. Everything else is inline.
- `HANDOFF.md`: this file.

## How the app works today

There are four tabs, all in one `<script>`:

| Tab | What it does | Key functions |
|---|---|---|
| Repaso | Spaced-repetition flashcards: SM-2 style, learning steps of 1 and 10 min, two cards per word (`|r` Spanish→English and `|p` English→Spanish; the `p` card unlocks once the `r` card reaches review). Also has a match-pairs mini game that doesn't touch the schedule. | `sched`, `pickNext`, `startSession`, `reveal`, `answer`, `startMatch` |
| Cuentos | Claude-generated stories that stay within the learner's vocabulary. Every word can be tapped to see its meaning, base form, and grammar, with an "Explain this form" option. Includes one built-in annotated sample story. | `renderSentences`, `bindReader`, `wordSheet`, `#genBtn` handler, `parseAnnotated` |
| Aventura | Choose-your-own-adventure, written about 3–5 sentences at a time with 3 choices each, ending after about 8 parts. | `renderAdv`, `advStep` |
| Palabras | Word list, add/edit, Duolingo import (Claude cleans up messy pasted text and falls back to `localParse`), and copy/paste backup and restore. | `renderWords`, `editWord`, `#importBtn`, `#copyBackup`, `#restoreBtn` |

**State.** A single object `S` is stored in `localStorage["palabras:v1"]`. Its parts are: `words` (keyed by `keyOf(es)`, which strips the article), `cards`, `log`, `newDay`, `settings`, `stories`, and `adv`. Every read and write is wrapped in try/catch.

**Story data shape.** Claude returns sentences in this shape (see `TOKEN_SPEC`): `{text, en, words:[{t, l, g, n}]}`. `t` is the word as written in the sentence, `l` is the lemma, `g` is the meaning in context, and `n` is a grammar note. The reader matches each `t` in order inside `text`.

**Design.** Colors are tokens on `:root`, with a dark theme under `prefers-color-scheme` and `[data-theme]`. Fonts are Literata for reading and Atkinson Hyperlegible for the UI. The accent is a violet. There is a fixed bottom tab bar and a bottom sheet for word details. Keep this design as it is.

## Product principles (from Brock, keep these)

- No streaks, no social pressure, no guilt notifications, no inflated progress. The stats show only honest numbers: cards due, new words available, "solid" words (interval of 21+ days), and the 30-day review recall rate.
- Latin American Spanish: `ustedes` rather than `vosotros`, and vocabulary like carro, celular, computadora, jugo.
- Fluent Forever ideas: gender colors on nouns, a personal "connection" note on every card, and pronunciation-first practice.

## Task 1: Host it

1. Put `index.html` on Brock's site, at a path like `/spanish/`, served over HTTPS (the microphone requires it).
2. **Data migration.** The artifact's data lives in the artifact's own browser origin. Brock moves it over with the existing backup flow: in the old artifact, Palabras → Backup → Copy backup; on the new site, paste it into Restore. Nothing needs to be built for this, but test that restoring a large backup (800+ words) works.
3. Optional: add a web app manifest and icon so it can be added to the home screen. Don't add a service worker unless asked.

## Task 2: Replace the Claude-in-artifact calls with the Anthropic API

Today `AI` comes from `await window.claude.use("sample")`. That only exists inside claude.ai and is `null` anywhere else, and when it's `null` the app hides the story, adventure, explain, and smart-import features. Replace it with an adapter that has **the same interface**, so none of the calling code needs to change:

- `AI(prompt, {signal, onText, modelTier})` resolves to `{text}`.
- `AI.json(prompt, opts)` resolves to the parsed value. Parse tolerantly: try the whole reply first, then the body of a code fence, then the span from the first `{` or `[` to the last `}` or `]`. On failure, reject with `{code: "invalid_json"}`.
- Reject with `{code}` objects, because `errCopy()` branches on `code`. Map errors this way: HTTP 401 → `not_granted`, 429 → `rate_limited`, abort → `cancelled`, anything else → `upstream_error`.
- Implementation: call `POST https://api.anthropic.com/v1/messages` from the browser with the headers `x-api-key`, `anthropic-version: 2023-06-01`, and `anthropic-dangerous-direct-browser-access: true`. Use `stream: true` and call `onText({text, delta})` with the **whole** text so far on each update.
- Map `modelTier` to models: `"quick"` → `claude-haiku-5-5`, and default → `claude-sonnet-5-5`. Check these model IDs against docs.claude.com.
- Keep `window.claude.use("sample")` as a first choice when it exists, so the file still works if it's republished as an artifact.
- Rewrite the "Claude isn't available" copy (`#aiOff` and the adventure panel) so it points to Settings.

## Task 3: Settings screen

Add a settings view: a gear icon in the header, or a fifth spot, whichever looks better. It needs:

- Anthropic API key and ElevenLabs API key fields. Store both only in `localStorage` (a separate key from `S`), show them masked, and add a "Test" button for each.
- An ElevenLabs voice picker: list voices with `GET /v1/voices`, and prefer Latin American Spanish voices (filter on labels or accent where available). Add a preview button. Store the `voice_id`.
- A speech speed setting.
- **Never put either key in `S`**, so it never ends up in the copy/paste backup. Never commit a key to the repo.

## Task 4: ElevenLabs text-to-speech

Every audio playback currently goes through `speak(text)`, which uses the device's Web Speech voice. That includes the `[data-speak]` buttons, the auto-play when an English→Spanish card is revealed, and taps in the match game. Change `speak` so that:

1. If an ElevenLabs key and voice are set, it calls `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` with the header `xi-api-key` and the body `{text, model_id, language_code: "es"}`. Model choice: `eleven_flash_v2_5` for single words (fast and cheap), and `eleven_multilingual_v2` or `eleven_v3` for whole stories. Brock can compare them; make the model a setting.
2. Cache the audio in IndexedDB, keyed by `voice_id + model + text`, so each word costs credits only once. Single words get replayed constantly, so this matters a lot.
3. If there's no key or the request fails, fall back to the current Web Speech code.
4. **Stories.** The "Hear the story" button should read one sentence at a time and highlight the current sentence, or each word using `/v1/text-to-speech/{voice_id}/with-timestamps`, which returns character alignment. Add a slower speed option.

## Task 5: Pronunciation practice (ElevenLabs speech-to-text)

1. Record audio with `navigator.mediaDevices.getUserMedia({audio: true})` and `MediaRecorder`. iOS Safari produces `audio/mp4` and Chrome produces `audio/webm`, so send whichever one you get.
2. Send it to `POST https://api.elevenlabs.io/v1/speech-to-text` as multipart with `model_id=scribe_v2`, `file`, and `language_code=es`, plus the `xi-api-key` header.
3. Compare the transcript to the target. Normalize case, punctuation, and inverted marks. Score both an accent-insensitive match and an exact match, and show a word-by-word diff where any word that wasn't recognized is highlighted. Be kind about it: show "heard: …" next to the target, not a grade.
4. Where to put it:
   - **Flashcards.** Add an optional "Say it" mic button on the English→Spanish card, before the answer is revealed. If the word is recognized, suggest Good. The learner still chooses the grade.
   - **A new "Habla" activity**, which can be a mode inside Repaso. Show a word or a short sentence from the learner's known words, play the native audio, let the learner record, and show the comparison. Prioritize words with lots of lapses.
   - **Stories.** Each sentence gets a "read it aloud" button.
5. Keep a small per-word pronunciation history in `S.words[k].pron = [{t, ok}]` so the learner can see which words keep tripping them up. Don't feed it into the SRS schedule.
6. Note for the UI copy: speech-to-text tells you whether the words were understood, not whether the accent was good. Say that honestly in the UI.

## Task 6: Vocabulary corrections

Brock imported 817 words from Duolingo. Claude is reviewing that list for wrong translations, missing or wrong `el`/`la`, wrong part of speech, and Spain-only words. The corrections will come back as a backup Brock restores, or as a patch file. If you get a patch file, apply it by matching on `keyOf(es)` and keep each card's review history.

## Acceptance checklist

- [ ] Works at about 390px wide on iOS Safari and Android Chrome, in both light and dark mode.
- [ ] With no keys set, everything works except AI and ElevenLabs. Those features show a "Add your key in Settings" prompt, with no errors.
- [ ] Keys never show up in a backup, the repo, or console logs.
- [ ] TTS audio is cached, so replaying a word makes no network request.
- [ ] Microphone permission is denied → a clear message, and the rest of the app keeps working.
- [ ] Restoring an 800-word backup works and the Repaso counts look right.
