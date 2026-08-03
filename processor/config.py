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


def _csv(raw) -> list[str]:
    """A comma-separated setting as a list. Accepts an already-parsed list too, so
    a test can assign the shape it means without going through the string form."""
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _pairs(raw) -> dict:
    """`label=value,other=value` as a dict, for an allowlist that maps one to the
    other. A duplicate label is kept as the FIRST value and the collision is left
    for the handler to refuse — silently preferring one of two spellings of the
    same person is how a message reaches the wrong number."""
    if isinstance(raw, dict):
        return {str(k).strip().lower(): str(v).strip() for k, v in raw.items()}
    out = {}
    for part in _csv(raw):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k and v and k not in out:
            out[k] = v
    return out


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

        # ---- notification severity routing (issue #91) ----------------------
        # Two multi-day outages in one week were DELIVERY failures, not
        # detection failures: ingest dead 2d6h (#77) and the site path-watcher
        # dead 1.5 days. Both alarmed correctly into ntfy and drowned. A
        # calendar alert breaks through iOS Focus (#66, confirmed by the
        # operator), so `critical` earns that channel. "off" keeps ntfy only.
        self.notify_escalate = (g("ATTICUS_NOTIFY_ESCALATE", "on") or "on").strip().lower()
        # Escalate on PERSISTENCE, not one bad pass — a single failure is
        # usually transient, and a calendar event for it would train the
        # operator to ignore the strong channel too.
        self.escalate_after_failures = int(g("ATTICUS_ESCALATE_AFTER_FAILURES", "3") or 1)
        # A separate, LONGER throttle for the calendar channel. The ntfy
        # throttle is 6h; a 15-minute timer failing all night would otherwise
        # book 96 events. 0 disables the bound.
        self.escalate_throttle_hours = float(g("ATTICUS_ESCALATE_THROTTLE_HOURS", "12") or 0)
        # Local window in which routine and alert messages are PARKED rather
        # than sent, e.g. "22:00-07:00". They are never dropped: the 07:00
        # brief reports them. Critical ignores this entirely. Blank disables.
        self.quiet_hours = (g("ATTICUS_QUIET_HOURS", "") or "").strip()
        # T-74: the split's benefit — the processor can be down and work waits —
        # is also its failure mode, because a dead processor looks exactly like
        # an idle one. Nothing errors; recordings just pile up. This was
        # documented in .env, .env.example and configuration.md and read by NO
        # code, so the alarm the operator believed was armed did not exist.
        # 0 disables.
        # 60 to match ops/.env.example, which is what a real deployment starts
        # from. Two different numbers in the two files is exactly the drift the
        # derived test fixture now catches.
        self.backlog_alarm_minutes = int(g("ATTICUS_BACKLOG_ALARM_MINUTES", "60"))
        # ---- real-money budgets, one per paid service ----------------------
        #
        # ONE budget per service, because exhaustion means different things.
        # These were a single combined ATTICUS_API_BUDGET_USD and that was a bug:
        # TTS spent against the pot the transcribe gate checks, so an audio-heavy
        # month would halt the core pipeline over an optional feature.
        #
        # The agent's own usage is in NEITHER: `claude -p` runs on the operator's
        # subscription and bills nothing per token. Its per-recording bound is
        # ATTICUS_MAX_BUDGET_USD, which is imputed, not money. 0 disables a cap.

        # Transcription (plus the wake adjudicator, which bills the same key and
        # is derived from a transcript). The pipeline cannot run without this, so
        # exhaustion is a HARD stop that needs a human. It is also pennies —
        # ~$0.003 a recording, most of them ten seconds — so this cap is here to
        # catch a runaway loop, not to ration normal use. It should never be hit.
        self.transcription_budget_usd = float(
            g("ATTICUS_TRANSCRIPTION_BUDGET_USD", "2.00") or 0)

        # Text-to-speech. Optional, on demand, and the expensive one per unit
        # (~$0.05-0.10 an episode). Exhaustion skips ONLY the audio: the
        # transcript, the agent run and the published report all still happen —
        # the report simply does not get an episode attached. Nothing fails.
        self.tts_budget_usd = float(g("ATTICUS_TTS_BUDGET_USD", "10.00") or 0)

        # Warn on the way up, not only on arrival. PERCENTAGES of each budget, so
        # one setting serves a $2 cap and a $10 cap and keeps meaning the same
        # thing after either is changed — the old absolute list (2,3,4 dollars)
        # was tied to one combined budget and silently stopped meaning anything
        # once there were two. Blank disables warnings; the caps still apply.
        self.budget_alert_pct = [float(t) for t in
                                 (g("ATTICUS_BUDGET_ALERT_PCT", "50,80,100")
                                  or "").replace(" ", "").split(",") if t]

        # Superseded. Named explicitly so a config that still sets it gets told,
        # rather than silently running on defaults it did not choose.
        self._legacy_api_budget = (g("ATTICUS_API_BUDGET_USD", "") or "").strip()
        if self._legacy_api_budget:
            print("[WARNING] ATTICUS_API_BUDGET_USD is set but no longer used. It "
                  "was one pot for transcription AND text-to-speech, which let "
                  "audio spend stop transcription. Replace it with "
                  "ATTICUS_TRANSCRIPTION_BUDGET_USD (default 2.00) and "
                  "ATTICUS_TTS_BUDGET_USD (default 10.00); those are the values "
                  "in force now, NOT the one you set.", flush=True)

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
        # Naming the wake word in the steering prompt is the cheapest available
        # mishearing fix and it was missing: of nine attempts in one day three
        # came back "Advocates", "Abacus" and "Artemis". Biasing the transcriber
        # toward the word reduces the rate at source, which is strictly better
        # than recovering afterwards with an LLM adjudicator — every recovery is
        # a probabilistic call that can also admit ambient speech.
        self.stt_prompt = g(
            "ATTICUS_STT_PROMPT",
            "Transcribe with proper capitalization, including sentence "
            "beginnings, proper nouns, titles, and standard English "
            "capitalization rules. The speaker is dictating a short "
            "instruction or request, and often begins by saying the name "
            "\"Atticus\".",
        )

        # ---- the outbox (issue #42) -----------------------------------------
        # How a sandboxed agent causes anything to happen outside itself. It holds
        # no credentials, so it writes intent into output/outbox/ and the pipeline
        # performs it. See processor/outbox.py.
        #
        # "off" records intent and performs NOTHING, which is also how you test a
        # new handler safely.
        self.outbox = (g("ATTICUS_OUTBOX", "on") or "on").strip().lower()

        # Per risk class: "auto" performs it unattended, "confirm" records the
        # intent and waits for a human. Nobody is present during a pass, so
        # "confirm" means "not this pass" — deliberately, for anything that cannot
        # be taken back.
        #
        #   internal  only you see it, trivially undone (a todo, a reminder)
        #   tracked   others see it but it is recoverable and expected (a GitHub
        #             issue, an ADO work item)
        #   outward   a message to a person, immediate, NOT recallable (Signal,
        #             mail, Slack)
        #
        # outward defaults to confirm because the instruction originates in a
        # microphone worn in public. Read SECURITY.md's prompt-scoping section
        # before setting it to auto.
        self.outbox_internal = (g("ATTICUS_OUTBOX_INTERNAL", "auto") or "").strip().lower()
        self.outbox_tracked = (g("ATTICUS_OUTBOX_TRACKED", "confirm") or "").strip().lower()
        self.outbox_outward = (g("ATTICUS_OUTBOX_OUTWARD", "confirm") or "").strip().lower()

        # Per-VERB overrides, which win over the risk class. The classes alone are
        # too coarse: ATTICUS_OUTBOX_TRACKED=auto, set so GitHub issues can flow,
        # also opens outlook.event — calendar invites to other people. Without an
        # override the only way to open the verb you want is to open several you do
        # not, which is an incentive to over-grant.
        #
        #   ATTICUS_OUTBOX_VERB_GITHUB_ISSUE=auto
        #   ATTICUS_OUTBOX_VERB_SIGNAL_SEND=confirm
        self.outbox_verbs = {
            k[len("ATTICUS_OUTBOX_VERB_"):].lower().replace("_", ".", 1):
                (v or "").strip().lower()
            for k, v in {**f, **os.environ}.items()
            if k.startswith("ATTICUS_OUTBOX_VERB_") and (v or "").strip()
        }

        # Bound the fan-out. One misheard sentence must not be able to send thirty
        # messages, and a legitimate request rarely needs more than a couple of
        # actions. 0 removes the cap.
        self.outbox_max_actions = int(g("ATTICUS_OUTBOX_MAX_ACTIONS", "5") or 0)

        # ---- meeting mode (issue #86, ADR-008) ------------------------------
        # OFF, and turning it on is an explicit act rather than a preference.
        # This is the only feature here whose input is OTHER PEOPLE — a meeting
        # contains voices that never agreed to be transcribed by an AI, filed
        # in a git repository, or summarised by an autonomous agent. ADR-008 is
        # Proposed until the operator decides they have the standing to record
        # the meetings they would use it for, and will announce it every time.
        self.meeting_mode = (g("ATTICUS_MEETING_MODE", "off") or "off").strip().lower()
        # ADR-008 §2. False means meeting audio is DELETED the moment the
        # transcript is durable, never committed. Retention would not do:
        # ops/retention.py removes audio from the working tree and git history
        # keeps it, which is filing rather than expiry. Setting this true
        # deliberately breaks the condition the feature was built under.
        self.meeting_keep_audio = (g("ATTICUS_MEETING_KEEP_AUDIO", "false")
                                   or "false").strip().lower()
        # A real meeting yields more action items than the ordinary fan-out cap
        # allows, and silently dropping the sixth is exactly the quiet failure
        # this project treats as the worst kind. Applies ONLY to meeting-mode
        # recordings; every other recording keeps ATTICUS_OUTBOX_MAX_ACTIONS.
        self.meeting_max_actions = int(g("ATTICUS_MEETING_MAX_ACTIONS", "20") or 20)

        # ---- named projects (issue #84) -------------------------------------
        # How much of a project's brief.md reaches the agent's prompt. Bounded
        # like everything else that enters a prompt: long enough for real
        # context, short enough that it cannot crowd out the instruction or the
        # output contract. The block is fenced as reference material, because
        # it mixes operator prose with agent-written artifact titles.
        self.project_context_chars = int(g("ATTICUS_PROJECT_CONTEXT_CHARS", "2000") or 2000)

        # ---- lifecycle verbs (issue #82) ------------------------------------
        # How far back "that thing" can reach. Bounded on purpose: a wider
        # window makes ambiguity certain and lets a stray phrase reach a
        # recording from last month. Seven days covers "this morning",
        # "yesterday" and "the one from the weekend", which is what people
        # actually say about work in flight.
        self.lifecycle_within_days = int(g("ATTICUS_LIFECYCLE_WITHIN_DAYS", "7") or 7)

        # ---- the approval queue (issue #83) ---------------------------------
        # Where a DECISION comes back from. Deliberately a second ntfy topic
        # and not an endpoint on this host: approving must not be reachable
        # from the sandbox, which shares the host network namespace and could
        # otherwise scrape the vault-API token out of a published page and
        # approve its own held actions. The agent's env allowlist excludes
        # every ATTICUS_* URL, so it cannot even discover this topic.
        #
        # BLANK KEEPS THE OLD BEHAVIOUR: `confirm` means held forever, nothing
        # is queued, and no approval push is sent. That is the right default —
        # a queue nobody configured must not start accepting decisions from a
        # topic nobody chose.
        self.approval_topic_url = (g("ATTICUS_APPROVAL_TOPIC_URL", "") or "").strip()
        self.approvals_enabled = bool(self.approval_topic_url)
        # How long a held action waits. Approving a three-day-old "post to
        # Slack" is rarely right, and an expired item is reported rather than
        # dropped — the operator believes it is still waiting.
        self.approval_ttl_hours = float(g("ATTICUS_APPROVAL_TTL_HOURS", "24") or 24)

        # ---- GitHub, through `gh` (issue #50) --------------------------------
        #
        # WHICH REPOSITORIES A SPOKEN SENTENCE MAY FILE INTO. This list is the
        # control, and it is why the target repo is configuration rather than
        # something the agent supplies: `gh` on this host is authenticated as a
        # write-capable token on the operator's own account (ops/pr.sh depends on
        # it), so anything the token can reach is reachable by whatever was said
        # near the pin. A request may only SELECT from this list; a name that is
        # not on it is refused by processor/handlers/github.py.
        #
        # BLANK DISABLES THE CAPABILITY, which is the right default: nothing in
        # this repo should name a particular repository, and an operator who has
        # not thought about the blast radius should not have one. The first entry
        # is the default when a request names no repo at all.
        # Comma-separated owner/name.
        self.github_repos = [r.strip() for r in
                             (g("ATTICUS_GITHUB_REPOS", "") or "").split(",")
                             if r.strip()]
        # Labels applied to every issue Atticus files, so machine-filed issues are
        # distinguishable from hand-filed ones. Each label must ALREADY EXIST in
        # the target repo — `gh` fails the whole create otherwise — so this ships
        # blank rather than assuming a label anyone has.
        self.github_labels = [t.strip() for t in
                              (g("ATTICUS_GITHUB_LABELS", "") or "").split(",")
                              if t.strip()]
        self.gh_bin = g("ATTICUS_GH_BIN", "gh")
        # One API call behind a local binary. Generous enough for a slow network,
        # short enough that a hung `gh` cannot hold the pass open.
        self.github_timeout = int(g("ATTICUS_GITHUB_TIMEOUT", "60"))

        # ---- reminders (issue #52) ------------------------------------------
        # The operator's LOCAL timezone, as an IANA name. Everything else in this
        # project is UTC ISO-8601 by convention, deliberately — but "remind me at
        # four" is unambiguously local, and reading it as UTC does not ship a
        # misconfigured feature, it ships one that looks broken: a push four hours
        # late reads as a bug in the reminder.
        #
        # A NAME, never a fixed offset, because a reminder can be on the far side
        # of a DST boundary from the moment it was set. Blank falls back to the
        # host zone read out of /etc/localtime (still a real IANA name, so still
        # DST-correct); a name zoneinfo does not know is REFUSED rather than
        # quietly treated as UTC. See processor/reminders.py.
        self.local_tz = (g("ATTICUS_LOCAL_TZ", "") or "").strip()
        # How late a reminder may still fire after the box was down. Inside the
        # window it fires with "this was due at four — 3h ago", because a late
        # reminder is usually still worth having and silently dropping one is the
        # only outcome with no recovery. Past it, the reminder is marked expired
        # and reported in a single grouped push — a week of downtime should not
        # fire nine days of stale errands at once. 0 disables the bound (fire
        # everything, however old).
        self.reminder_max_late_hours = float(g("ATTICUS_REMINDER_MAX_LATE_HOURS", "24") or 0)
        # Refuse a due date further out than this. Catches a misparsed year, which
        # would otherwise be stored as a reminder that simply never fires and
        # leaves a JSONL line as the only evidence. 0 disables the bound.
        self.reminder_max_days = int(g("ATTICUS_REMINDER_MAX_DAYS", "365") or 0)
        # Also drop a short event on the operator's OWN calendar when a reminder
        # is set (issue #66). The operator's verdict on ntfy alone was "too soft
        # among all the other notifications", and a calendar alert is the only
        # free notification class on iOS that breaks through Focus (Time
        # Sensitive). Best-effort: until Calendars.ReadWrite is consented the
        # event is skipped with a receipt line and the push still works. "off"
        # disables the attempt entirely.
        self.reminder_calendar = (g("ATTICUS_REMINDER_CALENDAR", "on") or "on").strip().lower()
        # Length of that event. 15 minutes reads as a block to act on, not a
        # meeting; the alert fires at the START (the reminder's moment).
        self.reminder_event_minutes = int(g("ATTICUS_REMINDER_EVENT_MINUTES", "15") or 15)

        # ---- outbox handler settings, one block per service -----------------
        # Every secret here defaults to EMPTY and every allowlist defaults to
        # EMPTY, so a fresh install has every integration OFF and each handler
        # refuses by naming what is missing. That is deliberate: a credential that
        # arrives before the operator decided to grant it is a credential nobody
        # chose. `ops/.env.example` keeps the secrets blank for the same reason —
        # the test `cfg` fixture is built from that file, and several handlers'
        # refusal tests depend on the credential being absent.

        # Signal (skills/signal). The highest-consequence handler: a message to a
        # person, immediate and not recallable. The recipient allowlist maps a
        # spoken label to E.164 and matching is EXACT — "Nadya" does not reach
        # "Nadia". Empty means every send refuses.
        self.signal_recipients = _pairs(g("ATTICUS_SIGNAL_RECIPIENTS", ""))
        self.signal_from = (g("ATTICUS_SIGNAL_FROM", "") or "").strip()
        self.signal_cli = g("ATTICUS_SIGNAL_CLI", "signal-cli")
        self.signal_config_dir = (g("ATTICUS_SIGNAL_CONFIG_DIR", "") or "").strip()
        self.signal_max_chars = int(g("ATTICUS_SIGNAL_MAX_CHARS", "1000") or 0)
        self.signal_timeout = int(g("ATTICUS_SIGNAL_TIMEOUT", "60"))

        # Slack (skills/slack). A bot token (xoxb-), never a user token, and the
        # channel is a SELECTION from this allowlist rather than a value from the
        # request — "the standup channel" is one mishearing from #general.
        self.slack_bot_token = (g("ATTICUS_SLACK_BOT_TOKEN", "") or "").strip()
        self.slack_channels = _csv(g("ATTICUS_SLACK_CHANNELS", ""))
        self.slack_default_channel = (g("ATTICUS_SLACK_DEFAULT_CHANNEL", "") or "").strip()
        self.slack_api_url = g("ATTICUS_SLACK_API_URL",
                               "https://slack.com/api/chat.postMessage")
        self.slack_timeout = int(g("ATTICUS_SLACK_TIMEOUT", "15"))

        # Azure DevOps (skills/azure-devops). Project, area and iteration come from
        # HERE, never from the request: the agent has no basis for guessing them.
        # Default type is Task because it is the only work-item type present in
        # every default ADO process, so any other default can 404 by project.
        self.ado_pat = (g("ATTICUS_ADO_PAT", "") or "").strip()
        self.ado_org = (g("ATTICUS_ADO_ORG", "") or "").strip()
        self.ado_project = (g("ATTICUS_ADO_PROJECT", "") or "").strip()
        self.ado_base_url = g("ATTICUS_ADO_BASE_URL", "https://dev.azure.com")
        self.ado_area_path = (g("ATTICUS_ADO_AREA_PATH", "") or "").strip()
        self.ado_iteration_path = (g("ATTICUS_ADO_ITERATION_PATH", "") or "").strip()
        self.ado_workitem_type = g("ATTICUS_ADO_WORKITEM_TYPE", "Task")
        self.ado_workitem_types = _csv(g(
            "ATTICUS_ADO_WORKITEM_TYPES",
            "Task,Bug,Issue,User Story,Product Backlog Item,Feature,Epic"))
        self.ado_assigned_to = (g("ATTICUS_ADO_ASSIGNED_TO", "") or "").strip()
        self.ado_tags = _csv(g("ATTICUS_ADO_TAGS", "atticus"))
        self.ado_timeout = int(g("ATTICUS_ADO_TIMEOUT", "30"))

        # The todo list (skills/todo) needs NO configuration: it is a ledger in
        # the vault (processor/todos.py), decided in #51 / ADR-007. The four
        # ATTICUS_TODO_* settings the Graph backend used were removed with it.

        # Outlook (skills/outlook). Draft and calendar-event writes only; reading
        # is issue #63, not a gap in this skill. Two accounts exist with different
        # licensing and organservices' calendar is empty, so writing to the wrong
        # one looks like success — hence an explicit account rather than a guess.
        self.outlook_account = g("ATTICUS_OUTLOOK_ACCOUNT", "default")
        self.outlook_secrets = (g("ATTICUS_OUTLOOK_SECRETS", "") or "").strip()
        self.outlook_timeout = int(g("ATTICUS_OUTLOOK_TIMEOUT", "30"))
        self.outlook_timezone = (g("ATTICUS_OUTLOOK_TIMEZONE", "") or "").strip()
        self.outlook_event_minutes = int(g("ATTICUS_OUTLOOK_EVENT_MINUTES", "30"))
        self.outlook_max_recipients = int(g("ATTICUS_OUTLOOK_MAX_RECIPIENTS", "5"))
        self.outlook_min_confidence = float(g("ATTICUS_OUTLOOK_MIN_CONFIDENCE", "0.9"))
        self.outlook_graph_url = g("ATTICUS_OUTLOOK_GRAPH_URL",
                                   "https://graph.microsoft.com/v1.0")
        self.outlook_login_url = g("ATTICUS_OUTLOOK_LOGIN_URL",
                                   "https://login.microsoftonline.com")

        # Contact resolution (processor/contacts.py, ADR-006). Pipeline-side
        # infrastructure the handlers call, NOT an agent-facing lookup — that would
        # be a read, which issue #63 covers. Confidence tiers occupy disjoint
        # bands so a phonetic hit can never outrank an exact match.
        self.contacts_sources = _csv(g("ATTICUS_CONTACTS_SOURCES",
                                       "m365:people,m365:contacts"))
        self.contacts_m365_accounts = _csv(g("ATTICUS_CONTACTS_M365_ACCOUNTS",
                                             "default,organservices"))
        self.contacts_m365_limit = int(g("ATTICUS_CONTACTS_M365_LIMIT", "25"))
        self.contacts_timeout = int(g("ATTICUS_CONTACTS_TIMEOUT", "20"))
        self.contacts_cache_ttl_hours = int(g("ATTICUS_CONTACTS_CACHE_TTL_HOURS", "168"))
        self.contacts_min_confidence = float(g("ATTICUS_CONTACTS_MIN_CONFIDENCE", "0.75"))
        self.contacts_ambiguity_margin = float(g("ATTICUS_CONTACTS_AMBIGUITY_MARGIN", "0.15"))
        self.contacts_phonetic = (g("ATTICUS_CONTACTS_PHONETIC", "on") or "").strip().lower()
        self.contacts_max_results = int(g("ATTICUS_CONTACTS_MAX_RESULTS", "8"))
        self.contacts_git_repos = _csv(g("ATTICUS_CONTACTS_GIT_REPOS", ""))
        self.contacts_git_max_commits = int(g("ATTICUS_CONTACTS_GIT_MAX_COMMITS", "2000"))
        self.contacts_cache_path = g("ATTICUS_CONTACTS_CACHE_PATH", "")

        # Daily AI briefing. Extra tags to file it under, beyond "ai brief"
        # which brief.py always applies. Comma-separated; empty is fine.
        self.brief_tags = [t.strip() for t in
                           (g("ATTICUS_BRIEF_TAGS", "") or "").split(",")
                           if t.strip()]

        # Should the daily briefing also be voiced? Recurring daily spend, so it
        # gets its own switch rather than riding on the podcast setting: about
        # $0.09 an episode on Gemini, so roughly $2.80 a month at daily cadence,
        # which is a real fraction of ATTICUS_API_BUDGET_USD.
        # OFF by default. Audio is generated when ASKED for, not by default —
        # a spoken request says "and make me a podcast" and the agent writes a
        # script. The briefing has no speaker to ask, so defaulting it on created
        # recurring daily spend nobody requested. Opt in deliberately.
        self.brief_audio = (g("ATTICUS_BRIEF_AUDIO", "false") or "").strip().lower() \
            in ("1", "true", "yes", "on")

        # Audio overview ("podcast"). Opt-in: the agent only writes
        # output/podcast-script.md when the spoken request asked to listen to the
        # report, so with no script this stage does nothing and costs nothing.
        # Same provider as transcription deliberately — one audio stack, and the
        # key is already in ~/.config/ai/env, so no new credential enters the
        # pipeline. NotebookLM itself has no API: Google's Discovery Engine
        # Podcast API was deprecated in 2026 with no new allowlisting.
        # Which engine voices the script. Gemini is the default after a blind
        # A/B/C/D on one excerpt on 2026-07-31: ElevenLabs v3 won on quality but
        # costs 12x, and Gemini beat both OpenAI variants while being marginally
        # CHEAPER per episode than OpenAI — not because the rate is lower (both
        # work out to $0.015/min) but because its delivery is faster for the same
        # words, so there are fewer seconds to bill.
        #
        # The reason it wins is architectural, not cosmetic: Gemini takes both
        # speakers in ONE call, so it paces the whole conversation. OpenAI
        # synthesises one line per call, so a reply never knows it is a reply, and
        # 56 isolated lines read as two narrators rather than a conversation. No
        # amount of per-turn styling fixed that; variant B tried and came third.
        self.tts_provider = (g("ATTICUS_TTS_PROVIDER", "gemini") or "gemini").strip().lower()

        # Gemini path. Audio bills at a MEASURED 25 tokens/second (verified
        # 24.97-25.00 across three runs), and the API returns usageMetadata — so
        # cost here is measured rather than derived from duration.
        self.gemini_tts_url = g(
            "ATTICUS_GEMINI_TTS_URL",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")
        self.gemini_tts_model = g("ATTICUS_GEMINI_TTS_MODEL",
                                  "gemini-2.5-flash-preview-tts")
        self.gemini_voice_a = g("ATTICUS_GEMINI_VOICE_A", "Charon")
        self.gemini_voice_b = g("ATTICUS_GEMINI_VOICE_B", "Aoede")
        # One call renders the whole episode, so this is minutes not seconds:
        # a 56-turn script took 162s to return. Too tight a bound turns a working
        # episode into a timeout.
        self.gemini_tts_timeout = int(g("ATTICUS_GEMINI_TTS_TIMEOUT", "900"))
        self.gemini_tts_style = g(
            "ATTICUS_GEMINI_TTS_STYLE",
            "Read this as a natural, brisk two-host podcast conversation. "
            "{a} explains and is confident; {b} is curious and slightly "
            "skeptical, and reacts quickly. Do not sound like an advertisement.")

        # OpenAI path, retained as a fallback and for comparison.
        self.tts_url = g("ATTICUS_TTS_URL", "https://api.openai.com/v1/audio/speech")
        self.tts_model = g("ATTICUS_TTS_MODEL", "gpt-4o-mini-tts")
        self.tts_timeout = int(g("ATTICUS_TTS_TIMEOUT", "120"))
        # Two clearly distinct voices. Same-sounding hosts defeat the format —
        # the listener cannot tell who is asking and who is answering.
        # 128 kbps on 24 kHz mono speech is wasteful — the 12 kHz ceiling is the
        # limiter, not the bitrate — and every episode is a permanent git blob.
        # 48 kbps is transparent for two people talking and cuts each file ~60%.
        self.tts_bitrate_kbps = int(g("ATTICUS_TTS_BITRATE_KBPS", "48"))
        self.tts_voice_a = g("ATTICUS_TTS_VOICE_A", "onyx")
        self.tts_voice_b = g("ATTICUS_TTS_VOICE_B", "nova")
        self.tts_instructions = g(
            "ATTICUS_TTS_INSTRUCTIONS",
            "Conversational podcast host. Natural pace, warm but not "
            "performative. Do not sound like an advertisement.",
        )
        # Per-episode ceiling on REAL money, checked against an estimate before
        # the first request rather than discovered afterwards. The estimate is
        # derived from script length; a ten-minute episode runs about $0.15, so
        # the default stops a runaway script, not a normal one.
        self.podcast_max_usd = float(g("ATTICUS_PODCAST_MAX_USD", "0.50") or 0)

        # Ingest (WarDog). The transport is a pluggable executable — see
        # ingest/poller.py. Whichever transport wins (SPEC §2.2.1), it ships
        # a fetcher implementing the same four-command CLI.
        self.fetcher = g("ATTICUS_FETCHER", "ingest/plaud_web.py")
        self.fetcher_timeout = int(g("ATTICUS_FETCHER_TIMEOUT", "300"))
        self.poll_days = int(g("PLAUD_POLL_DAYS", "2"))

        # Execution
        self.claude_bin = g("ATTICUS_CLAUDE_BIN", "claude")
        # How the agent AUTHENTICATES. Blank (the default) bind-mounts the
        # operator's ~/.claude/.credentials.json read-only — which works only
        # while some interactive session has refreshed its 8-hour access token,
        # so an idle overnight box fails every run until a human shows up
        # (observed 2026-07-30). Set this to a 0600 file holding the output of
        # `claude setup-token` (a long-lived subscription token, ~1 year) and
        # the pipeline instead passes CLAUDE_CODE_OAUTH_TOKEN into the sandbox
        # and stops binding the credential file entirely — no 8-hour dependency,
        # and the operator's refresh token never enters the sandbox (#68).
        # Set-but-unusable REFUSES the run loudly rather than silently falling
        # back to the credential this setting exists to retire.
        self.claude_token_file = (g("ATTICUS_CLAUDE_TOKEN_FILE", "") or "").strip()
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
        # Which of the operator's GLOBAL skills the agent may see. Binding the
        # whole ~/.claude/skills directory gave it an inventory of unrelated
        # infrastructure (M365 addresses, ntfy topics, provider cost sheets).
        # Blank = bind everything, which is the old behaviour.
        self.global_skills = [s.strip() for s in
                              (g("ATTICUS_GLOBAL_SKILLS",
                                 "html-artifact-output,dataviz")
                               or "").split(",") if s.strip()]
        # 'host' (default) shares the host network namespace — research works,
        # but so does reaching loopback services, which includes the vault's own
        # web UI and its write token. 'none' unshares the network entirely: the
        # right setting when no skill in use needs the internet.
        self.sandbox_net = (g("ATTICUS_SANDBOX_NET", "host") or "host").strip().lower()
        # Ceiling on what one utterance can commit. The vault is git, where
        # deletion is deliberately hard, so unbounded agent output is permanent.
        # A plain literal, not str(50 * 1024 * 1024): gen-config-docs.py reads
        # defaults out of this source with a regex that only matches string
        # literals, so an expression here silently omits the knob from
        # docs/configuration.md altogether.
        self.max_output_files = int(g("ATTICUS_MAX_OUTPUT_FILES", "50"))
        self.max_output_bytes = int(g("ATTICUS_MAX_OUTPUT_BYTES", "52428800"))  # 50 MB
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
        #
        # Raised from 50. At 50 the rule was "more likely than not", decided by a
        # small model, on the control that ADR-003 makes load-bearing for
        # credential safety now that the Plaud session shares this host. The
        # practical gate for a non-matching first word was "sounds like a name
        # and sounds like a task a computer would do" — which "Marcus, can you
        # look up train times for me" satisfies. A false accept executes ambient
        # speech; a false reject files a recoverable note. The costs are not
        # symmetric, so neither should the threshold be.
        self.wake_adjudicator_threshold = int(g("ATTICUS_WAKE_ADJUDICATOR_THRESHOLD", "75"))
        # Verdicts were cached forever, so one wrong admit permanently opened
        # that (word, context) pair and later passes logged only "cached verdict
        # … admit". A TTL bounds the damage and forces re-adjudication.
        self.wake_verdict_ttl_hours = int(g("ATTICUS_WAKE_VERDICT_TTL_HOURS", "168"))
        # Kept as a deterministic escape hatch, empty by default now that the
        # adjudicator does this job. Populate it to force a match without a call.
        self.wake_aliases = [w.strip().lower() for w in
                             (g("ATTICUS_WAKE_ALIASES", "") or "").split(",")
                             if w.strip()]

        self._openai_key = None
        self._gemini_key = None

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

    @property
    def gemini_key(self) -> str:
        """Read on demand from the shared credential file. Never logged.

        Same convention as openai_key. No prefix check: Google API keys start
        "AIza" today but that is not a documented contract, and rejecting a valid
        key on a guessed shape is worse than passing a bad one to the API, which
        answers with a clear 401.
        """
        if self._gemini_key is None:
            k = (os.environ.get("GEMINI_API_KEY")
                 or _parse_env(AI_ENV).get("GEMINI_API_KEY", ""))
            if not k:
                raise RuntimeError(
                    f"GEMINI_API_KEY not found in environment or {AI_ENV} — "
                    f"needed because ATTICUS_TTS_PROVIDER is 'gemini'"
                )
            self._gemini_key = k
        return self._gemini_key

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
