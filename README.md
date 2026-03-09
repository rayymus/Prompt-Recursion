# Prompt Recursion

Feeds a prompt through multiple AI chatbots in sequence, where each bot tries to improve the previous one's response. After all the loops, a designated "evaluation" bot gets the final version.

Built with Playwright against the live web UIs, no API keys needed. It copies your existing Chrome session so you don't have to deal with login flows.

---

## How it works

1. You type a prompt
2. It goes to the first bot in the `ORDER` list
3. That response gets handed to the next bot with "here's a response, improve it"
4. Repeat across all bots, N times
5. The final result lands in front of your evaluation bot

The idea is that each model has different blind spots, so passing responses around tends to smooth them out. This was built to help me with my own writing, after the Prompt Maxxing project which is similar.

---

## Configuration

All the knobs are at the top of `main.py`:

| Variable | What it does |
|---|---|
| `ORDER` | Sequence of bots the prompt passes through |
| `EVALUATION_BOT` | Bot that receives the final refined response |
| `RECURSION_LOOPS` | How many times to run the full `ORDER` sequence |
| `EXTRACTION_TIMEOUT` | Seconds to wait for a bot to finish responding |

---

## Usage

```bash
python main.py
```

The script copies just the essential bits of your Chrome profile (cookies, local storage, session data) into a temp directory, launches Chrome with that profile, and opens each bot in its own tab. Your real profile is never touched.

Type `QUIT` to exit. The temp profile gets deleted on the way out.

---

## Caveats

- Probably doesn't work outside of MacOS and the given chatbots. Other chatbots require other specialised checks to see the response content.
- Might not be able to handle other response types such as code, images, etc. Mainly tested with text response-based questions.
- No support for selecting different chat features (choosing models, temporary chat, web search mode, etc.).
- No support for multi-line prompts.

---
