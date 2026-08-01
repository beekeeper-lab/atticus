"""Outbox handlers, one module per service.

Importing this package registers every handler with `outbox`. A skill's SKILL.md
and its handler are two halves of one feature: the skill tells the agent which verb
to write, and the handler is the only thing that can perform it.

Each module is expected to fail CLEANLY when its credential is absent — raising
`OutboxError` with the name of the missing variable — because that is the normal
state for a service nobody has set up yet, and a stack trace is not a diagnosis.
"""
