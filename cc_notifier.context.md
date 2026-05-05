# cc_notifier.py Reference

Associated with: `cc_notifier.py` and `cc-notifier` bash wrapper

Primarily a high-level architectural reference, not a detailed implementation guide. It should be kept in sync with the actual codebase.

## Overview
`cc_notifier.py` is meant to be run as a background process via the `cc-notifier` bash wrapper, which in turn is called by Claude Code hooks. It provides intelligent notifications for both local macOS (desktop mode) and remote SSH (remote mode) environments.

**Desktop Mode**: macOS notifications with click-to-focus and optional push notifications
**Remote Mode**: Push notifications only (auto-detected via SSH environment variables)

## Key Components

- **Session Files**: `/tmp/cc_notifier/{session_id}` containing JSON with
  fields: `version`, `window_id`, `app_path`, `timestamp`, `tmux_session_id`,
  `tmux_window_id`, `tmux_pane_id`. The legacy 3-/4-line text format is
  parsed for backwards compatibility.
- **Window Management**: Hammerspoon CLI for cross-space window focusing
- **Local Notifications**: terminal-notifier with `-execute` parameter for click actions
- **Push Notifications**: Pushover API integration

## Core Functions

Flows are in the order they are executed, and are performed synchronously, unless otherwise noted.

### `cc-notifier init`
**Trigger**: Claude Code SessionStart hook (Runs when Claude Code starts a new session or resumes an existing session)
**Purpose**: Capture focused window + tmux session/window/pane identifiers
**Flow**:
1. Parse session data from stdin JSON
2. **Desktop Mode**: Get focused window ID via Hammerspoon CLI (`hs.window.focusedWindow()`)
   **Remote Mode**: Use placeholder "REMOTE" (auto-detected via SSH environment variables)
   **Hammerspoon Missing**: Falls back to "UNAVAILABLE" placeholder (graceful degradation)
3. Capture tmux identifiers (both modes, empty strings if not in tmux):
   - `tmux_session_id` via `tmux display-message -p '#{session_id}'`
   - `tmux_window_id` and `tmux_pane_id` via a single `tmux display-message` call
4. Save SessionState as JSON to `/tmp/cc_notifier/{session_id}`
5. Exit immediately

### `cc-notifier notify`
**Trigger**: Claude Code Notification hook (Runs when Claude needs user attention — permission prompts, idle timeouts, auth events). The Stop hook is intentionally not wired up in dotfiles to avoid notification spam during polling loops; cc-notifier still handles it correctly if a different settings.json wires it up.
**Purpose**: Send a single mutually-exclusive notification (silent / local / push)
**Flow**:
1. Parse hook data from stdin JSON
2. Acquire dedup lock; if another notify ran within 2s, exit silently
3. Load SessionState from `/tmp/cc_notifier/{session_id}` (JSON; falls back
   to legacy 3- or 4-line format for sessions that pre-date this version)
4. Call `decide_notification(state)` → SILENT | LOCAL | PUSH:
   - `tmux_list_clients(state.tmux_session_id)` → list of attached clients
   - If none: PUSH
   - If `min(tty_atime_idle(c.tty) for c in clients) >= 60s`: PUSH
   - If active client (`client_active=1`) AND focused Ghostty owns its TTY:
     - If `state.tmux_window_id`/`pane_id` match the current tmux window/pane: SILENT
     - Else: LOCAL
   - Else: LOCAL
5. SILENT → return
   LOCAL → terminal-notifier with click-to-focus on `state.window_id`
   PUSH  → Pushover (only when credentials are set)
6. Session timestamp is updated inside `check_deduplication`
7. Exit

### Decision constants

- `KEYBOARD_IDLE_THRESHOLD_SECONDS = 60` — clients idle this long are
  treated as "user away" (PUSH).
- `PID_WALK_MAX_DEPTH = 6` — bound on the focused-window process tree
  walk used to map focused PID → controlling TTY.

The previous `PUSH_IDLE_CHECK_INTERVALS_*` constants and progressive
idle-check ladder are removed. Decision is one-shot.

### `cc-notifier cleanup`
**Trigger**: Claude Code SessionEnd hook (Runs when a Claude Code session ends, which can be due to user logout, session clear, or exiting Claude Code while prompt input is visible–i.e. via Ctrl+C)
**Purpose**: Clean up session files after Claude Code session ends
**Flow**:
1. Parse session data from stdin JSON
2. ~~Remove file associated with session ID~~ (currently disabled due to Claude Code bug #7911)
3. Perform age-based cleanup of old session files (>5 days old)
4. Exit

### `cc-notifier --version` / `cc-notifier -v`
**Purpose**: Display current version (0.3.0)
**Flow**: Print version string and exit

### Debug Mode
**Usage**: Add `--debug` flag to any command (e.g., `cc-notifier --debug notify`)
**Behavior**:
- Enables debug logging to file (not console)
- Debugging is indicated in the local and push notifications, so that users don't forget they have debugging enabled

## Notes

**Hook Data Structure**

All cc-notifier commands receive JSON data via stdin from Claude Code hooks. HookData parses and filters to these fields:
```json
{
  "session_id": "string",       // Required, always present
  "cwd": "string",              // Current working directory (default: "")
  "hook_event_name": "string",  // Event type (default: "Stop")
  "message": "string"           // Notification message, e.g. permission prompts (default: "")
}
```
Note: Claude Code sends additional fields (e.g., `transcript_path`) that are filtered out by HookData.

**Session Files**
- Stored in `/tmp/cc_notifier/`
- Named by session ID (e.g., `/tmp/cc_notifier/abc123`)
- Format: JSON with `version`, `window_id`, `app_path`, `timestamp`,
  `tmux_session_id`, `tmux_window_id`, `tmux_pane_id`
- The legacy 3-/4-line text format is parsed for backwards compatibility

**Log Files**
- Stored in `~/.cc-notifier/cc-notifier.log`
- Auto-trim

## References
- [Claude Code hooks documentation](https://docs.claude.com/en/docs/claude-code/hooks) - Complete hook behavior and data structure reference