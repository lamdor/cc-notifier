# tmux-client-aware notification routing

**Status:** Proposed
**Date:** 2026-05-05
**Repo:** `lamdor/cc-notifier` (fork). Coordinated change in `~/dotfiles/claude/settings.json`.

## Problem

cc-notifier currently over-notifies in two ways:

1. **Local + Pushover fire concurrently.** On every Stop/Notification hook, the local terminal-notifier path and the Pushover path each run their own decision logic. When the user is away from the computer, they get *both* a macOS banner and a phone push for the same event.
2. **Pushover's idle threshold is too short.** Idle check intervals are `[3, 20]` seconds for desktop and tmux-attached cases. A 20-second pause while reading code triggers a phone push.
3. **`Stop` hook fires during background polling.** When Claude is running long-lived processes (e.g. Playwright tests, watch modes), each turn ends with a `Stop` event. In auto mode the model immediately starts another turn, but each `Stop` produces a notification.
4. **Same-tmux-session detection is coarse.** cc-notifier knows whether *any* client is attached to the recorded tmux session, not whether the user is currently looking at it. Switching to a different tmux window in the same session is treated as "still attached, suppress notification" — which is correct only if the user is actively typing in that session.

## Goal

Replace the current macOS-window-ID + tmux-session-attached check with a three-way ladder driven by tmux client state, TTY input idle, and the Ghostty window currently focused. After this change:

- **Silent** when the user is at the keyboard *and* the focused window is the one displaying the originating tmux session/window/pane.
- **Local notification** (terminal-notifier) when the user is at the keyboard but elsewhere (different tmux window, different session, different app).
- **Pushover** when the user is away from the computer (no keyboard input across attached clients for ≥60 s, or no clients attached at all).

The three outcomes are mutually exclusive — exactly one notification per event.

## Non-goals

- Adding a debounce for `Stop` events. The decision is to drop `Stop` from hooks entirely (see Settings change below).
- Reworking the remote/SSH path. It already uses TTY atime correctly via `get_tty_idle_time`.
- Changing the deduplication window (2 s).
- Adding a configuration UI. The 60 s threshold is a constant; settings.json is the only user-facing toggle.

## Architecture

### Init (`SessionStart` hook)

Today the session file holds four newline-separated fields: `window_id`, `app_path`, `timestamp`, `tmux_session_id`.

After this change, capture two additional tmux fields:

- `tmux_window_id` — `@N`, e.g. `@42`
- `tmux_pane_id` — `%N`, e.g. `%87`

Both come from a single `tmux display-message -p '#{window_id}|#{pane_id}'` call.

Persist as JSON to allow forward extension:

```json
{
  "version": 2,
  "window_id": "12345",
  "app_path": "/Applications/Ghostty.app",
  "timestamp": 0,
  "tmux_session_id": "$3",
  "tmux_window_id": "@42",
  "tmux_pane_id": "%87"
}
```

When loading, if the file isn't valid JSON, parse it as the legacy four-line format and treat `tmux_window_id` / `tmux_pane_id` as empty. This keeps existing in-flight sessions working through the upgrade.

### Notify (`Notification` hook only)

The decision becomes a single pure function over inputs gathered up front:

```
def decide_notification(hook_data, state) -> Decision:
    clients = tmux_list_clients(state.tmux_session_id)
    if not clients:
        return Decision.PUSH

    active_client = next((c for c in clients if c.active), None)
    min_idle = min(tty_atime_idle(c.tty) for c in clients)

    if min_idle >= KEYBOARD_IDLE_THRESHOLD_SECONDS:  # 60
        return Decision.PUSH

    if active_client and is_focused_ghostty_for_tty(active_client.tty):
        if (current_tmux_window_id() == state.tmux_window_id
                and current_tmux_pane_id() == state.tmux_pane_id):
            return Decision.SILENT
        return Decision.LOCAL

    return Decision.LOCAL
```

`cmd_notify` becomes:

```
parse hook data
load session state (with legacy fallback)
if check_deduplication: return
decision = decide_notification(hook_data, state)
match decision:
    SILENT: return
    LOCAL:  send terminal-notifier with click-to-focus
    PUSH:   send Pushover
update session timestamp (existing behavior)
```

### Helper: `tmux_list_clients(session_id) -> list[ClientInfo]`

Wraps:

```
tmux list-clients -t <session_id> \
  -F '#{client_tty}|#{client_active}'
```

Returns a list of `ClientInfo(tty: str, active: bool)`. `client_activity` is intentionally not used — it tracks I/O including process output to the terminal, so it stays "fresh" while Claude streams tokens even when the user has walked away. TTY atime (which only advances on `read` from the kernel, i.e. keyboard/mouse input) is the correct presence signal.

Pure parsing once the subprocess returns; trivially unit-testable with fixture stdout. Empty list when no clients attached or the session no longer exists.

### Helper: `tty_atime_idle(tty_path) -> int`

Generalize the existing `get_tty_idle_time`: stat the path, return `int(time.time() - st_atime)`. Errors → return a sentinel large value (e.g. `10**9`) so the caller treats the client as idle. Logged via `debug_log`.

### Helper: `is_focused_ghostty_for_tty(client_tty) -> bool`

```
1. Hammerspoon: focused window's owning PID
   hs.window.focusedWindow():application():pid()
2. Walk descendants of that PID:
   - seed = [pid]
   - repeat: for each pid in seed, run `pgrep -P <pid>` to get children;
     extend the worklist; bound depth at e.g. 6 levels to avoid runaway
3. For each descendant pid (including the seed), run `ps -o tty= -p <pid>`
   and normalize: 'ttys012' -> '/dev/ttys012', '?' -> None
4. Return True if any descendant TTY equals client_tty
```

Failure modes:

- Hammerspoon CLI missing or times out → `RuntimeError` → caller treats as "not focused" → LOCAL.
- `pgrep`/`ps` errors → log, treat as "not focused" → LOCAL.

This is conservative: when the focus check breaks, the user gets a local banner rather than silence.

### Decision: dropping `Stop` from settings.json

In `~/dotfiles/claude/settings.json`, the hook block becomes:

```json
"hooks": {
  "SessionStart": [{ "hooks": [{ "type": "command", "command": "cc-notifier init" }] }],
  "Notification": [{ "hooks": [{ "type": "command", "command": "cc-notifier notify" }] }],
  "SessionEnd":   [{ "hooks": [{ "type": "command", "command": "cc-notifier cleanup" }] }]
}
```

The `Stop` entry is removed. `Notification` already covers "Claude needs user attention" — permission prompts and idle waits — which is the only signal that matters in auto mode. Trade-off: no "task done" ping when the model finishes a turn that doesn't need user input. Accepted.

## Components and boundaries

Four pure functions, each independently testable:

| Function | Inputs | Outputs | Test approach |
|----------|--------|---------|---------------|
| `tmux_list_clients` | session id | list[ClientInfo] | mock subprocess, fixture stdout |
| `tty_atime_idle` | path | seconds | tmpfile + os.utime |
| `is_focused_ghostty_for_tty` | tty | bool | mock pid lookup + ps output |
| `decide_notification` | hook_data, state | Decision | exhaustive matrix over the three above |

`cmd_notify` orchestrates only — gather inputs, call `decide_notification`, dispatch. Easy to keep small.

## Data flow

```
SessionStart
  -> cmd_init
  -> capture(window_id, app_path, tmux_session_id, tmux_window_id, tmux_pane_id)
  -> save JSON to /tmp/cc_notifier/<session_id>

Notification (Stop removed)
  -> cmd_notify
  -> load state (JSON, with legacy fallback)
  -> check_deduplication (existing)
  -> decide_notification
       |
       +--> tmux_list_clients
       +--> tty_atime_idle (per client)
       +--> is_focused_ghostty_for_tty (only when needed)
       +--> current_tmux_{window,pane}_id (only when active client is focused)
  -> dispatch SILENT | LOCAL | PUSH

SessionEnd
  -> cmd_cleanup (unchanged)
```

## Error handling

| Failure | Behavior |
|---------|----------|
| `tmux list-clients` empty | PUSH — nothing attached |
| Hammerspoon missing/timeout | "not focused" → LOCAL |
| PID walk fails | "not focused" → LOCAL |
| TTY path missing | treat as max idle for that client |
| All client TTYs unstattable | min_idle huge → PUSH |
| Old session file format | parse legacy 4-line, log debug, continue |
| Legacy state missing tmux_window_id/pane_id | skip the window/pane match — focused-active-client → SILENT, otherwise LOCAL |

## Testing

Unit tests follow existing patterns in `tests/test_core.py` and `tests/test_integrations.py`. Subprocess calls are mocked.

Decision matrix tests (in `decide_notification`):

| State | Expect |
|-------|--------|
| no clients | PUSH |
| clients, all idle ≥ 60s | PUSH |
| active client focused, same window+pane | SILENT |
| active client focused, different window | LOCAL |
| active client focused, different pane | LOCAL |
| active client not focused | LOCAL |
| no active client, fresh TTY | LOCAL |
| Hammerspoon error during focus check | LOCAL |

Manual verification matrix (macOS, in tmux):

1. Type in originating pane → SILENT
2. Switch tmux window, type → LOCAL
3. Switch to a different Ghostty window attached to a different session → LOCAL
4. Walk away ≥ 60 s → PUSH (and only PUSH, no local)
5. Background process polling, user typing in originating pane → SILENT
6. Same session attached in two Ghostty windows, focus one → SILENT if it's the focused one's `client_active=1`, otherwise LOCAL
7. Multiple Ghostty.app instances running, focus moves between them → correct PID resolution

## Constants

```python
KEYBOARD_IDLE_THRESHOLD_SECONDS = 60
PID_WALK_MAX_DEPTH = 6
```

These supersede the existing `PUSH_IDLE_CHECK_INTERVALS_*` constants, which are removed.

## Rollout

In this repo (`cc-notifier`):

1. Implement on a `la/` branch.
2. Land it on the fork's main branch.

In `~/dotfiles` (separate commits, separate concern):

1. Bump `nix/overlays/cc-notifier.nix` to the new commit hash + sha256.
2. Edit `claude/settings.json` to drop the `Stop` block.
3. Run `./switch.sh` (user-side, requires sudo).

After a week of dogfooding: open a PR upstream to `trentmcnitt/cc-notifier`.

## Open questions resolved

- **Multiple Ghostty processes**: handled. Hammerspoon resolves the focused PID independent of which Ghostty bundle owns it; the PID walk stays inside that one process tree.
- **TTY path normalization**: `ps -o tty=` returns `ttys012`, tmux returns `/dev/ttys012`. Normalize by prefixing `/dev/` when missing.
- **Mouse mode and TTY atime**: when tmux mouse mode is on, mouse events update TTY atime. Acceptable — moving the mouse over the terminal is a reasonable proxy for presence.
