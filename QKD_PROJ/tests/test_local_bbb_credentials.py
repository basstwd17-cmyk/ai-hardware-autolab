"""No real credential-store or device access in this suite."""
import ctypes
import json
import os

import pytest

from QKD_PROJ.hwtools import local_credentials as vault
from QKD_PROJ.hwtools import ssh


ENDPOINT = {"host": "bbb-test", "user": "test-user", "port": 2222}
BOARD = {"bbb": {"access": ENDPOINT}}
SECRET = "fake-test-secret-not-a-device-password"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for key in ("BB_HOST", "BB_USER", "BB_PORT", "BB_PW"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(vault, "_api", lambda: pytest.fail("unexpected real credential-store access"))


def test_saved_credential_used_without_environment_and_never_in_description(monkeypatch):
    seen = []
    monkeypatch.setattr(vault, "read_password", lambda endpoint: seen.append(endpoint) or SECRET)
    target = ssh.SshTarget.from_env(board_map=BOARD, expected=ENDPOINT)
    assert target.password == SECRET
    assert seen == [ENDPOINT]
    assert target.describe() == ENDPOINT
    assert SECRET not in repr(target) + json.dumps(target.describe())


def test_explicit_environment_wins_and_empty_explicit_mapping_is_hermetic(monkeypatch):
    monkeypatch.setattr(vault, "read_password", lambda endpoint: pytest.fail("must not read OS store"))
    monkeypatch.setenv("BB_PW", SECRET)
    assert ssh.SshTarget.from_env(board_map=BOARD).password == SECRET
    with pytest.raises(ssh.SshError, match="authentication BB_PW"):
        ssh.SshTarget.from_env({}, board_map=BOARD)


def test_target_mismatch_is_rejected_before_credential_lookup(monkeypatch):
    monkeypatch.setattr(vault, "read_password", lambda endpoint: pytest.fail("must validate target first"))
    with pytest.raises(ssh.SshError, match="differs from approved"):
        ssh.SshTarget.from_env(board_map=BOARD, expected={**ENDPOINT, "host": "other-board"})


def test_vault_failure_is_not_misreported_as_missing_password(monkeypatch):
    def denied(endpoint):
        raise vault.CredentialError("Windows credential lookup failed (error 5); no device contacted.")
    monkeypatch.setattr(vault, "read_password", denied)
    with pytest.raises(ssh.SshError, match="error 5"):
        ssh.SshTarget.from_env(board_map=BOARD)


def test_keys_separate_host_user_case_port_and_delimiters():
    variants = [ENDPOINT, {**ENDPOINT, "host": "other"}, {**ENDPOINT, "user": "TEST-USER"},
                {**ENDPOINT, "port": 22}, {**ENDPOINT, "host": "a:b", "user": "c"},
                {**ENDPOINT, "host": "a", "user": "b:c"}]
    assert len({vault.credential_target(v) for v in variants}) == len(variants)
    with pytest.raises(vault.CredentialError):
        vault.credential_target({**ENDPOINT, "port": True})


@pytest.mark.parametrize("profile,tool,forwarded", [
    ("hardware", "run_bbb", True), ("hardware", "deploy_pru", True),
    ("hardware", "measure_scope", False), ("hardware", None, False),
    ("offline", None, False),
])
def test_mcp_forwards_names_only_for_bbb_device_turns(monkeypatch, profile, tool, forwarded):
    from QKD_PROJ.orchestrator.codex_runner import CodexRunnerConfig, build_app_server_command
    monkeypatch.setenv("BB_PW", SECRET)
    config = CodexRunnerConfig(profile=profile, codex_command=("codex",), hardware_tool=tool)
    command = build_app_server_command(config, role="builder")
    names = json.loads(next(v for v in command if ".env_vars=" in v).split("=", 1)[1])
    assert names == (["BB_HOST", "BB_USER", "BB_PORT", "BB_PW"] if forwarded else [])
    assert SECRET not in " ".join(command)


@pytest.fixture
def api(monkeypatch):
    if os.name != "nt":
        pytest.skip("Win32 struct/API contract")

    class FakeApi:
        error = 1168
        saved = None
        freed = 0
        fail_write = False

        def CredWriteW(self, pointer, flags):
            if self.fail_write:
                return 0
            item = ctypes.cast(pointer, ctypes.POINTER(vault._Credential)).contents
            self.saved = (item.TargetName, item.UserName,
                          ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize), item.Persist)
            self.write_buffer = item.CredentialBlob
            return 1

        def CredReadW(self, target, kind, flags, output):
            if self.saved is None or target != self.saved[0]:
                return 0
            _, user, raw, persist = self.saved
            self.blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            self.item = vault._Credential(UserName=user, CredentialBlob=self.blob,
                                          CredentialBlobSize=len(raw), Persist=persist)
            ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(vault._Credential)))[0] = ctypes.pointer(self.item)
            return 1

        def CredFree(self, pointer):
            self.freed += 1

    fake = FakeApi()
    monkeypatch.setattr(vault, "_api", lambda: fake)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: fake.error)
    return fake


def test_wincred_roundtrip_endpoint_binding_and_local_persistence(api):
    vault.store_password(ENDPOINT, SECRET)
    assert api.saved[3] == 2
    assert vault.read_password(ENDPOINT) == SECRET
    assert api.freed == 1
    assert vault.read_password({**ENDPOINT, "host": "another"}) is None


def test_wincred_missing_is_distinct_from_access_error(api):
    assert vault.read_password(ENDPOINT) is None
    api.error = 5
    with pytest.raises(vault.CredentialError, match="error 5"):
        vault.read_password(ENDPOINT)
    api.fail_write = True
    with pytest.raises(vault.CredentialError, match="error 5"):
        vault.store_password(ENDPOINT, SECRET)


def test_wincred_invalid_metadata_frees_buffer_and_does_not_leak(api):
    vault.store_password(ENDPOINT, SECRET)
    target, _, raw, persist = api.saved
    api.saved = (target, "wrong-user", raw, persist)
    with pytest.raises(vault.CredentialError, match="metadata") as caught:
        vault.read_password(ENDPOINT)
    assert SECRET not in str(caught.value)
    assert api.freed == 1


@pytest.mark.parametrize("password", ["", "   ", "x" * 2561])
def test_invalid_password_never_reaches_vault(api, password):
    with pytest.raises(vault.CredentialError):
        vault.store_password(ENDPOINT, password)
    assert api.saved is None
