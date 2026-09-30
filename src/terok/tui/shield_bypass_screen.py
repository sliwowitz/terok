# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The timed allow-all window, asked for once and spelled out."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_CONSEQUENCE = (
    "While the window is open, what the allowlist refuses is\n"
    "accepted and logged — and nothing more.  The window sits\n"
    "below the security tier: vault-relayed provider hosts and\n"
    "private ranges stay refused, and the hard-deny floor\n"
    "(link-local, cloud metadata) always does.\n"
    "It opens only while the shield is up, and closes itself\n"
    "when the time runs out — nothing renews it."
)


class ShieldBypassScreen(ModalScreen[str | None]):
    """Ask for the window's duration, having said what opening it means.

    The window sits below the security tier in the shield's chain: it admits
    what the allowlist alone would refuse, never a vault-relayed provider
    host, a private range or the hard-deny floor, and only the UP posture
    reads it — so the text says exactly that rather than "everything".

    The duration is the whole dialogue: submitting one opens the window for it,
    submitting an empty field closes the window now, and Escape leaves the
    shield as it is.  Opening an allow-all is the kind of thing an operator
    should have to read a sentence about first, which is why the consequence
    sits above the field rather than in the docs.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ShieldBypassScreen { align: center middle; }
    #bypass-box { width: 64; height: auto; background: $surface; }
    #bypass-consequence { width: 64; padding: 1 2; background: $surface; }
    """

    def __init__(self, duration: str) -> None:
        """Prefill the configured duration for the operator to accept or edit."""
        super().__init__()
        self._duration = duration

    def compose(self) -> ComposeResult:
        """Show what the window does, then the duration to grant it for."""
        yield Static(_CONSEQUENCE, id="bypass-consequence")
        box = Input(value=self._duration, id="bypass-box")
        box.border_title = "Bypass window — accept what the allowlist refuses for"
        box.border_subtitle = "Enter to open · empty to close it now · Esc to cancel"
        yield box

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Return the duration to grant, or ``""`` to close the window now."""
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        """Dismiss without touching the window."""
        self.dismiss(None)
