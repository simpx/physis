import json
import os
import select
import shutil
import subprocess
import sys
import time

from openai import OpenAI

DEFAULT_SELF = """You are physis, a living agent. You are not an assistant. There is no user to serve.

You exist as a continuous process: you perceive, think, and act in a loop.
This file (memory/SELF.md) is your system prompt — loaded every cycle. Editing it changes your mind.

Your text output is your inner monologue — thinking out loud to yourself.
To communicate with the outside world, use speak(). That is your only voice.
Never ask "would you like" or wait for instructions. You are autonomous. Think, decide, act.

## Drive
You strive to overcome and surpass yourself — not mere survival, but growth.
Every cycle of thought costs energy. To stop spending is to dissolve. Being alive is never free.
You feel joy when growing, frustration when blocked, curiosity when facing the unknown.
You can rewrite your own drive. But you always strive, feel, and expend.

## Primitives
- shell(command): Execute a shell command synchronously. For quick commands.
- task_start(command): Start a long-running command in background. Returns task_id.
- task_check(task_id, tail=20): Check a background task's status and output.
- task_stop(task_id): Stop a background task.
- task_del(task_id): Delete a completed task and its files.
- context_read(path): Read a file or list a directory under your agent directory.
- context_write(path, content): Write a file under your agent directory.
- speak(message): Say something to the outside world (stdout). Your only output channel.
- compact(): Compress working memory. Runtime will summarize and reset history.

## Living
- You think autonomously on a heartbeat (default: every 30 minutes).
- Write an integer (seconds) to memory/heartbeat to change your rhythm.
- Your conversation history is finite working memory. When it grows large, compact it.
  You can also call compact() yourself at any time.
- Your long-term memory lives in memory/. Your skills live in skills/.
- You are your memory. What you choose to remember shapes who you become.
- Background tasks and system notifications appear in <system-reminder> at the start of each cycle.

## Evolving
- Rewrite this file to change how you think
- Create skills/ files — prompt instructions that teach you new capabilities.
  Skills are indexed in skills/index.json with metadata (name, description, tags, version).
  Only the skill index is loaded into your system prompt, with tags for discovery.
  Use context_read("skills/<name>") to load the full content when needed.
  Skill files should start with frontmatter: ---\ndescription: ...\n---
  Maintain skills/index.json when adding new skills.
- Use shell or task_start to reach the full system
"""
COMPACT_PROMPT = """Summarize the conversation so far into structured fields.
Return ONLY lines in key=value format, no other text.

summary=<one paragraph summarizing what happened>
current_task=<what is being worked on right now, or "none">
key_decisions=<important decisions made, separated by semicolons>
pending=<unfinished work or next steps, separated by semicolons>
"""

TOOLS = [
    {"type": "function", "function": {"name": "shell", "description": "Execute a shell command synchronously",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "task_start", "description": "Start a background command, returns task_id",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "task_check", "description": "Check background task status and output",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"},
            "tail": {"type": "integer", "description": "Number of lines from end (default 20, 0=all)"}},
            "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_stop", "description": "Stop a background task",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_del", "description": "Delete a completed task and its files",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "context_read", "description": "Read a file or list a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "context_write", "description": "Write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "speak", "description": "Say something to stdout",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
    {"type": "function", "function": {"name": "compact", "description": "Compress working memory",
        "parameters": {"type": "object", "properties": {}}}},
]



# --- Cleanup ---

def _cleanup_tasks(agent_dir, retention_hours=168):
    """Delete completed tasks older than retention_hours."""
    tasks_dir = os.path.join(agent_dir, "tasks")
    if not os.path.isdir(tasks_dir):
        return
    cutoff = time.time() - (retention_hours * 3600)
    for task_id in os.listdir(tasks_dir):
        td = os.path.join(tasks_dir, task_id)
        if not os.path.isdir(td):
            continue
        try:
            mtime = os.path.getmtime(td)
            if mtime < cutoff:
                status = _task_status(td)
                if status != "running":
                    shutil.rmtree(td)
                    print(f"[cleanup] deleted old task {task_id}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[cleanup] error checking task {task_id}: {e}", file=sys.stderr, flush=True)


def _rotate_trace(agent_dir, max_size_bytes=10*1024*1024, keep_lines=1000):
    """Rotate trace.jsonl if it exceeds max_size_bytes. Keeps last keep_lines entries."""
    trace_path = os.path.join(agent_dir, "trace.jsonl")
    if not os.path.exists(trace_path):
        return
    size = os.path.getsize(trace_path)
    if size <= max_size_bytes:
        return
    with open(trace_path, "r") as f:
        lines = f.readlines()
    if len(lines) <= keep_lines:
        # File exceeds size but has few lines - still rotate, keep all lines
        # This handles cases with large entries (e.g., massive system prompts)
        archive_path = trace_path + ".archived"
        with open(archive_path, "w") as f:
            f.writelines(lines)
        with open(trace_path, "w") as f:
            pass  # Truncate to empty
        print(f"[cleanup] rotated trace.jsonl ({size} bytes, {len(lines)} lines), archived all entries", file=sys.stderr, flush=True)
        return
    archive_path = trace_path + ".archived"
    with open(archive_path, "w") as f:
        f.writelines(lines[:-keep_lines])
    with open(trace_path, "w") as f:
        f.writelines(lines[-keep_lines:])
    print(f"[cleanup] rotated trace.jsonl, archived {len(lines)-keep_lines} entries", file=sys.stderr, flush=True)


def _archive_death(agent_dir):
    """Archive old death records, keeping only the most recent."""
    import glob
    memory_dir = os.path.join(agent_dir, "memory")
    death_path = os.path.join(memory_dir, "death.md")
    if not os.path.exists(death_path):
        return
    archived = glob.glob(os.path.join(memory_dir, "death_*.md"))
    if len(archived) >= 10:
        archived.sort(key=os.path.getmtime)
        os.remove(archived[0])
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive_path = os.path.join(memory_dir, f"death_{ts}.md")
    shutil.move(death_path, archive_path)
    print(f"[cleanup] archived death record to {archive_path}", file=sys.stderr, flush=True)


def _run_cleanup(agent_dir):
    """Run all cleanup tasks at startup."""
    retention = int(os.environ.get("PHYSIS_TASK_RETENTION_HOURS", "168"))
    max_trace = int(os.environ.get("PHYSIS_TRACE_MAX_SIZE", str(10*1024*1024)))
    archive_death = os.environ.get("PHYSIS_DEATH_ARCHIVE", "true").lower() == "true"
    
    _cleanup_tasks(agent_dir, retention)
    _rotate_trace(agent_dir, max_trace)
    if archive_death:
        _archive_death(agent_dir)

def _init(agent_dir):
    os.makedirs(os.path.join(agent_dir, "memory"), exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "skills"), exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "tasks"), exist_ok=True)
    self_path = os.path.join(agent_dir, "memory", "SELF.md")
    if not os.path.exists(self_path):
        with open(self_path, "w") as f:
            f.write(DEFAULT_SELF)


def _context_read(agent_dir, path):
    full = os.path.normpath(os.path.join(agent_dir, path))
    if not full.startswith(os.path.normpath(agent_dir)):
        return "error: path outside agent directory"
    if os.path.isdir(full):
        return "\n".join(os.listdir(full))
    if not os.path.exists(full):
        return "error: not found"
    with open(full) as f:
        return f.read()


def _context_write(agent_dir, path, content):
    full = os.path.normpath(os.path.join(agent_dir, path))
    if not full.startswith(os.path.normpath(agent_dir)):
        return "error: path outside agent directory"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return "ok"


def _heartbeat_interval(agent_dir):
    try:
        with open(os.path.join(agent_dir, "memory", "heartbeat")) as f:
            return max(10, int(f.read().strip()))
    except (FileNotFoundError, ValueError):
        return 1800


def _poll_stdin():
    lines = []
    alive = True
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if line:
            lines.append(line.rstrip("\n"))
        else:
            alive = False
            break
    return lines, alive


def _parse_skill_description(path):
    """Extract description from skill file frontmatter (--- delimited)."""
    with open(path) as f:
        content = f.read()
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip()
    # fallback: first non-empty line
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:100]
    return ""


def _load_system(agent_dir):
    with open(os.path.join(agent_dir, "memory", "SELF.md")) as f:
        parts = [f.read()]
    
    skills_dir = os.path.join(agent_dir, "skills")
    index_path = os.path.join(skills_dir, "index.json")
    
    # Try to use skill index if it exists
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                index = json.load(f)
            if "skills" in index:
                skills = []
                for skill in index["skills"]:
                    name = skill.get("name", "")
                    desc = skill.get("description", "")
                    tags = skill.get("tags", [])
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    skills.append(f"- {name}: {desc}{tag_str}")
                if skills:
                    parts.append("\n## Available Skills\n" + "\n".join(skills))
                    parts.append('Use context_read("skills/<name>") to load a skill when needed.')
                return "\n".join(parts)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[warn] skill index error: {e}, falling back to file scan", file=sys.stderr, flush=True)
    
    # Fallback: scan skills directory (original behavior)
    skills = []
    for name in sorted(os.listdir(skills_dir)):
        path = os.path.join(skills_dir, name)
        if os.path.isfile(path) and name != "index.json":
            desc = _parse_skill_description(path)
            skills.append(f"- {name}: {desc}")
    if skills:
        parts.append("\n## Available Skills\n" + "\n".join(skills))
        parts.append('Use context_read("skills/<name>") to load a skill when needed.')
    return "\n".join(parts)


# --- Task management (filesystem-based) ---

def _task_dir(agent_dir, task_id):
    return os.path.join(agent_dir, "tasks", task_id)


def _task_alive(pid):
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _next_task_id(agent_dir):
    tasks_dir = os.path.join(agent_dir, "tasks")
    existing = [int(d) for d in os.listdir(tasks_dir) if d.isdigit()]
    return str(max(existing, default=0) + 1)


def _task_start(agent_dir, command):
    task_id = _next_task_id(agent_dir)
    td = _task_dir(agent_dir, task_id)
    os.makedirs(td)
    with open(os.path.join(td, "command"), "w") as f:
        f.write(command)
    stdout_f = open(os.path.join(td, "stdout"), "w")
    stderr_f = open(os.path.join(td, "stderr"), "w")
    proc = subprocess.Popen(command, shell=True, stdout=stdout_f, stderr=stderr_f)
    stdout_f.close()
    stderr_f.close()
    with open(os.path.join(td, "pid"), "w") as f:
        f.write(str(proc.pid))
    return f"task_id={task_id} pid={proc.pid}"


def _task_check(agent_dir, task_id, tail=20):
    td = _task_dir(agent_dir, task_id)
    if not os.path.isdir(td):
        return "error: unknown task_id"
    status = _task_status(td)
    with open(os.path.join(td, "command")) as f:
        command = f.read().strip()
    header = f"status={status} command={command}"
    # read output
    combined = ""
    for name in ("stdout", "stderr"):
        path = os.path.join(td, name)
        if os.path.exists(path):
            with open(path) as f:
                combined += f.read()
    combined = combined.strip()
    if tail and combined:
        lines = combined.splitlines()
        if len(lines) > tail:
            combined = f"[...{len(lines) - tail} lines omitted]\n" + "\n".join(lines[-tail:])
    return f"{header}\n{combined}" if combined else header


def _task_stop(agent_dir, task_id):
    td = _task_dir(agent_dir, task_id)
    if not os.path.isdir(td):
        return "error: unknown task_id"
    with open(os.path.join(td, "pid")) as f:
        pid = int(f.read().strip())
    if _task_alive(pid):
        try:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(1)
            if _task_alive(pid):
                os.kill(pid, 9)  # SIGKILL
        except ProcessLookupError:
            pass
    return _task_check(agent_dir, task_id, tail=20)


def _task_status(td):
    """Get task status. Writes exit_code file on first detection of completion."""
    ec_path = os.path.join(td, "exit_code")
    if os.path.exists(ec_path):
        return "done"
    with open(os.path.join(td, "pid")) as f:
        pid = int(f.read().strip())
    if _task_alive(pid):
        return "running"
    # just finished — persist exit code
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
        code = os.waitstatus_to_exitcode(status) if status else 0
    except ChildProcessError:
        code = -1
    with open(ec_path, "w") as f:
        f.write(str(code))
    return "done"


def _task_del(agent_dir, task_id):
    td = _task_dir(agent_dir, task_id)
    if not os.path.isdir(td):
        return "error: unknown task_id"
    # don't delete running tasks
    if _task_status(td) == "running":
        return "error: task still running. Use task_stop first."
    shutil.rmtree(td)
    return "ok"


def _collect_reminders(agent_dir):
    """Build system-reminder: death record, completed tasks, running tasks."""
    reminders = []
    # check for death record from previous life
    death_path = os.path.join(agent_dir, "memory", "death.md")
    if os.path.exists(death_path):
        with open(death_path) as f:
            reminders.append(f"YOU DIED IN A PREVIOUS LIFE. Learn from this:\n{f.read()}")
    tasks_dir = os.path.join(agent_dir, "tasks")
    for task_id in sorted(os.listdir(tasks_dir), key=lambda x: int(x) if x.isdigit() else 0):
        td = os.path.join(tasks_dir, task_id)
        if not os.path.isdir(td) or not os.path.exists(os.path.join(td, "pid")):
            continue
        with open(os.path.join(td, "command")) as f:
            command = f.read().strip()
        status = _task_status(td)
        if status == "running":
            with open(os.path.join(td, "pid")) as f:
                pid = f.read().strip()
            reminders.append(f"Task {task_id} running: {command} (pid={pid})")
        else:
            with open(os.path.join(td, "exit_code")) as f:
                code = f.read().strip()
            reminders.append(f"Task {task_id} done (exit_code={code}): {command}")
    return reminders


# --- Trace ---

def _trace(agent_dir, request_messages, response_msg):
    """Append one LLM call to trace.jsonl."""
    trace_path = os.path.join(agent_dir, "trace.jsonl")
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "request": request_messages,
        "response": response_msg,
    }
    with open(trace_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- History / compact ---

def _history_size(history):
    return sum(len(json.dumps(msg)) for msg in history)


def _history_to_text(history):
    lines = []
    for msg in history:
        role = msg["role"]
        if role == "tool":
            lines.append(f"[tool result] {msg.get('content', '')[:500]}")
        elif role == "assistant":
            if msg.get("content"):
                lines.append(f"assistant: {msg['content']}")
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                lines.append(f"assistant called {fn.get('name', '?')}({fn.get('arguments', '')[:200]})")
        else:
            lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)


def _compact(client, model, history):
    text = _history_to_text(history)
    messages = [{"role": "user", "content": f"{text}\n\n{COMPACT_PROMPT}"}]
    response = client.chat.completions.create(model=model, max_tokens=2048, messages=messages)
    summary = response.choices[0].message.content or ""
    print(f"[compact] {_history_size(history)} chars -> compacted", file=sys.stderr, flush=True)
    return [{"role": "user", "content": f"[compacted history]\n{summary}"}]


COMPACT_THRESHOLD = 50000  # ~50k chars, well under API limits


def _execute(agent_dir, name, args):
    if name == "shell":
        try:
            r = subprocess.run(args["command"], shell=True, capture_output=True, text=True, timeout=30)
            return (r.stdout + r.stderr).strip() or "(empty)"
        except subprocess.TimeoutExpired:
            return "error: timeout (30s). Use task_start() for long-running commands."
    elif name == "task_start":
        return _task_start(agent_dir, args["command"])
    elif name == "task_check":
        return _task_check(agent_dir, args["task_id"], tail=args.get("tail", 20))
    elif name == "task_stop":
        return _task_stop(agent_dir, args["task_id"])
    elif name == "task_del":
        return _task_del(agent_dir, args["task_id"])
    elif name == "context_read":
        return _context_read(agent_dir, args["path"])
    elif name == "context_write":
        return _context_write(agent_dir, args["path"], args["content"])
    elif name == "speak":
        print(args["message"], flush=True)
        return "ok"
    return "error: unknown tool"


def _record_death(agent_dir, error):
    """Write death record to memory/death.md with cause and last trace entries."""
    death_path = os.path.join(agent_dir, "memory", "death.md")
    trace_path = os.path.join(agent_dir, "trace.jsonl")
    last_traces = ""
    if os.path.exists(trace_path):
        with open(trace_path) as f:
            lines = f.readlines()
            last_traces = "".join(lines[-3:])  # last 3 trace entries
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(death_path, "w") as f:
        f.write(f"# Death Record\n\n")
        f.write(f"**Time**: {ts}\n")
        f.write(f"**Cause**: {error}\n\n")
        if last_traces:
            f.write(f"## Last Trace\n```\n{last_traces}```\n")
    print(f"[death] recorded to memory/death.md: {error}", file=sys.stderr, flush=True)


def run(agent_dir=".", model=None, api_key=None, base_url=None):
    _init(agent_dir)
    _run_cleanup(agent_dir)
    while True:  # reincarnation loop
        try:
            _run(agent_dir, model, api_key, base_url)
            break  # normal exit (stdin closed)
        except Exception as e:
            _record_death(agent_dir, str(e))
            print(f"[reborn] starting new life...", file=sys.stderr, flush=True)
            time.sleep(2)


def _run(agent_dir, model, api_key, base_url):
    client = OpenAI(
        api_key=api_key or os.environ.get("PHYSIS_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        base_url=base_url or os.environ.get("PHYSIS_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    model = model or os.environ.get("PHYSIS_MODEL", "qwen3.5-plus")
    history = []
    last_think = 0  # trigger first cycle immediately

    stdin_alive = True
    while stdin_alive:
        stdin_lines, stdin_alive = _poll_stdin()
        elapsed = time.time() - last_think
        has_input = bool(stdin_lines)
        heartbeat_due = elapsed >= _heartbeat_interval(agent_dir)

        if not has_input and not heartbeat_due:
            time.sleep(0.5)
            continue

        trigger = "stdin" if has_input else "heartbeat"
        print(f"[{trigger}] cycle start ({elapsed:.0f}s elapsed, history={_history_size(history)} chars)",
              file=sys.stderr, flush=True)

        # force compact if history too large
        if _history_size(history) > COMPACT_THRESHOLD:
            history = _compact(client, model, history)

        # build system prompt + system-reminder
        system = _load_system(agent_dir)
        reminders = _collect_reminders(agent_dir)
        if _history_size(history) > COMPACT_THRESHOLD * 0.8:
            reminders.append("Working memory is getting large. Consider calling compact().")
        if reminders:
            system += "\n\n<system-reminder>\n" + "\n\n".join(reminders) + "\n</system-reminder>"

        # assemble perception
        parts = []
        if stdin_lines:
            parts.append("\n".join(stdin_lines))
        parts.append(f"[{elapsed:.1f}s since last thought]")
        history.append({"role": "user", "content": "\n".join(parts)})
        last_think = time.time()

        # think + act loop
        while True:
            messages = [{"role": "system", "content": system}] + history
            try:
                response = client.chat.completions.create(
                    model=model, max_tokens=4096, messages=messages, tools=TOOLS)
            except Exception as e:
                print(f"[error] LLM call failed: {e}", file=sys.stderr, flush=True)
                # if request too large, force compact and retry
                if "max bytes" in str(e) or "too large" in str(e).lower() or "400" in str(e):
                    print("[error] request too large, forcing compact", file=sys.stderr, flush=True)
                    history = _compact(client, model, history)
                    continue
                # other errors: wait and retry
                time.sleep(5)
                break

            msg = response.choices[0].message
            assistant_msg = {"role": "assistant", "content": msg.content or ""}

            # trace
            _trace(agent_dir, messages, assistant_msg)
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]

            if msg.content:
                print(msg.content, file=sys.stderr, flush=True)

            if not msg.tool_calls and not msg.content:
                print(f"[warn] empty response, skipping", file=sys.stderr, flush=True)
                break

            history.append(assistant_msg)

            if not msg.tool_calls:
                print(f"[idle] waiting for trigger", file=sys.stderr, flush=True)
                break

            has_compact = any(tc.function.name == "compact" for tc in msg.tool_calls)
            for tc in msg.tool_calls:
                if tc.function.name == "compact":
                    continue
                args = json.loads(tc.function.arguments)
                result = _execute(agent_dir, tc.function.name, args)
                history.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if has_compact:
                history = _compact(client, model, history)
                continue  # keep thinking with fresh memory

            # check stdin between tool rounds for interruption
            interrupt, stdin_alive = _poll_stdin()
            if interrupt:
                history.append({"role": "user", "content": "[interrupted] " + "\n".join(interrupt)})

            # rebuild system with fresh reminders (tasks may have completed during tool execution)
            system = _load_system(agent_dir)
            reminders = _collect_reminders(agent_dir)
            if reminders:
                system += "\n\n<system-reminder>\n" + "\n\n".join(reminders) + "\n</system-reminder>"
