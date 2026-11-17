"""Test doubles for lane P2-app (account flow, transport selection, sign-in UI).

The cloud client, TDP discovery and ``LocalStreamSupervisor`` are built by other lanes; these fakes
mirror their contract (docs/CONTRACT_PHASE2.md) so the app lane can be tested on its own:

* :class:`FakeCloud` - ``cloud_factory`` for :class:`AccountManager`; its clients share one
  in-memory "cloud" (accounts, devices, MFA, offline switch) and log every call.
* ``CloudError`` / ``CloudAuthError`` / ``CloudMfaRequired`` - stand-ins with the SAME class names
  as ``tapo_desktop.core.cloud.client`` (AccountManager recognises errors by class name).
* :class:`FakeDiscover` - ``discover`` callable returning configurable :class:`LanDevice` lists.
* :class:`FakeSupervisor` / :class:`SupervisorFactory` - record how TapoCameraService builds
  local (:8800) and RTSP supervisors.
* :class:`FakeControlClient` - minimal control client for the getLastAlarmInfo poller.
* :class:`FakeAccounts` - QObject with AccountManager's signals for pure UI tests.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal

from tapo_desktop.core.errors import TapoConnectionError, TapoError
from tapo_desktop.core.media_engine.supervisor import StreamState
from tapo_desktop.models import AccountInfo, CloudDevice, LanDevice

EMAIL = "owner@example.com"
PASSWORD = "Tp-Link-Secret-9"
NICKNAME = "Owner"
MFA_CODE = "123456"


# ====================================================================== cloud errors
class CloudError(TapoError):
    """Stand-in for tapo_desktop.core.cloud.client.CloudError."""

    def __init__(self, message: str = "cloud error", code: int | str = -1):
        super().__init__(message)
        self.code = code


class CloudAuthError(CloudError):
    """Stand-in for CloudAuthError (wrong email/password, locked)."""


class CloudMfaRequired(CloudError):
    """Stand-in for CloudMfaRequired."""

    def __init__(self, process_id: str, mfa_types: list[int]):
        super().__init__("two-step verification required", -20677)
        self.process_id = process_id
        self.mfa_types = list(mfa_types)


# ====================================================================== cloud
def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def cloud_device(i: int, *, online: bool = True, alias: str | None = None, model: str = "C210") -> CloudDevice:
    return CloudDevice(device_id=f"80220000000000000000000000000000000000{i:02d}",
                       mac=f"a8:42:a1:00:00:{i:02x}", model=model, alias=alias or f"Cloud cam {i}",
                       online=online)


def lan_for(dev: CloudDevice, ip: str, *, by_mac_only: bool = False, http_port: int | None = None) -> LanDevice:
    return LanDevice(ip=ip, mac=dev.mac.replace(":", "-").upper(), model=dev.model,
                     device_type="SMART.IPCAMERA",
                     device_id_md5="" if by_mac_only else md5_hex(dev.device_id), http_port=http_port)


@dataclass
class CloudCall:
    name: str
    args: tuple
    terminal_uuid: str


@dataclass
class FakeCloud:
    accounts: dict[str, str] = field(default_factory=lambda: {EMAIL: PASSWORD})
    devices: list[CloudDevice] = field(default_factory=list)
    mfa_types: list[int] | None = None  # set -> login requires MFA
    offline: bool = False  # every call raises TapoConnectionError
    locked: bool = False
    fail_list: Exception | None = None
    calls: list[CloudCall] = field(default_factory=list)
    uuids: list[str] = field(default_factory=list)
    clients: list = field(default_factory=list)

    def __post_init__(self):
        self._lock = threading.Lock()

    def __call__(self, terminal_uuid: str) -> FakeCloudClient:
        with self._lock:
            self.uuids.append(terminal_uuid)
            client = FakeCloudClient(self, terminal_uuid)
            self.clients.append(client)
            return client

    def record(self, name: str, args: tuple, uuid: str) -> None:
        with self._lock:
            self.calls.append(CloudCall(name, args, uuid))

    def call_names(self) -> list[str]:
        with self._lock:
            return [c.name for c in self.calls]


class FakeCloudClient:
    """TapoCloudClient-like; behaviour comes from the shared :class:`FakeCloud`."""

    def __init__(self, cloud: FakeCloud, terminal_uuid: str):
        self._cloud = cloud
        self._uuid = terminal_uuid
        self._account: AccountInfo | None = None
        self._token: str | None = None
        self._pending: tuple[str, str] | None = None
        self._creds: tuple[str, str] | None = None

    def __repr__(self) -> str:
        return f"FakeCloudClient(uuid={self._uuid!r})"

    def _check_online(self) -> None:
        if self._cloud.offline:
            raise TapoConnectionError("cloud unreachable (fake)")

    def _finish(self, email: str) -> AccountInfo:
        self._account = AccountInfo(email=email, nickname=NICKNAME, account_id="acc-1",
                                    region_url="https://euw1-app-server.example")
        self._token = "tok-" + email
        return self._account

    def login(self, email: str, password: str) -> AccountInfo:
        self._cloud.record("login", (email,), self._uuid)
        self._check_online()
        if self._cloud.locked:
            raise CloudAuthError("account locked", -20661)
        if self._cloud.accounts.get(email) != password:
            raise CloudAuthError("credential mismatch", -20601)
        self._creds = (email, password)
        if self._cloud.mfa_types:
            self._pending = (email, password)
            raise CloudMfaRequired("proc-1", self._cloud.mfa_types)
        return self._finish(email)

    def request_mfa_code(self, mfa_type: int) -> None:
        self._cloud.record("request_mfa_code", (mfa_type,), self._uuid)
        self._check_online()
        if self._pending is None:
            raise CloudError("no MFA in progress")

    def complete_mfa(self, code: str, mfa_type: int) -> AccountInfo:
        self._cloud.record("complete_mfa", (code, mfa_type), self._uuid)
        self._check_online()
        if self._pending is None:
            raise CloudError("no MFA in progress")
        if code != MFA_CODE:
            raise CloudAuthError("wrong verification code", -20676)
        email, _ = self._pending
        self._pending = None
        return self._finish(email)

    def list_cameras(self) -> list[CloudDevice]:
        self._cloud.record("list_cameras", (), self._uuid)
        self._check_online()
        if self._token is None:
            raise CloudAuthError("not signed in", -20651)
        if self._cloud.fail_list is not None:
            raise self._cloud.fail_list
        return list(self._cloud.devices)

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def account(self) -> AccountInfo | None:
        return self._account


class FakeDiscover:
    """``discover()`` stand-in; ``devices`` can be changed between calls."""

    def __init__(self, devices: list[LanDevice] | None = None, *, error: Exception | None = None):
        self.devices = list(devices or [])
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs) -> list[LanDevice]:
        with self._lock:
            self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.devices)


# ====================================================================== stream supervisors
class FakeSupervisor:
    """Records construction + lifecycle; same public surface as (Local)StreamSupervisor."""

    def __init__(self, kind: str, args: tuple, kwargs: dict):
        self.kind = kind
        self.args = args
        self.kwargs = kwargs
        self.state = StreamState.IDLE
        self.pid = None
        self.stats = {}
        self.started = 0
        self.stopped = 0
        self.switches: list[tuple] = []

    def start(self) -> None:
        self.started += 1
        self.state = StreamState.CONNECTING

    def stop(self) -> None:
        self.stopped += 1
        self.state = StreamState.STOPPED

    def switch(self, *args) -> None:
        self.switches.append(args)


class SupervisorFactory:
    """Callable used as ``local_stream_factory`` / ``stream_factory`` of TapoCameraService."""

    def __init__(self, kind: str):
        self.kind = kind
        self.created: list[FakeSupervisor] = []

    def __call__(self, *args, **kwargs) -> FakeSupervisor:
        sup = FakeSupervisor(self.kind, args, kwargs)
        self.created.append(sup)
        return sup

    @property
    def last(self) -> FakeSupervisor:
        assert self.created, f"no {self.kind} supervisor was created"
        return self.created[-1]


class FakeControlClient:
    """Answers getLastAlarmInfo from a scripted list of (time, type) values (last one repeats)."""

    def __init__(self, alarms: list[tuple[str, str]] | None = None):
        self.alarms = list(alarms or [("0", "")])
        self.requests: list[dict] = []
        self.logged_in = True
        self.closed = False
        self._lock = threading.Lock()

    def login(self) -> None:
        self.logged_in = True

    def request(self, req: dict) -> dict:
        with self._lock:
            self.requests.append(req)
            index = min(len(self.requests) - 1, len(self.alarms) - 1)
            when, kind = self.alarms[index]
        assert req.get("method") == "getLastAlarmInfo", req
        return {"system": {"last_alarm_info": {"last_alarm_time": when, "last_alarm_type": kind}}}

    def alarm_polls(self) -> int:
        with self._lock:
            return len(self.requests)

    def close(self) -> None:
        self.closed = True


# ====================================================================== UI
class FakeAccounts(QObject):
    """AccountManager signal surface; records calls (for SignInDialog tests)."""

    signed_in = pyqtSignal(object)
    signed_out = pyqtSignal()
    mfa_required = pyqtSignal(object)
    sign_in_failed = pyqtSignal(str)
    cameras_refreshed = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self._account: AccountInfo | None = None

    def account(self) -> AccountInfo | None:
        return self._account

    def sign_in(self, email: str, password: str, remember: bool = True) -> None:
        self.calls.append(("sign_in", email, password, remember))

    def submit_mfa(self, code: str, mfa_type: int) -> None:
        self.calls.append(("submit_mfa", code, mfa_type))

    def request_mfa_code(self, mfa_type: int) -> None:
        self.calls.append(("request_mfa_code", mfa_type))

    def refresh_cameras(self) -> None:
        self.calls.append(("refresh_cameras",))

    def sign_out(self) -> None:
        self.calls.append(("sign_out",))

    def restore_session(self) -> bool:
        self.calls.append(("restore_session",))
        return False

    def wait_idle(self, timeout_ms: int = 5000) -> bool:
        return True
