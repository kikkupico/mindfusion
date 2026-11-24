# MindFusion

**A diffusion-style interface to agents, instead of a conversational one.**

Chat makes you talk *about* the thing you want. The real work piles up in a scrolling
transcript, and every change means describing it in a new message. MindFusion drops the
conversation. There is only the page.

You write rough notes and tap **✦**. Your agent refines the whole page in one pass, the way
an image model denoises a canvas. The result becomes a new version in a branching tree.
You steer the next pass on the page itself:

| Image diffusion | MindFusion |
|---|---|
| Noisy canvas | Your rough notes |
| Denoising step | One ✦ pass over the whole page |
| Prompt / conditioning | `//` lines and comments placed where they apply |
| Inpainting | Hand edits, which the next pass keeps and builds on |
| Reroll the seed | **↻ another take** on the same input |
| Seed history | A branching version tree with diffs |

The state is the artifact, not a transcript. You never scroll back to find "the good
version": you open the version tree and branch from it.

> "Diffusion" describes the interaction, not the model. Any LLM agent can do the generation.

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
