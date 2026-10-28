# Palabra Viva

A Latin American Spanish trainer for adults who want to actually learn the language, built
around the parts of Duolingo that work and without the parts that exist to keep you on the app.

| Review | Flashcard | Story | Voice |
|---|---|---|---|
| ![Review home with stats](docs/screenshots/repaso.png) | ![English to Spanish card with a personal connection note](docs/screenshots/tarjeta.png) | ![A story with a tapped word explained](docs/screenshots/cuento.png) | ![Choosing a Latin American voice, dark mode](docs/screenshots/voz.png) |

## What it's for

- **Remember words for good.** Spaced-repetition flashcards: Spanish → English first, then
English → Spanish once you know the meaning. Nouns are colored by gender, and every card has
room for your own connection to the word.
- **Read real Spanish early.** Claude writes short stories and choose-your-own-adventures using
the words you already know, plus a few new ones you can guess from context. Tap any word for
its meaning, base form and grammar.
- **Hear it the way people say it.** Native Latin American voices read every word and sentence,
and stories play along sentence by sentence.
- **Latin American Spanish throughout:** *ustedes*, not *vosotros*; *carro*, *celular*,
*computadora*, *jugo*.

## What it leaves out

No streaks, leagues, hearts, guilt notifications or inflated progress. The numbers are the plain ones:
cards due, new words ready today, words that have stuck for three weeks or more, and your real
recall rate over the last 30 days.

## Using it

Paste your Duolingo word list (any messy format works) and it becomes your deck, or start from
the built-in starter words. Everyone gets their own account through a personal login link, with
nothing to sign up for, and your words and history follow you between phone and laptop.

## Running your own

One HTML file and a small Python server with sqlite:

```sh
docker build -t palabras .
docker run -d --name palabras -p 8080:8080 -v palabras:/data \
  -e PV_PUBLIC=http://localhost:8080 \
  -e ANTHROPIC_API_KEY=… -e ELEVENLABS_API_KEY=… palabras
docker exec palabras python3 server.py adduser yourname # prints your login link
```

Both keys are optional. Without Claude, stories and adventures stay hidden; without ElevenLabs,
the device's own voice reads aloud. [AGENTS.md](AGENTS.md) explains how the pieces fit.

## License

[MIT](LICENSE)
