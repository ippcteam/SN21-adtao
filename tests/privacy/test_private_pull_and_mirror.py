"""Private pulls, the manifest check, the release mirror and the daily step
(docs/SN21_MODEL_PRIVACY.md; Sir Rob 16 Oct: the release is the point)."""
import base64
import hashlib
import io
import json
import urllib.error
from datetime import date, timedelta

import pytest

from hope.backtest import oci_pull as O
from hope.privacy import mirror as M
from hope.privacy import release_step as S

CREDS = ("sn21-puller", "s3cret")
BASIC = "Basic " + base64.b64encode(b"sn21-puller:s3cret").decode()


# ---- which account is used where ------------------------------------------------

def test_credentials_come_only_from_the_setting_for_that_host():
    env = {"SN21_PULL_AUTH": json.dumps({"ghcr.io": {"username": "u", "password": "p"},
                                          "docker.io": {"username": "d", "password": "q"}})}
    assert O.credentials_for("ghcr.io", env) == ("u", "p")
    assert O.credentials_for("registry-1.docker.io", env) == ("d", "q")     # alias
    assert O.credentials_for("quay.io", env) is None
    assert O.credentials_for("ghcr.io", {}) is None
    assert O.credentials_for("ghcr.io", {"SN21_PULL_AUTH": "not json"}) is None
    assert O.credentials_for("ghcr.io", {"SN21_PULL_AUTH": '{"ghcr.io": {"username": "u"}}'}) is None


class _Resp(io.BytesIO):
    def __init__(self, body=b"", headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _token_server(monkeypatch, seen):
    def fake(url, headers, timeout=120):
        seen.append((url, dict(headers)))
        return _Resp(json.dumps({"token": "t"}).encode())
    monkeypatch.setattr(O, "_http_get", fake)


def test_our_account_goes_to_the_registrys_own_token_service(monkeypatch):
    seen = []
    _token_server(monkeypatch, seen)
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:x/m:pull"'
    assert O._bearer_token("ghcr.io", "x/m", challenge, CREDS) == "t"
    assert seen[0][1].get("Authorization") == BASIC


def test_docker_hub_token_service_is_trusted_for_docker_hub_only(monkeypatch):
    seen = []
    _token_server(monkeypatch, seen)
    hub = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
    O._bearer_token("registry-1.docker.io", "x/m", hub, CREDS)
    O._bearer_token("ghcr.io", "x/m", hub, CREDS)
    assert seen[0][1].get("Authorization") == BASIC
    assert "Authorization" not in seen[1][1]


def test_a_realm_on_another_host_never_gets_our_account(monkeypatch):
    seen = []
    _token_server(monkeypatch, seen)
    evil = 'Bearer realm="https://collector.example/token",service="ghcr.io"'
    O._bearer_token("ghcr.io", "x/m", evil, CREDS)
    assert "Authorization" not in seen[0][1]


def test_without_an_account_the_pull_stays_anonymous(monkeypatch):
    seen = []
    _token_server(monkeypatch, seen)
    O._bearer_token("ghcr.io", "x/m", 'Bearer realm="https://ghcr.io/token"', None)
    assert "Authorization" not in seen[0][1]


def test_push_scope_is_asked_for_by_the_mirror(monkeypatch):
    seen = []
    _token_server(monkeypatch, seen)
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:o/r:pull"'
    O._bearer_token("ghcr.io", "o/r", challenge, CREDS, actions="pull,push")
    assert "scope=repository%3Ao/r%3Apull%2Cpush" in seen[0][0]


# ---- a registry in memory ----------------------------------------------------------

class FakeRegistry:
    """Enough of the registry v2 API for pull and push, with an account check."""

    def __init__(self, host, account=None):
        self.host, self.account = host, account
        self.blobs, self.manifests, self.uploads = {}, {}, 0
        self.tamper = None

    def put_image(self, repo, layers, index=False):
        cfg = json.dumps({"config": {"Cmd": ["/run"]}}).encode()
        blobs = [cfg] + layers
        for b in blobs:
            self.blobs[(repo, "sha256:" + hashlib.sha256(b).hexdigest())] = b
        man = json.dumps({"schemaVersion": 2,
                          "mediaType": "application/vnd.oci.image.manifest.v1+json",
                          "config": {"digest": "sha256:" + hashlib.sha256(cfg).hexdigest()},
                          "layers": [{"digest": "sha256:" + hashlib.sha256(b).hexdigest()}
                                     for b in layers]}, sort_keys=True).encode()
        md = "sha256:" + hashlib.sha256(man).hexdigest()
        self.manifests[(repo, md)] = (man, "application/vnd.oci.image.manifest.v1+json")
        if not index:
            return md
        idx = json.dumps({"schemaVersion": 2,
                          "mediaType": "application/vnd.oci.image.index.v1+json",
                          "manifests": [{"digest": md, "platform": {"os": "linux",
                                                                     "architecture": "amd64"}}]},
                         sort_keys=True).encode()
        idig = "sha256:" + hashlib.sha256(idx).hexdigest()
        self.manifests[(repo, idig)] = (idx, "application/vnd.oci.image.index.v1+json")
        return idig

    def handle(self, method, url, headers, body):
        path = url.split(self.host, 1)[1].split("?")[0]
        if self.account and headers.get("Authorization") != self.account:
            raise urllib.error.HTTPError(url, 401, "auth", {"WWW-Authenticate": 'Basic realm="r"'}, io.BytesIO())
        repo, _, rest = path[len("/v2/"):].partition("/manifests/") if "/manifests/" in path \
            else path[len("/v2/"):].partition("/blobs/")
        if "/manifests/" in path:
            if method == "PUT":
                self.manifests[(repo, rest)] = (body, headers.get("Content-Type"))
                return _Resp(status=201)
            if (repo, rest) not in self.manifests:
                raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO())
            data, mt = self.manifests[(repo, rest)]
            if self.tamper:
                data = self.tamper(data)
            return _Resp(b"" if method == "HEAD" else data, {"Content-Type": mt})
        if rest == "uploads/" and method == "POST":
            self.uploads += 1
            return _Resp(status=202, headers={"Location": f"/v2/{repo}/blobs/uploads/u{self.uploads}"})
        if rest.startswith("uploads/") and method == "PUT":
            digest = url.split("digest=")[1].replace("%3A", ":")
            data = body.read() if hasattr(body, "read") else body
            assert "sha256:" + hashlib.sha256(data).hexdigest() == digest
            self.blobs[(repo, digest)] = data
            return _Resp(status=201)
        if (repo, rest) not in self.blobs:
            raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO())
        return _Resp(b"" if method == "HEAD" else self.blobs[(repo, rest)])


@pytest.fixture
def net(monkeypatch):
    regs = {}

    def route(url, method, headers, body=None):
        host = url.split("://", 1)[1].split("/", 1)[0]
        return regs[host].handle(method, url, headers, body)

    monkeypatch.setattr(O, "_http_get", lambda url, headers, timeout=120: route(url, "GET", headers))

    def urlopen(req, timeout=None):
        return route(req.full_url, req.get_method(), dict(req.header_items()), req.data)
    monkeypatch.setattr(O.urllib.request, "urlopen", urlopen)
    return regs


def test_a_private_image_pulls_with_our_account_and_not_without(net, tmp_path, monkeypatch):
    reg = net["ghcr.io"] = FakeRegistry("ghcr.io", account=BASIC)
    dg = reg.put_image("miner/m", [b"layer"])
    reg.blobs.pop(next(k for k in reg.blobs if reg.blobs[k] == b"layer"))   # config only needed here
    monkeypatch.delenv("SN21_PULL_AUTH", raising=False)
    with pytest.raises(O.PullError):
        O._Registry("ghcr.io", "miner/m").manifest_bytes(dg)
    monkeypatch.setenv("SN21_PULL_AUTH", json.dumps({"ghcr.io": {"username": CREDS[0], "password": CREDS[1]}}))
    body, _ = O._Registry("ghcr.io", "miner/m").manifest_bytes(dg)
    assert "sha256:" + hashlib.sha256(body).hexdigest() == dg


def test_a_manifest_that_does_not_hash_to_the_committed_digest_is_refused(net):
    reg = net["ghcr.io"] = FakeRegistry("ghcr.io")
    dg = reg.put_image("miner/m", [b"layer"])
    reg.tamper = lambda b: b.replace(b'"schemaVersion": 2', b'"schemaVersion":  2')
    with pytest.raises(O.PullError, match="manifest digest mismatch"):
        O._Registry("ghcr.io", "miner/m").manifest(dg)


@pytest.mark.parametrize("index", [False, True])
def test_the_mirror_copies_the_image_byte_for_byte_under_the_same_digest(net, index):
    src = net["ghcr.io"] = FakeRegistry("ghcr.io")
    dst = net["mirror.example"] = FakeRegistry("mirror.example", account=BASIC)
    dg = src.put_image("miner/m", [b"layer-1", b"layer-2"], index=index)
    res = M.mirror_image("ghcr.io/miner/m", dg, "mirror.example/adtao/released",
                         target_credentials=CREDS)
    assert res["target"] == f"mirror.example/adtao/released@{dg}" and not res["already"]
    assert dst.manifests[("adtao/released", dg)][0] == src.manifests[("miner/m", dg)][0]
    assert ("adtao/released", M.mirror_tag(dg)) in dst.manifests
    assert {d for (_, d) in dst.blobs} == {d for (_, d) in src.blobs}
    again = M.mirror_image("ghcr.io/miner/m", dg, "mirror.example/adtao/released",
                           target_credentials=CREDS)
    assert again["already"] and again["blobs_copied"] == 0


def test_the_mirror_skips_blobs_it_already_holds(net):
    src = net["ghcr.io"] = FakeRegistry("ghcr.io")
    net["mirror.example"] = FakeRegistry("mirror.example")
    a = src.put_image("miner/a", [b"base", b"a-only"])
    b = src.put_image("miner/b", [b"base", b"b-only"])
    M.mirror_image("ghcr.io/miner/a", a, "mirror.example/r")
    res = M.mirror_image("ghcr.io/miner/b", b, "mirror.example/r")
    assert res["blobs_copied"] == 1                  # only b-only: the config and base are there


# ---- the daily step ----------------------------------------------------------------

D0 = date(2026, 11, 2)


def _day(root, day, weights, ran):
    (root / f"intended_weights_{day}.json").write_text(json.dumps({"weights": weights}))
    sh = root / "shadow" / (day - timedelta(days=1)).isoformat()
    sh.mkdir(parents=True, exist_ok=True)
    for hk, ref in ran.items():
        (sh / f"{hk}.jsonl").write_text(json.dumps({"image_digest": ref}) + "\n")


def test_off_until_the_start_date(tmp_path):
    assert "skipped" in S.run_release_step(str(tmp_path), D0, environ={})
    env = {"SN21_MODEL_RELEASE_FROM": (D0 + timedelta(days=1)).isoformat()}
    assert "skipped" in S.run_release_step(str(tmp_path), D0, environ=env)
    assert not (tmp_path / "privacy").exists()


def test_ledger_written_and_each_release_copied_once(tmp_path):
    _day(tmp_path, D0, {"hkA": 1.0}, {"hkA": "ghcr.io/x/a@sha256:aaa"})
    for i in range(1, 6):
        _day(tmp_path, D0 + timedelta(days=i), {"hkB": 1.0}, {"hkB": "ghcr.io/y/b@sha256:bbb"})
    calls = []

    def fake_mirror(src, dg, target, target_credentials=None):
        calls.append((src, dg, target, target_credentials))
        return {"target": f"{target}@{dg}"}

    env = {"SN21_MODEL_RELEASE_FROM": D0.isoformat(),
           "SN21_RELEASE_MIRROR": "ghcr.io/adtao/sn21-released",
           "SN21_RELEASE_MIRROR_AUTH": json.dumps({"username": "u", "password": "p"})}
    last = D0 + timedelta(days=5)
    s = S.run_release_step(str(tmp_path), last, environ=env, mirror_fn=fake_mirror)
    assert s["released_today"] == 1 and s["closed"] == 1 and s["mirror"]["copied"] == 1
    assert calls == [("ghcr.io/x/a", "sha256:aaa", "ghcr.io/adtao/sn21-released", ("u", "p"))]
    ledger = json.loads((tmp_path / "privacy" / "release_ledger.json").read_text())
    assert [(r["digest"], r["reason"]) for r in ledger["releases"]] == [("sha256:aaa", "left_earning_set")]
    assert [c["digest"] for c in ledger["closed"]] == ["sha256:bbb"]
    S.run_release_step(str(tmp_path), last, environ=env, mirror_fn=fake_mirror)
    assert len(calls) == 1                           # already copied: not again


def test_a_failed_copy_is_retried_and_never_stops_the_step(tmp_path):
    _day(tmp_path, D0, {"hkA": 1.0}, {"hkA": "ghcr.io/x/a@sha256:aaa"})
    for i in range(1, 6):
        _day(tmp_path, D0 + timedelta(days=i), {}, {})
    env = {"SN21_MODEL_RELEASE_FROM": D0.isoformat(), "SN21_RELEASE_MIRROR": "ghcr.io/o/r"}

    def broken(*a, **k):
        raise O.PullError("GET -> 404")
    s = S.run_release_step(str(tmp_path), D0 + timedelta(days=5), environ=env, mirror_fn=broken)
    assert s["mirror"]["copied"] == 0 and s["mirror"]["failed"][0]["digest"] == "sha256:aaa"
    s = S.run_release_step(str(tmp_path), D0 + timedelta(days=5), environ=env,
                           mirror_fn=lambda src, dg, t, target_credentials=None: {"target": t})
    assert s["mirror"]["copied"] == 1


def test_ledger_written_without_a_mirror_configured(tmp_path):
    _day(tmp_path, D0, {"hkA": 1.0}, {"hkA": "ghcr.io/x/a@sha256:aaa"})
    s = S.run_release_step(str(tmp_path), D0, environ={"SN21_MODEL_RELEASE_FROM": D0.isoformat()})
    assert s["closed"] == 1 and "not set" in s["mirror"]
