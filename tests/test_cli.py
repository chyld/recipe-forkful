from conftest import PASSWORDS, logged_in, make_user


def invoke(runner, *args, password=None):
    return runner.invoke(args=list(args), input=None if password is None else password + "\n")


def test_create_user_then_log_in(app, runner):
    make_user(runner, "alice")
    assert logged_in(app, "alice")


def test_create_user_rejects_duplicates_ignoring_case(runner):
    make_user(runner, "Alice", "first-password-1")
    result = invoke(runner, "create-user", "alice", "--password-stdin", password="second-password-2")
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert "Traceback" not in result.output


def test_create_user_rejects_bad_usernames(runner):
    for bad in ["has space", "x" * 33, "", "ünïcode", "semi;colon"]:
        result = invoke(runner, "create-user", bad, "--password-stdin", password="long-enough-pw")
        assert result.exit_code == 1, bad
        assert "Usernames are" in result.output


def test_create_user_rejects_short_passwords(runner):
    result = invoke(runner, "create-user", "carol", "--password-stdin", password="four")
    assert result.exit_code == 1
    assert "at least 5" in result.output
    assert invoke(runner, "create-user", "carol", "--password-stdin", password="fives").exit_code == 0


def test_create_user_prompts_twice_when_interactive(app, runner):
    result = runner.invoke(args=["create-user", "dana"], input="dana-password-1\ndana-password-1\n")
    assert result.exit_code == 0, result.output
    result = runner.invoke(args=["create-user", "erin"], input="erin-password-1\nnot-the-same-1\nerin-password-1\nerin-password-1\n")
    assert result.exit_code == 0
    assert "do not match" in result.output.lower()


def test_set_password_changes_password_and_ends_sessions(app, runner, users):
    alice = logged_in(app, "alice")
    assert alice.get("/").status_code == 200
    result = invoke(runner, "set-password", "ALICE", "--password-stdin", password="brand-new-password")
    assert result.exit_code == 0, result.output
    assert "ended 1 session" in result.output
    assert alice.get("/").status_code == 302          # the old session is gone
    PASSWORDS_BACKUP = dict(PASSWORDS)
    try:
        PASSWORDS["alice"] = "brand-new-password"
        assert logged_in(app, "alice").get("/").status_code == 200
    finally:
        PASSWORDS.update(PASSWORDS_BACKUP)


def test_set_password_unknown_user(runner, users):
    result = invoke(runner, "set-password", "nobody", "--password-stdin", password="whatever-password")
    assert result.exit_code == 1
    assert "No user named" in result.output
    assert "Traceback" not in result.output


def test_migrate_command_reports_up_to_date(runner):
    result = invoke(runner, "migrate")
    assert result.exit_code == 0
    assert "up to date" in result.output
