# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Native text editor / viewer modals.

The in-process editing surface for text the TUI edits — the only one
under the web TUI, where no terminal exists for ``$EDITOR`` to take
over (issue #473).  Both are plain in-process modals — no child
process, no terminal:

* [`TextEditorScreen`][terok.tui.text_screens.TextEditorScreen] — an
  editable ``TextArea``; dismisses with the edited text on Save,
  ``None`` on Cancel.  An optional validator refuses a Save and shows
  why; an optional language turns on syntax highlighting.
* [`TextViewScreen`][terok.tui.text_screens.TextViewScreen] — the same
  widget in read-only mode for display-only content.
* [`code_text_area`][terok.tui.text_screens.code_text_area],
  [`ValidationErrorLine`][terok.tui.text_screens.ValidationErrorLine] and
  [`edit_externally_into`][terok.tui.text_screens.edit_externally_into] —
  the syntax-highlighting ``TextArea`` setup, the refused-save line and
  the ``$EDITOR`` round-trip for a text area, shared across the TUI's
  text editors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


def code_text_area(text: str, *, language: str, widget_id: str) -> TextArea:
    """Return a syntax-highlighting ``TextArea`` over *text*, set up one way everywhere.

    Line numbers and the dark highlight theme come with ``code_editor``.
    Tab moves the focus instead of indenting, so Tab reaches the dialog
    buttons and the first Esc reaches the dialog's cancel binding.
    """
    return TextArea.code_editor(
        text, language=language, theme="vscode_dark", tab_behavior="focus", id=widget_id
    )


class ValidationErrorLine(Static):
    """The line under an editor that says why its text was refused, hidden when there is none.

    *validate* returns why a text must not be saved, or ``None``; without
    one, every text passes.
    """

    DEFAULT_CSS = """
    ValidationErrorLine {
        color: $error;
        height: auto;
        max-height: 8;
        overflow-y: auto;
        margin-bottom: 1;
        display: none;
    }
    """

    def __init__(self, validate: Callable[[str], str | None] | None = None) -> None:
        """Create the line empty and hidden."""
        super().__init__("", markup=False)
        self._validate = validate

    def check(self, text: str) -> bool:
        """Show the verdict on *text*, or hide the line when it passes; return whether it passed."""
        verdict = self._validate(text) if self._validate else None
        self.update(verdict or "")
        self.display = verdict is not None
        return verdict is None


async def edit_externally_into(
    area: TextArea, edit_externally: Callable[[str], Awaitable[str | None]], *, title: str
) -> bool:
    """Send *area*'s text through ``$EDITOR``; an accepted text replaces it.

    *edit_externally* returns the accepted text, or ``None`` when the
    operator abandons the edit.  A failure is reported under *title*,
    never raised.  Returns whether the text was replaced.
    """
    try:
        edited = await edit_externally(area.text)
    except Exception as exc:  # noqa: BLE001 - an edit must never fail silently
        area.notify(f"Editing {title} failed: {exc}", severity="error", markup=False)
        return False
    if edited is None:
        return False
    area.load_text(edited)
    return True


class TextEditorScreen(ModalScreen[str | None]):
    """Modal ``TextArea`` editor.

    Dismisses with the (possibly edited) text on Save, or ``None`` on
    Cancel / Escape — the caller decides whether to persist.  A
    *validate* callback returns why a text must not be saved, or
    ``None``: a refused Save keeps the editor open with the reason under
    the text.  The check also runs when the editor opens, so a text that
    is already invalid shows its problem at once.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    CSS = """
    TextEditorScreen {
        align: center middle;
    }

    #text-editor-dialog {
        width: 90%;
        height: 85%;
        border: heavy $primary;
        border-title-align: right;
        background: $surface;
        padding: 1 2;
    }

    #text-editor-area {
        height: 1fr;
        margin-bottom: 1;
    }

    #text-editor-buttons {
        height: 3;
        align-horizontal: right;
    }

    #text-editor-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        text: str,
        *,
        title: str,
        language: str | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> None:
        """Create the editor over *text* with *title* on the dialog border."""
        super().__init__()
        self._text = text
        self._title = title
        self._language = language
        self._validate = validate

    def compose(self) -> ComposeResult:
        """Lay out the editable text area, the error line and the Cancel / Save buttons."""
        dialog = Vertical(id="text-editor-dialog")
        dialog.border_title = self._title
        dialog.border_subtitle = "Ctrl+S save · Esc cancel"
        with dialog:
            if self._language is None:
                yield TextArea(self._text, id="text-editor-area")
            else:
                yield code_text_area(
                    self._text, language=self._language, widget_id="text-editor-area"
                )
            yield ValidationErrorLine(self._validate)
            with Horizontal(id="text-editor-buttons"):
                yield Button("Cancel", id="text-editor-cancel", variant="default")
                yield Button("Save", id="text-editor-save", variant="primary")

    def on_mount(self) -> None:
        """Show any problem the text already has, and focus the text area for typing."""
        self.query_one(ValidationErrorLine).check(self._text)
        self.query_one("#text-editor-area", TextArea).focus()

    def action_cancel(self) -> None:
        """Escape — dismiss without saving."""
        self.dismiss(None)

    def action_save(self) -> None:
        """Ctrl+S / Save — dismiss with the current text, unless the validator refuses it."""
        text = self.query_one("#text-editor-area", TextArea).text
        if self.query_one(ValidationErrorLine).check(text):
            self.dismiss(text)

    @on(Button.Pressed, "#text-editor-cancel")
    def _on_cancel(self) -> None:
        """Cancel button — dismiss without saving."""
        self.dismiss(None)

    @on(Button.Pressed, "#text-editor-save")
    def _on_save(self) -> None:
        """Save button — same as Ctrl+S."""
        self.action_save()


class TextViewScreen(ModalScreen[None]):
    """Read-only modal text viewer — scrollable, selectable, no terminal."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    CSS = """
    TextViewScreen {
        align: center middle;
    }

    #text-view-dialog {
        width: 90%;
        height: 85%;
        border: heavy $primary;
        border-title-align: right;
        background: $surface;
        padding: 1 2;
    }

    #text-view-area {
        height: 1fr;
        margin-bottom: 1;
    }

    #text-view-buttons {
        height: 3;
        align-horizontal: right;
    }
    """

    def __init__(self, text: str, *, title: str) -> None:
        """Create the viewer over *text* with *title* on the dialog border."""
        super().__init__()
        self._text = text
        self._title = title

    def compose(self) -> ComposeResult:
        """Lay out the read-only text area and a Close button."""
        dialog = Vertical(id="text-view-dialog")
        dialog.border_title = self._title
        with dialog:
            yield TextArea(self._text, id="text-view-area", read_only=True)
            with Horizontal(id="text-view-buttons"):
                yield Button("Close", id="text-view-close", variant="default")

    @on(Button.Pressed, "#text-view-close")
    def _on_close(self) -> None:
        """Close button — dismiss the viewer."""
        self.dismiss()
