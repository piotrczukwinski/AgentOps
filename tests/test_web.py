from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from agentops import web
from agentops.cli import build_parser
from agentops.state import StateStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "agentops@example.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "AgentOps Test"], check=True, capture_output=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    return repo


def _write_minimal_roadmap(tmp: Path, repo: Path) -> Path:
    prompt = tmp / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    roadmap_path = tmp / "roadmap.json"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roadmap_id": "r",
                "repo": {"id": "x", "path": str(repo)},
                "tasks": [
                    {
                        "id": "T1",
                        "kind": "guard",
                        "prompt": str(prompt),
                        "executor": "shell",
                        "executor_command": "true",
                        "branch_prefix": "agentops",
                        "allowed_files": ["a.txt"],
                        "review": {"codex": "never"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return roadmap_path


class CliServeTests(unittest.TestCase):
    def test_serve_subcommand_in_help(self) -> None:
        parser = build_parser()
        # argparse prints help and exits 0 when --help is passed; the goal of
        # this test is just to confirm the subcommand is wired into the CLI.
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["serve", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        # Also confirm we can parse normal serve args.
        args = parser.parse_args(["serve", "--host", "127.0.0.1", "--port", "9000"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9000)

    def test_serve_help_lists_local_only_default(self) -> None:
        result = subprocess.run(
            ["python", "-m", "agentops", "serve", "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("127.0.0.1", result.stdout)
        self.assertIn("--host", result.stdout)
        self.assertIn("--port", result.stdout)

    def test_make_server_rejects_non_loopback(self) -> None:
        with self.assertRaises(ValueError):
            web.make_server("0.0.0.0", _free_port())

    def test_make_server_rejects_public_ip(self) -> None:
        with self.assertRaises(ValueError):
            web.make_server("8.8.8.8", _free_port())


class WebRenderTests(unittest.TestCase):
    def test_render_index_html_is_non_empty_and_has_anchors(self) -> None:
        html = web.render_index_html()
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("AgentOps Local UI", html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/plan", html)
        self.assertIn("/api/run", html)
        self.assertIn("/api/logs", html)
        self.assertIn("/api/artifacts", html)
        # The page must not contain a generic shell or curl command helper.
        self.assertNotIn("shell", html.lower().split("style")[1].split("</style>")[0])


class WebSafetyTests(unittest.TestCase):
    def test_is_loopback_host(self) -> None:
        self.assertTrue(web.is_loopback_host("127.0.0.1"))
        self.assertTrue(web.is_loopback_host("localhost"))
        self.assertTrue(web.is_loopback_host("::1"))
        self.assertFalse(web.is_loopback_host("0.0.0.0"))
        self.assertFalse(web.is_loopback_host("8.8.8.8"))
        self.assertFalse(web.is_loopback_host("not-a-host"))

    def test_roadmap_allowlist_rejects_etc_passwd(self) -> None:
        with self.assertRaises(web.RoadmapPathError):
            web.validate_roadmap_path("/etc/passwd")

    def test_roadmap_allowlist_rejects_empty(self) -> None:
        with self.assertRaises(web.RoadmapPathError):
            web.validate_roadmap_path("")

    def test_roadmap_allowlist_rejects_traversal_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            # Pretend the AgentOps repo is the temporary directory itself; the
            # plan file lives at the same level, not under it, so the allowlist
            # for repo=/tmp/foo must still reject the sibling path.
            roots = web._AllowedRoots(repo_root=Path(tmp) / "agentops", tmp_root=Path(tmp) / "scratch")
            with self.assertRaises(web.RoadmapPathError):
                web.validate_roadmap_path(str(outside), roots=roots)

    def test_roadmap_allowlist_accepts_repo_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "agentops"
            repo.mkdir()
            (repo / "examples").mkdir()
            (repo / "examples" / "plan.json").write_text("{}", encoding="utf-8")
            roots = web._AllowedRoots(repo_root=repo, tmp_root=root / "scratch")
            resolved = web.validate_roadmap_path("examples/plan.json", roots=roots)
            self.assertEqual(resolved, repo / "examples" / "plan.json")

    def test_roadmap_allowlist_accepts_tmp_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()
            plan = scratch / "plan.json"
            plan.write_text("{}", encoding="utf-8")
            roots = web._AllowedRoots(repo_root=Path(tmp) / "agentops", tmp_root=scratch)
            resolved = web.validate_roadmap_path(str(plan), roots=roots)
            self.assertEqual(resolved, plan)


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.roadmap = _write_minimal_roadmap(self.tmp, self.repo)
        self.db = self.tmp / "state.sqlite"
        self.store = StateStore(self.db)
        self.store.init()
        self.port = _free_port()
        self.server = web.make_server("127.0.0.1", self.port, state=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        # Wait for the server to be ready.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.connect()
                conn.close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start")

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                path,
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def test_status_returns_valid_json_for_empty_state(self) -> None:
        status, data = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["task_count"], 0)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["events"], [])
        self.assertTrue(data["db_path"].endswith("state.sqlite"))

    def test_index_renders_html(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type", ""))
        self.assertIn("AgentOps Local UI", body)

    def test_roadmaps_lists_examples(self) -> None:
        # Use a dedicated server that resolves to the real AgentOps repo so
        # the examples/roadmaps directory is found in the listing.
        real_repo = Path(__file__).resolve().parent.parent
        port = _free_port()
        server = web.make_server("127.0.0.1", port, state=StateStore(self.tmp / "state2.sqlite"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        try:
            with mock.patch.object(web, "_resolve_allowed_roots", return_value=web._AllowedRoots(repo_root=real_repo, tmp_root=self.tmp)):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/api/roadmaps")
                    resp = conn.getresponse()
                    data = json.loads(resp.read().decode("utf-8"))
                finally:
                    conn.close()
            self.assertEqual(resp.status, 200)
            self.assertIsInstance(data.get("roadmaps"), list)
        finally:
            thread.join(timeout=5)

    def test_logs_requires_task_id(self) -> None:
        status, data = self._get("/api/logs")
        self.assertEqual(status, 400)
        self.assertIn("task_id", data["error"])

    def test_artifacts_returns_rows(self) -> None:
        status, data = self._get("/api/artifacts?task_id=T1")
        self.assertEqual(status, 200)
        self.assertEqual(data["task_id"], "T1")
        self.assertIsInstance(data["items"], list)

    def test_plan_endpoint_does_not_create_worktrees(self) -> None:
        with mock.patch("agentops.web.lint_roadmap", wraps=web.lint_roadmap) as spy:
            status, data = self._post("/api/plan", {"roadmap": str(self.roadmap)})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["report"]["ok"])
        spy.assert_called_once()
        # Ensure no worktree was created in the repo.
        listed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertNotIn("agentops", listed)

    def test_plan_endpoint_rejects_outside_roadmap(self) -> None:
        status, data = self._post("/api/plan", {"roadmap": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("outside allowed roots", data["error"])

    def test_run_endpoint_rejects_unsafe_roadmap(self) -> None:
        status, data = self._post("/api/run", {"roadmap": "/etc/passwd", "no_codex": True})
        self.assertEqual(status, 400)
        self.assertFalse(data["started"])
        self.assertIn("outside allowed roots", data["error"])

    def test_run_endpoint_rejects_codex_on(self) -> None:
        status, data = self._post("/api/run", {"roadmap": str(self.roadmap), "no_codex": False})
        self.assertEqual(status, 400)
        self.assertFalse(data["started"])

    def test_run_endpoint_does_not_use_shell(self) -> None:
        # The argv must be a list (no shell) and must not contain any string
        # of "no_codex" except the literal flag. We also assert no codex flag
        # is present in the constructed command.
        argv = web.build_run_command(str(self.roadmap), python_executable="python")
        self.assertIsInstance(argv, list)
        self.assertNotIn("--codex", argv)
        self.assertIn("--no-codex", argv)
        # argv must not contain any user-injected shell metacharacters as a
        # single argument.
        for arg in argv:
            self.assertNotIn("|", arg)
            self.assertNotIn(";", arg)
            self.assertNotIn("&&", arg)
            self.assertNotIn("$(", arg)
        # The first argument must be the python executable, second must be -m.
        self.assertEqual(argv[0], "python")
        self.assertEqual(argv[1], "-m")
        self.assertEqual(argv[2], "agentops")

    def test_run_endpoint_starts_real_subprocess(self) -> None:
        # Use a tiny shell-only roadmap to keep this test fast. The subprocess
        # is detached; we just need to observe that it was launched.
        with mock.patch("agentops.web.subprocess.Popen") as popen:
            popen.return_value = mock.Mock(pid=42424, poll=lambda: None)
            status, data = self._post("/api/run", {"roadmap": str(self.roadmap), "no_codex": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["started"])
        self.assertEqual(data["pid"], 42424)
        self.assertIn("--no-codex", data["argv"])
        # Popen must be called with a list, not a string, and shell=False.
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs.get("shell", False))
        args = popen.call_args.args[0]
        self.assertIsInstance(args, list)

    def test_runs_endpoint_reports_active_run(self) -> None:
        with mock.patch("agentops.web.subprocess.Popen") as popen:
            popen.return_value = mock.Mock(pid=12345, poll=lambda: None)
            self._post("/api/run", {"roadmap": str(self.roadmap), "no_codex": True})
        status, data = self._get("/api/runs")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["pid"] == 12345 for r in data["runs"]))

    def test_health_endpoint(self) -> None:
        status, data = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_unknown_route_returns_404(self) -> None:
        status, data = self._get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("not found", data["error"])

    def test_post_unknown_route_returns_404(self) -> None:
        status, data = self._post("/api/nope", {"x": 1})
        self.assertEqual(status, 404)

    def test_post_invalid_json(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/plan",
                body=b"not json",
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertEqual(resp.status, 400)
        self.assertIn("invalid JSON", data["error"])

    def test_runs_endpoint_empty(self) -> None:
        status, data = self._get("/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(data["runs"], [])

    def test_plan_endpoint_requires_roadmap(self) -> None:
        status, data = self._post("/api/plan", {})
        self.assertEqual(status, 400)
        self.assertIn("roadmap is required", data["error"])

    def test_run_endpoint_requires_roadmap(self) -> None:
        status, data = self._post("/api/run", {"no_codex": True})
        self.assertEqual(status, 400)
        self.assertIn("roadmap is required", data["error"])

    def test_index_html_does_not_contain_shell_endpoint(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type", ""))
        # The dashboard must not advertise a generic shell/exec endpoint.
        for forbidden in ("/api/exec", "/api/shell", "/api/command", "/api/run_command"):
            self.assertNotIn(forbidden, body)
        # It must reference the safe endpoints actually used by the
        # dashboard JavaScript so the static contract is locked in.
        for required in (
            "/api/status",
            "/api/roadmaps",
            "/api/plan",
            "/api/run",
            "/api/logs",
            "/api/artifacts",
            "/api/runs",
        ):
            self.assertIn(required, body)
        # The dashboard must never call the unsafe /codex/... endpoints
        # (operator can still run with codex via the CLI).
        self.assertNotIn("/api/codex", body)


class WebApiMissingStateDbTests(unittest.TestCase):
    """Lock down behavior when the state DB does not exist on disk yet.

    The web UI must still return valid JSON for status/health so a fresh
    checkout can boot the dashboard before the first roadmap run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # Intentionally do NOT create the SQLite file. StateStore is created
        # but never .init()'d in setUp; the request handler is expected to
        # call init() and create the schema on first request.
        self.db = self.tmp / "state.sqlite"
        self.store = StateStore(self.db)
        self.port = _free_port()
        self.server = web.make_server("127.0.0.1", self.port, state=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.connect()
                conn.close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start")

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def test_status_creates_db_and_returns_json(self) -> None:
        self.assertFalse(self.db.exists(), "state DB must not exist before first request")
        status, data = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["task_count"], 0)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["events"], [])
        # The handler should have created the DB as a side effect.
        self.assertTrue(self.db.exists(), "state DB should be created on first /api/status call")

    def test_health_works_without_db(self) -> None:
        status, data = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


class WebEnvSafetyTests(unittest.TestCase):
    def test_safe_subprocess_env_strips_tokens(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "secret",
            "OPENAI_API_KEY": "secret",
            "AGENTOPS_WEB_TOKEN": "secret",
            "USER": "tester",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            safe = web._safe_subprocess_env()
        self.assertNotIn("GITHUB_TOKEN", safe)
        self.assertNotIn("OPENAI_API_KEY", safe)
        self.assertNotIn("AGENTOPS_WEB_TOKEN", safe)
        self.assertEqual(safe["AGENTOPS_NO_CODEX"], "1")
        self.assertEqual(safe["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(safe["GIT_ASKPASS"], "/bin/false")


if __name__ == "__main__":
    unittest.main()



# ---------------------------------------------------------------------------
# Operator-run monitor endpoints (AO-CONTRACT-003)
# ---------------------------------------------------------------------------


def _seed_operator_run(root, run_id, *, combined_log="line1\nline2\nline3\n"):
    run = root / ".operator-runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "combined.log").write_text(combined_log, encoding="utf-8")
    (run / "status.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "name": "demo",
                "status": "exited",
                "exit_code": 0,
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:05+00:00",
                "pid": 0,
            }
        ),
        encoding="utf-8",
    )
    return run


class OperatorRunsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = _init_repo(self.tmp)
        self.roadmap = _write_minimal_roadmap(self.tmp, self.repo)
        self.db = self.tmp / "state.sqlite"
        self.store = StateStore(self.db)
        self.store.init()
        self.port = _free_port()
        self.server = web.make_server("127.0.0.1", self.port, state=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.connect()
                conn.close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start")

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def test_operator_runs_endpoint_empty_when_no_dir(self):
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.tmp / "empty")}, clear=False):
            status, data = self._get("/api/operator-runs")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"runs": []})

    def test_operator_runs_endpoint_lists_fake_run_dirs(self):
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False):
            _seed_operator_run(self.repo, "20260617T000000Z-fake-aaaaaaaa")
            _seed_operator_run(self.repo, "20260617T000100Z-fake-bbbbbbbb")
            status, data = self._get("/api/operator-runs")
        self.assertEqual(status, 200)
        run_ids = sorted(r["run_id"] for r in data["runs"])
        self.assertEqual(run_ids, [
            "20260617T000000Z-fake-aaaaaaaa",
            "20260617T000100Z-fake-bbbbbbbb",
        ])
        sample = data["runs"][0]
        for key in (
            "run_id",
            "name",
            "canonical_status",
            "runtime_status",
            "pid",
            "pid_alive",
            "active_attempt",
            "active_combined_log",
            "log_size_bytes",
            "idle_for_seconds",
            "result_json_present",
            "suggested_action",
        ):
            self.assertIn(key, sample)

    def test_operator_runs_tail_returns_latest_log(self):
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False):
            _seed_operator_run(self.repo, "20260617T000000Z-fake-cccccccc", combined_log="a\nb\nc\nd\n")
            status, data = self._get("/api/operator-runs/20260617T000000Z-fake-cccccccc/tail?lines=2")
        self.assertEqual(status, 200)
        self.assertEqual(data["run_id"], "20260617T000000Z-fake-cccccccc")
        self.assertEqual(data["lines"], 2)
        self.assertIn("c", data["text"])
        self.assertIn("d", data["text"])

    def test_operator_runs_tail_rejects_traversal(self):
        status, _ = self._get("/api/operator-runs/..%2F..%2Fetc%2Fpasswd/tail?lines=10")
        self.assertIn(status, {400, 404})

    def test_operator_runs_tail_unknown_run_returns_404(self):
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False):
            status, data = self._get("/api/operator-runs/20260617T000000Z-unknown/tail?lines=10")
        self.assertEqual(status, 404)
        # The harness's FileNotFoundError message contains the run id;
        # we accept any of "not found" or "no operator run directory".
        self.assertTrue(
            "not found" in data["error"].lower()
            or "no operator run directory" in data["error"].lower(),
            data,
        )

    def test_index_html_loads_with_operator_runs_card(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("Operator runs (monitor)", body)
        self.assertIn("/api/operator-runs", body)
        for forbidden in ("/api/exec", "/api/shell", "/api/command", "/api/run_command"):
            self.assertNotIn(forbidden, body)

    def test_no_shell_endpoint_exposed(self):
        for forbidden in ("/api/exec", "/api/shell", "/api/command"):
            status, data = self._get(forbidden)
            self.assertEqual(status, 404)
            self.assertIn("not found", data["error"].lower())


# ---------------------------------------------------------------------------
# Admin / operator panel (AO-ADMIN-002)
# ---------------------------------------------------------------------------


class AdminPanelHtmlTests(unittest.TestCase):
    """Static-HTML contract for the admin/operator panel.

    The admin card is rendered by :func:`agentops.web.render_index_html`
    and polled by the dashboard JavaScript. The tests below lock down
    the markers a developer would grep for when wiring the section.
    """

    def test_admin_section_present(self) -> None:
        html = web.render_index_html()
        self.assertIn("Admin / Operator panel", html)
        self.assertIn("/api/admin", html)

    def test_admin_section_has_key_subsections(self) -> None:
        html = web.render_index_html()
        for required in (
            "Roadmap state",
            "Latest events",
            "Operator-run status",
            "PR-loop cycles",
            "Watchdog failures",
            "Recommended next commands",
        ):
            self.assertIn(required, html, required)

    def test_admin_section_has_empty_state_anchors(self) -> None:
        # The empty-state rows must exist in the HTML so the JavaScript
        # has a tbody to fall back to when no data is present.
        html = web.render_index_html()
        for row_id in (
            "admin-roadmap-rows",
            "admin-event-rows",
            "admin-operator-rows",
            "admin-pr-loop-rows",
            "admin-watchdog-rows",
            "admin-commands-rows",
        ):
            self.assertIn(f'id="{row_id}"', html, row_id)

    def test_admin_section_renderer_wired_in_js(self) -> None:
        html = web.render_index_html()
        # The dashboard JavaScript must reference the admin renderer and
        # poll the /api/admin endpoint.
        self.assertIn("/api/admin", html)
        self.assertIn("renderAdmin", html)

    def test_admin_html_omits_unsafe_endpoints(self) -> None:
        html = web.render_index_html()
        for forbidden in ("/api/exec", "/api/shell", "/api/command"):
            self.assertNotIn(forbidden, html)

    def test_admin_html_contains_docs_visible_labels(self) -> None:
        # Smoke-test the docs-visible labels the README and the local
        # web UI docs reference. The JavaScript adds the four
        # recommended commands at runtime, so the static HTML only
        # carries the headings + empty-state strings.
        html = web.render_index_html()
        for label in (
            "Roadmap state",
            "Latest events",
            "Operator-run status",
            "PR-loop cycles",
            "Watchdog failures",
            "Recommended next commands",
        ):
            self.assertIn(label, html, label)
        # The watchdog-failures sub-section must surface its empty
        # state explicitly so a fresh checkout does not look broken.
        self.assertIn("No watchdog failures detected", html)
        # The empty states of the operator-run and pr-loop sub-sections
        # advertise their CLI fallbacks (the docs reference these).
        self.assertIn("agentops operator-run", html)
        self.assertIn("agentops pr-loop", html)


class AdminPanelApiTests(unittest.TestCase):
    """End-to-end tests for :func:`agentops.web.collect_admin_panel`.

    The admin panel is read-only; the tests below exercise the
    function directly (no live HTTP server needed) and confirm the
    payload is shaped the way the dashboard JavaScript expects.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "state.sqlite"
        self.store = StateStore(self.db)
        self.store.init()

    def test_admin_panel_empty_state_is_well_formed(self) -> None:
        # Isolate both the operator-runs root and the pr-loop root so the
        # "empty state" assertions do not pick up the on-disk state of
        # the actual repo.
        empty_runs_root = self.tmp / "empty-operator-runs"
        empty_pr_root = self.tmp / "empty-pr-loop"
        with mock.patch.dict(
            os.environ,
            {"AGENTOPS_OPERATOR_RUNS_ROOT": str(empty_runs_root)},
            clear=False,
        ), mock.patch.object(
            web, "_default_pr_loop_root", return_value=empty_pr_root
        ):
            panel = web.collect_admin_panel(self.store)
        # Top-level keys.
        for key in (
            "roadmap_state",
            "latest_events",
            "operator_runs",
            "pr_loop_cycles",
            "watchdog_failures",
            "recommended_commands",
        ):
            self.assertIn(key, panel, key)
        # Roadmap state on a fresh DB.
        self.assertEqual(panel["roadmap_state"]["roadmaps"], [])
        self.assertEqual(panel["roadmap_state"]["total_tasks"], 0)
        self.assertEqual(panel["roadmap_state"]["roadmap_count"], 0)
        # Latest events.
        self.assertEqual(panel["latest_events"], [])
        # Operator runs / watchdog failures.
        self.assertEqual(panel["operator_runs"]["summary"]["total"], 0)
        self.assertEqual(panel["operator_runs"]["summary"]["running"], 0)
        self.assertEqual(panel["operator_runs"]["recent"], [])
        self.assertEqual(panel["watchdog_failures"]["count"], 0)
        self.assertEqual(panel["watchdog_failures"]["items"], [])
        # PR-loop cycles.
        self.assertFalse(panel["pr_loop_cycles"]["exists"])
        self.assertEqual(panel["pr_loop_cycles"]["cycles"], [])
        self.assertEqual(panel["pr_loop_cycles"]["next_cycle"], 1)
        # Recommended commands must list all four CLI hints.
        names = [cmd["name"] for cmd in panel["recommended_commands"]]
        self.assertIn("agentops operator-status", names)
        self.assertIn("agentops operator-tail", names)
        self.assertIn("agentops task-tail", names)
        self.assertIn("agentops pr-loop", names)

    def test_admin_panel_rolls_up_roadmap_state(self) -> None:
        from agentops.models import RepoConfig, TaskConfig, TaskState
        from agentops.plan import RoadmapConfig

        repo = self.tmp / "repo"
        repo.mkdir()
        prompt = self.tmp / "prompt.md"
        prompt.write_text("hi", encoding="utf-8")
        tasks = (
            TaskConfig(id="T-1", kind="guard", prompt_path=prompt),
            TaskConfig(id="T-2", kind="guard", prompt_path=prompt),
        )
        roadmap = RoadmapConfig(
            version=1,
            roadmap_id="R1",
            repo=RepoConfig(id="x", path=repo),
            tasks=tasks,
        )
        self.store.import_roadmap(roadmap)
        self.store.transition_task("R1", "T-1", TaskState.ACCEPTED)
        self.store.transition_task("R1", "T-2", TaskState.FAILED)

        panel = web.collect_admin_panel(self.store)
        self.assertEqual(panel["roadmap_state"]["roadmap_count"], 1)
        self.assertEqual(panel["roadmap_state"]["total_tasks"], 2)
        entry = panel["roadmap_state"]["roadmaps"][0]
        self.assertEqual(entry["roadmap_id"], "R1")
        self.assertEqual(entry["total"], 2)
        # The roll-up must report both states.
        self.assertIn("accepted", entry["by_state"])
        self.assertIn("failed", entry["by_state"])

    def test_admin_panel_collects_pr_loop_cycles(self) -> None:
        # When the .agentops/pr-loop/cycle-N layout exists on disk, the
        # panel must surface the cycle numbers without spawning any
        # process.
        root = self.tmp / ".agentops" / "pr-loop"
        (root / "cycle-1").mkdir(parents=True)
        (root / "cycle-2").mkdir(parents=True)
        (root / "not-a-cycle").mkdir()  # must be ignored
        # Re-point the helper at the temporary root for this test.
        with mock.patch.object(web, "_default_pr_loop_root", return_value=root):
            panel = web.collect_admin_panel(self.store)
        cycles = panel["pr_loop_cycles"]
        self.assertTrue(cycles["exists"])
        self.assertEqual(
            [c["cycle"] for c in cycles["cycles"]], [1, 2]
        )
        self.assertEqual(cycles["next_cycle"], 3)

    def test_admin_panel_surfaces_watchdog_failures(self) -> None:
        # A fake operator-run directory with a needs_operator status is
        # classified as a watchdog failure by the admin panel.
        runs_root = self.tmp / "runs-root"
        run_id = "20260617T000000Z-fake-aaaaaaaa"
        run_dir = runs_root / ".operator-runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "name": "demo",
                    "status": "needs_operator",
                    "exit_code": 0,
                    "pid": 0,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {"AGENTOPS_OPERATOR_RUNS_ROOT": str(runs_root)},
            clear=False,
        ):
            panel = web.collect_admin_panel(self.store)
        self.assertEqual(panel["watchdog_failures"]["count"], 1)
        self.assertEqual(panel["watchdog_failures"]["items"][0]["run_id"], run_id)
        self.assertEqual(panel["operator_runs"]["summary"]["total"], 1)

    def test_admin_panel_endpoint_returns_json(self) -> None:
        port = _free_port()
        server = web.make_server("127.0.0.1", port, state=self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("GET", "/api/admin")
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()
            self.assertEqual(resp.status, 200)
            for key in (
                "roadmap_state",
                "latest_events",
                "operator_runs",
                "pr_loop_cycles",
                "watchdog_failures",
                "recommended_commands",
            ):
                self.assertIn(key, data, key)
        finally:
            thread.join(timeout=5)

    def test_admin_panel_does_not_spawn_subprocesses(self) -> None:
        # The admin panel must be cheap to poll. The dashboard JavaScript
        # calls /api/admin every 3 seconds, so any subprocess.Popen call
        # would be a regression.
        with mock.patch("subprocess.Popen") as popen:
            panel = web.collect_admin_panel(self.store)
        popen.assert_not_called()
        self.assertIn("recommended_commands", panel)

    def test_admin_panel_recommended_commands_match_docs(self) -> None:
        # The README and docs/local-web-ui.md list the four CLI
        # command hints surfaced by the panel. The panel's
        # ``recommended_commands`` payload must list exactly these
        # four, in the same order, with the same name field. This
        # locks the docs-to-API contract so a future rename in either
        # place triggers a test failure.
        empty_runs_root = self.tmp / "empty-operator-runs"
        empty_pr_root = self.tmp / "empty-pr-loop"
        with mock.patch.dict(
            os.environ,
            {"AGENTOPS_OPERATOR_RUNS_ROOT": str(empty_runs_root)},
            clear=False,
        ), mock.patch.object(
            web, "_default_pr_loop_root", return_value=empty_pr_root
        ):
            panel = web.collect_admin_panel(self.store)
        names = [cmd["name"] for cmd in panel["recommended_commands"]]
        self.assertEqual(
            names,
            [
                "agentops operator-status",
                "agentops operator-tail",
                "agentops task-tail",
                "agentops pr-loop",
            ],
        )
        # Every command must carry a non-empty description (the docs
        # copy these out as the "What it does" column).
        for cmd in panel["recommended_commands"]:
            self.assertTrue(cmd["description"], cmd)

    def test_admin_panel_watchdog_failure_statuses_match_docs(self) -> None:
        # The docs-visible "Watchdog failures" section must classify a
        # run as a watchdog failure iff its ``runtime_status`` is one
        # of the four canonical names the morning checklist greps for.
        # This test seeds one run per status and asserts each is
        # surfaced exactly once.
        runs_root = self.tmp / "runs-root"
        runs_root.mkdir()
        for status in ("needs_operator", "transient_failed", "stale_pid", "exited_or_stale"):
            run_id = f"20260617T000000Z-{status}-aaaaaaaa"
            run_dir = runs_root / ".operator-runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "name": "demo",
                        "status": status,
                        "exit_code": 0,
                        "pid": 0,
                    }
                ),
                encoding="utf-8",
            )
        with mock.patch.dict(
            os.environ,
            {"AGENTOPS_OPERATOR_RUNS_ROOT": str(runs_root)},
            clear=False,
        ):
            panel = web.collect_admin_panel(self.store)
        self.assertEqual(panel["watchdog_failures"]["count"], 4)
        surfaced = {item["runtime_status"] for item in panel["watchdog_failures"]["items"]}
        self.assertEqual(
            surfaced,
            {"needs_operator", "transient_failed", "stale_pid", "exited_or_stale"},
        )
