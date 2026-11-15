"""#7081: one ELITEA MCP process per user; the tray or `serve` holds client_id.lock for its whole life."""
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import appdirs
import pytest

import elitea_mcp.commands.run_servers as elitea_run_servers
import src.elitea_mcp.main as main_module
import src.elitea_mcp.tray as tray_module
from src.elitea_mcp.utils import client_identity, diagnostics, single_instance, update_helper
from src.elitea_mcp.utils.logging_setup import SERVE, TRAY
from elitea_mcp.utils import single_instance as elitea_single_instance

ROOT = Path(__file__).resolve().parents[1]
HOLDER = ("import sys, time\n"
          "from pathlib import Path\n"
          "from elitea_mcp.utils import client_identity, single_instance\n"
          "client_identity.get_config_dir = lambda: Path(sys.argv[1])\n"
          "try:\n"
          "    single_instance.hold_instance(sys.argv[2])\n"
          "except single_instance.AlreadyRunningError as error:\n"
          "    print('REFUSED', error, flush=True); sys.exit(1)\n"
          "if len(sys.argv) > 3 and sys.argv[3] == 'record':\n"
          "    Path(sys.argv[4]).write_text('HELD'); sys.exit(0)\n"
          "if len(sys.argv) > 3 and sys.argv[3] == 'child':\n"
          "    import subprocess; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
          "print('READY', flush=True)\n"
          "sys.stdin.readline()\n")
OLD_CLIENT = ("import fcntl, sys, time\n"
              "handle = open(sys.argv[1], 'a+')\n"
              "fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
              "print('READY', flush=True)\n"
              "sys.stdin.readline()\n")
PID_FILE_HOLDER = ("import signal, sys\n"
                   "from elitea_mcp.utils.pid_file import write_pid_file\n"
                   "write_pid_file(sys.argv[1])\n"
                   "signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))\n"
                   "print('READY', flush=True)\n"
                   "signal.pause()\n")

posix_only = pytest.mark.skipif(client_identity.fcntl is None, reason="flock holders")


def config_dir_under(home):
    relative = ("Library", "Application Support") if sys.platform == "darwin" else (".config",)
    return home.joinpath(*relative, "elitea-mcp-client")


def child_env(home=None):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    if home:
        env["HOME"] = str(home)
    return env


def start_ready(*args):
    process = subprocess.Popen([sys.executable, "-c", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, env=child_env())
    assert process.stdout.readline().strip() == "READY"
    return process


def try_second(config_dir, command):
    return subprocess.run([sys.executable, "-c", HOLDER, str(config_dir), command, "record", str(config_dir / "rec")],
                          capture_output=True, text=True, env=child_env())


def wait_until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.05)


@pytest.fixture
def holders():
    started = []

    def start(config_dir, command, *extra):
        started.append(start_ready(HOLDER, str(config_dir), command, *extra))
        return started[-1]

    yield start
    for process in started:
        process.kill()
        process.wait()


def fail_lock_with(monkeypatch, number):
    def refuse(handle):
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(client_identity, "lock_exclusively", refuse)


@posix_only
@pytest.mark.parametrize("first", [TRAY, SERVE])
@pytest.mark.parametrize("second", [TRAY, SERVE])
def test_a_second_tray_or_serve_is_refused_with_the_holders_pid_and_command(tmp_path, holders, first, second):
    holder = holders(tmp_path, first)

    refused = try_second(tmp_path, second)

    assert refused.returncode == 1
    assert f"already running (PID {holder.pid}, {first})" in refused.stdout


@posix_only
def test_a_killed_holder_never_blocks_the_next_start(tmp_path, holders):
    holder = holders(tmp_path, TRAY)
    holder.send_signal(signal.SIGKILL)
    holder.wait()

    assert try_second(tmp_path, SERVE).returncode == 0


@posix_only
def test_a_child_the_holder_started_never_keeps_the_lock(tmp_path, holders):
    holder = holders(tmp_path, TRAY, "child")
    holder.send_signal(signal.SIGKILL)
    holder.wait()

    assert try_second(tmp_path, TRAY).returncode == 0


@posix_only
def test_a_connected_client_older_than_this_check_also_refuses_a_new_one(tmp_path):
    old_client = start_ready(OLD_CLIENT, str(tmp_path / client_identity.CLIENT_ID_LOCK_NAME))
    try:
        refused = try_second(tmp_path, SERVE)
    finally:
        old_client.kill()
        old_client.wait()

    assert refused.returncode == 1
    assert "already running (PID unknown)" in refused.stdout
    assert "elitea-mcp doctor" in refused.stdout


@posix_only
def test_an_older_client_holding_the_lock_after_a_new_one_crashed_reads_pid_unknown_not_the_dead_pid(tmp_path, holders):
    gone = holders(tmp_path, TRAY)
    gone.send_signal(signal.SIGKILL)
    gone.wait(timeout=10)
    assert str(gone.pid) in (tmp_path / single_instance.OWNER_FILE_NAME).read_text()
    old_client = start_ready(OLD_CLIENT, str(tmp_path / client_identity.CLIENT_ID_LOCK_NAME))
    try:
        refused = try_second(tmp_path, SERVE)
        doctor = diagnostics.check_instance()
    finally:
        old_client.kill()
        old_client.wait()

    assert "already running (PID unknown)" in refused.stdout
    assert str(gone.pid) not in refused.stdout
    assert doctor == diagnostics.ok("Instance", "running (PID unknown)")


@posix_only
def test_a_holder_that_exits_cleanly_removes_its_owner_file(tmp_path, holders):
    holder = holders(tmp_path, TRAY)
    assert (tmp_path / single_instance.OWNER_FILE_NAME).exists()

    holder.stdin.close()
    holder.wait(timeout=10)

    assert not (tmp_path / single_instance.OWNER_FILE_NAME).exists()


def test_the_exit_cleanup_leaves_an_owner_file_a_successor_wrote(tmp_path):
    owner_path = tmp_path / single_instance.OWNER_FILE_NAME
    owner_path.write_text(json.dumps({"pid": os.getpid() + 1, "command": "serve"}))

    single_instance.remove_owner_if_mine(owner_path, os.getpid())

    assert owner_path.exists()


def test_a_live_pid_is_alive_and_an_ended_one_is_not():
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()

    assert single_instance.is_alive(os.getpid())
    assert not single_instance.is_alive(ended.pid)


@posix_only
def test_the_update_helper_relaunches_a_tray_that_wins_the_lock_once_the_old_tray_exits(tmp_path, holders):
    old_tray = holders(tmp_path, TRAY)
    env_python = tmp_path / "env-python"
    env_python.write_text("#!/bin/sh\necho 9.9.9\n")
    env_python.chmod(0o755)
    record = tmp_path / "relaunched"
    job = {"tray_pid": old_tray.pid, "current": "0.1.0", "target": "9.9.9", "env_python": str(env_python),
           "upgrade": [sys.executable, "-c", "pass"],
           "relaunch": [sys.executable, "-c", HOLDER, str(tmp_path), TRAY, "record", str(record)],
           "log": str(tmp_path / "update.log"), "result": str(tmp_path / "result.json"),
           "servers_were_running": False, "server_daemon_mode": False}
    (tmp_path / "job.json").write_text(json.dumps(job))
    helper = subprocess.Popen([sys.executable, str(Path(update_helper.__file__)), str(tmp_path / "job.json")],
                              env=child_env())
    time.sleep(0.5)
    old_tray.stdin.close()
    old_tray.wait(timeout=10)
    helper.wait(timeout=30)

    wait_until(record.exists)
    assert record.read_text() == "HELD"


@posix_only
def test_doctor_names_the_holder_and_never_takes_the_lock(tmp_path, holders):
    holder = holders(tmp_path, SERVE)

    assert diagnostics.check_instance() == diagnostics.ok("Instance", f"running (PID {holder.pid}, serve)")
    assert try_second(tmp_path, TRAY).returncode == 1


def test_doctor_reports_no_instance_when_the_lock_is_free_and_creates_no_lock_file(tmp_path):
    assert diagnostics.check_instance() == diagnostics.ok("Instance", "none")
    assert not (tmp_path / client_identity.CLIENT_ID_LOCK_NAME).exists()


def test_the_doctor_environment_group_lists_the_instance_before_the_logs(tmp_path):
    results = diagnostics.check_environment(diagnostics.DoctorContext(tmp_path, tmp_path / "logs"))

    assert [result.title for result in results][-2:] == ["Instance", "Logs"]


def test_doctor_reports_unknown_when_the_lock_cannot_be_used(tmp_path, monkeypatch):
    (tmp_path / client_identity.CLIENT_ID_LOCK_NAME).write_text("")
    fail_lock_with(monkeypatch, errno.ENOLCK)

    assert diagnostics.check_instance() == diagnostics.warn(
        "Instance", f"unknown (cannot lock {tmp_path / client_identity.CLIENT_ID_LOCK_NAME})")


@posix_only
def test_a_start_that_meets_a_peek_holding_the_lock_for_an_instant_still_wins(tmp_path):
    peek = os.open(tmp_path / client_identity.CLIENT_ID_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    client_identity.fcntl.flock(peek, client_identity.fcntl.LOCK_EX | client_identity.fcntl.LOCK_NB)
    threading.Timer(0.03, os.close, [peek]).start()

    single_instance.hold_instance(TRAY)

    assert client_identity.is_held_here(tmp_path / client_identity.CLIENT_ID_LOCK_NAME)


@posix_only
@pytest.mark.parametrize("owner", ['{"pid": "1; rm -rf", "command": "tray"}', '{"pid": 0, "command": "tray"}',
                                   '{"pid": 42, "command": "run --auth_token x"}', "not json", None])
def test_a_missing_malformed_or_unexpected_owner_file_reads_pid_unknown(tmp_path, holders, owner):
    holders(tmp_path, TRAY)
    owner_path = tmp_path / single_instance.OWNER_FILE_NAME
    if owner is None:
        owner_path.unlink()
    else:
        owner_path.write_text(owner)

    refused = try_second(tmp_path, SERVE)

    assert "already running (PID unknown)" in refused.stdout
    assert "auth_token" not in refused.stdout


@posix_only
def test_a_stale_owner_file_from_a_crashed_holder_is_replaced_before_the_new_holder_runs(tmp_path, holders):
    (tmp_path / single_instance.OWNER_FILE_NAME).write_text(json.dumps({"pid": 999999, "command": "serve"}))
    holder = holders(tmp_path, TRAY)

    assert f"PID {holder.pid}, tray" in try_second(tmp_path, SERVE).stdout


def test_an_owner_file_that_cannot_be_written_leaves_pid_unknown_never_the_last_holders(tmp_path, monkeypatch):
    (tmp_path / single_instance.OWNER_FILE_NAME).write_text(json.dumps({"pid": os.getppid(), "command": "serve"}))

    def cannot_write(path, content):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(single_instance, "write_file_atomically", cannot_write)
    single_instance.hold_instance(TRAY)

    assert single_instance.read_holder() == single_instance.Holder(None, "")


@pytest.mark.parametrize("number", [errno.ENOLCK, errno.EIO])
def test_a_lock_that_cannot_be_used_warns_and_starts_without_it(tmp_path, monkeypatch, caplog, number):
    fail_lock_with(monkeypatch, number)

    single_instance.hold_instance(SERVE)

    assert not client_identity.is_held_here(tmp_path / client_identity.CLIENT_ID_LOCK_NAME)
    assert "cannot be detected" in caplog.text


@pytest.mark.parametrize("number", [errno.ENOLCK, errno.EIO])
def test_the_peek_reports_unavailable_never_running_when_the_lock_cannot_be_used(tmp_path, monkeypatch, number):
    (tmp_path / client_identity.CLIENT_ID_LOCK_NAME).write_text("")
    fail_lock_with(monkeypatch, number)

    assert single_instance.find_holder() is single_instance.UNAVAILABLE


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs file permissions")
def test_a_lock_file_that_cannot_be_opened_warns_and_starts_without_it(tmp_path, caplog):
    lock = tmp_path / client_identity.CLIENT_ID_LOCK_NAME
    lock.write_text("")
    lock.chmod(0)
    try:
        single_instance.hold_instance(SERVE)
        peeked = single_instance.find_holder()
    finally:
        lock.chmod(0o600)

    assert not client_identity.is_held_here(lock)
    assert f"Could not open {lock}" in caplog.text
    assert peeked is single_instance.UNAVAILABLE


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_lock_file_is_owner_only_and_an_existing_one_is_tightened(tmp_path):
    lock = tmp_path / client_identity.CLIENT_ID_LOCK_NAME
    lock.write_text("")
    lock.chmod(0o644)

    single_instance.hold_instance(TRAY)

    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@posix_only
def test_the_owner_file_names_this_process_and_the_lock_stays_until_released(tmp_path):
    single_instance.hold_instance(SERVE)
    owner = json.loads((tmp_path / single_instance.OWNER_FILE_NAME).read_text())
    assert (owner["pid"], owner["command"]) == (os.getpid(), "serve")
    assert try_second(tmp_path, TRAY).returncode == 1

    single_instance.release_instance()

    assert try_second(tmp_path, TRAY).returncode == 0


def test_a_holder_keeps_its_durable_client_id(tmp_path):
    (tmp_path / client_identity.CLIENT_ID_FILE_NAME).write_text(f"{client_identity.read_machine_part()}-durable")

    single_instance.hold_instance(TRAY)

    assert client_identity.load_client_id() == f"{client_identity.read_machine_part()}-durable"


def test_holding_twice_in_one_process_returns_at_once(tmp_path):
    single_instance.hold_instance(TRAY)
    single_instance.hold_instance(TRAY)

    assert client_identity.is_held_here(tmp_path / client_identity.CLIENT_ID_LOCK_NAME)


@posix_only
def test_serve_refuses_before_reading_the_config_or_writing_a_pid_file(tmp_path, holders, monkeypatch, capsys):
    holder = holders(tmp_path, TRAY)
    load_config = MagicMock()
    monkeypatch.setattr(elitea_run_servers, "load_config", load_config)
    pid_file = tmp_path / "serve.pid"

    with pytest.raises(SystemExit) as exit_:
        elitea_run_servers.run_servers(pid_file=str(pid_file))

    assert exit_.value.code == 1
    assert f"PID {holder.pid}, tray" in capsys.readouterr().err
    load_config.assert_not_called()
    assert not pid_file.exists()


def test_serve_writes_its_pid_file_once_it_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(elitea_run_servers, "load_config", MagicMock(side_effect=RuntimeError("stop here")))
    pid_file = tmp_path / "serve.pid"

    with pytest.raises(RuntimeError):
        elitea_run_servers.run_servers(pid_file=str(pid_file))

    assert pid_file.read_text() == f"{os.getpid()}\n"


@posix_only
@pytest.mark.parametrize("on_terminal", [True, False])
def test_a_refused_tray_never_builds_the_tray_and_shows_the_box_only_without_a_terminal(tmp_path, holders,
                                                                                       monkeypatch, capsys,
                                                                                       on_terminal):
    holders(tmp_path, SERVE)
    tray_app = MagicMock()
    monkeypatch.setattr(tray_module, "MCPTrayApp", tray_app)
    monkeypatch.setattr(tray_module, "setup_logging", MagicMock())
    monkeypatch.setattr(tray_module, "read_config_leniently", MagicMock(return_value={}))
    monkeypatch.setattr(sys.stderr, "isatty", lambda: on_terminal)
    tk = MagicMock()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk.messagebox)
    pid_file = tmp_path / "tray.pid"

    with pytest.raises(SystemExit) as exit_:
        tray_module.run_tray(pid_file=str(pid_file))

    assert exit_.value.code == 1
    tray_app.assert_not_called()
    assert tk.Tk.called is not on_terminal
    assert tk.messagebox.showerror.called is not on_terminal
    assert "already running" in capsys.readouterr().err
    assert not pid_file.exists()


@pytest.mark.parametrize("command", [["settings", "--from-tray"], ["doctor"], ["validate"], ["run"]])
def test_commands_other_than_tray_and_serve_never_take_the_lock(monkeypatch, command):
    for module in (single_instance, elitea_single_instance):
        monkeypatch.setattr(module, "hold_instance", MagicMock(side_effect=AssertionError))
    for name in ("run_settings_window", "doctor", "validate", "main"):
        monkeypatch.setattr(main_module, name, MagicMock(return_value=0))
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", *command])
    try:
        main_module.cli()
    except SystemExit as exit_:
        assert exit_.code in (0, None)


@posix_only
@pytest.mark.parametrize("command", ["serve", "tray"])
def test_a_daemon_launch_refuses_in_the_terminal_and_keeps_the_holders_pid_file(tmp_path, command):
    home = tmp_path / "home"
    config_dir = config_dir_under(home)
    config_dir.mkdir(parents=True)
    holder = start_ready(HOLDER, str(config_dir), SERVE)
    pid_file = tmp_path / "holder.pid"
    pid_file.write_text(f"{holder.pid}\n")
    try:
        launched = subprocess.run([sys.executable, "-m", "elitea_mcp.main", command, "--daemon", "--pid-file",
                                   str(pid_file), "--log-file", str(tmp_path / "d.log")],
                                  capture_output=True, text=True, env=child_env(home), timeout=60)
    finally:
        holder.kill()
        holder.wait()

    assert launched.returncode == 1
    assert f"already running (PID {holder.pid}, serve)" in launched.stderr
    assert pid_file.read_text() == f"{holder.pid}\n"


@posix_only
def test_a_daemon_child_that_gets_past_the_peek_never_touches_the_holders_pid_file(tmp_path):
    home = tmp_path / "home"
    config_dir = config_dir_under(home)
    config_dir.mkdir(parents=True)
    holder = start_ready(HOLDER, str(config_dir), SERVE)
    pid_file = tmp_path / "holder.pid"
    pid_file.write_text(f"{holder.pid}\n")
    race_past_the_peek = ("import sys\nimport elitea_mcp.main as m\nm.find_holder = lambda: None\n"
                          "sys.argv = ['elitea-mcp', 'serve', '--daemon', '--pid-file', sys.argv[1], "
                          "'--log-file', sys.argv[2]]\n"
                          "m.cli()\n")
    try:
        subprocess.run([sys.executable, "-c", race_past_the_peek, str(pid_file), str(tmp_path / "d.log")],
                       env=child_env(home), timeout=60)
        time.sleep(3)
        survived = pid_file.read_text() if pid_file.exists() else None
    finally:
        holder.kill()
        holder.wait()

    assert survived == f"{holder.pid}\n"


@posix_only
def test_a_terminated_daemon_removes_its_pid_file(tmp_path):
    pid_file = tmp_path / "serve.pid"
    daemon = start_ready(PID_FILE_HOLDER, str(pid_file))
    assert pid_file.read_text() == f"{daemon.pid}\n"

    daemon.send_signal(signal.SIGTERM)
    daemon.wait(timeout=10)

    assert not pid_file.exists()


def test_the_macos_launcher_writes_no_pid_file_and_hands_its_path_to_the_child(tmp_path, monkeypatch):
    started = {}

    def fake_popen(cmd, **kwargs):
        started["cmd"] = cmd
        return MagicMock(pid=4242)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/elitea-mcp")
    pid_file = tmp_path / "tray.pid"

    with pytest.raises(SystemExit):
        main_module.macos_gui_daemonize(str(pid_file), None)

    assert started["cmd"][-2:] == ["--pid-file", str(pid_file)]
    assert not pid_file.exists()


def test_the_windows_launcher_pauses_when_the_tray_exits_with_an_error():
    lines = [line.strip() for line in (ROOT / "scripts" / "launch-tray-windows.bat").read_text().splitlines()]

    assert lines[lines.index("elitea-mcp tray") + 1] == "if errorlevel 1 pause"


def test_tests_never_reach_the_real_config_dir(tmp_path):
    real = Path(appdirs.user_config_dir("elitea-mcp-client"))
    assert single_instance.get_lock_path().parent == tmp_path
    assert elitea_single_instance.get_lock_path().parent == tmp_path
    assert real != tmp_path
