"""Configuration for the Atticus processor.

Two sources, deliberately separate:

  ops/.env           pipeline settings — safe to read, safe to log
  ~/.config/ai/env   shared credentials — read, never logged, never echoed

That split matches the machine convention (see the hyprwhspr-doctor skill):
one private file owns every API key, and consumers read it rather than
keeping their own copy.
"""
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AI_ENV = Path.home() / ".config/ai/env"

_ASSIGN = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def _parse_env(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


class Config:
    def __init__(self, env_file: Path | None = None):
        f = _parse_env(env_file or (REPO / "ops/.env"))

        def g(k, default=None):
            return os.environ.get(k) or f.get(k) or default

        self.vault = Path(g("ATTICUS_VAULT_PATH", str(REPO / ".scratch-vault"))).expanduser()
        self.log_level = g("ATTICUS_LOG_LEVEL", "INFO")
        self.notify_url = g("ATTICUS_NOTIFY_URL", "") or None
        self.push_retries = int(g("ATTICUS_PUSH_RETRIES", "3"))
        self.git_name = g("ATTICUS_GIT_AUTHOR_NAME", "Atticus Processor")
        self.git_email = g("ATTICUS_GIT_AUTHOR_EMAIL", "atticus@localhost")

        # Transcription — same endpoint and steering prompt as the machine's
        # dictation (hyprwhspr), but a bigger model. Dictation uses
        # gpt-4o-mini-transcribe because the user is watching a cursor and
        # feels the latency. Here nobody is waiting, so accuracy is worth
        # buying: a misheard word becomes an autonomous agent's instruction
        # instead of a typo the user immediately sees and fixes.
        self.stt_url = g("ATTICUS_STT_URL", "https://api.openai.com/v1/audio/transcriptions")
        self.stt_model = g("ATTICUS_STT_MODEL", "gpt-4o-transcribe")
        self.stt_timeout = int(g("ATTICUS_STT_TIMEOUT", "60"))
        self.stt_prompt = g(
            "ATTICUS_STT_PROMPT",
            "Transcribe with proper capitalization, including sentence "
            "beginnings, proper nouns, titles, and standard English "
            "capitalization rules. The speaker is dictating a short "
            "instruction or request.",
        )

        # Ingest (WarDog). The transport is a pluggable executable — see
        # ingest/poller.py. Whichever transport wins (SPEC §2.2.1), it ships
        # a fetcher implementing the same four-command CLI.
        self.fetcher = g("ATTICUS_FETCHER", "ingest/plaud_web.py")
        self.fetcher_timeout = int(g("ATTICUS_FETCHER_TIMEOUT", "300"))
        self.poll_days = int(g("PLAUD_POLL_DAYS", "2"))

        # Execution
        self.claude_bin = g("ATTICUS_CLAUDE_BIN", "claude")
        self.claude_model = g("ATTICUS_CLAUDE_MODEL", "") or None
        self.exec_timeout = int(g("ATTICUS_EXEC_TIMEOUT", "1800"))
        self.skills_dir = Path(g("ATTICUS_SKILLS_DIR", str(REPO / "skills")))

        # Sanity gate — below this many words we refuse to execute.
        self.min_words = int(g("ATTICUS_MIN_WORDS", "3"))
        # Optional wake phrase. Empty = execute everything that transcribes.
        self.wake_phrase = (g("ATTICUS_WAKE_PHRASE", "") or "").strip().lower()

        self._openai_key = None

    @property
    def openai_key(self) -> str:
        """Read on demand from the shared credential file. Never logged."""
        if self._openai_key is None:
            k = os.environ.get("OPENAI_API_KEY") or _parse_env(AI_ENV).get("OPENAI_API_KEY", "")
            if not k:
                raise RuntimeError(
                    f"OPENAI_API_KEY not found in environment or {AI_ENV}"
                )
            if not k.startswith("sk-"):
                raise RuntimeError("OPENAI_API_KEY does not look like an OpenAI key")
            self._openai_key = k
        return self._openai_key

    def redacted(self) -> dict:
        """Safe to log. Deliberately omits anything credential-shaped."""
        return {
            "vault": str(self.vault),
            "stt_model": self.stt_model,
            "claude_model": self.claude_model or "(default)",
            "skills_dir": str(self.skills_dir),
            "exec_timeout": self.exec_timeout,
            "fetcher": str(self.fetcher),
            "min_words": self.min_words,
            "wake_phrase": self.wake_phrase or "(none — execute everything)",
        }
