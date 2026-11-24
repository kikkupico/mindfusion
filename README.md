# MindFusion

A diffusion-style writing studio for LLMs. You write rough notes on a page and tap **✦**.
An agent "diffuses" them into a polished HTML page, which becomes a new version in a
branching version tree. You then steer the next pass with `//` instructions, comments and
hand edits.

It is a zero-dependency Python server ([`mindfusion/server.py`](mindfusion/server.py)) with a
single-file, phone-friendly UI. It ships as an agent skill
([`.agents/skills/mindfusion/SKILL.md`](.agents/skills/mindfusion/SKILL.md), also linked at
`.claude/skills/mindfusion`), so **whichever agent runs the skill does the writing**: Claude
Code, Codex, Gemini CLI, Antigravity, or any other harness that can run a background command.

## Using it

| Do this | How |
|---|---|
| Diffuse | Tap **✦** or press ⌘/Ctrl+Enter. Tap it again while it spins to cancel. |
| Steer | Put an instruction on its own line starting with `//`, e.g. `// add a timeline`. It's highlighted in the page and sent to the agent as a comment. |
| Comment on text | Select text, tap **//** (or press ⌘/Ctrl+/), and type what should change. |
| Edit | Type anywhere. Edits to a generated version are kept as a **draft** (orange dot on the version pill), so the original stays intact. The next ✦ uses the draft. |
| Versions | Open the **v‹n›** menu to jump or branch from any version. From there you can also save edits as a version, discard edits, or ask for **↻ another take** on the same input. |
| Compare | **±** shows a word-level diff against the parent version. |
| Documents | Tap the title to switch documents or start a new one. Each document is a folder in `diffusions/`. |

The dot next to the title shows who will do the writing:

- **Green:** a live agent session is listening.
- **Faded green:** no live session, so a fallback backend will be used.
- **Amber:** nothing is available.
- **Red:** the server is unreachable.

## Running

```bash
# via the skill helper (reuses a running server)
bash .agents/skills/mindfusion/scripts/start_server.sh --port 8765 --open-browser

# or directly
python3 -m mindfusion.server --host 0.0.0.0 --port 8765 --open-browser
```

### Who generates

1. **Live agent session (preferred).** The agent runs
   `agent_bridge.py --agent-name "<name>" listen` in the background. When you tap ✦ it
   receives the job, writes the HTML and runs `agent_bridge.py push`. See SKILL.md for the
   full loop.
2. **Headless agent CLI (fallback).** Set `MINDFUSION_AGENT_CMD` (for example
   `"claude -p"`, `"codex exec"` or `"gemini -p"`); the prompt is appended as the last
   argument. Without it, the first of `claude`, `codex`, `gemini` or `agy` found on PATH is
   used.
3. **APIs.** `GEMINI_API_KEY` or `OPENAI_API_KEY`.
4. **`local_synth`.** An offline template synthesizer, mainly for tests.

### Agent bridge

```bash
python3 .agents/skills/mindfusion/scripts/agent_bridge.py status          # active version, draft, comments, pending jobs
python3 .agents/skills/mindfusion/scripts/agent_bridge.py listen          # block until the user taps ✦
python3 .agents/skills/mindfusion/scripts/agent_bridge.py push --job-id <id> --title "…" --content-file out.html
```

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT. See [LICENSE](LICENSE).
