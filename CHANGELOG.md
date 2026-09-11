# Changelog
## [0.9.0] — Past Prologue — 2026-09-11

### Added

- DNS tiers: terok shows a degraded tier at launch, in the task details and in `terok sickbay`. `shield.dnsmasq_path` selects the dnsmasq binary.
- Vault: set the passphrase on a new install, change it, keep it in the kernel keyring, and re-encrypt a locked database.
- Git gate: a safe-sync report before destructive operations, backups that you can restore, and a warning when the gate branch is ahead of an open MR or PR.
- Tasks: GPUs by vendor, `run.perf`, `run.podman_args`, `services.mode` per project, and `terok task run --debug`.
- `terok setup apparmor` and `terok setup selinux` show the command and the rules before they install.
- Agents resume their session after a restart, Codex too. `--terok-new-session` starts a new session.

### Changed

- **Breaking:** custom LLM provider files go in `~/.config/terok/providers/`. terok still reads `~/.config/terok/agent/providers/`.
- **Breaking:** a task from an earlier terok does not resume. Re-create it. `terok task restart` stops before it changes anything, and `terok sickbay` lists these tasks.
- **Breaking:** shield settings have new names: `shield.disable_firewall_no_protection` (was `shield.bypass_firewall_no_protection`), `shield.down_on_task_run` (was `shield.drop_on_task_run`), and `terok shield down --disengage` (was `--all`). An old name stops terok at startup.
- **Breaking:** an allowlist entry for a vault-protected provider endpoint no longer opens direct access. Use `shield.override`, which now also accepts a CIDR.
- The egress allowlist comes from the agent roster. terok computes the policy again on every restart.
- Where a systemd user manager runs, the supervisor of each task is a user unit. Its log is in `journalctl --user`.
- terok logs to the systemd journal when one is present.

### Fixed

- Codex through the vault: compressed responses, WebSockets and Codex Apps MCP authentication.
- DNS on hosts where AppArmor confines dnsmasq. The lookup tier accepts `drill` as well as `dig`.
- git over HTTPS in Ubuntu 24.04 images.
- The git gate after an upstream default-branch rename.
- A TUI that froze on a locked keyring, a vault probe or a suspended program.
- `terok task restart --recreate` on a container that did not stop.

### Security

- A real credential in a shared mount is an error at task start.
- The project wizard keeps the gatekeeping posture.

[0.9.0]: https://github.com/terok-ai/terok/compare/v0.8.5...v0.9.0

## v0.8.5 — You Exist Here

* Keep container on plain restart, make image upgrade opt-in, https://github.com/terok-ai/terok/pull/1135
* Resilient gate restart after upgrade, https://github.com/terok-ai/terok/pull/1141

**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.8.4...v0.8.5

## v0.8.4 — The Celestial Temple

* Codex device-code auth login, https://github.com/terok-ai/terok/pull/1096
* UI improvements https://github.com/terok-ai/terok/pull/1097, https://github.com/terok-ai/terok/pull/1099, https://github.com/terok-ai/terok/pull/1124 
* Restart semantics changed to resume-or-recreate, https://github.com/terok-ai/terok/pull/1115

**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.8.3...v0.8.4

## v0.8.3 — Start Again

hotfix: restart a task container after host reboot in https://github.com/terok-ai/terok/pull/1092

**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.8.2...v0.8.3

## v0.8.2 — Locks and Hooks

Hotfix for vault passphrase error modes [1083](https://github.com/terok-ai/terok/pull/1083) and supervisor restart [1085](https://github.com/terok-ai/terok/pull/1085)

**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.8.1...v0.8.2

## v0.8.1 — Emissary, Part II

Agents and providers are now independent axes — the coding harness and the LLM endpoint it talks to 
are picked separately. This enables proper support for multi-provider harnesses like Pi or OpenCode 
with any configured provider. Vault SSH keys get a routing matrix in the TUI, and the TUI itself gets 
smoother: event-driven task tracking replaces polling, so changes made outside the TUI show up live.

## What's Changed

  - **Agent × provider split & Pi support**:
     agent (claude/codex/pi/...) and provider (anthropic/openai/openrouter...) are orthogonal now, 
     so the Pi harness runs against any configured provider [#1063](https://github.com/terok-ai/terok/pull/1063);
     `terok agents dir` locates the agent config mounts [#1060](https://github.com/terok-ai/terok/pull/1060)
  - **SSH key ↔ project routing matrix**: 
     a TUI patchbay for linking, unlinking, and minting deploy keys across all projects at once, 
     replacing the one-key-at-a-time flow [#1071](https://github.com/terok-ai/terok/pull/1071);
     panic now also wipes every stored passphrase tier, not just the session unlock [#1072](https://github.com/terok-ai/terok/pull/1072);
      `terok sickbay --system` runs quick host-only checks, skipping the per-container walk [#1073](https://github.com/terok-ai/terok/pull/1073)
  - **Smoother TUI**: 
    task tracking is event-driven instead of polled, so tasks created, deleted, or finished outside the TUI are reflected live
    [#1062](https://github.com/terok-ai/terok/pull/1062), [#1056](https://github.com/terok-ai/terok/pull/1056), [#1061](https://github.com/terok-ai/terok/pull/1061)
    and interrupted deletes resume cleanly ([#1055](https://github.com/terok-ai/terok/pull/1055));
    Enter confirms multiline prompts, with hjkl navigation and a focus-aware hint 
    [#1057](https://github.com/terok-ai/terok/pull/1057), [#1067](https://github.com/terok-ai/terok/pull/1067); 
    duplicate tmux session names re-attach instead of failing [#1054](https://github.com/terok-ai/terok/pull/1054);
    the initial prompt survives dismissing the launch modal [#1078](https://github.com/terok-ai/terok/pull/1078);
    the "autopilot" run mode is renamed "unattended" ([#1059](https://github.com/terok-ai/terok/pull/1059))

**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.8.0...v0.8.1

## v0.8.0 — The Emissary

**First public PyPi release**

## What's Changed

* gate NVIDIA base on host CDI presence, https://github.com/terok-ai/terok/pull/1044
* native Textual auth — API key form, OAuth via terminal, https://github.com/terok-ai/terok/pull/1042
* opt-in per-project authentication scope, https://github.com/terok-ai/terok/pull/1028
* surface AppArmor dnsmasq confinement advisory, https://github.com/terok-ai/terok/pull/1046
* per-container supervisor; retire host vault and gate daemons, https://github.com/terok-ai/terok/pull/1045
* start a new task with `t` from the project pane (#1025), https://github.com/terok-ai/terok/pull/1050


**Full Changelog**: https://github.com/terok-ai/terok/compare/v0.7.9...v0.8.0

