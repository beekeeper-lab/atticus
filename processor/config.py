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
            # UNSET (None) falls back to the default; an explicit EMPTY string
            # does not. Or-chaining collapsed "" into the default, so a setting
            # meant to be blank-to-disable (a spend ceiling, a notify URL) could
            # never actually be blank — execute.py's "no spend ceiling" warning
            # was unreachable as a result. Callers that still want ""→default
            # keep their own `or default` (see skills_dir).
            v = os.environ.get(k)
            if v is None:
                v = f.get(k)
            return default if v is None else v

        self.vault = Path(g("ATTICUS_VAULT_PATH", str(REPO / ".scratch-vault"))).expanduser()
        self.log_level = g("ATTICUS_LOG_LEVEL", "INFO")
        self.notify_url = g("ATTICUS_NOTIFY_URL", "") or None
        # A recurring condition (dead Plaud session) is rediscovered every
        # tick. One alarm per window per condition, or you learn to ignore it.
        self.alarm_throttle_hours = float(g("ATTICUS_ALARM_THROTTLE_HOURS", "6"))

        # Results. A finished voice command is the whole point of the system, so
        # it gets a push carrying a link to the page it produced. Separate knob
        # from the alarm URL so results and alarms can be split onto different
        # topics later; falls back to the alarm URL when unset.
        self.result_notify_url = (g("ATTICUS_RESULT_NOTIFY_URL", "")
                                  or self.notify_url)
        # Public base URL of the vault browser. Blank = no links in
        # notifications, which is correct for anyone without a published site.
        self.site_base_url = (g("ATTICUS_SITE_BASE_URL", "") or "").rstrip("/")
        # Gated notes are the common case (a wearable overhears a lot), so they
        # are silent by default. Turn on to catch a misheard wake word — the
        # failure where a real command is silently filed as a note.
        # title | summary | full. Notifications travel through a third-party
        # push service and land on lock screens, watches and phone backups.
        # "full" is right for a single operator on a private topic; it is the
        # wrong default for anyone else, so it is a conscious setting.
        self.notification_detail = (g("ATTICUS_NOTIFICATION_DETAIL", "full")
                                    or "full").strip().lower()
        self.notify_notes = (g("ATTICUS_NOTIFY_NOTES", "false").lower()
                             in ("1", "true", "yes", "on"))
        self.push_retries = int(g("ATTICUS_PUSH_RETRIES", "3"))
        # Expire raw audio after this many days; 0 keeps it forever. Transcripts
        # and outputs are never expired. The vault holds recordings of other
        # people who did not consent to permanent retention. NOTE: this removes
        # audio from the working tree, not from git history — see ops/retention.py.
        self.audio_retention_days = int(g("ATTICUS_AUDIO_RETENTION_DAYS", "30"))
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
        # Contain the agent in its own mount namespace. Off is a real choice
        # with a real cost: without it the agent can read every credential on
        # the host, including the vault deploy key.
        self.sandbox = (g("ATTICUS_SANDBOX", "on").lower()
                        not in ("0", "off", "false", "no"))
        self.claude_model = g("ATTICUS_CLAUDE_MODEL", "") or None
        self.exec_timeout = int(g("ATTICUS_EXEC_TIMEOUT", "1800"))
        # Tools the agent may use. WebSearch/WebFetch are granted because
        # denying them bought NO security: the sandbox deliberately leaves the
        # network namespace intact (research needs it), and the agent has bash,
        # curl, python3 and working DNS — verified. So a tool denial prevented
        # convenient research while leaving exfiltration entirely available.
        # Real egress control is a network-namespace + allowlist proxy, and it
        # should be built for its own sake, not simulated by a tool list.
        self.allowed_tools = [t.strip() for t in
                              (g("ATTICUS_ALLOWED_TOOLS",
                                 "WebSearch,WebFetch,Read,Write,Edit,Glob,Grep,Bash")
                               or "").split(",") if t.strip()]
        # Hard spend ceiling per recording. Wall-clock alone is a poor proxy: a
        # research fan-out can spend a lot in a few minutes, and one sentence
        # spoken near the device should not be able to run up an unbounded bill.
        # Blank disables it (and says so at startup rather than silently).
        self.max_budget_usd = (g("ATTICUS_MAX_BUDGET_USD", "2.00") or "").strip()
        # `or` fallback, not g()'s default: ATTICUS_SKILLS_DIR ships BLANK in
        # ops/.env, and now that "" is preserved rather than collapsed, an empty
        # value would otherwise become Path("") == cwd. Blank means "use the
        # repo's skills dir".
        self.skills_dir = Path(g("ATTICUS_SKILLS_DIR") or str(REPO / "skills"))

        # Upper bound on how much of a recording is ever transcribed.
        #
        # A command is 10-30 seconds and the wake phrase must be at the START,
        # so everything past a couple of minutes is silence or ambient audio
        # nobody meant to capture. Observed failure: a recording left running
        # for 39.6 minutes, of which the first ~12s was the actual command.
        #
        # We TRUNCATE rather than reject, because rejecting would have silently
        # discarded a real instruction — the same failure as a misheard wake
        # word. It is also a security bound: it caps how much of the operator's
        # day can ever reach the transcription API or the agent, whatever the
        # device does.
        self.max_command_seconds = int(g("ATTICUS_MAX_COMMAND_SECONDS", "180"))
        # Absurd length — do not even download. Plaud reports duration in the
        # listing, so this check is free.
        self.max_ingest_seconds = int(g("ATTICUS_MAX_INGEST_SECONDS", "7200"))

        # Chunking is the DOCUMENT path and is off by default, because
        # truncation is correct for a command: the wake phrase comes first, so
        # everything past the opening seconds is silence or ambient speech, and
        # transcribing 40 minutes of someone's day is both wasteful and a
        # privacy problem. Turn it on globally, or mark one recording by setting
        # "chunk_audio": true in its metadata JSON.
        self.chunk_long_audio = (g("ATTICUS_CHUNK_LONG_AUDIO", "off").lower()
                                 in ("1", "on", "true", "yes"))
        self.chunk_seconds = int(g("ATTICUS_CHUNK_SECONDS", "1200"))
        self.chunk_overlap_seconds = int(g("ATTICUS_CHUNK_OVERLAP_SECONDS", "10"))

        # Sanity gate — below this many words we refuse to execute.
        self.min_words = int(g("ATTICUS_MIN_WORDS", "3"))
        # Hard bound on the prompt handed to the agent, cut at a sentence
        # boundary. The full transcript is still written to the vault; this only
        # limits how much ambient speech can reach an autonomous agent. 600
        # chars comfortably fits every real command observed (longest ~640
        # chars of transcript, ~500 after the wake phrase).
        self.max_command_chars = int(g("ATTICUS_MAX_COMMAND_CHARS", "600"))
        # Sentence bound, which bites earlier than the character cap on dense
        # speech. The longest real command observed was 5 sentences.
        self.max_command_sentences = int(g("ATTICUS_MAX_COMMAND_SENTENCES", "6"))
        # Optional wake phrase. Empty = execute everything that transcribes.
        self.wake_phrase = (g("ATTICUS_WAKE_PHRASE", "") or "").strip().lower()
        # Known mishearings, exact-matched. NOT fuzzy matching: measured against
        # the real failure, "advocates" scores 0.375 similarity to "atticus" —
        # LOWER than unrelated words like "status" (0.615) and "practice"
        # (0.533). Any threshold loose enough to catch the real mistake would
        # fire on ordinary speech, and a false positive runs an autonomous agent
        # on words never addressed to it. A curated list is explicit, auditable,
        # and grows only when you observe a mishearing.
        # Probabilistic recovery when the strict gate fails. Asks a small model
        # whether the first word could be a mishearing of the wake phrase —
        # phonetics only, one word in, one token out, failing closed. Replaces
        # the need to maintain a whitelist: verdicts are cached, so the system
        # learns its own aliases. See processor/wake.py for the safety design.
        self.wake_adjudicator = (g("ATTICUS_WAKE_ADJUDICATOR", "on").lower()
                                 not in ("0", "off", "false", "no"))
        self.wake_adjudicator_model = g("ATTICUS_WAKE_ADJUDICATOR_MODEL", "gpt-4o-mini")
        self.wake_adjudicator_timeout = int(g("ATTICUS_WAKE_ADJUDICATOR_TIMEOUT", "15"))
        # Score at or above which a misheard word is admitted. Tuned against the
        # three real mishearings observed and a set of words that must NOT open
        # the gate — see tests/unit/test_wake.py.
        self.wake_adjudicator_threshold = int(g("ATTICUS_WAKE_ADJUDICATOR_THRESHOLD", "50"))
        # Kept as a deterministic escape hatch, empty by default now that the
        # adjudicator does this job. Populate it to force a match without a call.
        self.wake_aliases = [w.strip().lower() for w in
                             (g("ATTICUS_WAKE_ALIASES", "") or "").split(",")
                             if w.strip()]

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
