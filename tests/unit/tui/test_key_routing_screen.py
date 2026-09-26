# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Verify SSH key routing labels, mutation guards, and minting shortcuts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
from textual.app import App
from textual.widgets import Input, SelectionList, Static

from terok.lib.api.ssh_routing import KeyRouting
from terok.tui import key_routing_screen
from terok.tui.key_routing_screen import (
    KeyInventoryScreen,
    KeyRoutingScreen,
    _BaseRoutingScreen,
    _hint,
    _inventory_label,
    _key_label,
    _ProjectPickerScreen,
)
from terok.tui.ssh_key_screens import ShowSshKeyScreen, SshKeyCommentScreen
from terok.tui.widgets.routing_matrix import RoutingMatrix


def _row(*, comment: str = "", key_type: str = "ed25519", fingerprint: str = "SHA256:abcdef"):
    """A stand-in key row carrying the fields the label helpers read."""
    return SimpleNamespace(comment=comment, key_type=key_type, fingerprint=fingerprint)


class _RoutingHost(App[None]):
    """Run the routing screen in the smallest app that can exercise its bindings."""

    def on_mount(self) -> None:
        """Open the routing screen."""
        self.push_screen(KeyRoutingScreen())


@pytest.fixture
def mint_calls(monkeypatch):
    """Serve one routed key and record every project passed to the minting API."""
    key = SimpleNamespace(
        id=1,
        comment="alpha-1",
        key_type="ed25519",
        fingerprint="SHA256:abcdef",
    )
    routing = KeyRouting(
        keys=(key,),
        projects=("alpha", "beta"),
        links=frozenset({("alpha", key.id)}),
        defaults={"alpha": key.id},
    )
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(key_routing_screen, "load_key_routing", lambda: routing)
    monkeypatch.setattr(key_routing_screen, "suggested_key_comment", lambda scope: f"{scope}-2")
    monkeypatch.setattr(
        key_routing_screen, "mint_key", lambda scope, *, comment: calls.append((scope, comment))
    )
    return calls


class TestMintShortcuts:
    """Every advertised ``n`` path reaches the project-scoped minting API."""

    @pytest.mark.asyncio
    async def test_matrix_mints_for_cursor_project(self, mint_calls) -> None:
        """Matrix mode derives the project from the cursor column."""
        app = _RoutingHost()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, SshKeyCommentScreen)
            assert app.screen.query_one(Input).value == "alpha-2"
            assert not mint_calls
            await pilot.press("enter")

        assert mint_calls == [("alpha", None)]

    @pytest.mark.asyncio
    async def test_list_mode_opens_picker_and_mints_selection(self, mint_calls) -> None:
        """List mode asks for the otherwise ambiguous project and mints the selection."""
        app = _RoutingHost()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("m", "n")
            await pilot.pause()
            assert isinstance(app.screen, _ProjectPickerScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SshKeyCommentScreen)
            await pilot.press("enter")

        assert mint_calls == [("alpha", None)]

    @pytest.mark.asyncio
    async def test_inventory_picker_mints_selection(self, mint_calls) -> None:
        """Inventory mode carries the project picker result into the minting API."""
        app = _RoutingHost()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("i", "n")
            await pilot.pause()
            assert isinstance(app.screen, _ProjectPickerScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SshKeyCommentScreen)
            await pilot.press("enter")
            assert isinstance(app.screen, KeyInventoryScreen)

        assert mint_calls == [("alpha", None)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("typed", "expected"), [("deploy-key", "deploy-key"), ("", None)])
    async def test_comment_can_override_or_accept_suggestion(self, mint_calls, typed, expected):
        """Custom comments reach mint unchanged; a blank input keeps the suggestion."""
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press("n")
            app.screen.query_one(Input).value = typed
            await pilot.press("enter")
        assert mint_calls == [("alpha", expected)]

    @pytest.mark.asyncio
    async def test_accepted_suggestion_is_allocated_when_minting(self, mint_calls, monkeypatch):
        """Another mint may occupy the previewed name while the dialog is open."""
        next_comment = "alpha-2"
        allocated = []
        monkeypatch.setattr(
            key_routing_screen,
            "mint_key",
            lambda scope, *, comment: allocated.append(
                next_comment if comment is None else comment
            ),
        )
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press("n")
            assert app.screen.query_one(Input).value == "alpha-2"
            next_comment = "alpha-3"
            await pilot.press("enter")
        assert allocated == ["alpha-3"]

    @pytest.mark.asyncio
    async def test_cancel_comment_does_not_mint(self, mint_calls):
        """Escape leaves the vault unchanged."""
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press("n", "escape")
            assert isinstance(app.screen, KeyRoutingScreen)
        assert not mint_calls

    def test_suggestion_failure_is_reported(self, monkeypatch):
        """A locked vault produces a notification rather than a broken prompt."""
        monkeypatch.setattr(
            key_routing_screen,
            "suggested_key_comment",
            mock.Mock(side_effect=RuntimeError("locked")),
        )
        duck = SimpleNamespace(app=mock.Mock())
        _BaseRoutingScreen._mint(duck, "alpha")
        duck.app.push_screen.assert_not_called()
        duck.app.notify.assert_called_once_with("Mint failed: locked", severity="error")


class TestPublicKey:
    """Every key view exposes the selected public line for terminal copying."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [(), ("m",), ("i",)])
    async def test_public_key_shortcut(self, mint_calls, monkeypatch, mode):
        """Matrix, list, and inventory all display a complete public key."""
        public_line = f"ssh-rsa {'A' * 720} alpha-1"
        read = mock.Mock(return_value=public_line)
        monkeypatch.setattr(key_routing_screen, "public_key", read)
        app = _RoutingHost()
        async with app.run_test(size=(50, 30)) as pilot:
            await pilot.press(*mode, "p")
            assert isinstance(app.screen, ShowSshKeyScreen)
            pubkey = app.screen.query_one("#wizard-ssh-show-pubkey", Static)
            displayed = pubkey.content
            assert displayed.plain == public_line
            assert displayed.overflow == "fold"
            assert pubkey.size.height > 1
            assert app.screen.styles.padding.left == app.screen.styles.padding.right == 0
            await pilot.press("escape")
            assert not isinstance(app.screen, ShowSshKeyScreen)
        read.assert_called_once_with(1)
        assert not mint_calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [(), ("m",), ("i",)])
    async def test_no_key_is_a_noop(self, monkeypatch, mode):
        """Empty views do not request a public key or open a modal."""
        monkeypatch.setattr(
            key_routing_screen,
            "load_key_routing",
            lambda: KeyRouting(keys=(), projects=("alpha",), links=frozenset()),
        )
        read = mock.Mock()
        monkeypatch.setattr(key_routing_screen, "public_key", read)
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press(*mode, "p")
            assert not isinstance(app.screen, ShowSshKeyScreen)
        read.assert_not_called()

    def test_read_failure_is_reported(self, monkeypatch):
        """A deleted key or locked vault is reported without opening a viewer."""
        monkeypatch.setattr(
            key_routing_screen, "public_key", mock.Mock(side_effect=RuntimeError("locked"))
        )
        duck = SimpleNamespace(app=mock.Mock())
        _BaseRoutingScreen._show_public_key(duck, 1)
        duck.app.push_screen.assert_not_called()
        duck.app.notify.assert_called_once_with("Public key unavailable: locked", severity="error")


class TestDefaultKey:
    """The default belongs to a linked key and a selected project, not its comment."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [(), ("m", "l")])
    async def test_linked_key_can_be_made_default(self, mint_calls, monkeypatch, mode):
        """Both routing views persist the highlighted scope/key pair."""
        choose = mock.Mock()
        monkeypatch.setattr(key_routing_screen, "set_default_key", choose)
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press(*mode, "f")
        choose.assert_called_once_with("alpha", 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [("down",), ("m", "down", "l")])
    async def test_changed_default_is_reloaded(self, mint_calls, monkeypatch, mode):
        """Choosing another key repaints its default marker without changing links."""
        routing = key_routing_screen.load_key_routing()
        second = SimpleNamespace(**(vars(routing.keys[0]) | {"id": 2, "comment": "alpha-2"}))
        routing = replace(
            routing, keys=(*routing.keys, second), links=routing.links | {("alpha", 2)}
        )
        monkeypatch.setattr(key_routing_screen, "load_key_routing", lambda: routing)
        choose = mock.Mock(
            side_effect=lambda scope, key_id: routing.defaults.update({scope: key_id})
        )
        monkeypatch.setattr(key_routing_screen, "set_default_key", choose)
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press(*mode, "f")
            matrix = app.screen.query_one(RoutingMatrix)
            assert "*" in matrix._render_row(1, matrix._keys[1]).plain
            if "m" in mode:
                checklist = app.screen.query_one("#kr-projects", SelectionList)
                assert str(checklist.get_option_at_index(0).prompt) == "alpha (default)"
        choose.assert_called_once_with("alpha", 2)
        assert routing.links == frozenset({("alpha", 1), ("alpha", 2)})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [("right",), ("m", "l", "down")])
    async def test_unlinked_key_cannot_be_made_default(self, mint_calls, monkeypatch, mode):
        """Default selection does not silently create a link."""
        choose = mock.Mock()
        monkeypatch.setattr(key_routing_screen, "set_default_key", choose)
        app = _RoutingHost()
        async with app.run_test() as pilot:
            with mock.patch.object(app, "notify") as notify:
                await pilot.press(*mode, "f")
                notify.assert_called_once()
                assert "Link this key" in notify.call_args.args[0]
        choose.assert_not_called()

    @pytest.mark.asyncio
    async def test_checklist_marks_default_scope(self, mint_calls):
        """List mode labels the default connection without changing its scope value."""
        app = _RoutingHost()
        async with app.run_test() as pilot:
            await pilot.press("m")
            checklist = app.screen.query_one("#kr-projects", SelectionList)
            option = checklist.get_option_at_index(0)
            assert str(option.prompt) == "alpha (default)"
            assert option.value == "alpha"


@pytest.mark.asyncio
async def test_rename_can_clear_comment(mint_calls, monkeypatch):
    """The shared comment prompt still permits an explicitly blank rename."""
    rename = mock.Mock()
    monkeypatch.setattr(key_routing_screen, "rename_key", rename)
    app = _RoutingHost()
    async with app.run_test() as pilot:
        await pilot.press("c")
        app.screen.query_one(Input).value = ""
        await pilot.press("enter")
    rename.assert_called_once_with("SHA256:abcdef", "")


class TestKeyLabel:
    """A key's display label prefers its comment, else type + fingerprint."""

    def test_uses_comment_when_present(self):
        """A commented key shows its comment."""
        assert _key_label(_row(comment="tk-main:foo")) == "tk-main:foo"

    def test_falls_back_to_type_and_fingerprint(self):
        """A blank comment yields the key type and a 16-char fingerprint prefix."""
        label = _key_label(_row(comment="", fingerprint="SHA256:0123456789abcdef0"))
        assert label == "ed25519 SHA256:012345678"


class TestInventoryLabel:
    """The catalog row carries metadata and the projects served."""

    def test_lists_served_projects(self):
        """Linked scopes appear; type and fingerprint are spelled out in full."""
        label = _inventory_label(_row(comment="k"), ["bar", "foo"])
        assert "ed25519  SHA256:abcdef" in label
        assert "projects: bar, foo" in label

    def test_unlinked_key_shows_dash(self):
        """A key with no projects renders an em dash."""
        assert "projects: —" in _inventory_label(_row(), [])


class TestApplyGuard:
    """Every vault mutation funnels through _apply, which must catch failures."""

    def test_success_reloads_without_toast(self):
        """A clean mutation repaints and raises no notification."""
        duck = SimpleNamespace(app=mock.Mock(), reload=mock.Mock())
        _BaseRoutingScreen._apply(duck, lambda: None, "Boom")
        duck.reload.assert_called_once()
        duck.app.notify.assert_not_called()

    def test_failure_toasts_and_skips_reload(self):
        """A raising mutation is caught, surfaced as an error, and does not repaint."""
        duck = SimpleNamespace(app=mock.Mock(), reload=mock.Mock())

        def boom():
            raise RuntimeError("vault locked")

        _BaseRoutingScreen._apply(duck, boom, "Unlink failed")
        duck.app.notify.assert_called_once()
        assert "Unlink failed" in duck.app.notify.call_args[0][0]
        assert duck.app.notify.call_args.kwargs["severity"] == "error"
        duck.reload.assert_not_called()


class TestHint:
    """The footer-style hint flips the mode label and colours its keys."""

    def test_m_names_the_target_mode(self):
        """``m`` advertises the mode it switches to, not the current one."""
        assert "m[/] list mode" in _hint(list_mode=False)
        assert "m[/] matrix mode" in _hint(list_mode=True)

    def test_keys_wear_the_footer_colour(self):
        """Every shortcut key is wrapped in the footer key-colour variable."""
        assert "[$footer-key-foreground]space[/]" in _hint(list_mode=False)
        assert "[$footer-key-foreground]r[/]" in _hint(list_mode=False)
