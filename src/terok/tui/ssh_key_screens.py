# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""SSH key comments and public-key views shared by routing and setup."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class SshKeyCommentScreen(ModalScreen[str | None]):
    """An editable key comment; Escape cancels without creating or changing a key.

    A blank submission accepts the displayed suggestion. Rename callers
    set ``allow_empty`` so an existing comment can also be removed.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    SshKeyCommentScreen { align: center middle; }
    #ssh-key-comment { width: 60; background: $surface; }
    """

    def __init__(self, comment: str, *, title: str, allow_empty: bool = False) -> None:
        """Prefill the comment and label the requested key operation."""
        super().__init__()
        self._comment = comment
        self._title = title
        self._allow_empty = allow_empty

    def compose(self) -> ComposeResult:
        """Offer an editable comment with explicit accept and cancel shortcuts."""
        box = Input(value=self._comment, id="ssh-key-comment")
        box.border_title = self._title
        box.border_subtitle = "Enter to save · Esc to cancel"
        yield box

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Return the chosen comment, retaining a mint suggestion on blank input."""
        self.dismiss(event.value if event.value or self._allow_empty else self._comment)

    def action_cancel(self) -> None:
        """Dismiss without changing a key."""
        self.dismiss(None)


class ShowSshKeyScreen(ModalScreen[None]):
    """Borderless full-screen public key for host-terminal mouse copying.

    Clipboard helpers may be unavailable over SSH. Shift-drag bypasses
    Textual's mouse handling, so the key has no border or horizontal
    padding that terminal selection could accidentally include. Character
    wrapping keeps long public keys visible on narrow terminals.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("enter", "close", "Close"),
    ]

    CSS = """
    ShowSshKeyScreen {
        background: $surface;
        padding: 1 0;
    }

    #wizard-ssh-show-hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    #wizard-ssh-show-pubkey {
        height: auto;
    }
    """

    def __init__(self, public_line: str) -> None:
        """Store the SSH public key line to render."""
        super().__init__()
        self._public_line = public_line

    def compose(self) -> ComposeResult:
        """Build the hint and bare public key."""
        yield Static(
            "Shift-drag to select the key below  ·  Esc, Enter or q to return",
            id="wizard-ssh-show-hint",
        )
        yield Static(
            Text(self._public_line, overflow="fold", no_wrap=False),
            id="wizard-ssh-show-pubkey",
        )

    def action_close(self) -> None:
        """Return to the key operation that opened this view."""
        self.dismiss(None)
