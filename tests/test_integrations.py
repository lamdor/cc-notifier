"""
System integration tests for cc-notifier.

Tests external system interactions including Hammerspoon CLI, terminal-notifier,
error handling, and notification system integration. Focuses on system boundary
interactions and external dependency contracts.
"""

import os
import subprocess
import sys
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

import cc_notifier


class TestHammerspoonIntegration:
    """Test Hammerspoon CLI integration for window management."""

    @patch("cc_notifier.run_command")
    def test_hammerspoon_cli_integration(self, mock_run_command):
        """Test Hammerspoon CLI integration with success, timeout, and error scenarios."""
        # Test 1: Success scenario
        mock_run_command.return_value = (
            "98765|/System/Applications/Utilities/Terminal.app"
        )
        window_id, app_path = cc_notifier.get_focused_window_id()
        assert window_id == "98765"
        assert app_path == "/System/Applications/Utilities/Terminal.app"

        # Verify command construction
        args = mock_run_command.call_args[0][0]
        assert str(cc_notifier.HAMMERSPOON_CLI) in args
        assert "-c" in args

        # Test 2: Timeout handling
        mock_run_command.reset_mock()
        mock_run_command.side_effect = subprocess.TimeoutExpired("hs", 10)

        with pytest.raises(RuntimeError, match="timed out"):
            cc_notifier.get_focused_window_id()

        # Test 3: ERROR response handling
        mock_run_command.reset_mock()
        mock_run_command.side_effect = None
        mock_run_command.return_value = "ERROR"

        with pytest.raises(RuntimeError, match="Failed to get focused window ID"):
            cc_notifier.get_focused_window_id()


class TestExternalSystemErrorHandling:
    """Test error handling for external system interactions."""

    def test_json_parsing_error_recovery(self, tmp_path):
        """Test error handling for malformed JSON from Claude Code hooks."""
        session_dir = tmp_path / "cc_notifier"

        # Test with completely invalid JSON
        with (
            patch("sys.stdin", StringIO("not valid json at all")),
            patch.object(sys, "argv", ["cc-notifier", "init"]),
            patch.object(cc_notifier, "SESSION_DIR", session_dir),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch("cc_notifier.run_background_command"),
        ):
            try:
                cc_notifier.main()
                raise AssertionError("Should have raised SystemExit due to JSON error")
            except SystemExit as e:
                assert e.code == 1

        # Test with valid JSON but missing required fields
        with (
            patch("sys.stdin", StringIO('{"invalid": "missing session_id"}')),
            patch.object(sys, "argv", ["cc-notifier", "init"]),
            patch.object(cc_notifier, "SESSION_DIR", session_dir),
            patch.dict(os.environ, {"CC_NOTIFIER_WRAPPER": "1"}),
            patch("cc_notifier.run_background_command"),
        ):
            try:
                cc_notifier.main()
                raise AssertionError(
                    "Should have raised SystemExit due to missing session_id"
                )
            except SystemExit as e:
                assert e.code == 1

    def test_corrupted_session_file_handling(self, tmp_path):
        """Test handling corrupted session files."""
        session_dir = tmp_path / "cc_notifier"
        session_dir.mkdir()
        (session_dir / "test").write_bytes(b"\xff\xfe")

        with (
            patch.object(cc_notifier, "SESSION_DIR", session_dir),
            pytest.raises(UnicodeDecodeError),
        ):
            cc_notifier.load_session_state("test")


class TestNotificationSystemIntegration:
    """Test notification system integration and command construction."""

    def test_create_focus_command_generates_correct_script(self):
        """Test create_focus_command() generates correct Hammerspoon script."""
        window_id = "12345"
        command = cc_notifier.create_focus_command(window_id)

        assert len(command) == 3
        assert str(cc_notifier.HAMMERSPOON_CLI) == command[0]
        assert command[1] == "-c"
        assert "12345" in command[2]
        assert "w:focus()" in command[2]
        assert "hs.window.filter" in command[2]

    @patch("subprocess.Popen")
    def test_terminal_notifier_command_construction(self, mock_popen):
        """Test proper command construction for notification scenarios."""
        # Test basic notification command construction
        cc_notifier.send_notification(
            title="Test Title", subtitle="Test Subtitle", message="Test Message"
        )

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]

        # Verify complete command structure
        expected_basic_cmd = [
            cc_notifier.TERMINAL_NOTIFIER,
            "-title",
            "Test Title",
            "-subtitle",
            "Test Subtitle",
            "-message",
            "Test Message",
            "-sound",
            "Glass",
            "-ignoreDnD",
        ]
        assert cmd == expected_basic_cmd

        # Test command with focus parameter (execute parameter)
        mock_popen.reset_mock()
        window_id = "98765"
        cc_notifier.send_notification(
            title="Focus Test",
            subtitle="Test Subtitle",
            message="Test Message",
            focus_window_id=window_id,
        )

        cmd = mock_popen.call_args[0][0]

        # Verify execute parameter is included
        assert "-execute" in cmd
        execute_index = cmd.index("-execute")
        execute_command = cmd[execute_index + 1]

        # Verify the execute command contains the window ID and focus logic
        assert window_id in execute_command
        assert "w:id()==98765" in execute_command
        assert "w:focus()" in execute_command
        assert "hs.window.filter" in execute_command

    def test_basic_error_logging_functionality(self, tmp_path):
        """Test basic error logging functionality."""
        log_file = tmp_path / ".cc-notifier" / "cc-notifier.log"

        with patch.object(cc_notifier, "LOG_FILE", log_file):
            cc_notifier.log_error("Test error", ValueError("test"))

            assert log_file.exists()
            content = log_file.read_text()
            assert "Test error" in content
            assert "ValueError: test" in content


class TestTmuxListClients:
    """Test tmux_list_clients parsing."""

    def test_returns_empty_list_when_session_missing(self):
        """No clients attached -> empty list."""
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
        """tmux not running or session vanished -> empty list."""
        with patch("cc_notifier.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = cc_notifier.tmux_list_clients("$3")
            assert result == []

    def test_returns_empty_when_tmux_not_found(self):
        """tmux binary missing -> empty list."""
        with patch(
            "cc_notifier.subprocess.run", side_effect=FileNotFoundError()
        ):
            result = cc_notifier.tmux_list_clients("$3")
            assert result == []


class TestTtyAtimeIdle:
    """Test tty_atime_idle with explicit paths."""

    def test_returns_seconds_since_atime(self, tmp_path):
        """Stat a real file, set atime, get back idle seconds."""
        f = tmp_path / "fake_tty"
        f.write_text("")
        thirty_ago = time.time() - 30
        os.utime(f, (thirty_ago, time.time()))
        idle = cc_notifier.tty_atime_idle(str(f))
        assert 28 <= idle <= 32

    def test_returns_huge_idle_when_path_missing(self):
        """Missing TTY path -> very large value (treated as idle)."""
        idle = cc_notifier.tty_atime_idle("/dev/this-does-not-exist")
        assert idle >= 10**8

    def test_returns_huge_idle_on_oserror(self):
        """Permission errors etc -> very large value."""
        with patch("cc_notifier.os.stat", side_effect=PermissionError()):
            idle = cc_notifier.tty_atime_idle("/dev/ttys012")
            assert idle >= 10**8


class TestIsFocusedGhostty:
    """Test is_focused_ghostty_for_tty PID-walk logic."""

    def test_returns_true_when_focused_pid_owns_client_tty(self):
        """Focused window's process tree contains the client TTY."""
        with (
            patch("cc_notifier.get_focused_window_pid", return_value=999),
            patch(
                "cc_notifier.walk_descendant_ttys",
                return_value={"/dev/ttys012", "/dev/ttys015"},
            ),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012") is True
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
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012") is False
            )

    def test_returns_false_when_hammerspoon_fails(self):
        """Hammerspoon error -> conservative LOCAL -> return False."""
        with patch(
            "cc_notifier.get_focused_window_pid",
            side_effect=RuntimeError("no hs"),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012") is False
            )

    def test_returns_false_when_no_descendants(self):
        """Focused app isn't a terminal — empty TTY set."""
        with (
            patch("cc_notifier.get_focused_window_pid", return_value=999),
            patch("cc_notifier.walk_descendant_ttys", return_value=set()),
        ):
            assert (
                cc_notifier.is_focused_ghostty_for_tty("/dev/ttys012") is False
            )


class TestWalkDescendantTtys:
    """Test process tree TTY collection."""

    def test_collects_ttys_from_immediate_children(self):
        """ps returns a TTY for the child PID, normalized."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0] == "pgrep":
                pid = cmd[-1]
                mock.stdout = "200\n" if pid == "100" else ""
            elif cmd[0] == "ps":
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
                parent = int(cmd[-1])
                mock.stdout = f"{parent + 1}\n"
            else:
                mock.stdout = "?"
            return mock

        with patch("cc_notifier.subprocess.run", side_effect=fake_run):
            # If walk were unbounded this would never return.
            result = cc_notifier.walk_descendant_ttys(100)
            assert result == set()


class TestDecideNotification:
    """Exhaustive matrix over the notification decision."""

    def _state(self, **overrides):
        defaults = {
            "window_id": "99",
            "app_path": "/Applications/Ghostty.app",
            "timestamp": 0.0,
            "tmux_session_id": "$3",
            "tmux_window_id": "@42",
            "tmux_pane_id": "%87",
        }
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
            # state has empty window/pane → can't match → LOCAL
            assert (
                cc_notifier.decide_notification(
                    self._state(tmux_window_id="", tmux_pane_id="")
                )
                is cc_notifier.Decision.LOCAL
            )
