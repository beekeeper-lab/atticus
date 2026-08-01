"""Outbox handlers, one module per service.

Importing this package registers every handler with `outbox`. A skill's SKILL.md
and its handler are two halves of one feature: the skill tells the agent which verb
to write, and the handler is the only thing that can perform it.

Each module is expected to fail CLEANLY when its credential is absent — raising
`OutboxError` with the name of the missing variable — because that is the normal
state for a service nobody has set up yet, and a stack trace is not a diagnosis.

Imported for their side effect. A module missing from this list registers nothing,
and its verb is then refused as unknown — which is loud, but the loudness happens
in production rather than here, so add the import in the same commit as the module.
"""
# Importing a module is what REGISTERS its verbs. A handler absent from this list
# has passing tests (they import it directly) and is refused as an unknown verb in
# production — so this list is load-bearing, not bookkeeping.
from . import ado  # noqa: F401        ado.workitem, ado.comment
from . import github  # noqa: F401     github.issue, github.comment
from . import outlook  # noqa: F401    outlook.draft, outlook.event
from . import reminders  # noqa: F401  reminders.set — needs no credential at all
from . import signal  # noqa: F401     signal.send
from . import slack  # noqa: F401      slack.post
from . import todo  # noqa: F401       todo.add
