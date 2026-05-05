# tmux-client-aware notification routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace cc-notifier's overlapping local + push notification logic with a single mutually-exclusive decision (silent / local / push) driven by tmux client state, TTY input idle, and Ghostty window focus.

**Architecture:** Add a pure `decide_notification` function to `cc_notifier.py` that consumes three new helpers — `tmux_list_clients`, `tty_atime_idle` (generalized from existing), and `is_focused_ghostty_for_tty`. Session state moves from a 4-line text format to JSON to carry tmux window/pane IDs. `cmd_notify` becomes a thin orchestrator. The progressive idle-check intervals (`PUSH_IDLE_CHECK_INTERVALS_*`) are removed — the decision is one-shot using a 60s threshold. A coordinated dotfiles change drops the `Stop` hook from `claude/settings.json`.

**Tech Stack:** Python 3.9+ stdlib only, pytest, mypy, ruff. macOS-specific: `tmux`, `ps`, `pgrep`, Hammerspoon CLI, terminal-notifier.

**Spec:** `docs/superpowers/specs/2026-05-05-tmux-client-aware-routing-design.md`

---

## File Map

**This repo (`~/code/cc-notifier`):**
- Modify: `cc_notifier.py` — most changes here
- Modify: `tests/test_core.py` — add session-state JSON tests
- Modify: `tests/test_integrations.py` — add new helpers + decision tests
- Modify: `cc_notifier.context.md` — keep architectural docs in sync
- Modify: `pyproject.toml` — bump version to `0.4.0`

**Coordinated change in `~/dotfiles`:**
- Modify: `claude/settings.json` — drop `Stop` hook
- Modify: `nix/overlays/cc-notifier.nix` — bump `rev` and `hash` after fork lands

The dotfiles changes happen in Task 12 as the final integration step.

---

## Conventions

- **Branch:** Work on `la/tmux-client-routing-design` (already exists with the spec). All commits land on this bookmark.
- **VCS:** This repo uses `jj`. Commit with `jj commit -m "..."`. Never amend; always create new commits. After committing, run `jj bookmark set la/tmux-client-routing-design -r @-` so the bookmark tracks HEAD. The dotfiles repo also uses jj.
- **Tests:** `make test` from repo root. Single test: `pytest tests/test_integrations.py::TestName::test_method -v`.
- **Lint/type:** `make lint typecheck` after substantive changes.
- **Style:** Follow existing patterns — `@dataclass`, `@handle_command_errors` decorator, `debug_log()` for tracing, `run_command`/`run_background_command` helpers.
- **Imports:** Add new stdlib imports to the existing block at the top of `cc_notifier.py` (alphabetical).

---

## Task 1: Add `ClientInfo` dataclass and `tmux_list_clients`

**Files:**
- Modify: `cc_notifier.py` — add helper near `is_tmux_session_attached` (around line 380)
- Modify: `tests/test_integrations.py` — add `TestTmuxListClients` class

- [ ] **Step 1: Write the failing tests**

Add at the bottom of `tests/test_integrations.py`:

```python
class TestTmuxListClients:
    """Test tmux_list_clients parsing."""

    def test_returns_empty_list_when_session_missing(self):
        """No clients attached → empty list."""
        with patch("cc_notifier.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            result = cc_notifier.tmux_list_clients("$3")
            assert result == []

    def test_parses_single_attached_client(self):
        """One client, active=1."""
        with patch("cc_notifier.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "/dev/ttys012|1\n"
            result = cc_notifier.tmux_list_clients("$3")
            assert len(result) == 1
            assert result[0].tty == "/dev/ttys012"
            assert result[0].active is True

    def test_parses_multiple_clients_mixed_active(self):
        """Two clients, only one active for this session."""
        with patch("cc_notifier.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "/dev/ttys012|1\n/dev/ttys020|0\n"
            result = cc_notifier.tmux_list_clients("$3")
            assert len(result) == 2
            assert result[0].active is True
            assert result[1].active is False

    def test_returns_empty_on_tmux_failure(self):
        """tmux not running or session vanished → empty list."""
        with patch("cc_notifier.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = cc_notifier.tmux_list_clients("$3")
            assert result == []

    def test_returns_empty_when_tmux_not_found(self):
        """tmux binary missing → empty list."""
        with patch(
            "cc_notifier.subprocess.run", side_effect=FileNotFoundError()
        ):
            result = cc_notifier.tmux_list_clients("$3")
            assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations.py::TestTmuxListClients -v`
Expected: FAIL — `ClientInfo` and `tmux_list_clients` don't exist yet.

- [ ] **Step 3: Implement `ClientInfo` dataclass and `tmux_list_clients`**

In `cc_notifier.py`, after the existing `is_tmux_session_attached` function, add:

```python
@dataclass
class ClientInfo:
    """A tmux client attached to a session."""

    tty: str
    active: bool


def tmux_list_clients(session_id: str) -> list[ClientInfo]:
    """List tmux clients attached to a given session.

    Uses client_tty + client_active. client_activity is intentionally NOT
    used — it tracks I/O including process output, so it stays fresh while
    Claude streams tokens even when the user has walked away. TTY atime
    (only advances on read from the kernel, i.e. keyboard/mouse input)
    is the correct presence signal.

    Returns:
        List of ClientInfo. Empty list if tmux is missing, the session
        is gone, or no clients are attached.
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-clients",
                "-t",
                session_id,
                "-F",
                "#{client_tty}|#{client_active}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            debug_log(
                f"tmux list-clients returned {result.returncode} for {session_id}"
            )
            return []
        clients: list[ClientInfo] = []
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            tty, active = line.split("|", 1)
            clients.append(ClientInfo(tty=tty, active=active == "1"))
        debug_log(f"tmux clients for {session_id}: {clients}")
        return clients
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        debug_log(f"tmux list-clients error: {type(e).__name__}: {e}")
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_integrations.py::TestTmuxListClients -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: add tmux_list_clients helper

Adds a ClientInfo dataclass and tmux_list_clients() that wraps
'tmux list-clients -F' to expose attached clients' TTYs and
client_active flag. Used by the upcoming notification decision
function. client_activity is deliberately not used — TTY atime is
the correct presence signal."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 2: Generalize `tty_atime_idle`

**Files:**
- Modify: `cc_notifier.py` — refactor `get_tty_idle_time` (current location around line 670)
- Modify: `tests/test_integrations.py` — add `TestTtyAtimeIdle` class

The current `get_tty_idle_time` reads `CC_NOTIFIER_TTY` env var. We need a version that takes an explicit path so we can stat each client's TTY.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_integrations.py`:

```python
class TestTtyAtimeIdle:
    """Test tty_atime_idle with explicit paths."""

    def test_returns_seconds_since_atime(self, tmp_path):
        """Stat a real file, set atime, get back idle seconds."""
        f = tmp_path / "fake_tty"
        f.write_text("")
        # Set atime to 30s ago, mtime to now
        thirty_ago = time.time() - 30
        os.utime(f, (thirty_ago, time.time()))
        idle = cc_notifier.tty_atime_idle(str(f))
        # Allow ±2s for test execution slop
        assert 28 <= idle <= 32

    def test_returns_huge_idle_when_path_missing(self):
        """Missing TTY path → very large value (treated as idle)."""
        idle = cc_notifier.tty_atime_idle("/dev/this-does-not-exist")
        assert idle >= 10**8  # huge; caller treats as "idle"

    def test_returns_huge_idle_on_oserror(self):
        """Permission errors etc → very large value."""
        with patch("cc_notifier.os.stat", side_effect=PermissionError()):
            idle = cc_notifier.tty_atime_idle("/dev/ttys012")
            assert idle >= 10**8
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations.py::TestTtyAtimeIdle -v`
Expected: FAIL — `tty_atime_idle` doesn't exist.

- [ ] **Step 3: Replace `get_tty_idle_time` with `tty_atime_idle`**

Find the current `get_tty_idle_time` function in `cc_notifier.py` (around line 660–680). Replace it with:

```python
TTY_IDLE_HUGE = 10**9  # Sentinel: stat failed, treat client as idle


def tty_atime_idle(tty_path: str) -> int:
    """Get TTY idle time in seconds based on st_atime (last read).

    st_atime advances on read() from the kernel — i.e. when the kernel
    delivers keyboard or mouse input to a process reading the TTY. It does
    NOT advance on writes (process output to the terminal). This makes it
    the correct presence signal even while Claude is streaming output.

    Returns:
        Seconds since last read on the TTY. Returns TTY_IDLE_HUGE on any
        stat failure so callers treat the client as idle.
    """
    try:
        debug_log(f"TTY idle stat: path={tty_path}")
        tty_stat = os.stat(tty_path)
        idle = int(time.time() - tty_stat.st_atime)
        debug_log(f"TTY idle: path={tty_path}, atime={tty_stat.st_atime:.1f}, idle={idle}s")
        return idle
    except (OSError, ValueError) as e:
        debug_log(f"TTY idle error for {tty_path}: {type(e).__name__}: {e}")
        return TTY_IDLE_HUGE
```

Then find the existing `get_idle_time()` function (just below `get_tty_idle_time` was). Update its remote branch to call the new function:

```python
def get_idle_time() -> int:
    """Get idle time in seconds, environment-aware."""
    if is_remote_session():
        tty_path = os.getenv("CC_NOTIFIER_TTY")
        if not tty_path:
            raise RuntimeError("CC_NOTIFIER_TTY not set by wrapper")
        return tty_atime_idle(tty_path)
    return get_macos_idle_time()
```

- [ ] **Step 4: Run new + existing tests**

Run: `pytest tests/test_integrations.py::TestTtyAtimeIdle tests/ -v -k "idle or remote"`
Expected: PASS for new tests; existing remote/idle tests should still pass since `get_idle_time` keeps its behavior. If any existing test referenced `get_tty_idle_time` directly, update it to call `tty_atime_idle("...")` instead.

- [ ] **Step 5: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "refactor: generalize TTY idle helper to take explicit path

Replaces get_tty_idle_time (which read CC_NOTIFIER_TTY env var) with
tty_atime_idle(path), so callers can stat any TTY. Returns a sentinel
huge value on stat failure rather than raising — callers treat that as
'idle'. get_idle_time still wraps it for the existing remote-mode flow."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 3: Add `is_focused_ghostty_for_tty`

**Files:**
- Modify: `cc_notifier.py` — add helper near other Hammerspoon code (around line 470)
- Modify: `tests/test_integrations.py` — add `TestIsFocusedGhostty` class

- [ ] **Step 1: Write the failing tests**

```python
class TestIsFocusedGhostty:
    """Test is_focused_ghostty_for_tty PID-walk logic."""

    def test_returns_true_when_focused_pid_owns_client_tty(self):
        """Focused window's process tree contains the client TTY."""
        with (
            patch(
                "cc_notifier.get_focused_window_pid", return_value=999
            ),
            patch(
                "cc_notifier.walk_descendant_ttys",
                return_value={"/dev/ttys012", "/dev/ttys015"},
            ),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012")
                is True
            )

    def test_returns_false_when_client_tty_not_in_tree(self):
        """Different Ghostty window has focus."""
        with (
            patch("cc_notifier.get_focused_window_pid", return_value=999),
            patch(
                "cc_notifier.walk_descendant_ttys",
                return_value={"/dev/ttys020"},
            ),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012")
                is False
            )

    def test_returns_false_when_hammerspoon_fails(self):
        """Hammerspoon error → conservative LOCAL → return False."""
        with patch(
            "cc_notifier.get_focused_window_pid",
            side_effect=RuntimeError("no hs"),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012")
                is False
            )

    def test_returns_false_when_no_descendants(self):
        """Focused app isn't a terminal — empty TTY set."""
        with (
            patch("cc_notifier.get_focused_window_pid", return_value=999),
            patch("cc_notifier.walk_descendant_ttys", return_value=set()),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012")
                is False
            )


class TestWalkDescendantTtys:
    """Test process tree TTY collection."""

    def test_collects_ttys_from_immediate_children(self):
        """ps returns a TTY for the child PID, normalized."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                # pgrep -P 100 → "200\n"; pgrep -P 200 → ""
                pid = cmd[-1]
                mock.stdout = "200\n" if pid == "100" else ""
            elif cmd[0] == "ps":
                # ps -o tty= -p 100 → "??", -p 200 → "ttys012"
                pid = cmd[-1]
                mock.stdout = "??" if pid == "100" else "ttys012"
            return mock

        with patch("cc_notifier.subprocess.run", side_effect=fake_run):
            result = cc_notifier.walk_descendant_ttys(100)
            assert "/dev/ttys012" in result

    def test_normalizes_already_prefixed_paths(self):
        """ps sometimes returns /dev/ttys012 already — don't double-prefix."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                mock.stdout = ""
            else:
                mock.stdout = "/dev/ttys012"
            return mock

        with patch("cc_notifier.subprocess.run", side_effect=fake_run):
            result = cc_notifier.walk_descendant_ttys(100)
            assert result == {"/dev/ttys012"}

    def test_skips_question_mark_tty(self):
        """ps -o tty= returns '?' for processes with no controlling TTY."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "?" if cmd[0] == "ps" else ""
            return mock

        with patch("cc_notifier.subprocess.run", side_effect=fake_run):
            result = cc_notifier.walk_descendant_ttys(100)
            assert result == set()

    def test_bounded_depth(self):
        """Walk doesn't recurse forever — depth caps at PID_WALK_MAX_DEPTH."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                # Each level produces one child, pid = parent + 1
                parent = int(cmd[-1])
                mock.stdout = f"{parent + 1}\n"
            else:
                mock.stdout = "?"
            return mock

        with patch("cc_notifier.subprocess.run", side_effect=fake_run):
            # If walk were unbounded this would never return.
            result = cc_notifier.walk_descendant_ttys(100)
            assert result == set()  # All TTYs are '?'
```

Make sure `from unittest.mock import MagicMock` is in the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations.py::TestIsFocusedGhostty tests/test_integrations.py::TestWalkDescendantTtys -v`
Expected: FAIL — these helpers don't exist yet.

- [ ] **Step 3: Implement the helpers**

Add a constant near the other constants at the top of `cc_notifier.py`:

```python
PID_WALK_MAX_DEPTH = 6
```

After `create_focus_command` (around line 480), add:

```python
def get_focused_window_pid() -> int:
    """Get the PID of the application owning the focused macOS window.

    Raises RuntimeError if Hammerspoon is missing or returns no window.
    """
    try:
        output = run_command(
            [
                HAMMERSPOON_CLI,
                "-c",
                "local w=hs.window.focusedWindow(); "
                "if w then local app=w:application(); "
                "print(app and app:pid() or 'ERROR') "
                "else print('ERROR') end",
            ]
        )
        if output == "ERROR" or not output:
            raise RuntimeError("Failed to get focused window PID from Hammerspoon")
        return int(output.strip())
    except (subprocess.TimeoutExpired, ValueError) as e:
        raise RuntimeError(f"Hammerspoon PID lookup failed: {e}") from e


def _normalize_tty(raw: str) -> Optional[str]:
    """Normalize ps -o tty= output to a /dev/<name> path or None."""
    raw = raw.strip()
    if not raw or raw == "?" or raw == "??":
        return None
    if raw.startswith("/dev/"):
        return raw
    return f"/dev/{raw}"


def walk_descendant_ttys(root_pid: int) -> set[str]:
    """Collect controlling TTYs of a process and all its descendants.

    Walks the process tree depth-first, bounded by PID_WALK_MAX_DEPTH to
    avoid runaway recursion in pathological cases. Uses pgrep -P to find
    children and ps -o tty= to read each PID's controlling TTY.
    """
    ttys: set[str] = set()
    frontier: list[tuple[int, int]] = [(root_pid, 0)]  # (pid, depth)
    seen: set[int] = set()

    while frontier:
        pid, depth = frontier.pop()
        if pid in seen:
            continue
        seen.add(pid)

        # TTY for this pid
        try:
            ps_result = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if ps_result.returncode == 0:
                tty = _normalize_tty(ps_result.stdout)
                if tty:
                    ttys.add(tty)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            debug_log(f"ps -o tty= failed for pid={pid}: {e}")

        if depth >= PID_WALK_MAX_DEPTH:
            continue

        # Children
        try:
            pgrep_result = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if pgrep_result.returncode == 0:
                for line in pgrep_result.stdout.strip().splitlines():
                    line = line.strip()
                    if line.isdigit():
                        frontier.append((int(line), depth + 1))
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            debug_log(f"pgrep -P failed for pid={pid}: {e}")

    debug_log(f"Descendant TTYs for pid={root_pid}: {ttys}")
    return ttys


def is_focused_ghostty_for_tty(client_tty: str) -> bool:
    """Whether the focused macOS window's process tree owns client_tty.

    True when the focused application's PID, or any descendant, has
    client_tty as its controlling TTY. False on any failure (Hammerspoon
    missing, ps/pgrep error, focused app isn't a terminal). False is the
    conservative answer — callers send a local notification.
    """
    try:
        pid = get_focused_window_pid()
    except RuntimeError as e:
        debug_log(f"is_focused_ghostty_for_tty: {e}")
        return False

    ttys = walk_descendant_ttys(pid)
    result = client_tty in ttys
    debug_log(
        f"is_focused_ghostty_for_tty(pid={pid}, tty={client_tty}) = {result}"
    )
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_integrations.py::TestIsFocusedGhostty tests/test_integrations.py::TestWalkDescendantTtys -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: add focused-Ghostty-owns-TTY check

Adds get_focused_window_pid (Hammerspoon), walk_descendant_ttys (bounded
PID-tree walk via pgrep + ps), and is_focused_ghostty_for_tty which
combines them to decide whether the focused macOS window's process tree
controls a given TTY. Survives multiple Ghostty.app instances, since the
walk is rooted at the focused app's actual PID."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 4: JSON session-state schema with legacy fallback

**Files:**
- Modify: `cc_notifier.py` — add `SessionState` dataclass and `save_session_state` / `load_session_state`
- Modify: `tests/test_core.py` — add `TestSessionState` class

The current `save_window_id` / `load_window_id` write four newline-separated lines. Replace with JSON, but keep a parser for the old format so in-flight sessions don't break across the upgrade.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_core.py`:

```python
class TestSessionState:
    """Test JSON session state with legacy fallback."""

    def test_round_trip_json(self, tmp_path, monkeypatch):
        """Save then load returns equivalent state."""
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        state = cc_notifier.SessionState(
            window_id="12345",
            app_path="/Applications/Ghostty.app",
            timestamp=1700000000.0,
            tmux_session_id="$3",
            tmux_window_id="@42",
            tmux_pane_id="%87",
        )
        cc_notifier.save_session_state("abc", state)
        loaded = cc_notifier.load_session_state("abc")
        assert loaded == state

    def test_loads_legacy_four_line_format(self, tmp_path, monkeypatch):
        """Old session file (4 lines) loads with empty tmux window/pane."""
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        legacy = tmp_path / "abc"
        legacy.write_text("12345\n/Applications/Ghostty.app\n0\n$3")
        loaded = cc_notifier.load_session_state("abc")
        assert loaded.window_id == "12345"
        assert loaded.app_path == "/Applications/Ghostty.app"
        assert loaded.timestamp == 0.0
        assert loaded.tmux_session_id == "$3"
        assert loaded.tmux_window_id == ""
        assert loaded.tmux_pane_id == ""

    def test_loads_legacy_three_line_format(self, tmp_path, monkeypatch):
        """Older session file with no tmux line at all."""
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        legacy = tmp_path / "abc"
        legacy.write_text("12345\n/Applications/Ghostty.app\n0")
        loaded = cc_notifier.load_session_state("abc")
        assert loaded.tmux_session_id == ""
        assert loaded.tmux_window_id == ""
        assert loaded.tmux_pane_id == ""

    def test_save_creates_session_dir(self, tmp_path, monkeypatch):
        """Save creates SESSION_DIR if missing."""
        target = tmp_path / "subdir"
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", target)
        state = cc_notifier.SessionState(
            window_id="12345",
            app_path="UNKNOWN",
            timestamp=0.0,
            tmux_session_id="",
            tmux_window_id="",
            tmux_pane_id="",
        )
        cc_notifier.save_session_state("xyz", state)
        assert (target / "xyz").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_core.py::TestSessionState -v`
Expected: FAIL — `SessionState`, `save_session_state`, `load_session_state` don't exist.

- [ ] **Step 3: Implement the dataclass and functions**

In `cc_notifier.py`, find the existing `save_window_id` / `load_window_id` functions. Replace them with:

```python
@dataclass
class SessionState:
    """Persisted state for a Claude Code session."""

    window_id: str
    app_path: str
    timestamp: float
    tmux_session_id: str
    tmux_window_id: str  # e.g. "@42"; empty if not in tmux
    tmux_pane_id: str  # e.g. "%87"; empty if not in tmux


SESSION_STATE_VERSION = 2


def save_session_state(session_id: str, state: SessionState) -> None:
    """Write SessionState to the session file as JSON."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_file = SESSION_DIR / session_id
    payload = {
        "version": SESSION_STATE_VERSION,
        "window_id": state.window_id,
        "app_path": state.app_path,
        "timestamp": state.timestamp,
        "tmux_session_id": state.tmux_session_id,
        "tmux_window_id": state.tmux_window_id,
        "tmux_pane_id": state.tmux_pane_id,
    }
    session_file.write_text(json.dumps(payload))
    debug_log(f"Session saved: {session_id} -> {payload}")


def load_session_state(session_id: str) -> SessionState:
    """Read SessionState from the session file.

    Falls back to the legacy 3- or 4-line text format for sessions that
    were initialized before this version landed.
    """
    session_file = SESSION_DIR / session_id
    raw = session_file.read_text()
    try:
        data = json.loads(raw)
        return SessionState(
            window_id=data.get("window_id", ""),
            app_path=data.get("app_path", "UNKNOWN"),
            timestamp=float(data.get("timestamp", 0)),
            tmux_session_id=data.get("tmux_session_id", ""),
            tmux_window_id=data.get("tmux_window_id", ""),
            tmux_pane_id=data.get("tmux_pane_id", ""),
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        # Legacy format: lines [0]=window_id, [1]=app_path, [2]=timestamp,
        # [3]=tmux_session_id (optional)
        debug_log(f"Session {session_id}: parsing legacy format")
        lines = raw.strip().split("\n")
        return SessionState(
            window_id=lines[0] if len(lines) > 0 else "",
            app_path=lines[1] if len(lines) > 1 else "UNKNOWN",
            timestamp=float(lines[2]) if len(lines) > 2 else 0.0,
            tmux_session_id=lines[3] if len(lines) > 3 else "",
            tmux_window_id="",
            tmux_pane_id="",
        )
```

Keep the old `save_window_id` and `load_window_id` for now — they're called by `cmd_init` and `cmd_notify` and we'll update those in Task 5. To prevent dead-code warnings during this task, mark them deprecated by adding a docstring note, or just leave them; they get removed in Task 5.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_core.py::TestSessionState -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: SessionState dataclass with JSON persistence

Adds SessionState (window_id, app_path, timestamp, tmux session/window/pane
ids) and save/load helpers that use JSON. Falls back to the legacy 3- or
4-line text format so in-flight sessions survive the upgrade. Old
save_window_id/load_window_id remain for now; Task 5 swaps callers over."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 5: Capture tmux window/pane in `cmd_init`; swap callers to SessionState

**Files:**
- Modify: `cc_notifier.py` — `cmd_init`, deduplication helper, `cmd_notify` (only the loading part for now), remove `save_window_id` / `load_window_id`
- Modify: `tests/test_core.py` — add `TestCmdInitTmuxCapture`

- [ ] **Step 1: Write the failing test**

```python
class TestCmdInitTmuxCapture:
    """Test cmd_init captures tmux window and pane IDs."""

    def test_init_captures_tmux_window_and_pane(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)

        with (
            patch.object(
                sys,
                "argv",
                ["cc-notifier", "init"],
            ),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch(
                "cc_notifier.HookData.from_stdin",
                return_value=cc_notifier.HookData(session_id="sess-123"),
            ),
            patch("cc_notifier.is_remote_session", return_value=False),
            patch(
                "cc_notifier.get_focused_window_id",
                return_value=("99", "/Applications/Ghostty.app"),
            ),
            patch("cc_notifier.get_tmux_session_id", return_value="$3"),
            patch(
                "cc_notifier.get_tmux_window_pane_ids",
                return_value=("@42", "%87"),
            ),
        ):
            cc_notifier.main()

        loaded = cc_notifier.load_session_state("sess-123")
        assert loaded.tmux_session_id == "$3"
        assert loaded.tmux_window_id == "@42"
        assert loaded.tmux_pane_id == "%87"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_core.py::TestCmdInitTmuxCapture -v`
Expected: FAIL — `get_tmux_window_pane_ids` doesn't exist yet, and `cmd_init` doesn't write the new fields.

- [ ] **Step 3: Add `get_tmux_window_pane_ids` and rewrite `cmd_init`**

In `cc_notifier.py`, near `get_tmux_session_id`, add:

```python
def get_tmux_window_pane_ids() -> tuple[str, str]:
    """Get the current tmux window id (@N) and pane id (%N).

    Returns ("", "") if not in tmux.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{window_id}|#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0 or "|" not in result.stdout:
            return ("", "")
        window_id, pane_id = result.stdout.strip().split("|", 1)
        debug_log(f"tmux window/pane: {window_id}/{pane_id}")
        return (window_id, pane_id)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("", "")
```

Then rewrite `cmd_init`:

```python
@handle_command_errors("init")
def cmd_init() -> None:
    """Initialize session: capture focused window + tmux session/window/pane."""
    hook_data = HookData.from_stdin()
    if is_remote_session():
        window_id, app_path = "REMOTE", "REMOTE"
        debug_log("Remote session detected, skipping window capture")
    else:
        try:
            window_id, app_path = get_focused_window_id()
        except (RuntimeError, OSError) as e:
            window_id, app_path = "UNAVAILABLE", "UNAVAILABLE"
            debug_log(f"Window capture failed, continuing without: {e}")
    tmux_session_id = get_tmux_session_id() or ""
    tmux_window_id, tmux_pane_id = get_tmux_window_pane_ids()
    state = SessionState(
        window_id=window_id,
        app_path=app_path,
        timestamp=0.0,
        tmux_session_id=tmux_session_id,
        tmux_window_id=tmux_window_id,
        tmux_pane_id=tmux_pane_id,
    )
    save_session_state(hook_data.session_id, state)
```

Update `check_deduplication` (currently reads/writes the 4-line format) to use SessionState:

```python
def check_deduplication(session_id: str) -> bool:
    """Check whether to skip this notification (called within 2s of prior)."""
    session_file = SESSION_DIR / session_id
    try:
        with open(session_file, "r+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = load_session_state(session_id)
            if (
                time.time() - state.timestamp
                < NOTIFICATION_DEDUPLICATION_THRESHOLD_SECONDS
            ):
                return True
            state.timestamp = time.time()
            f.seek(0)
            f.write(
                json.dumps(
                    {
                        "version": SESSION_STATE_VERSION,
                        "window_id": state.window_id,
                        "app_path": state.app_path,
                        "timestamp": state.timestamp,
                        "tmux_session_id": state.tmux_session_id,
                        "tmux_window_id": state.tmux_window_id,
                        "tmux_pane_id": state.tmux_pane_id,
                    }
                )
            )
            f.truncate()
            return False
    except BlockingIOError:
        return True
```

Note: existing callers pass the `session_file` Path; the new signature takes `session_id` (string). Update callers in `cmd_notify` accordingly. Let `cmd_notify` keep its existing structure for this task — Task 6 will rewrite it. Just change `check_deduplication(session_file)` to `check_deduplication(hook_data.session_id)`, and replace the 4-line load with `state = load_session_state(hook_data.session_id)`.

Then delete `save_window_id` and `load_window_id` from the file — they're unused now.

Update existing tests that call `save_window_id` or `load_window_id` to use the new functions. Run:

```bash
grep -n "save_window_id\|load_window_id" tests/
```

Update any matches to use `save_session_state` / `load_session_state` (constructing a `SessionState` for saves).

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -v`
Expected: PASS for the new test plus all existing tests. Tests that previously poked at the 4-line format need updating; if any fail, fix them to use SessionState.

- [ ] **Step 5: Lint and typecheck**

Run: `make lint typecheck`
Expected: PASS. Fix any new warnings (likely just unused-import or formatting).

- [ ] **Step 6: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: capture tmux window/pane in cmd_init, swap callers to SessionState

cmd_init now records tmux window_id (@N) and pane_id (%N) alongside the
existing tmux session id, persisted as JSON via save_session_state.
check_deduplication and cmd_notify load via load_session_state. The
legacy save_window_id/load_window_id functions are removed."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 6: Implement `decide_notification` decision function

**Files:**
- Modify: `cc_notifier.py` — add `Decision` enum and `decide_notification`
- Modify: `tests/test_integrations.py` — add `TestDecideNotification`

- [ ] **Step 1: Write the failing tests**

```python
class TestDecideNotification:
    """Exhaustive matrix over the notification decision."""

    def _state(self, **overrides):
        defaults = dict(
            window_id="99",
            app_path="/Applications/Ghostty.app",
            timestamp=0.0,
            tmux_session_id="$3",
            tmux_window_id="@42",
            tmux_pane_id="%87",
        )
        defaults.update(overrides)
        return cc_notifier.SessionState(**defaults)

    def test_no_clients_returns_push(self):
        with patch("cc_notifier.tmux_list_clients", return_value=[]):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.PUSH
            )

    def test_all_clients_idle_returns_push(self):
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=120),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.PUSH
            )

    def test_active_focused_same_window_pane_returns_silent(self):
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
            patch("cc_notifier.is_focused_ghostty_for_tty", return_value=True),
            patch(
                "cc_notifier.get_tmux_window_pane_ids",
                return_value=("@42", "%87"),
            ),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.SILENT
            )

    def test_active_focused_different_window_returns_local(self):
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
            patch("cc_notifier.is_focused_ghostty_for_tty", return_value=True),
            patch(
                "cc_notifier.get_tmux_window_pane_ids",
                return_value=("@99", "%87"),
            ),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.LOCAL
            )

    def test_active_focused_different_pane_returns_local(self):
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
            patch("cc_notifier.is_focused_ghostty_for_tty", return_value=True),
            patch(
                "cc_notifier.get_tmux_window_pane_ids",
                return_value=("@42", "%99"),
            ),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.LOCAL
            )

    def test_active_not_focused_returns_local(self):
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
            patch("cc_notifier.is_focused_ghostty_for_tty", return_value=False),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.LOCAL
            )

    def test_no_active_client_with_fresh_idle_returns_local(self):
        """Attached but no client_active=1 — user is in tmux somewhere."""
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=False)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
        ):
            assert (
                cc_notifier.decide_notification(self._state())
                is cc_notifier.Decision.LOCAL
            )

    def test_legacy_state_no_window_pane_falls_through_to_local_when_focused(self):
        """Pre-upgrade session lacks tmux_window_id/pane_id."""
        clients = [cc_notifier.ClientInfo(tty="/dev/ttys012", active=True)]
        with (
            patch("cc_notifier.tmux_list_clients", return_value=clients),
            patch("cc_notifier.tty_atime_idle", return_value=2),
            patch("cc_notifier.is_focused_ghostty_for_tty", return_value=True),
            patch(
                "cc_notifier.get_tmux_window_pane_ids",
                return_value=("@42", "%87"),
            ),
        ):
            # state has empty window/pane → can't match → SILENT not justified → LOCAL
            assert (
                cc_notifier.decide_notification(
                    self._state(tmux_window_id="", tmux_pane_id="")
                )
                is cc_notifier.Decision.LOCAL
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations.py::TestDecideNotification -v`
Expected: FAIL — `Decision` enum and `decide_notification` don't exist.

- [ ] **Step 3: Implement `Decision` and `decide_notification`**

Add the import at the top of `cc_notifier.py` if not present:

```python
from enum import Enum
```

Add the constant near `PID_WALK_MAX_DEPTH`:

```python
KEYBOARD_IDLE_THRESHOLD_SECONDS = 60
```

Add after `is_focused_ghostty_for_tty`:

```python
class Decision(Enum):
    SILENT = "silent"
    LOCAL = "local"
    PUSH = "push"


def decide_notification(state: SessionState) -> Decision:
    """Decide which notification (if any) to send for this hook event.

    Outcomes are mutually exclusive:
      SILENT — user is at the keyboard AND focused on the originating
               tmux pane in the originating window.
      LOCAL  — user is at the keyboard somewhere else (different tmux
               window/session, different macOS window, etc.).
      PUSH   — user is away (no clients attached, or all attached
               clients have been keyboard-idle for >= 60s).
    """
    clients = tmux_list_clients(state.tmux_session_id)
    if not clients:
        debug_log("decide: no clients attached -> PUSH")
        return Decision.PUSH

    min_idle = min(tty_atime_idle(c.tty) for c in clients)
    if min_idle >= KEYBOARD_IDLE_THRESHOLD_SECONDS:
        debug_log(f"decide: all clients idle (min={min_idle}s) -> PUSH")
        return Decision.PUSH

    active_client = next((c for c in clients if c.active), None)
    if active_client and is_focused_ghostty_for_tty(active_client.tty):
        if state.tmux_window_id and state.tmux_pane_id:
            current_window, current_pane = get_tmux_window_pane_ids()
            if (
                current_window == state.tmux_window_id
                and current_pane == state.tmux_pane_id
            ):
                debug_log("decide: focused on originating pane -> SILENT")
                return Decision.SILENT
        debug_log("decide: focused but different pane/legacy state -> LOCAL")
        return Decision.LOCAL

    debug_log("decide: keyboard fresh but not focused here -> LOCAL")
    return Decision.LOCAL
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_integrations.py::TestDecideNotification -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: decide_notification produces SILENT/LOCAL/PUSH

Pure decision function over (SessionState, tmux clients, TTY atime,
focused window). No clients or all-idle >= 60s -> PUSH. Active client
focused on originating window+pane -> SILENT. Anything else -> LOCAL.
Mutually exclusive outcomes — replaces today's overlapping local+push."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 7: Rewrite `cmd_notify` around `decide_notification`

**Files:**
- Modify: `cc_notifier.py` — replace body of `cmd_notify`; remove `send_local_notification_if_needed`, `check_idle_and_notify_push`, the `PUSH_IDLE_CHECK_INTERVALS_*` constants
- Modify: `tests/test_core.py` — add `TestCmdNotifyDispatch`

- [ ] **Step 1: Write the failing tests**

```python
class TestCmdNotifyDispatch:
    """Verify cmd_notify dispatches to the right notifier based on Decision."""

    def _hook_stdin(self, session_id="abc", message=""):
        return json.dumps(
            {
                "session_id": session_id,
                "cwd": "/tmp",
                "hook_event_name": "Notification",
                "message": message,
            }
        )

    def _state_file(self, tmp_path, session_id="abc"):
        state = cc_notifier.SessionState(
            window_id="99",
            app_path="/Applications/Ghostty.app",
            timestamp=0.0,
            tmux_session_id="$3",
            tmux_window_id="@42",
            tmux_pane_id="%87",
        )
        cc_notifier.save_session_state(session_id, state)
        return state

    def test_silent_decision_sends_no_notification(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        self._state_file(tmp_path)
        with (
            patch.object(sys, "argv", ["cc-notifier", "notify"]),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch("sys.stdin", StringIO(self._hook_stdin())),
            patch(
                "cc_notifier.decide_notification",
                return_value=cc_notifier.Decision.SILENT,
            ),
            patch("cc_notifier.send_notification") as mock_local,
            patch("cc_notifier.send_pushover_notification") as mock_push,
        ):
            cc_notifier.main()
        mock_local.assert_not_called()
        mock_push.assert_not_called()

    def test_local_decision_sends_terminal_notifier(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        self._state_file(tmp_path)
        with (
            patch.object(sys, "argv", ["cc-notifier", "notify"]),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch("sys.stdin", StringIO(self._hook_stdin())),
            patch(
                "cc_notifier.decide_notification",
                return_value=cc_notifier.Decision.LOCAL,
            ),
            patch("cc_notifier.send_notification") as mock_local,
            patch("cc_notifier.send_pushover_notification") as mock_push,
        ):
            cc_notifier.main()
        mock_local.assert_called_once()
        mock_push.assert_not_called()

    def test_push_decision_sends_pushover_only(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        self._state_file(tmp_path)
        with (
            patch.object(sys, "argv", ["cc-notifier", "notify"]),
            patch.dict(
                os.environ,
                {
                    "CC_NOTIFIER_WRAPPER": "1",
                    "PUSHOVER_API_TOKEN": "tok",
                    "PUSHOVER_USER_KEY": "usr",
                },
            ),
            patch("sys.stdin", StringIO(self._hook_stdin())),
            patch(
                "cc_notifier.decide_notification",
                return_value=cc_notifier.Decision.PUSH,
            ),
            patch("cc_notifier.send_notification") as mock_local,
            patch(
                "cc_notifier.send_pushover_notification", return_value=True
            ) as mock_push,
        ):
            cc_notifier.main()
        mock_local.assert_not_called()
        mock_push.assert_called_once()

    def test_push_decision_without_credentials_is_noop(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        self._state_file(tmp_path)
        with (
            patch.object(sys, "argv", ["cc-notifier", "notify"]),
            patch.dict(
                os.environ,
                {"CC_NOTIFIER_WRAPPER": "1"},
                clear=True,
            ),
            patch("sys.stdin", StringIO(self._hook_stdin())),
            patch(
                "cc_notifier.decide_notification",
                return_value=cc_notifier.Decision.PUSH,
            ),
            patch("cc_notifier.send_notification") as mock_local,
            patch(
                "cc_notifier.send_pushover_notification"
            ) as mock_push,
        ):
            cc_notifier.main()
        mock_local.assert_not_called()
        mock_push.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_core.py::TestCmdNotifyDispatch -v`
Expected: FAIL — current `cmd_notify` calls `send_local_notification_if_needed` and `check_idle_and_notify_push`, not the new dispatch.

- [ ] **Step 3: Rewrite `cmd_notify`**

Replace the body of `cmd_notify` with:

```python
@handle_command_errors("notify")
def cmd_notify() -> None:
    """Send notification based on tmux/focus/idle decision."""
    global _CURRENT_APP_PATH
    hook_data = HookData.from_stdin()

    if check_deduplication(hook_data.session_id):
        return

    state = load_session_state(hook_data.session_id)
    _CURRENT_APP_PATH = state.app_path

    decision = decide_notification(state)
    debug_log(f"cmd_notify decision: {decision.value}")

    if decision is Decision.SILENT:
        return

    title, subtitle, message = create_notification_data(
        hook_data, for_push=(decision is Decision.PUSH)
    )

    if decision is Decision.LOCAL:
        try:
            send_notification(
                title=title,
                subtitle=subtitle,
                message=message,
                focus_window_id=state.window_id
                if state.window_id not in ("UNAVAILABLE", "REMOTE", "")
                else None,
            )
        except (RuntimeError, OSError) as e:
            log_error("Local notification failed", e)
        return

    # decision is Decision.PUSH
    push_config = PushConfig.from_env()
    if not push_config:
        debug_log("PUSH decision but no Pushover credentials configured")
        return
    push_url = build_push_url(hook_data)
    debug_log(f"Sending push notification: '{title}'")
    send_pushover_notification(push_config, title, message, url=push_url)
```

Now delete the obsolete code:

- Remove `send_local_notification_if_needed` (no longer called).
- Remove `check_idle_and_notify_push` (no longer called).
- Remove `PUSH_IDLE_CHECK_INTERVALS_DESKTOP`, `PUSH_IDLE_CHECK_INTERVALS_REMOTE`, `PUSH_IDLE_CHECK_INTERVALS_ATTACHED`.
- Remove `is_tmux_session_attached` (no longer called) — confirm with grep before removing.

Run:

```bash
grep -n "send_local_notification_if_needed\|check_idle_and_notify_push\|PUSH_IDLE_CHECK\|is_tmux_session_attached" cc_notifier.py tests/
```

Anything left in tests should be deleted or updated.

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -v`
Expected: PASS. Some old tests for the deleted functions will need to go. Delete tests for `send_local_notification_if_needed`, `check_idle_and_notify_push`, `is_tmux_session_attached`, and the old idle interval logic.

- [ ] **Step 5: Lint, typecheck, deadcode**

Run: `make lint typecheck deadcode`
Expected: PASS. The deadcode pass should not flag anything we want to keep.

- [ ] **Step 6: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "feat: rewrite cmd_notify around decide_notification

cmd_notify now: load state, dedupe, call decide_notification, dispatch
to one of SILENT (return), LOCAL (terminal-notifier), or PUSH (Pushover).
Outcomes are mutually exclusive — no more concurrent local+push.

Removes send_local_notification_if_needed, check_idle_and_notify_push,
PUSH_IDLE_CHECK_INTERVALS_*, and is_tmux_session_attached. The
progressive idle-check ladder is replaced by the single 60s threshold
inside decide_notification."
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 8: Drop `Stop` hook handling assumptions; verify `Notification` path

**Files:**
- Modify: `cc_notifier.py` — `create_notification_data` may simplify since Stop won't fire here, but keep behavior identical (we don't control hook config)
- Modify: `tests/test_core.py` — add `TestNotificationEventOnly`

The settings.json change in dotfiles drops `Stop`, but cc-notifier should still handle a Stop event correctly if some other settings.json has it wired up. So the cc-notifier code itself doesn't gate on event name. Just confirm via tests.

- [ ] **Step 1: Write the test**

```python
class TestNotificationEventOnly:
    """cc-notifier handles Notification events the same as Stop events."""

    def test_notification_event_uses_message_field(self, tmp_path, monkeypatch):
        """For Notification hooks, the 'message' from stdin becomes body."""
        monkeypatch.setattr(cc_notifier, "SESSION_DIR", tmp_path)
        state = cc_notifier.SessionState(
            window_id="99",
            app_path="/Applications/Ghostty.app",
            timestamp=0.0,
            tmux_session_id="$3",
            tmux_window_id="@42",
            tmux_pane_id="%87",
        )
        cc_notifier.save_session_state("abc", state)
        stdin_payload = json.dumps(
            {
                "session_id": "abc",
                "cwd": "/tmp",
                "hook_event_name": "Notification",
                "message": "Permission needed for Bash",
            }
        )
        with (
            patch.object(sys, "argv", ["cc-notifier", "notify"]),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch("sys.stdin", StringIO(stdin_payload)),
            patch(
                "cc_notifier.decide_notification",
                return_value=cc_notifier.Decision.LOCAL,
            ),
            patch("cc_notifier.send_notification") as mock_local,
        ):
            cc_notifier.main()
        # message kwarg in send_notification should equal the stdin message
        kwargs = mock_local.call_args.kwargs
        assert kwargs["message"] == "Permission needed for Bash"
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_core.py::TestNotificationEventOnly -v`
Expected: PASS — the existing `create_notification_data` already special-cases `Notification` events. If it fails, debug; otherwise this just confirms the path works.

- [ ] **Step 3: Commit (test-only addition, possibly no code change)**

```bash
jj -R ~/code/cc-notifier commit -m "test: confirm Notification hook payload propagates to message body"
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

If `jj` reports no changes, this means the working copy is empty — skip the commit and move on.

---

## Task 9: Update `cc_notifier.context.md`

**Files:**
- Modify: `cc_notifier.context.md`

- [ ] **Step 1: Rewrite the `cc-notifier notify` flow section**

Open `cc_notifier.context.md`. Find the `### cc-notifier notify` section. Replace its **Flow** subsection with:

```markdown
**Flow**:
1. Parse hook data from stdin JSON
2. Acquire dedup lock; if another notify ran within 2s, exit silently
3. Load SessionState from /tmp/cc_notifier/{session_id} (JSON; falls back
   to legacy 3- or 4-line format for sessions that pre-date this version)
4. Call decide_notification(state) → SILENT | LOCAL | PUSH:
   - tmux_list_clients(state.tmux_session_id) → list of attached clients
   - If none: PUSH
   - If min(tty_atime_idle(c.tty) for c in clients) >= 60s: PUSH
   - If active client (client_active=1) AND focused Ghostty owns its TTY:
     - If state.tmux_window_id/pane_id match the current tmux window/pane: SILENT
     - Else: LOCAL
   - Else: LOCAL
5. SILENT → return
   LOCAL → terminal-notifier with click-to-focus on state.window_id
   PUSH  → Pushover (only when credentials are set)
6. Update session timestamp (handled inside check_deduplication)
```

Find the section listing constants (or the `Notes` block). Add:

```markdown
**Decision constants**

- `KEYBOARD_IDLE_THRESHOLD_SECONDS = 60` — clients idle this long are
  treated as "user away" (PUSH).
- `PID_WALK_MAX_DEPTH = 6` — bound on the focused-window process tree
  walk used to map focused PID → controlling TTY.

The previous `PUSH_IDLE_CHECK_INTERVALS_*` constants and progressive
idle-check ladder are removed. Decision is one-shot.
```

Find the **Session Files** description and update the format note to:

```markdown
- **Session Files**: `/tmp/cc_notifier/{session_id}` containing JSON with
  fields: version, window_id, app_path, timestamp, tmux_session_id,
  tmux_window_id, tmux_pane_id. Legacy 3-/4-line text format is parsed
  for backwards compatibility.
```

- [ ] **Step 2: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "docs: refresh context.md for the new decision flow"
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 10: Bump version to 0.4.0

**Files:**
- Modify: `pyproject.toml`
- Modify: `cc_notifier.py` — `VERSION` constant

- [ ] **Step 1: Update `VERSION` in `cc_notifier.py`**

Find the line:

```python
VERSION = "0.3.0"
```

Change it to:

```python
VERSION = "0.4.0"
```

- [ ] **Step 2: Update `pyproject.toml`**

Find:

```toml
version = "0.3.0"
```

Change it to:

```toml
version = "0.4.0"
```

- [ ] **Step 3: Run version test**

Run: `pytest tests/test_core.py -v -k version`
Expected: PASS — the existing test just checks the version string is printed; it picks up whatever VERSION is set to.

- [ ] **Step 4: Commit**

```bash
jj -R ~/code/cc-notifier commit -m "chore: bump version to 0.4.0"
jj -R ~/code/cc-notifier bookmark set la/tmux-client-routing-design -r @-
```

---

## Task 11: Full quality check + push fork

**Files:** none (verification + push only)

- [ ] **Step 1: Run the full quality matrix**

Run: `make check`
Expected: `🎉 CHECK PASSED` for format, lint, typecheck, test, deadcode, shell-lint.

If anything fails, fix in place and commit a separate `fix:` commit before proceeding.

- [ ] **Step 2: Push the bookmark to origin**

```bash
jj -R ~/code/cc-notifier git push --bookmark la/tmux-client-routing-design
```

Expected: pushes the branch to `lamdor/cc-notifier` on GitHub. Note the commit SHA at the head of the bookmark — needed in Task 12.

- [ ] **Step 3: Capture the new commit hash and prefetch hash**

Run:

```bash
HEAD_REV=$(jj -R ~/code/cc-notifier log -r 'la/tmux-client-routing-design' --no-graph -T 'commit_id' --limit 1)
echo "rev=$HEAD_REV"
nix-prefetch-url --unpack "https://github.com/lamdor/cc-notifier/archive/${HEAD_REV}.tar.gz" 2>/dev/null | tail -1
```

Expected: prints a SHA-256 hash. Save both the commit hash and the SRI-converted hash for Task 12.

To convert the prefetch hash to the SRI form Nix expects, run:

```bash
nix hash to-sri --type sha256 <hash-from-prefetch>
```

Save the resulting `sha256-...` string.

---

## Task 12: Update dotfiles (overlay + settings.json)

**Files:**
- Modify: `~/dotfiles/nix/overlays/cc-notifier.nix`
- Modify: `~/dotfiles/claude/settings.json`

- [ ] **Step 1: Update the overlay**

Open `~/dotfiles/nix/overlays/cc-notifier.nix`. Update the version, rev, and hash. Use the values captured in Task 11 Step 3.

```nix
final: prev: {
  cc-notifier = final.stdenv.mkDerivation {
    pname = "cc-notifier";
    version = "0.4.0-unstable-2026-05-05";

    src = final.fetchFromGitHub {
      owner = "lamdor";
      repo = "cc-notifier";
      rev = "<HEAD_REV from Task 11>";
      hash = "<sha256-... from Task 11>";
    };
    # ... rest unchanged
  };
}
```

- [ ] **Step 2: Drop `Stop` from claude/settings.json**

Open `~/dotfiles/claude/settings.json`. Find the `"hooks"` block. Remove the `"Stop"` key entirely. The result should look like:

```json
"hooks": {
  "SessionStart": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "cc-notifier init"
        }
      ]
    }
  ],
  "Notification": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "cc-notifier notify"
        }
      ]
    }
  ],
  "SessionEnd": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "cc-notifier cleanup"
        }
      ]
    }
  ]
}
```

- [ ] **Step 3: Hand back to the user for `./switch.sh`**

`./switch.sh` requires sudo and must run in the user's terminal — not from Claude Code. Output:

> "Overlay and settings.json updated. Run `./switch.sh` in your terminal to apply, then restart Claude Code so the new hook config takes effect."

- [ ] **Step 4: Commit dotfiles changes**

```bash
jj -R ~/dotfiles commit -m "cc-notifier: bump to 0.4.0 and drop Stop hook

Picks up the tmux-client-aware notification routing from
lamdor/cc-notifier branch la/tmux-client-routing-design. Drops the
Stop hook so polling loops in auto mode no longer spam notifications;
Notification still covers permission prompts."
jj -R ~/dotfiles bookmark create la/cc-notifier-tmux-aware -r @-
```

- [ ] **Step 5: Manual verification (post `switch.sh`)**

Walk through this matrix in actual use over the next 1–2 sessions:

1. Start a Claude session in tmux pane A. Stay typing in pane A. Trigger a Notification (e.g. permission prompt). → No notification on screen.
2. Switch to tmux pane B in the same session. Trigger Notification. → terminal-notifier banner.
3. Switch to a different Ghostty window with a different tmux session attached. Trigger. → terminal-notifier banner.
4. Walk away from the keyboard for ~70s. Trigger. → Pushover only, no terminal-notifier.
5. Run a polling background process (e.g. a watch script). Confirm no flood of notifications — `Stop` hook is gone, only `Notification` events fire.

Check `~/.cc-notifier/cc-notifier.log` for `decide:` lines if behavior is unexpected. Run `cc-notifier --debug notify` manually with a stdin payload to trace.

---

## Self-review notes

- All spec sections map to tasks: Init capture (T5), JSON state (T4), Notify decision (T6), helpers (T1/T2/T3), `cmd_notify` rewrite (T7), settings.json change (T12), context docs (T9), version bump (T10), rollout (T11/T12).
- No placeholders. Every code step shows the code. Every test step shows the test.
- Type/name consistency: `Decision` enum used consistently across T6/T7. `SessionState` field names match across T4/T5/T6/T7. `ClientInfo` fields stable across T1/T6.
- `is_tmux_session_attached` removal is gated on a grep check (T7 step 3) so we don't break callers.
- The `cmd_notify` rewrite (T7) is the largest single task — broken into test → code → cleanup → quality steps to stay reviewable.
