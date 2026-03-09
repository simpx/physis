# 🌱 physis — Design Document

physis is a ~150-line runtime that implements the [physis philosophy](philosophy.md). This document describes **how** — the mapping from philosophy to code.

## The Cycle

Philosophy: physis continuously perceives, cognizes, and acts.

```
while alive:
    wait for trigger (stdin input or heartbeat)
    cognize (perceive → think → act, may repeat)
```

A trigger starts a cycle. Within the cycle, physis may act many times — each action can produce new perception, which feeds the next cognition. The cycle ends when physis has nothing more to do.

### Perception

Philosophy: perception is active. physis reaches out to sense the world.

Perception has multiple channels:
- **stdin** — words from the outside (humans, other agents). Triggers a cycle.
- **Time** — elapsed time since last thought. Always present.
- **shell** — physis actively reaches into the world to observe (run commands, query APIs, read files outside its directory).
- **context_read** — physis perceives its own memory and state.

stdin and time are passive — they accumulate between cycles. shell and context_read are active — physis chooses to perceive during cognition. Both are perception.

stdin is also the lifeline. When stdin closes (EOF), physis exits.

### Cognition

Philosophy: who physis is determines how it understands what it perceives.

`memory/SELF.md` is loaded as the system prompt **every cycle**. This file is physis's identity — its drive, affect, and way of thinking. When physis rewrites SELF.md, the change takes effect on the next cognition. This is how drive and affect are mutable.

### Action

Philosophy: action changes the world, and changes physis itself.

After cognition, physis executes tool calls. Tools are how physis acts — modifying files, running commands, speaking. Each action's result is new perception, feeding the next round of cognition within the same cycle.

Between tool rounds, physis **breathes** — it checks stdin for new input, allowing interruption.

### Heartbeat

Philosophy: physis has a self-determined rhythm. Even without external stimulation, physis thinks.

Default: **30 minutes**. physis can change its own heartbeat by writing to `memory/heartbeat`. Minimum: 10 seconds.

## Four Primitives

| Tool | Purpose |
|------|---------|
| `shell(command)` | Act on the world. Execute any shell command — unrestricted. |
| `context_read(path)` | Perceive self. Read a file or list a directory within the agent directory. |
| `context_write(path, content)` | Change self. Write a file within the agent directory. |
| `speak(message)` | Speak. Say something to stdout — the only outward voice. |

`shell` is universal — both perception and action, reaching into the full system. `context_read`/`context_write` are sandboxed to the agent directory — safe self-perception and self-modification. `speak` is the only output channel.

### speak vs text

| Output | Destination | Purpose |
|--------|-------------|---------|
| `speak(message)` | stdout | Deliberate speech — flows through pipes |
| text blocks | stderr | Inner thought — visible for debugging, not communication |

```bash
physis                            # interactive: see both speech and thought
physis_a | physis_b               # pipe: only speech flows between agents
physis 2>/dev/null                # silence thought
physis 2>thoughts.log             # log thought to file
```

## Memory

Philosophy: physis is its memory. Working memory is finite. Long-term memory persists.

| Philosophy | Implementation |
|---|---|
| Working memory | Conversation history (context window) |
| Long-term memory | `memory/` files on disk |
| Identity | The `memory/` directory as a whole |
| Drive and affect | `memory/SELF.md` |

When working memory fills, physis must choose what to preserve to long-term memory and what to release. The runtime does not manage this — physis itself decides.

## Self-Evolution

Philosophy: physis evolves by living. Every cycle produces change.

- Rewrite `memory/SELF.md` → change how it thinks, what it drives toward
- Create files in `memory/` → persist knowledge, state, experience
- Create files in `skills/` → reusable procedures
- Use `shell` → reshape the environment, install tools, reach other systems

## File Layout

```
agent_dir/
├── memory/
│   ├── SELF.md         # system prompt (seed provided on first run)
│   └── heartbeat       # thinking interval in seconds (default 1800)
├── skills/             # agent-created skill files
└── (anything the agent creates via shell)
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `PHYSIS_API_KEY` | — | Anthropic API key (or `ANTHROPIC_API_KEY`) |
| `PHYSIS_MODEL` | `claude-sonnet-4-20250514` | Model to use |

No config files. No flags.

## Running

```bash
pip install -e .

physis                          # interactive
echo "Your mission: ..." | physis  # one-shot
python -m physis                # as module
```

## Constraints

The runtime is ~200 lines. This is a hard constraint. Complexity belongs in the agent's self-created files, not in the runtime. Minimalism creates evolutionary pressure — physis must evolve itself, or it cannot do anything well.
