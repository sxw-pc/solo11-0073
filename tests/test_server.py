import http.client
import json
import threading

import pytest

from splitguard.cli import main
from splitguard.models import InputError
from splitguard.server import create_server, load_runs, validate_host


@pytest.fixture()
def running_server(tmp_path):
    workdir = tmp_path / "work"
    assert main(["demo", "--workdir", str(workdir)]) == 0
    runs = load_runs(workdir)
    server = create_server(workdir, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], runs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def test_invalid_host_is_rejected() -> None:
    for host in ("0.0.0.0", "example.invalid", "192.0.2.10"):
        with pytest.raises(InputError) as error:
            validate_host(host)
        assert error.value.code == "SERVER_INVALID_HOST"


def test_health_index_detail_api_and_artifact(running_server) -> None:
    port, runs = running_server
    leaky_id = next(
        run_id
        for run_id, registered in runs.items()
        if registered.report["conclusion"] == "block"
    )
    status, headers, body = _request(port, "GET", "/healthz")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert headers.get("Content-Security-Policy")

    status, _, index = _request(port, "GET", "/")
    assert status == 200
    assert leaky_id.encode() in index
    assert b"http://" not in index and b"https://" not in index

    status, _, detail = _request(port, "GET", f"/run/{leaky_id}")
    assert status == 200
    assert b"EXACT_DUPLICATE_CROSS_SPLIT" in detail

    status, _, api = _request(port, "GET", f"/api/run/{leaky_id}")
    assert status == 200
    report = json.loads(api)
    assert report["run_id"] == leaky_id
    assert report["conclusion"] == "block"

    status, headers, artifact = _request(
        port, "GET", f"/artifact/{leaky_id}/report.md"
    )
    assert status == 200
    assert b"# \xe5\x88\x87\xe5\x88\x86\xe9\x98\xb2\xe6\xbc\x8f\xe5\x8f\xb0" in artifact
    assert "attachment" in headers.get("Content-Disposition", "")


def test_not_found_and_read_only(running_server) -> None:
    port, runs = running_server
    run_id = sorted(runs)[0]
    assert _request(port, "GET", "/run/does-not-exist")[0] == 404
    assert _request(port, "GET", f"/api/run/{run_id}")[0] == 200
    assert _request(port, "GET", f"/api/run/{run_id}/extra")[0] == 404
    status, headers, body = _request(
        port, "POST", f"/run/{run_id}"
    )
    assert status == 405
    assert headers.get("Allow") == "GET"
    assert b"read-only" in body or b"\xe5\x8f\xaa\xe8\xaf\xbb" in body
