import html as html_lib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).parent / "static"

ACTIVE_JOB_STATUSES = ("pending", "denoising")
TERMINAL_JOB_STATUSES = ("completed", "error", "cancelled")
MAX_TERMINAL_JOBS = 8
LIVE_SESSION_WINDOW = 30.0
BLANK_TITLES = ("Blank Seed Canvas", "MindFusion Workspace", "Untitled Diffusion")


DEFAULT_AGENT_NAME = "Agent"

# Headless agent CLIs MindFusion can fall back to when no live agent session is listening.
# Each entry: (display name, binary, argv builder taking (binary, prompt)).
_KNOWN_AGENT_CLIS = [
    ("Claude Code", "claude", lambda b, p: [b, "-p", p]),
    ("Codex CLI", "codex", lambda b, p: [b, "exec", p]),
    ("Gemini CLI", "gemini", lambda b, p: [b, "-p", p]),
    ("Antigravity CLI", "agy", lambda b, p: [b, "--disable-slash-commands", "--print-timeout", "170s", f"--print={p}"]),
]


def _which(binary: str) -> Optional[str]:
    found = shutil.which(binary)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / binary
    return str(fallback) if fallback.exists() else None


def find_agent_cli() -> Optional[Dict[str, Any]]:
    """Resolve the fallback agent CLI.

    `MINDFUSION_AGENT_CMD` wins (e.g. "claude -p", "codex exec", "my-agent --print"); the prompt is
    appended as the final argument. Otherwise the first known agent CLI found on PATH is used.
    """
    custom = os.environ.get("MINDFUSION_AGENT_CMD", "").strip()
    if custom:
        parts = shlex.split(custom)
        return {"name": f"Custom agent ({parts[0]})", "path": custom, "argv": lambda p: parts + [p]}
    for name, binary, build in _KNOWN_AGENT_CLIS:
        path = _which(binary)
        if path:
            return {"name": name, "path": path, "argv": (lambda b, f: (lambda p: f(b, p)))(path, build)}
    return None


_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "table", "thead", "tbody", "tr", "td", "th", "section", "article", "aside",
    "header", "footer", "details", "summary", "figure", "figcaption", "hr", "dl", "dt", "dd",
}
_COMMENT_PREFIXES = ("//", ">>")


def is_html(text: str) -> bool:
    return bool(re.search(r"</?[a-z][^>]*>", text or "", flags=re.IGNORECASE))


def html_to_lines(content: str) -> str:
    """Flatten an HTML fragment to plain text with one line per block (keeps markdown-ish markers)."""
    if not is_html(content):
        return content or ""
    text = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    text = re.sub(r"</?(p|div|li|h[1-6]|blockquote|pre|tr|ul|ol|table|section|article|details|summary)(\s[^>]*)?>", "\n", text, flags=re.IGNORECASE)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    return "\n".join(ln.rstrip() for ln in text.splitlines() if ln.strip())


def markdown_to_html(md: str) -> str:
    """Tiny markdown -> HTML converter for the offline synthesizer (headings, lists, emphasis, paragraphs)."""
    if is_html(md):
        return md

    def inline(t: str) -> str:
        t = html_lib.escape(t, quote=False)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)

    out: List[str] = []
    list_open = False
    for raw in md.splitlines():
        line = raw.strip()
        bullet = re.match(r"^[-*•]\s+(.*)", line)
        if bullet:
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
            continue
        if list_open:
            out.append("</ul>")
            list_open = False
        if not line:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading:
            lvl = len(heading.group(1))
            out.append(f"<h{lvl}>{inline(heading.group(2))}</h{lvl}>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if list_open:
        out.append("</ul>")
    return "\n".join(out)


def extract_inline_comments(content: str) -> Tuple[List[Dict[str, Any]], str]:
    """Pull `// note` / `>> note` lines out of canvas content (HTML or plain text).

    Returns (comments, cleaned_content). Each comment is anchored to the nearest preceding
    line of real content. Lines inside <pre> blocks are left alone so code comments survive.
    """
    if not content or not content.strip():
        return [], content or ""
    if not is_html(content):
        return DiffusionEngine._extract_inline_canvas_comments(content)

    tokens = re.split(r"(<[^>]+>)", content)
    comments: List[Dict[str, Any]] = []
    line: List[int] = []  # token indices belonging to the current visual line
    last_text = ""
    pre_depth = 0

    def flush(br_index: Optional[int] = None) -> None:
        nonlocal last_text
        text_idx = [i for i in line if not tokens[i].startswith("<")]
        text = html_lib.unescape("".join(tokens[i] for i in text_idx)).replace("\xa0", " ").strip()
        if pre_depth == 0 and text.startswith(_COMMENT_PREFIXES):
            note = text[2:].strip()
            if note:
                comments.append({
                    "id": f"inline-{uuid.uuid4().hex[:6]}",
                    "quote": last_text[:120],
                    "comment": note,
                    "source": "inline",
                    "resolved": False,
                })
            for i in line:
                tokens[i] = ""
            if br_index is not None:
                tokens[br_index] = ""
        elif text:
            last_text = text
        line.clear()

    for idx, tok in enumerate(tokens):
        if not tok:
            continue
        m = re.match(r"<\s*(/?)\s*([a-zA-Z0-9]+)", tok)
        if not m:
            line.append(idx)
            continue
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name == "br":
            flush(br_index=idx)
        elif name in _BLOCK_TAGS:
            flush()
            if name == "pre":
                pre_depth = max(0, pre_depth + (-1 if closing else 1))
        else:
            line.append(idx)  # inline tag (strong, em, span, a...) stays with its line
    flush()

    if not comments:
        return [], content
    cleaned = "".join(tokens)
    # Drop blocks that are now empty because their only content was a comment.
    empty_block = re.compile(r"<(p|div|li|h[1-6])(\s[^>]*)?>\s*(<br\s*/?>\s*)*</\1>", flags=re.IGNORECASE)
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = empty_block.sub("", cleaned)
    return comments, cleaned.strip()


class StateStore:
    """Manages persistent document folders, each with its own version tree and saved HTML versions."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.base_dir = self.state_path.parent
        if self.base_dir.name == ".mindfusion":
            self.documents_dir = self.base_dir.parent / "diffusions"
        else:
            self.documents_dir = self.base_dir / "diffusions"
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.documents_dir / "index.json"
        self.lock = threading.RLock()
        self.state_version = 1
        self.cond = threading.Condition(self.lock)
        self.server_id = uuid.uuid4().hex[:8]  # lets clients notice a restart (state_version resets)
        self._written_html: Dict[str, str] = {}
        self._doc_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._backends_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        with self.lock:
            self.doc_id, self.doc_dir, self.data = self._boot_storage()

    def wait_for_pending_job(self, timeout: float = 300.0, agent_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Blocks until at least one job is pending (or timeout expires), registering active session connection."""
        deadline = time.time() + timeout
        with self.cond:
            if agent_name:
                self.data["agent_status"]["agent_name"] = agent_name
            while True:
                self.data["agent_status"]["last_seen"] = time.time()
                self.data["agent_status"]["connected"] = True
                self.data["agent_status"]["mode"] = "live_session"
                pending = [
                    j for j in self.data.get("jobs", {}).values()
                    if j.get("status") == "pending" and j.get("provider_used") != "claimed_by_live_session"
                ]
                if pending:
                    for j in pending:
                        j["provider_used"] = "claimed_by_live_session"
                        j["claimed_at"] = time.time()
                        j["stage_label"] = "Agent is writing…"
                    self._save_unlocked()
                    return json.loads(json.dumps(pending))
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self.cond.wait(timeout=min(remaining, 10.0))

    def wait_for_change(self, since: int, timeout: float) -> bool:
        """Long-poll helper: blocks until state_version moves past `since` or timeout. Returns True if changed."""
        deadline = time.time() + max(0.0, min(timeout, 55.0))
        with self.cond:
            while self.state_version == since:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(timeout=remaining)
            return True

    def agent_summary(self) -> Dict[str, Any]:
        """What the UI status dot needs: is a live agent listening, and which fallback backend exists."""
        with self.lock:
            status = self.data.get("agent_status", {})
            live = (
                status.get("mode") in ("live_session", "manual_bridge")
                and (time.time() - status.get("last_seen", 0)) < LIVE_SESSION_WINDOW
            )
            backends = self._detect_backends()
            fallback = [b["name"] for k, b in backends.items() if k != "agent_bridge" and b["available"]]
            return {"live_session": live, "fallback_backends": fallback}

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = self.data["jobs"].get(job_id)
            return json.loads(json.dumps(job)) if job else {}

    @staticmethod
    def _slugify(text: str) -> str:
        s = re.sub(r"<[^>]+>", "", text or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
        return s[:36] or "untitled"

    def _boot_storage(self) -> tuple[str, Path, Dict[str, Any]]:
        # Check index.json for active document folder
        active_folder_name = None
        if self.index_path.exists():
            try:
                idx = json.loads(self.index_path.read_text(encoding="utf-8"))
                active_folder_name = idx.get("active_doc_id")
            except Exception:
                pass

        if active_folder_name:
            cand_dir = self.documents_dir / active_folder_name
            if (cand_dir / "tree.json").exists():
                self.state_path = cand_dir / "tree.json"
                data = self._load_or_init(doc_id=active_folder_name, doc_dir=cand_dir)
                return active_folder_name, cand_dir, data

        # If legacy .mindfusion/state.json exists, migrate it into its own folder inside diffusions/
        if self.state_path.exists() and self.state_path.name == "state.json":
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if "nodes" in raw and "root_id" in raw:
                    active_node = raw["nodes"].get(raw.get("active_node_id"), {})
                    title = active_node.get("title") or raw.get("project_title") or "diffusion"
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    doc_id = f"{ts}-{self._slugify(title)}-{uuid.uuid4().hex[:4]}"
                    doc_dir = self.documents_dir / doc_id
                    doc_dir.mkdir(parents=True, exist_ok=True)
                    raw["doc_id"] = doc_id
                    raw["folder_path"] = str(doc_dir)
                    self.state_path = doc_dir / "tree.json"
                    self.doc_id = doc_id
                    self.doc_dir = doc_dir
                    self._save_unlocked(raw)
                    self._write_index(doc_id)
                    return doc_id, doc_dir, raw
            except Exception:
                pass

        # Otherwise create a fresh document folder
        return self._create_new_doc_unlocked()

    def _create_new_doc_unlocked(self, title: str = "Untitled Diffusion") -> tuple[str, Path, Dict[str, Any]]:
        ts = time.strftime("%Y%m%d-%H%M%S")
        doc_id = f"{ts}-{self._slugify(title)}-{uuid.uuid4().hex[:4]}"
        doc_dir = self.documents_dir / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        self.doc_id = doc_id
        self.doc_dir = doc_dir
        self.state_path = doc_dir / "tree.json"
        data = self._init_blank_tree(doc_id=doc_id, doc_dir=doc_dir, title=title)
        self._save_unlocked(data)
        self._write_index(doc_id)
        return doc_id, doc_dir, data

    def _write_index(self, active_doc_id: str) -> None:
        self.index_path.write_text(
            json.dumps({"active_doc_id": active_doc_id, "updated_at": time.time()}, indent=2),
            encoding="utf-8",
        )

    def _load_or_init(self, doc_id: Optional[str] = None, doc_dir: Optional[Path] = None) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if "nodes" in raw and "root_id" in raw:
                    raw.setdefault("jobs", {})
                    # A job that was mid-flight when the server stopped will never finish; don't let it
                    # lock the UI in "Diffusing…" forever.
                    for job in raw["jobs"].values():
                        if job.get("status") in ACTIVE_JOB_STATUSES:
                            job["status"] = "error"
                            job["error"] = "Interrupted: the server restarted before this diffusion finished."
                            job["stage_label"] = "Interrupted"
                            job["updated_at"] = time.time()
                    raw.setdefault("doc_id", doc_id or getattr(self, "doc_id", "default"))
                    raw.setdefault("folder_path", str(doc_dir or self.state_path.parent))
                    raw.setdefault("agent_status", {
                        "connected": False,
                        "mode": "auto",
                        "last_seen": 0,
                        "agent_name": DEFAULT_AGENT_NAME,
                    })
                    return raw
            except Exception as e:
                print(f"[MindFusion] Warning: Could not load state file, initializing fresh: {e}", file=sys.stderr)

        return self._init_blank_tree(
            doc_id=doc_id or getattr(self, "doc_id", "default"),
            doc_dir=doc_dir or self.state_path.parent,
        )

    def _init_blank_tree(self, doc_id: str, doc_dir: Path, title: str = "Blank Seed Canvas") -> Dict[str, Any]:
        root_id = "v0-root"
        now = time.time()
        initial_node = {
            "id": root_id,
            "parent_id": None,
            "version_num": 0,
            "label": "v0 · Blank Canvas",
            "title": title,
            "seed_outline": "",
            "content": "",
            "comments": [],
            "params": {
                "strength": 0.75,
                "guidance_scale": 7.5,
                "steps": 3,
                "style_preset": "Polished & Structured",
                "negative_prompt": "",
                "seed": 42,
                "provider": "auto",
            },
            "diffusion_steps": [],
            "diff_summary": "Initial blank editable canvas",
            "source_mode": "init",
            "created_at": now,
        }
        return {
            "doc_id": doc_id,
            "folder_path": str(doc_dir),
            "project_title": title,
            "root_id": root_id,
            "active_node_id": root_id,
            "next_version_num": 1,
            "nodes": {root_id: initial_node},
            "jobs": {},
            "agent_status": {
                "connected": False,
                "mode": "auto",
                "last_seen": 0,
                "agent_name": DEFAULT_AGENT_NAME,
            },
        }

    def _save_unlocked(self, data: Optional[Dict[str, Any]] = None) -> None:
        target = data if data is not None else self.data
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(target, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

        # Also write each version's HTML file & latest.html inside this document's folder.
        # Only touch files whose content actually changed (autosave fires on every pause in typing).
        doc_folder = self.state_path.parent
        active_id = target.get("active_node_id")

        def write_if_changed(path: Path, body: str) -> None:
            key = str(path)
            if self._written_html.get(key) == body and path.exists():
                return
            path.write_text(body, encoding="utf-8")
            self._written_html[key] = body

        for node in target.get("nodes", {}).values():
            vnum = node.get("version_num", 0)
            body_html = (node.get("content") or node.get("seed_outline") or "").strip()
            if body_html or vnum == 0:
                write_if_changed(doc_folder / f"v{vnum}.html", body_html)
            if node.get("id") == active_id:
                shown = node.get("draft") if node.get("draft") is not None else body_html
                write_if_changed(doc_folder / "latest.html", (shown or "").strip())

        self.state_version += 1
        self.cond.notify_all()

    @staticmethod
    def _node_display_html(node: Dict[str, Any]) -> str:
        if node.get("draft") is not None:
            return node["draft"]
        return node.get("content") or node.get("seed_outline") or ""

    def _summarize_doc(self, folder: Path, raw: Dict[str, Any]) -> Dict[str, Any]:
        nodes = raw.get("nodes", {})
        act_node = nodes.get(raw.get("active_node_id")) or {}
        raw_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", self._node_display_html(act_node)))
        preview = re.sub(r"\s+", " ", raw_text).strip()[:120]
        title = act_node.get("title") or raw.get("project_title") or folder.name
        if title in BLANK_TITLES and preview:
            title = preview[:48]
        return {
            "doc_id": folder.name,
            "folder_path": str(folder),
            "title": title,
            "preview": preview or "(Blank canvas)",
            "is_blank": len(nodes) <= 1 and not preview,
            "version_count": len(nodes),
            "active_version_num": act_node.get("version_num", 0),
        }

    def list_documents(self) -> List[Dict[str, Any]]:
        with self.lock:
            docs: List[Dict[str, Any]] = []
            if not self.documents_dir.exists():
                return docs
            for folder in self.documents_dir.iterdir():
                tree_file = folder / "tree.json"
                if not folder.is_dir() or not tree_file.exists():
                    continue
                try:
                    mtime = tree_file.stat().st_mtime
                    cached = self._doc_cache.get(folder.name)
                    if cached and cached[0] == mtime:
                        summary = cached[1]
                    else:
                        raw = json.loads(tree_file.read_text(encoding="utf-8"))
                        summary = self._summarize_doc(folder, raw)
                        self._doc_cache[folder.name] = (mtime, summary)
                    is_current = folder.name == self.doc_id
                    # Untouched blank docs (from tapping "+" repeatedly) are noise in the picker.
                    if summary["is_blank"] and not is_current:
                        continue
                    docs.append({**summary, "updated_at": mtime, "is_current": is_current})
                except Exception:
                    continue
            docs.sort(key=lambda d: d["updated_at"], reverse=True)
            return docs

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            now = time.time()
            last_seen = self.data["agent_status"].get("last_seen", 0)
            self.data["agent_status"]["connected"] = (now - last_seen) < 30 or self._has_direct_backends()
            self.data["agent_status"]["backends"] = self._detect_backends()
            snap = json.loads(json.dumps(self.data))
            snap["doc_id"] = self.doc_id
            snap["folder_path"] = str(self.doc_dir)
            snap["documents"] = self.list_documents()
            snap["state_version"] = self.state_version
            snap["agent"] = self.agent_summary()
            snap["server_id"] = self.server_id
            return snap

    def _has_direct_backends(self) -> bool:
        backends = self._detect_backends()
        return any(b["available"] for b in backends.values())

    def _detect_backends(self) -> Dict[str, Any]:
        cached_at, cached = self._backends_cache
        if time.time() - cached_at < 15 and cached:
            cli = cached["_cli"]
        else:
            cli = find_agent_cli()
        last_seen = self.data["agent_status"].get("last_seen", 0)
        bridge_alive = (time.time() - last_seen) < LIVE_SESSION_WINDOW
        agent_name = self.data["agent_status"].get("agent_name") or DEFAULT_AGENT_NAME
        result = {
            "agent_bridge": {
                "name": f"Live session ({agent_name})",
                "available": bridge_alive,
                "detail": "Connected via live agent session" if bridge_alive else "Standby (use respond_job.py or auto-fallback)",
            },
            "agent_cli": {
                "name": cli["name"] if cli else "Agent CLI",
                "available": bool(cli),
                "detail": cli["path"] if cli else "No agent CLI found (set MINDFUSION_AGENT_CMD)",
            },
            "gemini_api": {
                "name": "Gemini API",
                "available": bool(os.environ.get("GEMINI_API_KEY")),
                "detail": "GEMINI_API_KEY detected" if os.environ.get("GEMINI_API_KEY") else "Not configured",
            },
            "openai_api": {
                "name": "OpenAI API",
                "available": bool(os.environ.get("OPENAI_API_KEY")),
                "detail": "OPENAI_API_KEY detected" if os.environ.get("OPENAI_API_KEY") else "Not configured",
            },
        }
        if time.time() - cached_at >= 15 or not cached:
            self._backends_cache = (time.time(), {**result, "_cli": cli})
        return result

    def register_agent_heartbeat(self, agent_name: str = DEFAULT_AGENT_NAME, mode: str = "agent_bridge") -> Dict[str, Any]:
        with self.lock:
            status = self.data["agent_status"]
            live_now = status.get("mode") == "live_session" and (time.time() - status.get("last_seen", 0)) < LIVE_SESSION_WINDOW
            status["last_seen"] = time.time()
            status["connected"] = True
            status["agent_name"] = agent_name
            # `status`/`push` heartbeats from the same agent must not knock a listening session offline.
            if not live_now:
                status["mode"] = mode
            self._save_unlocked()
            return self.data["agent_status"]

    def update_node(self, node_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if node_id not in self.data["nodes"]:
                raise KeyError(f"Node {node_id} not found")
            node = self.data["nodes"][node_id]
            is_root = node_id == self.data.get("root_id")
            for k in ("title", "label", "comments", "params"):
                if k in updates:
                    node[k] = updates[k]
            if is_root:
                # v0 is the user's own seed page: edits go straight into it.
                for k in ("seed_outline", "content"):
                    if k in updates:
                        node[k] = updates[k]
            elif "content" in updates:
                # Generated versions stay immutable; hand edits live in a draft layered on top,
                # which feeds the next diffusion or can be saved as its own version.
                if (updates["content"] or "").strip() == (node.get("content") or "").strip():
                    node.pop("draft", None)
                else:
                    node["draft"] = updates["content"]
            if updates.get("discard_draft"):
                node.pop("draft", None)
            if "active" in updates and updates["active"]:
                self.data["active_node_id"] = node_id
            self._save_unlocked()
            return node

    def select_node(self, node_id: str) -> Dict[str, Any]:
        with self.lock:
            if node_id not in self.data["nodes"]:
                raise KeyError(f"Node {node_id} not found")
            self.data["active_node_id"] = node_id
            self._save_unlocked()
            return self.data["nodes"][node_id]

    def add_comment(self, node_id: str, comment_data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if node_id not in self.data["nodes"]:
                raise KeyError(f"Node {node_id} not found")
            node = self.data["nodes"][node_id]
            comment = {
                "id": comment_data.get("id") or f"c-{uuid.uuid4().hex[:8]}",
                "quote": comment_data.get("quote", "").strip(),
                "comment": comment_data.get("comment", "").strip(),
                "section": comment_data.get("section", "General"),
                "resolved": bool(comment_data.get("resolved", False)),
                "created_at": time.time(),
            }
            node.setdefault("comments", []).append(comment)
            self._save_unlocked()
            return comment

    def update_comment(self, node_id: str, comment_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if node_id not in self.data["nodes"]:
                raise KeyError(f"Node {node_id} not found")
            node = self.data["nodes"][node_id]
            for c in node.get("comments", []):
                if c["id"] == comment_id:
                    if "comment" in updates:
                        c["comment"] = updates["comment"]
                    if "resolved" in updates:
                        c["resolved"] = bool(updates["resolved"])
                    if "quote" in updates:
                        c["quote"] = updates["quote"]
                    self._save_unlocked()
                    return c
            raise KeyError(f"Comment {comment_id} not found")

    def delete_comment(self, node_id: str, comment_id: str) -> None:
        with self.lock:
            if node_id not in self.data["nodes"]:
                raise KeyError(f"Node {node_id} not found")
            node = self.data["nodes"][node_id]
            node["comments"] = [c for c in node.get("comments", []) if c["id"] != comment_id]
            self._save_unlocked()

    def create_Manual_Fork(self, parent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            parent = self.data["nodes"].get(parent_id)
            if not parent:
                raise KeyError(f"Parent node {parent_id} not found")
            vnum = self.data["next_version_num"]
            self.data["next_version_num"] = vnum + 1
            node_id = f"v{vnum}-{uuid.uuid4().hex[:6]}"
            label = payload.get("label") or f"v{vnum} · Manual Edit"
            new_node = {
                "id": node_id,
                "parent_id": parent_id,
                "version_num": vnum,
                "label": label,
                "title": payload.get("title") or parent.get("title", f"Version {vnum}"),
                "seed_outline": payload.get("seed_outline", parent.get("seed_outline", "")),
                "content": payload.get("content", parent.get("content", "")),
                "comments": json.loads(json.dumps(payload.get("comments", parent.get("comments", [])))),
                "params": json.loads(json.dumps(payload.get("params", parent.get("params", {})))),
                "diffusion_steps": [
                    {
                        "step": 1,
                        "total_steps": 1,
                        "stage": "Manual Edit Snapshot",
                        "preview": payload.get("content", "")[:240],
                    }
                ],
                "diff_summary": payload.get("diff_summary") or "Manual user edit checkpoint",
                "source_mode": "manual_edit",
                "created_at": time.time(),
            }
            if payload.get("from_draft"):
                parent.pop("draft", None)
            self.data["nodes"][node_id] = new_node
            self.data["active_node_id"] = node_id
            self._save_unlocked()
            return new_node

    def _prune_jobs_unlocked(self) -> None:
        jobs = self.data.get("jobs", {})
        terminal = sorted(
            (j for j in jobs.values() if j.get("status") in TERMINAL_JOB_STATUSES),
            key=lambda j: j.get("updated_at", 0),
            reverse=True,
        )
        for old in terminal[MAX_TERMINAL_JOBS:]:
            jobs.pop(old["id"], None)

    def create_generation_job(self, req: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if any(j.get("status") in ACTIVE_JOB_STATUSES for j in self.data["jobs"].values()):
                raise ValueError("A diffusion is already running. Wait for it or cancel it first.")
            parent_id = req.get("parent_id") or self.data["active_node_id"]
            if parent_id not in self.data["nodes"]:
                parent_id = self.data["root_id"]
            parent = self.data["nodes"][parent_id]
            is_root = parent_id == self.data.get("root_id")

            # Persist seed text onto the v0 seed page; generated versions are never rewritten here.
            if req.get("sync_parent", True):
                if "seed_outline" in req and is_root:
                    parent["seed_outline"] = req["seed_outline"]
                if "params" in req:
                    parent["params"] = req["params"]

            # `// note` lines typed on the page become structured comments for every backend
            # (including the live agent session), and are stripped from the working draft.
            raw_content = req.get("content", self._node_display_html(parent))
            inline_comments, working_content = extract_inline_comments(raw_content)
            base_comments = req.get("comments")
            if base_comments is None:
                base_comments = [c for c in parent.get("comments", []) if not c.get("resolved")]
            seed_outline = req.get("seed_outline", parent.get("seed_outline", ""))
            if inline_comments and seed_outline:
                seed_outline = extract_inline_comments(seed_outline)[1]

            job_id = f"job-{uuid.uuid4().hex[:8]}"
            params = req.get("params") or parent.get("params") or {}
            total_steps = max(1, min(int(params.get("steps", 3)), 6))
            job = {
                "id": job_id,
                "parent_id": parent_id,
                "status": "pending",  # pending -> denoising -> completed / error
                "created_at": time.time(),
                "updated_at": time.time(),
                "current_step": 0,
                "total_steps": total_steps,
                "stage_label": "Initializing latent text space...",
                "intermediate_content": "",
                "steps_log": [],
                "seed_outline": seed_outline,
                "working_content": working_content,
                "raw_content": raw_content,
                "comments": list(base_comments) + inline_comments,
                "instruction": req.get("instruction", "").strip(),
                "selected_region": req.get("selected_region", "").strip(),
                "params": params,
                "result_node_id": None,
                "error": None,
                "provider_used": None,
            }
            self.data["jobs"][job_id] = job
            self._prune_jobs_unlocked()
            self._save_unlocked()
            return job

    def cancel_job(self, job_id: Optional[str] = None) -> List[str]:
        """Cancel one job (or every running job if no id). Late results for it are then refused."""
        with self.lock:
            cancelled = []
            for job in self.data["jobs"].values():
                if job.get("status") not in ACTIVE_JOB_STATUSES:
                    continue
                if job_id and job["id"] != job_id:
                    continue
                job["status"] = "cancelled"
                job["stage_label"] = "Cancelled"
                job["error"] = None
                job["updated_at"] = time.time()
                cancelled.append(job["id"])
            if cancelled:
                self._save_unlocked()
            return cancelled

    def update_job_step(
        self,
        job_id: str,
        step: int,
        stage_label: str,
        intermediate_content: str,
        status: str = "denoising",
        provider_used: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            job = self.data["jobs"][job_id]
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return job  # e.g. cancelled while a backend was still working; don't resurrect it
            if job.get("claimed_at") and status == "pending":
                return job  # a live agent already claimed it; don't un-claim (would allow re-delivery)
            job["status"] = status
            job["current_step"] = step
            job["stage_label"] = stage_label
            job["intermediate_content"] = intermediate_content
            job["updated_at"] = time.time()
            if provider_used:
                job["provider_used"] = provider_used
            job["steps_log"].append({
                "step": step,
                "total_steps": job["total_steps"],
                "stage": stage_label,
                "content": intermediate_content,
                "timestamp": time.time(),
            })
            self._save_unlocked()
            return job

    def complete_job(
        self,
        job_id: str,
        final_content: str,
        title: Optional[str] = None,
        label: Optional[str] = None,
        diff_summary: Optional[str] = None,
        provider_used: str = "agent",
    ) -> Dict[str, Any]:
        with self.lock:
            job = self.data["jobs"].get(job_id)
            if not job:
                raise KeyError(f"Job {job_id} not found")
            if job["status"] == "completed" and job.get("result_node_id"):
                return self.data["nodes"][job["result_node_id"]]
            if job["status"] == "cancelled":
                raise ValueError(f"Job {job_id} was cancelled by the user")

            parent_id = job["parent_id"]
            parent = self.data["nodes"].get(parent_id, self.data["nodes"][self.data["root_id"]])
            vnum = self.data["next_version_num"]
            self.data["next_version_num"] = vnum + 1
            node_id = f"v{vnum}-{uuid.uuid4().hex[:6]}"

            active_comments = [c for c in job.get("comments", []) if not c.get("resolved")]
            style = job.get("params", {}).get("style_preset", "Polished")
            if not label:
                if active_comments:
                    label = f"v{vnum} · Comment Guided ({len(active_comments)})"
                elif job.get("working_content") and parent.get("content"):
                    label = f"v{vnum} · Img2Img Polish ({style.split()[0]})"
                else:
                    label = f"v{vnum} · Seed Generation ({style.split()[0]})"

            if not title:
                first_heading = ""
                h_match = re.search(r"<h[123][^>]*>(.*?)</h[123]>", final_content, flags=re.IGNORECASE | re.DOTALL)
                if h_match:
                    first_heading = re.sub(r"<[^>]+>", "", h_match.group(1)).strip()[:60]
                else:
                    for line in html_to_lines(final_content).splitlines():
                        s = re.sub(r"<[^>]+>", "", line).strip().lstrip("#").strip()
                        if s:
                            first_heading = s[:60]
                            break
                title = first_heading or f"Generation v{vnum}"

            if not diff_summary:
                reasons = []
                if job.get("seed_outline") and not job.get("working_content"):
                    reasons.append("Expanded from seed outline")
                elif job.get("working_content"):
                    reasons.append(f"Refined content (denoising strength {job.get('params', {}).get('strength', 0.75)})")
                if active_comments:
                    reasons.append(f"Addressed {len(active_comments)} inline comment(s)")
                if job.get("instruction"):
                    reasons.append(f"Prompt guidance: '{job['instruction'][:45]}'")
                diff_summary = " · ".join(reasons) or f"Generated via {provider_used}"

            # Carry forward comments marked as resolved after addressing them
            carried_comments = []
            for c in job.get("comments", []):
                c_copy = dict(c)
                c_copy["resolved"] = True
                c_copy["resolved_in"] = f"v{vnum}"
                carried_comments.append(c_copy)

            steps_log = job.get("steps_log", [])
            if not steps_log or steps_log[-1].get("content") != final_content:
                steps_log.append({
                    "step": job["total_steps"],
                    "total_steps": job["total_steps"],
                    "stage": "Final Polished Output",
                    "content": final_content,
                    "timestamp": time.time(),
                })

            new_node = {
                "id": node_id,
                "parent_id": parent_id,
                "version_num": vnum,
                "label": label,
                "title": title,
                "seed_outline": job.get("seed_outline", ""),
                "content": final_content,
                "comments": carried_comments,
                "params": job.get("params", {}),
                "instruction": job.get("instruction", ""),
                "input_content": job.get("raw_content", job.get("working_content", "")),
                "diffusion_steps": steps_log,
                "diff_summary": diff_summary,
                "provider_used": provider_used,
                "source_mode": "diffusion_generate",
                "created_at": time.time(),
            }

            # The parent's draft was consumed by this diffusion (it's kept as input_content).
            if parent_id in self.data["nodes"] and parent_id != self.data.get("root_id"):
                self.data["nodes"][parent_id].pop("draft", None)
            self.data["nodes"][node_id] = new_node
            self.data["active_node_id"] = node_id
            job["status"] = "completed"
            job["current_step"] = job["total_steps"]
            job["stage_label"] = "Generation complete"
            job["intermediate_content"] = final_content
            job["result_node_id"] = node_id
            job["provider_used"] = provider_used
            job["updated_at"] = time.time()
            self._save_unlocked()
            return new_node

    def fail_job(self, job_id: str, error_msg: str) -> None:
        with self.lock:
            if job_id in self.data["jobs"]:
                job = self.data["jobs"][job_id]
                if job.get("status") in TERMINAL_JOB_STATUSES:
                    return
                job["status"] = "error"
                job["error"] = error_msg
                job["stage_label"] = f"Error: {error_msg}"
                job["updated_at"] = time.time()
                self._save_unlocked()

    def reset_workspace(self) -> Dict[str, Any]:
        """Preserves the current diffusion folder and creates a brand-new diffusion folder."""
        with self.lock:
            self._save_unlocked()
            prev_status = dict(self.data.get("agent_status", {}))
            self.doc_id, self.doc_dir, self.data = self._create_new_doc_unlocked()
            self.data["agent_status"].update(prev_status)
            self._save_unlocked()
            return self.get_snapshot()

    def load_document(self, doc_id: str) -> Dict[str, Any]:
        """Switches the active workspace to an existing diffusion folder."""
        with self.lock:
            target_dir = (self.documents_dir / doc_id).resolve()
            if not str(target_dir).startswith(str(self.documents_dir.resolve())) or not (target_dir / "tree.json").exists():
                raise KeyError(f"Diffusion folder '{doc_id}' not found")
            prev_status = dict(self.data.get("agent_status", {}))
            self.doc_id = doc_id
            self.doc_dir = target_dir
            self.state_path = target_dir / "tree.json"
            self.data = self._load_or_init(doc_id=doc_id, doc_dir=target_dir)
            self.data["agent_status"].update(prev_status)
            self._write_index(doc_id)
            self._save_unlocked()
            return self.get_snapshot()


class DiffusionEngine:
    """Executes multi-step LLM diffusion / iterative denoising across available backends."""

    def __init__(self, store: StateStore):
        self.store = store

    def start_job_async(self, job_id: str) -> None:
        t = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        t.start()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            return

        params = job.get("params") or {}
        provider_pref = params.get("provider", "auto")
        total_steps = max(1, min(int(params.get("steps", 3)), 6))

        # Check if the current active agent session is connected (via live_session long-poll listener or manual_bridge)
        live_session_active = self.store.agent_summary()["live_session"]
        bridge_only = provider_pref == "agent_bridge" or (
            provider_pref == "auto" and live_session_active
        )

        if bridge_only:
            self.store.update_job_step(
                job_id,
                step=1,
                stage_label="Connected to active agent session · Diffusing...",
                intermediate_content=job.get("working_content") or job.get("seed_outline") or "",
                status="pending",
                provider_used="awaiting_live_session",
            )
            # Wait up to 90s for the live session to pick the job up. Once it has claimed the job the
            # agent is actively writing, so give it much longer (the user can always cancel).
            start = time.time()
            while True:
                time.sleep(0.4)
                curr = self.store.get_job(job_id)
                if not curr or curr.get("status") in TERMINAL_JOB_STATUSES:
                    return
                claimed_at = curr.get("claimed_at")
                limit = (claimed_at + 600.0) if claimed_at else (start + 90.0)
                if time.time() >= limit:
                    break
            if provider_pref == "agent_bridge":
                self.store.fail_job(job_id, "The agent session did not respond in time.")
                return
            # Live session went quiet: fall back to the agent CLI / API backends.

        try:
            self._execute_diffusion_pipeline(job_id, job, total_steps, provider_pref)
        except Exception as e:
            self.store.fail_job(job_id, str(e))

    def _execute_diffusion_pipeline(
        self, job_id: str, job: Dict[str, Any], total_steps: int, provider_pref: str
    ) -> None:
        seed_outline = (job.get("seed_outline") or "").strip()
        working_content = (job.get("working_content") or "").strip()
        comments = [c for c in job.get("comments", []) if not c.get("resolved")]
        instruction = (job.get("instruction") or "").strip()
        selected_region = (job.get("selected_region") or "").strip()
        params = job.get("params") or {}

        strength = float(params.get("strength", 0.75))
        guidance = float(params.get("guidance_scale", 7.5))
        style_preset = params.get("style_preset", "Polished & Structured")
        negative_prompt = (params.get("negative_prompt") or "").strip()

        # Parse inline comments directly from canvas content as well (e.g., // comment, [comment: ...], <!-- ... -->, >> ...)
        extracted_comments, cleaned_working = self._extract_inline_canvas_comments(working_content)
        all_comments = list(comments) + extracted_comments
        effective_working = cleaned_working if extracted_comments else working_content

        # Step 1: Coarse Latent Structural Denoising
        stage1_text = self._build_stage1_scaffold(seed_outline, effective_working, all_comments, instruction, style_preset)
        self.store.update_job_step(
            job_id,
            step=1,
            stage_label=f"Step 1/{total_steps} · Diffusing structure...",
            intermediate_content=stage1_text,
            status="denoising",
        )
        time.sleep(0.25)

        if total_steps >= 3:
            stage2_text = self._build_stage2_draft(
                seed_outline, effective_working, all_comments, instruction, style_preset, stage1_text
            )
            self.store.update_job_step(
                job_id,
                step=2,
                stage_label=f"Step 2/{total_steps} · Resolving comments & refining...",
                intermediate_content=stage2_text,
                status="denoising",
            )
            time.sleep(0.25)

        # Final Step: High-resolution LLM Polish
        self.store.update_job_step(
            job_id,
            step=max(1, total_steps - 1),
            stage_label=f"Step {total_steps}/{total_steps} · Final polish...",
            intermediate_content=effective_working or stage1_text,
            status="denoising",
        )

        prompt = self._build_llm_prompt(
            seed_outline=seed_outline,
            working_content=effective_working,
            comments=all_comments,
            instruction=instruction,
            selected_region=selected_region,
            strength=strength,
            guidance=guidance,
            style_preset=style_preset,
            negative_prompt=negative_prompt,
            total_steps=total_steps,
        )

        final_text, provider_used = self._invoke_llm(
            prompt=prompt,
            provider_pref=provider_pref,
            seed_outline=seed_outline,
            working_content=effective_working,
            comments=all_comments,
            instruction=instruction,
            style_preset=style_preset,
            strength=strength,
            negative_prompt=negative_prompt,
        )

        # Check if an external agent completed it (or the user cancelled) while we were generating
        curr = self.store.get_job(job_id)
        if curr.get("status") in TERMINAL_JOB_STATUSES:
            return

        self.store.complete_job(
            job_id=job_id,
            final_content=final_text,
            provider_used=provider_used,
        )

    @staticmethod
    def _extract_inline_canvas_comments(text: str) -> tuple[List[Dict[str, Any]], str]:
        """Extracts inline comments typed directly in the notepad (e.g. // note, [comment: note], >> note)."""
        if not text:
            return [], ""
        extracted: List[Dict[str, Any]] = []
        cleaned_lines: List[str] = []
        prev_content_line = ""

        for line in text.splitlines():
            stripped = line.strip()
            # Full-line comment syntax: // ..., >> ...
            if stripped.startswith("//") or stripped.startswith(">>"):
                note = stripped[2:].strip()
                if note:
                    extracted.append({
                        "id": f"inline-{len(extracted)+1}",
                        "quote": prev_content_line[:120],
                        "comment": note,
                        "resolved": False,
                    })
                continue

            # Inline bracket or HTML comments: [comment: ...] or <!-- ... -->
            work_line = line
            for match in re.finditer(r"\[(?:comment|note|edit):\s*([^\]]+)\]|<!--\s*(.+?)\s*-->", line, flags=re.IGNORECASE):
                note = (match.group(1) or match.group(2) or "").strip()
                quote_ctx = re.sub(r"\[(?:comment|note|edit):\s*[^\]]+\]|<!--\s*.+?\s*-->", "", line, flags=re.IGNORECASE).strip()
                if note:
                    extracted.append({
                        "id": f"inline-{len(extracted)+1}",
                        "quote": (quote_ctx or prev_content_line)[:120],
                        "comment": note,
                        "resolved": False,
                    })
                work_line = work_line.replace(match.group(0), "").rstrip()

            cleaned_lines.append(work_line)
            if work_line.strip():
                prev_content_line = work_line.strip()

        return extracted, "\n".join(cleaned_lines).strip()

    def _build_stage1_scaffold(
        self,
        seed_outline: str,
        working_content: str,
        comments: List[Dict[str, Any]],
        instruction: str,
        style_preset: str,
    ) -> str:
        base = working_content or seed_outline or "Blank Seed Canvas"
        lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
        preview_lines = [f"<!-- [LATENT DENOISING PASS 1 · Style: {style_preset}] -->"]
        if not lines:
            preview_lines.append("# Conceptual Structure")
            preview_lines.append("- Initializing primary thesis and key structural pillars...")
        else:
            for ln in lines[:12]:
                if ln.startswith("#"):
                    preview_lines.append(ln)
                elif ln.startswith(("-", "*", "1.")):
                    preview_lines.append(f"{ln}  ⟶  [Expanding latent node...]")
                else:
                    preview_lines.append(ln)
        if comments:
            preview_lines.append("\n---")
            preview_lines.append(f"**Conditioning Vectors ({len(comments)} active comments):**")
            for c in comments:
                q = f' on "{c.get("quote")}"' if c.get("quote") else ""
                preview_lines.append(f"- [ Conditioning{q} ]: {c.get('comment')}")
        if instruction:
            preview_lines.append(f"\n**Global Prompt Guidance:** {instruction}")
        return "\n".join(preview_lines)

    def _build_stage2_draft(
        self,
        seed_outline: str,
        working_content: str,
        comments: List[Dict[str, Any]],
        instruction: str,
        style_preset: str,
        stage1_text: str,
    ) -> str:
        lines = [f"<!-- [LATENT DENOISING PASS 2 · Integrating Comments & Harmonizing Tone] -->"]
        if working_content:
            lines.append(working_content)
        elif seed_outline:
            lines.append(stage1_text)
        if comments:
            lines.append("\n> **Inpainting / Attention Zones Active:**")
            for idx, c in enumerate(comments, 1):
                quote = c.get("quote") or "Document level"
                lines.append(f"> {idx}. Target `{quote}` → Applying transformation: *{c.get('comment')}*")
        return "\n\n".join(lines)

    def _build_llm_prompt(
        self,
        seed_outline: str,
        working_content: str,
        comments: List[Dict[str, Any]],
        instruction: str,
        selected_region: str,
        strength: float,
        guidance: float,
        style_preset: str,
        negative_prompt: str,
        total_steps: int,
    ) -> str:
        strength_desc = (
            "LOW (0.1-0.35): Preserve 80%+ of existing wording; perform subtle micro-polishing and apply explicit comments only."
            if strength < 0.36
            else "MEDIUM (0.36-0.75): Balanced img2img refinement. Keep the core structure and key facts, elevate prose quality, and seamlessly weave in all comments."
            if strength <= 0.75
            else "HIGH (0.76-1.0): Deep creative expansion and re-synthesis. Transform rough seeds/outlines into a comprehensive, richly articulated, publication-grade document."
        )

        comment_block = "None"
        if comments:
            c_lines = []
            for idx, c in enumerate(comments, 1):
                anchor = f'Anchored to text "{c["quote"]}"' if c.get("quote") else "General document comment"
                c_lines.append(f"{idx}. [{anchor}] -> User Directive: {c['comment']}")
            comment_block = "\n".join(c_lines)

        return f"""You are MindFusion, an iterative diffusion-style LLM document generator.
Your task is to synthesize or refine the document into a rich, semantic HTML fragment based on the seed/outline, current canvas content, inline comments, and diffusion hyperparameters.

=== DIFFUSION HYPERPARAMETERS ===
- Style Preset: {style_preset}
- Denoising Strength: {strength:.2f} ({strength_desc})
- Guidance Scale (CFG): {guidance:.1f} (Strictly obey inline comments and global prompt directives)
- Denoising Steps: {total_steps}
- Negative Prompt (Avoid these elements/tones): {negative_prompt or "None"}

=== SEED / OUTLINE CONTENT ===
{seed_outline or "(No separate seed outline provided)"}

=== CURRENT CANVAS CONTENT (Working Draft) ===
{working_content or "(Blank canvas - generate fresh from seed/outline and instructions)"}

=== INLINE COMMENTS & INPAINTING DIRECTIVES (MUST ADDRESS ALL) ===
{comment_block}

=== SELECTED REGION FOCUS ===
{selected_region or "(Full document generation/refinement)"}

=== GLOBAL PROMPT INSTRUCTION ===
{instruction or "Transform the seed/draft and comments into a cohesive, compelling, well-structured semantic HTML document."}

=== OUTPUT RULES ===
1. Output ONLY a clean, semantic HTML fragment suitable for direct rendering inside a contenteditable article canvas.
2. You decide the best HTML tags for the content structure and clarity—such as <h1>, <h2>, <h3>, <p>, <ul>, <ol>, <li>, <blockquote>, <pre><code>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <details><summary>, <mark>, <strong>, <em>, <hr>, or <aside>.
3. Do NOT include <html>, <head>, or <body> wrapper tags, and do NOT wrap the output in ```html code fences or add conversational filler.
4. Directly integrate every inline comment's requested change into the appropriate section of the document.
"""

    def _invoke_llm(
        self,
        prompt: str,
        provider_pref: str,
        seed_outline: str,
        working_content: str,
        comments: List[Dict[str, Any]],
        instruction: str,
        style_preset: str,
        strength: float,
        negative_prompt: str,
    ) -> tuple[str, str]:
        order = []
        if provider_pref == "gemini":
            order = ["gemini", "openai", "agent_cli"]
        elif provider_pref == "openai":
            order = ["openai", "gemini", "agent_cli"]
        elif provider_pref in ("agent_cli", "agy"):
            order = ["agent_cli", "gemini", "openai"]
        elif provider_pref == "local_synth":
            order = ["local_synth"]
        else:
            order = ["agent_cli", "openai", "gemini"]

        errors = []
        for backend in order:
            try:
                if backend == "gemini" and os.environ.get("GEMINI_API_KEY"):
                    res = self._call_gemini(prompt)
                    if res:
                        return res, "Gemini API"
                elif backend == "openai" and os.environ.get("OPENAI_API_KEY"):
                    res = self._call_openai(prompt)
                    if res:
                        return res, "OpenAI API"
                elif backend == "agent_cli":
                    cli = find_agent_cli()
                    if cli:
                        res = self._call_agent_cli(cli, prompt)
                        if res:
                            return res, cli["name"]
                elif backend == "local_synth":
                    res = self._local_diffusion_synthesizer(
                        seed_outline=html_to_lines(seed_outline),
                        working_content=html_to_lines(working_content),
                        comments=comments,
                        instruction=instruction,
                        style_preset=style_preset,
                        strength=strength,
                        negative_prompt=negative_prompt,
                    )
                    return markdown_to_html(res), "MindFusion Local Synthesizer"
            except Exception as e:
                print(f"[MindFusion] Backend {backend} failed, trying next: {e}", file=sys.stderr)
                errors.append(f"{backend}: {e}")
                continue

        if not errors:
            raise RuntimeError(
                "No LLM backend available. Start the agent listener (agent_bridge.py listen), "
                "install an agent CLI (claude, codex, gemini, agy) or set MINDFUSION_AGENT_CMD, "
                "or set GEMINI_API_KEY / OPENAI_API_KEY."
            )
        raise RuntimeError(f"All LLM backends failed ({'; '.join(errors)}). Ensure the server runs with network/unsandboxed access.")

    def _call_gemini(self, prompt: str) -> Optional[str]:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key or api_key.startswith("AQ."):
            # OAuth token or invalid key for public generativelanguage REST endpoint; try anyway with short timeout
            pass
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            candidates = body.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return self._strip_outer_fences(text)
        return None

    def _call_openai(self, prompt: str) -> Optional[str]:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are MindFusion, an expert iterative text-diffusion engine."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "").strip()
                if text:
                    return self._strip_outer_fences(text)
        return None

    def _call_agent_cli(self, cli: Dict[str, Any], prompt: str) -> Optional[str]:
        proc = subprocess.run(
            cli["argv"](prompt),
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return self._strip_outer_fences(proc.stdout.strip())
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
        raise RuntimeError(f"{cli['name']} failed: {detail[0][:200]}")

    @staticmethod
    def _strip_outer_fences(text: str) -> str:
        t = text.strip()
        for fence in ("```html", "```markdown", "```md", "```"):
            if t.startswith(fence):
                t = t[len(fence):].strip()
                if t.endswith("```"):
                    t = t[:-3].strip()
                break
        return t

    def _local_diffusion_synthesizer(
        self,
        seed_outline: str,
        working_content: str,
        comments: List[Dict[str, Any]],
        instruction: str,
        style_preset: str,
        strength: float,
        negative_prompt: str,
    ) -> str:
        """Deterministic rich Markdown synthesizer so MindFusion works offline/sandboxed too."""
        source = working_content.strip() or seed_outline.strip()

        style_flavour = {
            "Polished & Structured": (
                "Clear, authoritative, and logically organized with executive precision.",
                "Executive Summary",
            ),
            "Technical Spec & Architecture": (
                "Rigorous systems design breakdown with concrete operational invariants and data flows.",
                "System Overview & Architecture",
            ),
            "Narrative & Essay": (
                "Engaging, vivid, and fluidly paced prose that connects core themes with narrative momentum.",
                "Prologue & Core Thesis",
            ),
            "Concise & Punchy": (
                "High-signal, zero-fluff distillation focused on immediate actionability.",
                "Key Takeaways",
            ),
            "Creative & Speculative": (
                "Imaginative, evocative exploration pushing conceptual boundaries and rich sensory detail.",
                "Vision & Exploration",
            ),
        }.get(style_preset, ("Cohesive, structured, and thoughtfully articulated.", "Overview"))

        # If existing working content is already well-formed and strength < 0.85, perform targeted inpainting + polish
        if working_content.strip() and self._looks_like_polished_doc(working_content):
            result = working_content.strip()
            result = self._apply_comments_in_place(result, comments, style_preset, strength)
            if instruction:
                result = self._apply_global_instruction(result, instruction, style_preset, strength)
            if negative_prompt:
                result = self._filter_negative_terms(result, negative_prompt)
            return result

        # Otherwise expand from seed / outline notes into a rich polished document
        raw_lines = [ln.strip() for ln in source.splitlines() if ln.strip()] if source else []
        title = "Untitled Synthesis"
        bullets: List[str] = []
        paragraphs: List[str] = []

        for ln in raw_lines:
            if ln.startswith("#"):
                if title == "Untitled Synthesis":
                    title = ln.lstrip("#").strip()
                else:
                    bullets.append(ln.lstrip("#").strip())
            elif re.match(r"^[-*•]|\d+\.", ln):
                cleaned = re.sub(r"^([-*•]|\d+\.)\s*", "", ln).strip()
                if cleaned:
                    bullets.append(cleaned)
            else:
                if title == "Untitled Synthesis" and len(ln) < 75 and not paragraphs:
                    title = ln
                else:
                    paragraphs.append(ln)

        if title == "Untitled Synthesis" and instruction:
            title = instruction[:60].strip().capitalize()
        elif title == "Untitled Synthesis" and bullets:
            title = bullets[0][:55].strip().capitalize()

        out: List[str] = [f"# {title}", ""]

        # Opening synthesis
        if paragraphs:
            intro = " ".join(paragraphs[:2])
            if strength >= 0.5:
                out.append(
                    f"{intro} This synthesis expands the foundational seed into a cohesive framework—"
                    f"balancing clarity, structural depth, and actionable execution."
                )
            else:
                out.append(intro)
        else:
            out.append(
                f"## {style_flavour[1]}\n\n"
                f"This document synthesizes the core seed concepts for **{title}**. "
                f"{style_flavour[0]}"
            )
        out.append("")

        # Expand bullet points into rich sections
        if bullets:
            for idx, b in enumerate(bullets, 1):
                heading = b.split(":")[0].strip() if ":" in b else b[:52].strip()
                detail = b.split(":", 1)[1].strip() if ":" in b else b
                out.append(f"## {idx}. {heading.capitalize()}")
                out.append("")
                if strength >= 0.65:
                    out.append(
                        f"**{detail.capitalize().rstrip('.')}** forms a critical pillar of this initiative. "
                        f"By formalizing clear interfaces, iterative feedback loops, and measurable outcomes around "
                        f"*{heading.lower()}*, teams can iterate rapidly while preserving high quality and coherence."
                    )
                    out.append("")
                    out.append(
                        f"- **Primary Objective**: Translate *{detail.lower().rstrip('.')}* into a repeatable, observable workflow.\n"
                        f"- **Key Mechanism**: Combine structured checkpoints with low-friction refinement cycles.\n"
                        f"- **Expected Impact**: Higher signal-to-noise ratio and traceable evolution across iterations."
                    )
                else:
                    out.append(f"{detail.capitalize().rstrip('.')}. Developed with focus on clarity and direct execution.")
                out.append("")

        if len(paragraphs) > 2:
            out.append("## Deep Dive & Context")
            out.append("")
            for p in paragraphs[2:]:
                out.append(p)
                out.append("")

        doc = "\n".join(out).strip()

        # Apply inline comments and global instruction
        if comments:
            doc = self._apply_comments_in_place(doc, comments, style_preset, strength)
        if instruction:
            doc = self._apply_global_instruction(doc, instruction, style_preset, strength)
        if negative_prompt:
            doc = self._filter_negative_terms(doc, negative_prompt)

        return doc.strip()

    @staticmethod
    def _looks_like_polished_doc(text: str) -> bool:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        has_heading = any(ln.strip().startswith("#") for ln in lines)
        has_prose = any(len(ln.strip()) > 95 and not ln.strip().startswith(("-", "*")) for ln in lines)
        return has_heading and has_prose and len(lines) >= 4

    def _apply_comments_in_place(
        self, doc: str, comments: List[Dict[str, Any]], style_preset: str, strength: float
    ) -> str:
        updated = doc
        unanchored_notes: List[str] = []

        for c in comments:
            quote = (c.get("quote") or "").strip()
            directive = (c.get("comment") or "").strip()
            if not directive:
                continue

            if quote and quote in updated:
                replacement = self._synthesize_inpainted_span(quote, directive, style_preset, strength)
                updated = updated.replace(quote, replacement, 1)
            else:
                unanchored_notes.append(self._synthesize_comment_section(quote, directive, style_preset))

        if unanchored_notes:
            updated = updated.rstrip() + "\n\n## Refinements & Addressed Feedback\n\n" + "\n\n".join(unanchored_notes)
        return updated

    @staticmethod
    def _synthesize_inpainted_span(quote: str, directive: str, style_preset: str, strength: float) -> str:
        d_lower = directive.lower()
        if any(k in d_lower for k in ("remove", "delete", "drop", "cut")):
            return ""
        if any(k in d_lower for k in ("shorter", "concise", "tighten", "brief")):
            words = quote.split()
            short = " ".join(words[: max(4, len(words) // 2)]).rstrip(".,;:") + "."
            return short
        if any(k in d_lower for k in ("example", "concret", "illustrat", "metric")):
            return (
                f"{quote} (for example, demonstrating measurable improvements through concrete benchmarks "
                f"and real-world validation: *{directive}*)"
            )
        if any(k in d_lower for k in ("expand", "elaborate", "detail", "more", "deeper")):
            return (
                f"{quote} — specifically, {directive.rstrip('.')}. This dimension strengthens the surrounding "
                f"architecture by addressing edge cases and clarifying operational trade-offs."
            )
        # Direct replacement or enhancement guided by comment
        return f"{quote} — refined to incorporate: **{directive.rstrip('.')}**"

    @staticmethod
    def _synthesize_comment_section(quote: str, directive: str, style_preset: str) -> str:
        if quote:
            return (
                f"### Regarding *{quote[:50]}*\n"
                f"{directive.capitalize().rstrip('.')}. This adjustment has been integrated into the core narrative "
                f"to align with the **{style_preset}** preset."
            )
        return (
            f"- **Integrated Directive**: {directive.capitalize().rstrip('.')}. "
            f"Synthesized across the document structure for cohesive flow."
        )

    @staticmethod
    def _apply_global_instruction(doc: str, instruction: str, style_preset: str, strength: float) -> str:
        inst = instruction.strip()
        if not inst:
            return doc
        return (
            f"{doc.rstrip()}\n\n"
            f"## Synthesis & Direction: {inst[:58].capitalize()}\n\n"
            f"In alignment with the directive (*\"{inst}\"*), this iteration emphasizes **{style_preset.lower()}** "
            f"execution at a denoising strength of `{strength:.2f}`, ensuring both conceptual fidelity to the seed "
            f"and polished structural presentation."
        )

    @staticmethod
    def _filter_negative_terms(doc: str, negative_prompt: str) -> str:
        terms = [t.strip() for t in re.split(r"[,;]", negative_prompt) if len(t.strip()) >= 3]
        result = doc
        for term in terms:
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", flags=re.IGNORECASE)
            result = pattern.sub("", result)
        result = re.sub(r"  +", " ", result)
        return result


class MindFusionHTTPHandler(BaseHTTPRequestHandler):
    store: StateStore
    engine: DiffusionEngine

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet standard polling logs unless error
        if self.command != "GET" or "/api/state" not in self.path:
            sys.stderr.write(f"[MindFusion HTTP] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/state":
            # Long-poll: `?since=<state_version>&wait=<seconds>` blocks until something changes, so
            # clients get updates instantly without hammering the server.
            query = parse_qs(parsed.query)
            since = query.get("since", [None])[0]
            if since is not None:
                try:
                    since_v = int(since)
                    wait = float(query.get("wait", ["0"])[0])
                except ValueError:
                    since_v, wait = -1, 0.0
                changed = self.store.wait_for_change(since_v, wait) if wait > 0 else self.store.state_version != since_v
                if not changed:
                    self._send_json({
                        "unchanged": True,
                        "state_version": self.store.state_version,
                        "server_id": self.store.server_id,
                        "agent": self.store.agent_summary(),
                    })
                    return
            self._send_json(self.store.get_snapshot())
            return

        if path == "/api/agent/wait":
            query = parse_qs(parsed.query)
            timeout = float(query.get("timeout", ["300"])[0])
            pending = self.store.wait_for_pending_job(timeout=timeout, agent_name=query.get("agent", [None])[0])
            self._send_json({"pending_jobs": pending})
            return

        if path == "/api/agent/pending":
            query = parse_qs(parsed.query)
            agent_name = query.get("agent", [DEFAULT_AGENT_NAME])[0]
            mode = query.get("mode", ["agent_bridge"])[0]
            self.store.register_agent_heartbeat(agent_name=agent_name, mode=mode)
            snap = self.store.get_snapshot()
            pending = [
                j for j in snap["jobs"].values() if j["status"] in ("pending", "denoising")
            ]
            self._send_json({"pending_jobs": pending, "state_version": snap["state_version"]})
            return

        if path == "/" or path == "/index.html":
            index_file = STATIC_DIR / "index.html"
            if not index_file.exists():
                self._send_json({"error": "UI index.html not found"}, status=404)
                return
            content = index_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
            return

        # Serve static assets if any
        rel = path.lstrip("/")
        asset = (STATIC_DIR / rel).resolve()
        if str(asset).startswith(str(STATIC_DIR.resolve())) and asset.is_file():
            ctype = "text/plain; charset=utf-8"
            if asset.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif asset.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif asset.suffix == ".svg":
                ctype = "image/svg+xml"
            data = asset.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_json({"error": f"Not found: {path}"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json_body()
        except Exception as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, status=400)
            return

        try:
            if path == "/api/generate":
                job = self.store.create_generation_job(body)
                self.engine.start_job_async(job["id"])
                self._send_json({"job": job, "state": self.store.get_snapshot()})
                return

            if path == "/api/node/update":
                node_id = body.get("node_id") or self.store.data["active_node_id"]
                node = self.store.update_node(node_id, body)
                self._send_json({"node": node, "state": self.store.get_snapshot()})
                return

            if path == "/api/node/select":
                node_id = body["node_id"]
                node = self.store.select_node(node_id)
                self._send_json({"node": node, "state": self.store.get_snapshot()})
                return

            if path == "/api/job/cancel":
                cancelled = self.store.cancel_job(body.get("job_id"))
                self._send_json({"cancelled": cancelled, "state": self.store.get_snapshot()})
                return

            if path == "/api/node/fork":
                parent_id = body.get("parent_id") or self.store.data["active_node_id"]
                node = self.store.create_Manual_Fork(parent_id, body)
                self._send_json({"node": node, "state": self.store.get_snapshot()})
                return

            if path == "/api/comment/add":
                node_id = body.get("node_id") or self.store.data["active_node_id"]
                comment = self.store.add_comment(node_id, body)
                self._send_json({"comment": comment, "state": self.store.get_snapshot()})
                return

            if path == "/api/comment/update":
                node_id = body.get("node_id") or self.store.data["active_node_id"]
                comment = self.store.update_comment(node_id, body["comment_id"], body)
                self._send_json({"comment": comment, "state": self.store.get_snapshot()})
                return

            if path == "/api/comment/delete":
                node_id = body.get("node_id") or self.store.data["active_node_id"]
                self.store.delete_comment(node_id, body["comment_id"])
                self._send_json({"ok": True, "state": self.store.get_snapshot()})
                return

            if path == "/api/agent/heartbeat":
                status = self.store.register_agent_heartbeat(
                    agent_name=body.get("agent_name", DEFAULT_AGENT_NAME),
                    mode=body.get("mode", "agent_bridge"),
                )
                self._send_json({"agent_status": status})
                return

            if path == "/api/agent/step":
                job = self.store.update_job_step(
                    job_id=body["job_id"],
                    step=int(body.get("step", 1)),
                    stage_label=body.get("stage_label", "Agent denoising pass..."),
                    intermediate_content=body.get("intermediate_content", ""),
                    status=body.get("status", "denoising"),
                    provider_used="agent_bridge_claimed",
                )
                self._send_json({"job": job, "state": self.store.get_snapshot()})
                return

            if path == "/api/agent/complete":
                node = self.store.complete_job(
                    job_id=body["job_id"],
                    final_content=body["content"],
                    title=body.get("title"),
                    label=body.get("label"),
                    diff_summary=body.get("diff_summary"),
                    provider_used=body.get("provider_used") or f"{self.store.data['agent_status'].get('agent_name') or DEFAULT_AGENT_NAME} (live session)",
                )
                self._send_json({"node": node, "state": self.store.get_snapshot()})
                return

            if path == "/api/reset" or path == "/api/doc/new":
                snap = self.store.reset_workspace()
                self._send_json({"state": snap})
                return

            if path == "/api/doc/load":
                doc_id = body["doc_id"]
                snap = self.store.load_document(doc_id)
                self._send_json({"state": snap})
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, status=404)
        except KeyError as e:
            self._send_json({"error": str(e).strip("'\"")}, status=404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=409)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)


def create_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT, state_path: Optional[Path] = None) -> ThreadingHTTPServer:
    if state_path is None:
        state_path = Path.cwd() / ".mindfusion" / "state.json"
    store = StateStore(state_path)
    engine = DiffusionEngine(store)

    class BoundHandler(MindFusionHTTPHandler):
        pass

    BoundHandler.store = store
    BoundHandler.engine = engine
    ThreadingHTTPServer.daemon_threads = True  # long-poll threads must not block shutdown
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    return httpd


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MindFusion: LLM Diffusion & Version Tree Studio")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--state-file",
        default=str(Path.cwd() / ".mindfusion" / "state.json"),
        help="Path to persistent version tree JSON state file",
    )
    parser.add_argument("--open-browser", action="store_true", help="Automatically open the MindFusion UI in a browser")
    parser.add_argument("--agent-connected", action="store_true", help="Mark the active agent as connected on startup")
    args = parser.parse_args()

    state_path = Path(args.state_file).resolve()
    httpd = create_server(host=args.host, port=args.port, state_path=state_path)
    if args.agent_connected:
        httpd.RequestHandlerClass.store.register_agent_heartbeat(os.environ.get("MINDFUSION_AGENT_NAME", DEFAULT_AGENT_NAME), mode="agent_bridge")

    url = f"http://{args.host}:{args.port}"
    print(f"============================================================")
    print(f"  MindFusion LLM Diffusion Studio running at: {url}")
    print(f"  State file: {state_path}")
    print(f"============================================================")
    sys.stdout.flush()

    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MindFusion] Shutting down server...")
        httpd.server_close()


if __name__ == "__main__":
    main()
