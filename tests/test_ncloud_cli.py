"""조회 전용 차단 테스트. 실제 NCP 는 호출하지 않는다 (subprocess 를 mock)."""
import subprocess
from unittest import mock

import pytest

from src import ncloud_cli

BLOCKED = [
    "createServerInstances",
    "terminateServerInstances",
    "stopServerInstances",
    "startServerInstances",
    "rebootServerInstances",
    "createMemberServerImageInstance",
    "deleteMemberServerImageInstances",
    "createBlockStorageInstance",
    "deleteBlockStorageInstances",
    "attachBlockStorageInstance",
    "createPublicIpInstance",
    "deletePublicIpInstance",
    "disassociatePublicIpFromServerInstance",
    "changeServerInstanceSpec",
    "GETSERVERINSTANCELIST",      # 대소문자 변형
    "getServerInstanceList ",     # 공백
    "getServerInstanceList;rm",   # 주입 시도
    "",
    None,
]


@pytest.mark.parametrize("api", BLOCKED)
def test_blocked_api_raises_and_never_executes(api):
    with mock.patch.object(subprocess, "run") as run:
        with pytest.raises(PermissionError):
            ncloud_cli.call(api)
        run.assert_not_called()


def test_whitelist_is_read_only():
    assert all(name.startswith("get") for name in ncloud_cli.ALLOWED_APIS)


def test_allowed_api_executes_without_shell():
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=b'{"ok": 1}', stderr=b"")
    with mock.patch.object(subprocess, "run", return_value=fake) as run:
        assert ncloud_cli.call("getRegionList") == {"ok": 1}
    _, kwargs = run.call_args
    assert kwargs["shell"] is False
    cmd = run.call_args[0][0]
    assert "getRegionList" in cmd and "vserver" in cmd


@pytest.mark.parametrize("value", ["--foo", "a b", "x;del", "$(x)"])
def test_bad_param_value_rejected(value):
    with mock.patch.object(subprocess, "run") as run:
        with pytest.raises(ValueError):
            ncloud_cli.call("getServerInstanceList", serverName=value)
        run.assert_not_called()
