from discord.ext import commands


class VCError(commands.CommandError):
    """User-facing error (shown as  ❌ message)."""


class CommandDisabled(commands.CheckFailure):
    """The command was turned off in this channel / category with $disablecommand."""

    def __init__(self, name: str):
        super().__init__(f"{name} is disabled here")
        self.name = name
