#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

# Common environment setup for terok ACP wrappers.
#
# Sourced (not exec'd) by per-agent ACP wrapper scripts.
# Sets up git identity using the shared helper.
#
# Expected environment:
#   TEROK_UNRESTRICTED  - "1" to enable unrestricted mode
#   TEROK_GIT_AUTHORSHIP - authorship policy (default: agent-human)
#   HUMAN_GIT_NAME       - human git name
#   HUMAN_GIT_EMAIL      - human git email
#
# Usage (from a wrapper script):
#   _AGENT_NAME="Claude" _AGENT_EMAIL="noreply@anthropic.com"
#   . /usr/local/share/terok/terok-acp-env.sh

# Source git identity helper and apply identity for this agent
if [ -r /usr/local/share/terok/terok-git-identity.sh ]; then
    . /usr/local/share/terok/terok-git-identity.sh
    _terok_apply_git_identity "${_AGENT_NAME:?}" "${_AGENT_EMAIL:?}"
fi

# Copy per-task instructions to ~/.claude/CLAUDE.md for Claude ACP discovery.
# This is a shared volume (last writer wins) — acceptable for toad's interactive
# one-session-at-a-time model.  The CLI path (--append-system-prompt) remains
# authoritative for headless runs.
_terok_install_claude_instructions() {
    local src="/home/dev/.terok/instructions.md"
    local dst="$HOME/.claude/CLAUDE.md"
    if [ -f "$src" ]; then
        mkdir -p "$HOME/.claude"
        cp "$src" "$dst"
    fi
}
