from __future__ import annotations

import io
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
from typing import Any
from unittest import mock

from agentops import bundles, web
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


class FrontendBundlesTests(unittest.TestCase):
    """T6 frontend contracts: bundles + run-launcher flags render into HTML.

    These are pure HTML-string assertions (no browser); they match the
    existing :class:`WebRenderTests` pattern.
    """

    def test_render_has_bundle_and_run_anchors(self) -> None:
        html = web.render_index_html()
        # New anchors required for the Bundles page and Run Launcher.
        self.assertIn("/api/bundles", html)
        self.assertIn("/api/runs", html)
        self.assertIn("bundle-upload-btn", html)
        self.assertIn("bundle-validate-btn", html)
        self.assertIn("Run with Codex review", html)
        self.assertIn("run-autonomous", html)
        # The title and legacy endpoints must still be present.
        self.assertIn("AgentOps Local UI", html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/plan", html)
        self.assertIn("/api/run", html)

    def test_render_has_operator_runs_stream_anchor(self) -> None:
        html = web.render_index_html()
        # T7 will wire a real live stream button; for T6 we only require the
        # word "stream" to be reachable in the rendered template so the
        # follow-up task can replace the placeholder with a real button.
        self.assertIn("stream", html)


class FrontendMonitorHistoryTests(unittest.TestCase):
    """T7 frontend contracts: Monitor (live SSE) + History browser render.

    These are pure HTML-string assertions (no browser) following the same
    pattern as :class:`WebRenderTests` and :class:`FrontendBundlesTests`.
    """

    def test_render_has_monitor_and_history_anchors(self) -> None:
        html = web.render_index_html()
        # New anchors required for the Monitor (live SSE) + History sections.
        self.assertIn("operator-run-select", html)
        self.assertIn("monitor-start-btn", html)
        self.assertIn("EventSource", html)
        self.assertIn("history-rows", html)
        self.assertIn("log-view-btn", html)
        self.assertIn("/api/run-history", html)
        self.assertIn("/api/run-logs", html)

    def test_render_keeps_legacy_anchors(self) -> None:
        # T7 regression guard: every anchor added by T1..T6 must still
        # render so the page is forward-compatible with the merged stack.
        html = web.render_index_html()
        self.assertIn("AgentOps Local UI", html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/plan", html)
        self.assertIn("/api/run", html)
        self.assertIn("/api/bundles", html)

    def test_render_escapes_javascript_newlines(self) -> None:
        # Python triple-quoted templates must emit JS "\\n" escapes, not raw
        # newline characters inside string literals, otherwise the whole
        # dashboard script fails to parse and no buttons are wired.
        html = web.render_index_html()
        self.assertIn('+ "\\n";', html)
        self.assertIn('[truncated, showing tail]\\n', html)
        self.assertNotIn('+ "\n";', html)
        self.assertNotIn('[truncated, showing tail]\n', html)


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

    def test_run_endpoint_allows_codex_review(self) -> None:
        fake_proc = mock.Mock(pid=1234)
        with mock.patch("agentops.web.subprocess.Popen", return_value=fake_proc) as popen:
            status, data = self._post(
                "/api/run",
                {"roadmap": str(self.roadmap), "no_codex": False, "reviewer": "codex"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["started"])
        argv = popen.call_args.args[0]
        db_idx = argv.index("--db")
        self.assertEqual(Path(argv[db_idx + 1]), self.db.resolve())
        self.assertNotIn("--no-codex", argv)
        self.assertIn("--reviewer", argv)
        self.assertEqual(argv[argv.index("--reviewer") + 1], "codex")

    def test_run_endpoint_does_not_use_shell(self) -> None:
        # The argv must be a list (no shell) and can enable Codex review by
        # omitting --no-codex unless the caller explicitly asks for it.
        argv = web.build_run_command(str(self.roadmap), python_executable="python")
        self.assertIsInstance(argv, list)
        self.assertNotIn("--codex", argv)
        self.assertNotIn("--no-codex", argv)
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
            safe = web._safe_subprocess_env(no_codex=True)
        self.assertNotIn("GITHUB_TOKEN", safe)
        self.assertNotIn("OPENAI_API_KEY", safe)
        self.assertNotIn("AGENTOPS_WEB_TOKEN", safe)
        self.assertEqual(safe["AGENTOPS_NO_CODEX"], "1")
        self.assertEqual(safe["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(safe["GIT_ASKPASS"], "/bin/false")

    def test_safe_subprocess_env_allows_model_keys_for_codex(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "secret",
            "OPENAI_API_KEY": "model-secret",
            "ANTHROPIC_API_KEY": "model-secret",
            "AGENTOPS_NO_CODEX": "1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            safe = web._safe_subprocess_env(no_codex=False)
        self.assertNotIn("GITHUB_TOKEN", safe)
        self.assertEqual(safe["OPENAI_API_KEY"], "model-secret")
        self.assertEqual(safe["ANTHROPIC_API_KEY"], "model-secret")
        self.assertNotIn("AGENTOPS_NO_CODEX", safe)


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
            "status",
            "canonical_status",
            "runtime_status",
            "runtime_status_alias",
            "runtime_status_note",
            "pid",
            "pid_alive",
            "active_attempt",
            "active_combined_log",
            "log_size_bytes",
            "idle_for_seconds",
            "failure_category",
            "result_json_present",
            "suggested_action",
        ):
            self.assertIn(key, sample)

    def test_operator_runs_endpoint_surfaces_stale_pid_overlay(self):
        # AO-AUDIT C9: a run whose persisted status.json says "running"
        # but whose pid is gone must surface as stale_pid in the web UI
        # without the operator having to run `operator-status --reconcile`.
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False):
            run = self.repo / ".operator-runs" / "20260617T000200Z-stale-deadbeef"
            run.mkdir(parents=True, exist_ok=True)
            (run / "combined.log").write_text("line\n", encoding="utf-8")
            (run / "status.json").write_text(
                json.dumps(
                    {
                        "run_id": "20260617T000200Z-stale-deadbeef",
                        "name": "stale",
                        "status": "running",
                        "pid": 0,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "failure_category": "stale_pid",
                    }
                ),
                encoding="utf-8",
            )
            status, data = self._get("/api/operator-runs")
        self.assertEqual(status, 200)
        row = next(r for r in data["runs"] if r["run_id"] == "20260617T000200Z-stale-deadbeef")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["canonical_status"], "running")
        self.assertEqual(row["runtime_status"], "stale_pid")
        self.assertEqual(row["runtime_status_alias"], "exited")
        self.assertIn("pid not alive", row["runtime_status_note"] or "")
        self.assertFalse(row["pid_alive"])
        self.assertEqual(row["failure_category"], "stale_pid")
        self.assertEqual(row["suggested_action"], "operator-retry")

    def test_operator_runs_tail_returns_latest_log(self):
        with mock.patch.dict(os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False):
            _seed_operator_run(self.repo, "20260617T000000Z-fake-cccccccc", combined_log="a\nb\nc\nd\n")
            status, data = self._get("/api/operator-runs/20260617T000000Z-fake-cccccccc/tail?lines=2")
        self.assertEqual(status, 200)
        self.assertEqual(data["run_id"], "20260617T000000Z-fake-cccccccc")
        self.assertEqual(data["lines"], 2)
        self.assertIn("c", data["text"])
        self.assertIn("d", data["text"])
        # AO-AUDIT C9: the per-run tail endpoint also surfaces the
        # runtime overlay so the detail view shows stale_pid /
        # failure_category without a separate status endpoint.
        run = data.get("run")
        self.assertIsInstance(run, dict)
        self.assertEqual(run["run_id"], "20260617T000000Z-fake-cccccccc")
        for key in (
            "runtime_status",
            "runtime_status_alias",
            "runtime_status_note",
            "failure_category",
            "idle_for_seconds",
            "suggested_action",
        ):
            self.assertIn(key, run)

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
        # AO-AUDIT C9: the operator-runs table surfaces the persisted
        # status, the runtime overlay, and failure_category columns.
        self.assertIn("runtime-stale", body)
        self.assertIn("status-dot stale", body)
        for forbidden in ("/api/exec", "/api/shell", "/api/command", "/api/run_command"):
            self.assertNotIn(forbidden, body)

    def test_no_shell_endpoint_exposed(self):
        for forbidden in ("/api/exec", "/api/shell", "/api/command"):
            status, data = self._get(forbidden)
            self.assertEqual(status, 404)
            self.assertIn("not found", data["error"].lower())


# ---------------------------------------------------------------------------
# Bundle + validation + run-launcher API (AO-ADMIN-T3-WEB-BUNDLE-RUN-API)
# ---------------------------------------------------------------------------


class BundleApiTests(unittest.TestCase):
    """Unit tests for the bundle, validation, and run-flag API additions.

    These tests call the module-level data fetchers and ``build_run_command``
    directly (no HTTP server). The HTTP-shape tests for upload/validate live
    in the existing :class:`WebApiTests` set when the server is wired up.
    """

    def test_build_run_command_with_flags(self) -> None:
        real_repo = Path(__file__).resolve().parent.parent
        roadmap = real_repo / "examples" / "roadmaps" / "demo-shell.json"
        argv = web.build_run_command(
            str(roadmap),
            no_codex=True,
            autonomous=True,
            reviewer="heuristic",
            max_tasks=2,
        )
        self.assertIn("--autonomous", argv)
        reviewer_idx = argv.index("--reviewer")
        self.assertEqual(argv[reviewer_idx + 1], "heuristic")
        max_tasks_idx = argv.index("--max-tasks")
        self.assertEqual(argv[max_tasks_idx + 1], "2")
        self.assertIn("--no-codex", argv)

    def test_build_run_command_defaults(self) -> None:
        real_repo = Path(__file__).resolve().parent.parent
        roadmap = real_repo / "examples" / "roadmaps" / "demo-shell.json"
        argv = web.build_run_command(str(roadmap))
        self.assertNotIn("--no-codex", argv)
        self.assertNotIn("--autonomous", argv)
        self.assertNotIn("--reviewer", argv)
        self.assertNotIn("--max-tasks", argv)

    def test_collect_bundles_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.assertFalse((tmp_path / "bundles").exists())
            result = web.collect_bundles(repo_root=tmp_path)
            self.assertEqual(result, {"bundles": []})
            self.assertTrue((tmp_path / "bundles").is_dir())

    def test_collect_bundles_lists_unpacked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "bundles" / "demo"
            bundle_dir.mkdir(parents=True)
            (bundle_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "1.0.0",
                        "roadmap": "roadmap.json",
                    }
                ),
                encoding="utf-8",
            )
            (bundle_dir / "roadmap.json").write_text("{}", encoding="utf-8")
            result = web.collect_bundles(repo_root=tmp_path)
            self.assertEqual(len(result["bundles"]), 1)
            entry = result["bundles"][0]
            self.assertEqual(entry["name"], "demo")
            self.assertEqual(entry["version"], "1.0.0")
            self.assertEqual(entry["roadmap_path"], str(bundle_dir / "roadmap.json"))
            self.assertEqual(entry["dir"], str(bundle_dir))

    def test_collect_bundle_validation_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            web.collect_bundle_validation("../x", repo_root=Path(tmp))

    def test_unpack_bundle_endpoint_flow(self) -> None:
        with tempfile.TemporaryDirectory() as src_tmp:
            src = Path(src_tmp) / "srcbundle"
            src.mkdir()
            (src / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "1.0.0",
                        "roadmap": "roadmap.json",
                    }
                ),
                encoding="utf-8",
            )
            (src / "roadmap.json").write_text("{}", encoding="utf-8")
            zip_path = Path(src_tmp) / "demo.zip"
            bundles.pack_bundle(src, zip_path)
            with tempfile.TemporaryDirectory() as dest_tmp:
                dest_root = Path(dest_tmp)
                bundles_dir = dest_root / "bundles"
                bundles_dir.mkdir()
            unpacked = bundles.unpack_bundle(zip_path, bundles_dir)
            self.assertEqual(unpacked.manifest.name, "demo")
            self.assertEqual(unpacked.manifest.version, "1.0.0")


# ---------------------------------------------------------------------------
# SSE live-log streaming endpoints (AO-ADMIN-T4-WEB-SSE-STREAMS)
# ---------------------------------------------------------------------------


class SseStreamTests(unittest.TestCase):
    """Unit + smoke tests for the SSE live-log streaming endpoints.

    The pure-helper tests exercise the framing format, the single-component
    path validator, and the per-task log resolver without an HTTP server.
    The endpoint tests use the real :class:`ThreadingHTTPServer` +
    :class:`http.client.HTTPConnection` pair to confirm the wire format and
    the path-traversal rejection over a real socket.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Spin up one server for the endpoint tests; the pure-helper tests
        # ignore it. A class-level fixture keeps the pure tests fast while
        # still letting the end-to-end tests use the same pattern as the
        # rest of this file.
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.repo = _init_repo(cls.tmp)
        cls.db = cls.tmp / "state.sqlite"
        cls.store = StateStore(cls.db)
        cls.store.init()
        cls.port = _free_port()
        cls.server = web.make_server("127.0.0.1", cls.port, state=cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.connect()
                conn.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("SSE test server did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    # --- pure-helper tests -------------------------------------------------

    def test_format_sse_frame_log_event(self) -> None:
        out = web.format_sse_frame("log", {"run_id": "r1", "text": "hello\nworld"})
        # Event line comes first, then one data: line per source line, then
        # the SSE blank line terminator.
        self.assertTrue(out.startswith("event: log\n"))
        self.assertIn("data: {\"run_id\": \"r1\", \"text\": \"hello\\nworld\"}\n", out)
        self.assertTrue(out.endswith("\n\n"))

    def test_format_sse_frame_string_payload(self) -> None:
        out = web.format_sse_frame("done", "ok")
        self.assertEqual(out, "event: done\ndata: ok\n\n")

    def test_format_sse_frame_no_event(self) -> None:
        out = web.format_sse_frame("", "raw message")
        self.assertTrue(out.startswith("data: raw message\n"))
        self.assertTrue(out.endswith("\n\n"))
        self.assertNotIn("event:", out)

    def test_format_sse_frame_multiline_payload(self) -> None:
        out = web.format_sse_frame("log", "line1\nline2\nline3")
        # Each source line becomes its own data: line; no raw newlines leak.
        self.assertIn("data: line1\n", out)
        self.assertIn("data: line2\n", out)
        self.assertIn("data: line3\n", out)
        self.assertTrue(out.endswith("\n\n"))
        # A raw newline must NEVER appear inside a data: line; the SSE wire
        # format requires the data to be split.
        for bad in ("data: line1\nline2", "data: line2\nline3"):
            self.assertNotIn(bad, out)

    def test_require_single_component_accepts_simple(self) -> None:
        self.assertEqual(web._require_single_component("foo"), "foo")
        self.assertEqual(web._require_single_component("foo-bar_1.0"), "foo-bar_1.0")

    def test_require_single_component_rejects_traversal(self) -> None:
        with self.assertRaises(ValueError):
            web._require_single_component("../x")
        with self.assertRaises(ValueError):
            web._require_single_component("foo/../bar")
        with self.assertRaises(ValueError):
            web._require_single_component("a/b")
        with self.assertRaises(ValueError):
            web._require_single_component("a\\b")
        with self.assertRaises(ValueError):
            web._require_single_component("")
        with self.assertRaises(ValueError):
            web._require_single_component("   ")

    def test_resolve_task_combined_log_picks_highest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            t1 = runs / "rmap" / "T1"
            (t1 / "1").mkdir(parents=True)
            (t1 / "1" / "executor.combined.log").write_text("first\n", encoding="utf-8")
            (t1 / "3").mkdir(parents=True)
            (t1 / "3" / "executor.combined.log").write_text("third\n", encoding="utf-8")
            # Out-of-order creation: the resolver must sort by attempt
            # number, not by mtime.
            (t1 / "2").mkdir(parents=True)
            (t1 / "2" / "executor.combined.log").write_text("second\n", encoding="utf-8")
            result = web.resolve_task_combined_log(runs, "rmap", "T1")
            self.assertEqual(result, t1 / "3" / "executor.combined.log")

    def test_resolve_task_combined_log_skips_attempts_without_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            t1 = runs / "rmap" / "T1"
            (t1 / "1").mkdir(parents=True)
            (t1 / "1" / "executor.combined.log").write_text("first\n", encoding="utf-8")
            (t1 / "2").mkdir(parents=True)
            # Attempt 2 has no log file; the resolver must fall back to 1.
            result = web.resolve_task_combined_log(runs, "rmap", "T1")
            self.assertEqual(result, t1 / "1" / "executor.combined.log")

    def test_resolve_task_combined_log_returns_none_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            # Runs root does not even exist yet.
            self.assertIsNone(web.resolve_task_combined_log(runs, "rmap", "T1"))
            # Runs root exists but the per-task dir is missing.
            runs.mkdir()
            self.assertIsNone(web.resolve_task_combined_log(runs, "rmap", "T1"))
            # Task dir exists but no attempts.
            (runs / "rmap" / "T1").mkdir(parents=True)
            self.assertIsNone(web.resolve_task_combined_log(runs, "rmap", "T1"))
            # Task dir has a non-numeric entry only.
            bogus = runs / "rmap" / "T1" / "notanumber"
            bogus.mkdir()
            self.assertIsNone(web.resolve_task_combined_log(runs, "rmap", "T1"))

    def test_resolve_task_combined_log_handles_missing_root(self) -> None:
        # The runs root does not exist; the resolver must not raise.
        self.assertIsNone(web.resolve_task_combined_log(Path("/no/such/path"), "r", "t"))

    def test_resolve_task_combined_log_any_roadmap_picks_highest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            old = runs / "old-roadmap" / "T1" / "5"
            old.mkdir(parents=True)
            (old / "executor.combined.log").write_text("old\n", encoding="utf-8")
            latest = runs / "new-roadmap" / "T1" / "6"
            latest.mkdir(parents=True)
            (latest / "executor.combined.log").write_text("latest\n", encoding="utf-8")
            ignored = runs / "../bad" / "T1" / "99"
            ignored.mkdir(parents=True)

            result = web.resolve_task_combined_log_any_roadmap(runs, "T1")

            self.assertIsNotNone(result)
            roadmap, log_path = result or ("", Path())
            self.assertEqual(roadmap, "new-roadmap")
            self.assertEqual(log_path, latest / "executor.combined.log")

    def test_default_agentops_runs_root_under_repo(self) -> None:
        real_repo = Path(__file__).resolve().parent.parent
        with mock.patch.object(
            web, "_resolve_allowed_roots", return_value=web._AllowedRoots(
                repo_root=real_repo, tmp_root=Path("/tmp")
            )
        ):
            self.assertEqual(
                web._default_agentops_runs_root(), real_repo / ".agentops" / "runs"
            )

    # --- endpoint tests ----------------------------------------------------

    def _get_raw(self, path: str, *, timeout: float = 5.0) -> bytes:
        # Use a raw socket because ``http.client.HTTPResponse.read`` reads
        # through a ``BufferedReader`` that tries to fill its 8 KiB buffer
        # before returning; for a small SSE response that would block
        # forever. ``socket.makefile`` with a small read buffer streams
        # the response as it arrives.
        import socket as _socket
        with _socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            s.sendall(
                f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode("ascii")
            )
            chunks: list[bytes] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    chunk = s.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if b"event: done" in b"".join(chunks):
                    break
        data = b"".join(chunks)
        # Split off headers; the test only asserts on the body.
        _, _, body = data.partition(b"\r\n\r\n")
        return body

    def test_operator_stream_endpoint_rejects_traversal(self) -> None:
        # Path-traversal run ids are rejected with 400 (no SSE upgrade).
        with mock.patch.dict(
            os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False
        ):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request(
                    "GET", "/api/operator-runs/..%2F..%2Fetc%2Fpasswd/stream"
                )
                resp = conn.getresponse()
                # Read the small JSON error body.
                body = resp.read()
            finally:
                conn.close()
        self.assertEqual(resp.status, 400)
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("error", payload)
        self.assertIn("single path component", payload["error"])

    def test_operator_stream_endpoint_sends_initial_tail_and_done(self) -> None:
        # End-to-end smoke: a complete, static log must produce one
        # ``event: log`` frame containing the last 200 lines, then a
        # ``event: done`` frame once the loop decides the run is closed.
        with mock.patch.dict(
            os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(self.repo)}, clear=False
        ):
            _seed_operator_run(
                self.repo,
                "20260617T000000Z-fake-stream-1",
                combined_log="alpha\nbeta\ngamma\n",
            )
            body = self._get_raw(
                "/api/operator-runs/20260617T000000Z-fake-stream-1/stream"
                "?max_seconds=1&idle_seconds=1",
                timeout=8.0,
            )
        text = body.decode("utf-8", errors="replace")
        # The body must contain the SSE frames; the HTTP headers (which
        # carry ``text/event-stream``) are stripped by ``_get_raw``.
        self.assertIn("event: log", text)
        self.assertIn("data: {\"run_id\":", text)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertIn("gamma", text)
        self.assertIn("event: done", text)
        self.assertIn("\"reason\":", text)
        # pid_alive is forwarded by the operator-run endpoint.
        self.assertIn("\"pid_alive\":", text)

    def test_task_stream_endpoint_resolves_log_without_roadmap(self) -> None:
        runs = self.repo / ".agentops" / "runs"
        log_dir = runs / "roadmap-a" / "T1" / "2"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "executor.combined.log").write_text(
            "task-alpha\ntask-beta\n", encoding="utf-8"
        )

        with mock.patch.object(
            web, "_resolve_allowed_roots", return_value=web._AllowedRoots(
                repo_root=self.repo, tmp_root=Path("/tmp")
            )
        ):
            body = self._get_raw(
                "/api/tasks/T1/stream?max_seconds=1&idle_seconds=1",
                timeout=8.0,
            )

        text = body.decode("utf-8", errors="replace")
        self.assertIn("event: log", text)
        self.assertIn("task-alpha", text)
        self.assertIn("task-beta", text)
        self.assertIn("event: done", text)

    def test_stream_log_loop_flushes_final_buffer_before_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "executor.combined.log"
            log_path.write_text("", encoding="utf-8")
            handler = web.AgentOpsRequestHandler.__new__(web.AgentOpsRequestHandler)
            handler.wfile = io.BytesIO()

            def append_partial() -> None:
                time.sleep(0.1)
                log_path.write_text("partial-final", encoding="utf-8")

            writer = threading.Thread(target=append_partial)
            writer.start()
            try:
                handler._stream_log_loop(
                    log_path=log_path,
                    id_field="task_id",
                    id_value="T1",
                    max_seconds=1,
                    idle_seconds=5,
                    from_end=True,
                    tail_lines=200,
                    is_alive=None,
                    include_pid_alive=False,
                )
            finally:
                writer.join(timeout=5)

            text = handler.wfile.getvalue().decode("utf-8")
            self.assertIn("event: log", text)
            self.assertIn("partial-final", text)
            self.assertLess(text.index("partial-final"), text.index("event: done"))


class HistoryApiTests(unittest.TestCase):
    """Tests for the T5 run-history, task-attempts, and run-log APIs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # The repo_root for read_run_log tests is a *plain* directory; we
        # do not need a real git repo because the log read never invokes
        # git, just resolves a path under .agentops/runs.
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.store = StateStore(self.tmp / "state.sqlite")

    # --- read_run_log -------------------------------------------------------

    def test_read_run_log_rejects_traversal(self) -> None:
        with self.assertRaises(ValueError):
            web.read_run_log("..", "t", "1", "executor.combined.log", repo_root=self.repo)
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "..", "1", "executor.combined.log", repo_root=self.repo)
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "t", "..", "executor.combined.log", repo_root=self.repo)
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "t", "1", "../x", repo_root=self.repo)
        # Path-separator inside a component also rejected.
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "t", "1", "sub/dir.log", repo_root=self.repo)
        # Unknown kind rejected even if every other component is fine.
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "t", "1", "secret.log", repo_root=self.repo)
        with self.assertRaises(ValueError):
            web.read_run_log("rmap", "t", "1", "", repo_root=self.repo)

    def test_read_run_log_missing_and_present(self) -> None:
        log_path = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "1"
        log_path.mkdir(parents=True)
        (log_path / "executor.combined.log").write_text("hello world", encoding="utf-8")

        present = web.read_run_log(
            "rmap", "T1", "1", "executor.combined.log", repo_root=self.repo
        )
        self.assertTrue(present["found"])
        self.assertIn("hello world", present["text"])
        self.assertFalse(present["truncated"])
        self.assertEqual(present["size"], len("hello world"))

        missing = web.read_run_log(
            "rmap", "T1", "1", "executor.stderr.log", repo_root=self.repo
        )
        self.assertFalse(missing["found"])
        self.assertTrue(missing["path"].endswith("executor.stderr.log"))

        no_dir = web.read_run_log(
            "rmap", "T1", "9", "executor.combined.log", repo_root=self.repo
        )
        self.assertFalse(no_dir["found"])

    def test_read_run_log_truncates_large_file(self) -> None:
        log_dir = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "1"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "executor.combined.log"
        log_file.write_bytes(b"x" * 300_000)

        result = web.read_run_log(
            "rmap",
            "T1",
            "1",
            "executor.combined.log",
            max_bytes=1000,
            repo_root=self.repo,
        )
        self.assertTrue(result["found"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"]), 1000)
        # The returned text is the tail, so the byte at index -1 must be 'x'.
        self.assertTrue(result["text"].endswith("x"))

    def test_read_run_log_refuses_path_outside_runs_root(self) -> None:
        # A symlink inside the runs root that resolves outside the runs
        # root must NOT be served: the contained check should trip.
        # We create a directory that is a symlink to a file outside.
        runs_root = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "1"
        runs_root.mkdir(parents=True)
        outside = self.tmp / "outside.txt"
        outside.write_text("outside-data", encoding="utf-8")
        try:
            (runs_root / "executor.combined.log").symlink_to(outside)
            symlink_path = runs_root / "executor.combined.log"
            self.assertTrue(symlink_path.is_symlink())
            # The file is resolvable but resolves OUTSIDE the runs root, so
            # the helper must refuse to read it.
            with self.assertRaises(ValueError):
                web.read_run_log(
                    "rmap", "T1", "1", "executor.combined.log", repo_root=self.repo
                )
        except (OSError, NotImplementedError):
            # Some filesystems (Windows) do not support symlinks; skip.
            self.skipTest("symlink unsupported on this filesystem")

    def test_read_run_log_refuses_symlink_to_other_attempt(self) -> None:
        attempt_one = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "1"
        attempt_two = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "2"
        attempt_one.mkdir(parents=True)
        attempt_two.mkdir(parents=True)
        target = attempt_two / "executor.combined.log"
        target.write_text("attempt-two-secret", encoding="utf-8")
        try:
            (attempt_one / "executor.combined.log").symlink_to(target)
            with self.assertRaises(ValueError):
                web.read_run_log(
                    "rmap", "T1", "1", "executor.combined.log", repo_root=self.repo
                )
        except (OSError, NotImplementedError):
            self.skipTest("symlink unsupported on this filesystem")

    # --- collect_run_history ------------------------------------------------

    def test_collect_run_history_from_state(self) -> None:
        self.store.init()
        self.store.event(
            "rmap-history", "T-history", "1", "roadmap.finished", {"run_verdict": "passed"}
        )
        result = web.collect_run_history(self.store)
        runs = result["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["roadmap_id"], "rmap-history")
        self.assertEqual(runs[0]["run_verdict"], "passed")
        self.assertIsInstance(runs[0]["seq"], int)

    def test_collect_run_history_ignores_other_event_types(self) -> None:
        self.store.init()
        self.store.event("rmap-1", "T1", "1", "attempt.started", {"attempt_no": 1})
        self.store.event("rmap-1", "T1", "1", "roadmap.finished", {"run_verdict": "ok"})
        runs = web.collect_run_history(self.store)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_verdict"], "ok")

    def test_collect_run_history_handles_corrupt_payload(self) -> None:
        # Inject a corrupt payload directly: not a JSON object, not a JSON
        # string. The helper must return an empty dict for run_verdict
        # and still include the row, never raising.
        self.store.init()
        self.store.event("rmap-x", "T-x", "1", "roadmap.finished", {"run_verdict": "ok"})
        # Direct DB poke so the payload_json column holds a non-JSON string.
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE events SET payload_json=? WHERE type='roadmap.finished'",
                ("not-json-{",),
            )
        runs = web.collect_run_history(self.store)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertIsNone(runs[0]["run_verdict"])

    # --- collect_task_attempts ---------------------------------------------

    def test_collect_task_attempts_unknown(self) -> None:
        self.store.init()
        result = web.collect_task_attempts(self.store, "NOPE")
        self.assertFalse(result["found"])
        self.assertEqual(result["task_id"], "NOPE")
        self.assertEqual(result["attempts"], [])

    def test_collect_task_attempts_known(self) -> None:
        # Use the public StateStore API to record an attempt, then make
        # sure the helper surfaces it.
        from agentops.models import (
            RepoConfig,
            ReviewConfig,
            RoadmapConfig,
            TaskConfig,
            TaskState,
        )

        self.store.init()
        repo = RepoConfig(
            id="r", path=Path("/tmp"), base_branch="main", integration_branch="int"
        )
        # Build a minimal task + roadmap to satisfy the schema and
        # ``create_attempt``.
        task = TaskConfig(
            id="T1",
            kind="guard",
            risk=1,
            priority=10,
            prompt_path=Path("/tmp/p.md"),
            branch_prefix="agentops",
            allowed_files=[],
            review=ReviewConfig(codex="never"),
        )
        self.store.import_roadmap(
            RoadmapConfig(
                version=1,
                roadmap_id="r-collect",
                repo=repo,
                tasks=[task],
            )
        )
        self.store.transition_task("r-collect", "T1", TaskState.EXECUTOR_RUNNING)
        self.store.create_attempt(
            "r-collect", task, 1, self.tmp / "ws", "branch-x", "base-sha"
        )

        result = web.collect_task_attempts(self.store, "T1")
        self.assertTrue(result["found"])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["attempt_no"], 1)
        self.assertIsNotNone(result["task"])

    def test_collect_task_attempts_empty_task_id(self) -> None:
        self.store.init()
        result = web.collect_task_attempts(self.store, "")
        self.assertFalse(result["found"])
        self.assertEqual(result["attempts"], [])

    # --- endpoint integration ----------------------------------------------

    def _start_server(self) -> int:
        port = _free_port()
        server = web.make_server("127.0.0.1", port, state=self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=5))
        # Wait for the server to be ready.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=1)
                conn.connect()
                conn.close()
                return port
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start")

    def _get(self, port: int, path: str) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def test_run_history_endpoint_empty(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(port, "/api/run-history")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"runs": []})

    def test_run_history_endpoint_returns_runs(self) -> None:
        self.store.init()
        self.store.event(
            "r-end", "T1", "1", "roadmap.finished", {"run_verdict": "ok"}
        )
        port = self._start_server()
        status, data = self._get(port, "/api/run-history?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["roadmap_id"], "r-end")
        self.assertEqual(data["runs"][0]["run_verdict"], "ok")

    def test_task_attempts_endpoint_rejects_traversal(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(port, "/api/tasks/..%2Fbad/attempts")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_task_attempts_endpoint_unknown(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(port, "/api/tasks/UNKNOWN/attempts")
        self.assertEqual(status, 200)
        self.assertFalse(data["found"])

    def test_run_logs_endpoint_serves_text(self) -> None:
        self.store.init()
        log_dir = self.repo / ".agentops" / "runs" / "rmap" / "T1" / "1"
        log_dir.mkdir(parents=True)
        (log_dir / "executor.combined.log").write_text("payload-line", encoding="utf-8")
        with mock.patch.object(
            web, "_resolve_allowed_roots", return_value=web._AllowedRoots(
                repo_root=self.repo, tmp_root=self.tmp
            )
        ):
            port = self._start_server()
            status, data = self._get(
                port,
                "/api/run-logs?roadmap=rmap&task=T1&attempt=1&kind=executor.combined.log",
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["found"])
        self.assertIn("payload-line", data["text"])

    def test_run_logs_endpoint_missing_param(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(
            port, "/api/run-logs?roadmap=rmap&task=T1&attempt=1"
        )
        self.assertEqual(status, 400)
        self.assertIn("kind is required", data["error"])

    def test_run_logs_endpoint_rejects_unknown_kind(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(
            port,
            "/api/run-logs?roadmap=rmap&task=T1&attempt=1&kind=etc-passwd",
        )
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_run_logs_endpoint_rejects_traversal(self) -> None:
        self.store.init()
        port = self._start_server()
        status, data = self._get(
            port,
            "/api/run-logs?roadmap=..&task=T1&attempt=1&kind=executor.combined.log",
        )
        self.assertEqual(status, 400)
        self.assertIn("error", data)


# ---------------------------------------------------------------------------
# /api/admin — public-facing maintainer/operator snapshot
# ---------------------------------------------------------------------------


class AdminSnapshotShapeTests(unittest.TestCase):
    """Stable JSON shape contract for ``/api/admin``.

    The admin snapshot is the public surface of the operator panel.
    These tests lock down the top-level keys, the caps, and the
    empty-state metadata so future refactors cannot break the UI or
    the CLI consumers of the same snapshot.
    """

    REQUIRED_TOP_LEVEL_KEYS = {
        "roadmap_state",
        "latest_events",
        "operator_runs",
        "attention_needed",
        "pr_loop_cycles",
        "recommended_commands",
        "diagnostics",
        "usage_summary",
    }

    def _server(self, store):
        port = _free_port()
        server = web.make_server("127.0.0.1", port, state=store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return port, server, thread

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def _http_get(self, port, path):
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def test_admin_endpoint_returns_stable_shape_for_fresh_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        self.assertEqual(status, 200)
        self.assertEqual(set(data.keys()), self.REQUIRED_TOP_LEVEL_KEYS)

    def test_admin_roadmap_state_empty_for_fresh_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTOPS_OPERATOR_RUNS_ROOT": str(Path(tmp) / "empty")},
                    clear=False,
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        rs = data["roadmap_state"]
        self.assertTrue(rs["empty"])
        self.assertEqual(rs["task_count"], 0)
        self.assertEqual(rs["per_roadmap"], [])
        self.assertEqual(rs["recent_tasks"], [])
        self.assertEqual(rs["state_histogram"], {})

    def test_admin_latest_events_capped_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        events = data["latest_events"]
        self.assertEqual(events["cap"], 10)
        self.assertEqual(events["count"], 0)
        self.assertTrue(events["empty"])
        self.assertEqual(events["items"], [])

    def test_admin_operator_runs_empty_when_no_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTOPS_OPERATOR_RUNS_ROOT": str(Path(tmp) / "empty")},
                    clear=False,
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        op = data["operator_runs"]
        self.assertFalse(op["exists"])
        self.assertEqual(op["count"], 0)
        self.assertEqual(op["items"], [])
        self.assertEqual(op["cap"], 5)
        self.assertEqual(op["runtime_status_histogram"], {})

    def test_admin_attention_empty_for_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTOPS_OPERATOR_RUNS_ROOT": str(Path(tmp) / "empty")},
                    clear=False,
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        att = data["attention_needed"]
        self.assertTrue(att["empty"])
        self.assertEqual(att["count"], 0)
        self.assertEqual(att["items"], [])
        self.assertEqual(att["cap"], 25)

    def test_admin_pr_loop_cycles_empty_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                with mock.patch.object(web, "_resolve_allowed_roots", return_value=web._AllowedRoots(
                    repo_root=Path(tmp) / "agentops", tmp_root=Path(tmp) / "scratch",
                )):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        pl = data["pr_loop_cycles"]
        self.assertFalse(pl["exists"])
        self.assertEqual(pl["count"], 0)
        self.assertEqual(pl["items"], [])
        # root is still surfaced so the operator can run the CLI to inspect.
        self.assertTrue(pl["root"].endswith(".agentops/pr-loop"))

    def test_admin_diagnostics_excludes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "GITHUB_TOKEN": "ghp_shouldneverappear",
                        "OPENAI_API_KEY": "sk-shouldneverappear",
                    },
                    clear=False,
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        diag = data["diagnostics"]
        serialized = json.dumps(diag, sort_keys=True)
        for forbidden in (
            "ghp_shouldneverappear",
            "sk-shouldneverappear",
            "AGENTOPS_WEB_TOKEN",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("generated_at", diag)
        self.assertIn("db_path", diag)
        self.assertIn("repo_root", diag)

    def test_admin_recommended_commands_are_copyable_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        cmds = data["recommended_commands"]
        self.assertIsInstance(cmds, list)
        self.assertGreater(len(cmds), 0)
        joined = " ".join(cmds)
        for required in (
            "agentops status",
            "agentops review-queue",
            "agentops operator-status",
            "agentops operator-tail",
            "agentops operator-result",
            "agentops operator-retry",
            "agentops logs",
            "agentops pr-loop",
        ):
            self.assertIn(required, joined)

    def test_admin_does_not_include_raw_prompt_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            store.event("rmap", None, None, "roadmap.imported", {"tasks": 1})
            store.event("rmap", "T1", None, "task.ready", {"prompt_body": "SECRET_PROMPT"})
            store.event("rmap", "T1", "A1", "attempt.finished", {"exit_code": 0, "head_sha": "deadbeef12345678"})
            port, server, thread = self._server(store)
            try:
                _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        serialized = json.dumps(data, sort_keys=True)
        self.assertNotIn("SECRET_PROMPT", serialized)
        self.assertNotIn("prompt_body", serialized)
        # Latest events are capped at 10.
        self.assertLessEqual(data["latest_events"]["count"], 10)

    def test_admin_latest_events_capped_at_10(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            for i in range(30):
                store.event("rmap", f"T{i:02d}", None, "task.ready", {"i": i})
            port, server, thread = self._server(store)
            try:
                _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        self.assertEqual(data["latest_events"]["cap"], 10)
        self.assertEqual(len(data["latest_events"]["items"]), 10)

    def test_admin_attention_includes_stale_pid_operator_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.sqlite")
            store.init()
            runs_root = tmp_path / "runs"
            run_dir = runs_root / ".operator-runs" / "stale-run-001"
            run_dir.mkdir(parents=True)
            (run_dir / "combined.log").write_text("line\n", encoding="utf-8")
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "run_id": "stale-run-001",
                        "name": "stale",
                        "status": "running",
                        "pid": 0,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "failure_category": "stale_pid",
                    }
                ),
                encoding="utf-8",
            )
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(runs_root)}, clear=False
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        att = data["attention_needed"]
        self.assertFalse(att["empty"])
        row = next(
            (
                item for item in att["items"]
                if item.get("kind") == "operator_run" and item.get("run_id") == "stale-run-001"
            ),
            None,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("stale_pid", row["reasons"])
        self.assertIn("agentops operator-tail stale-run-001 --lines 200", row["first_cli"])

    def test_admin_attention_includes_awaiting_review_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_repo(tmp_path)
            roadmap_path = _write_minimal_roadmap(tmp_path, repo)
            store = StateStore(tmp_path / "state.sqlite")
            store.init()
            from agentops.config import load_roadmap
            roadmap = load_roadmap(roadmap_path)
            store.import_roadmap(roadmap)
            from agentops.models import TaskState
            store.transition_task("r", "T1", TaskState.READY)
            store.transition_task("r", "T1", TaskState.AWAITING_REVIEW)
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTOPS_OPERATOR_RUNS_ROOT": str(tmp_path / "empty")},
                    clear=False,
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        att = data["attention_needed"]
        row = next(
            (
                item for item in att["items"]
                if item.get("kind") == "task" and item.get("task_id") == "T1"
            ),
            None,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["state"], "awaiting_review")
        self.assertIn("agentops decide T1", row["first_cli"])

    def test_admin_operator_runs_capped_at_5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.sqlite")
            store.init()
            runs_root = tmp_path / "runs"
            for i in range(8):
                run_id = f"run-{i:02d}"
                run_dir = runs_root / ".operator-runs" / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "status.json").write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "name": run_id,
                            "status": "exited",
                            "exit_code": 0,
                            "started_at": "2026-01-01T00:00:00+00:00",
                        }
                    ),
                    encoding="utf-8",
                )
            port, server, thread = self._server(store)
            try:
                with mock.patch.dict(
                    os.environ, {"AGENTOPS_OPERATOR_RUNS_ROOT": str(runs_root)}, clear=False
                ):
                    _status, data = self._http_get(port, "/api/admin")
            finally:
                self._stop(server, thread)
        op = data["operator_runs"]
        self.assertEqual(op["cap"], 5)
        self.assertLessEqual(len(op["items"]), 5)
        self.assertGreaterEqual(op["count"], 8)

    def test_admin_html_renders_admin_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
            finally:
                self._stop(server, thread)
        self.assertEqual(resp.status, 200)
        self.assertIn("Admin / Operator panel", body)
        self.assertIn('id="admin-roadmap-rows"', body)
        self.assertIn('id="admin-event-rows"', body)
        self.assertIn('id="admin-operator-runs-rows"', body)
        self.assertIn('id="admin-attention-rows"', body)
        self.assertIn('id="admin-pr-loop-rows"', body)
        self.assertIn('id="admin-recommended-commands"', body)
        self.assertIn("/api/admin", body)
        # The page must not advertise a generic shell/exec endpoint.
        for forbidden in ("/api/exec", "/api/shell", "/api/command", "/api/run_command"):
            self.assertNotIn(forbidden, body)

    def test_admin_html_includes_empty_state_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
            finally:
                self._stop(server, thread)
        self.assertIn("No roadmaps recorded yet", body)
        self.assertIn("No events yet", body)
        self.assertIn("Nothing needs operator attention", body)
        self.assertIn("No PR repair cycles yet", body)

    def test_admin_html_calls_api_admin_in_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
            finally:
                self._stop(server, thread)
        self.assertIn('"/api/admin"', body)
        # renderAdmin must be invoked from the periodic refresh so the card
        # stays in sync with the rest of the dashboard.
        self.assertIn("renderAdmin()", body)

    def test_no_exec_or_shell_endpoint_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            port, server, thread = self._server(store)
            try:
                for forbidden in ("/api/exec", "/api/shell", "/api/command", "/api/run_command"):
                    status, _ = self._http_get(port, forbidden)
                    self.assertEqual(status, 404)
            finally:
                self._stop(server, thread)


class AdminSnapshotSeedTests(unittest.TestCase):
    """End-to-end shape test: seed a state DB and a runs directory and
    verify that the admin snapshot renders the expected rollups.
    """

    def test_seeded_snapshot_rolls_up_state_histogram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_repo(tmp_path)
            store = StateStore(tmp_path / "state.sqlite")
            store.init()
            from agentops.config import load_roadmap
            from agentops.models import TaskState
            roadmap_a = load_roadmap(_write_minimal_roadmap(tmp_path, repo))
            roadmap_b_path = tmp_path / "rmap-b.json"
            prompt_b = tmp_path / "prompt-b.md"
            prompt_b.write_text("hi", encoding="utf-8")
            roadmap_b_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roadmap_id": "rmap-b",
                        "repo": {"id": "x", "path": str(repo)},
                        "tasks": [
                            {
                                "id": "T2",
                                "kind": "guard",
                                "prompt": str(prompt_b),
                                "executor": "shell",
                                "executor_command": "true",
                                "branch_prefix": "agentops",
                                "allowed_files": ["b.txt"],
                                "review": {"codex": "never"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            roadmap_b = load_roadmap(roadmap_b_path)
            store.import_roadmap(roadmap_a)
            store.import_roadmap(roadmap_b)
            store.transition_task("r", "T1", TaskState.READY)
            store.transition_task("r", "T1", TaskState.EXECUTOR_RUNNING)
            store.transition_task("rmap-b", "T2", TaskState.READY)
            store.transition_task("rmap-b", "T2", TaskState.AWAITING_REVIEW)
            # Add events.
            store.event("r", "T1", None, "task.executor_running", {"attempt": 1})
            store.event("r", "T1", None, "attempt.finished", {"exit_code": 0})
            data = web.collect_admin_snapshot(store)
        rs = data["roadmap_state"]
        self.assertFalse(rs["empty"])
        self.assertGreaterEqual(rs["task_count"], 2)
        self.assertEqual(rs["state_histogram"].get("awaiting_review", 0), 1)
        self.assertEqual(rs["state_histogram"].get("executor_running", 0), 1)
        per_roadmap = {row["roadmap_id"]: row for row in rs["per_roadmap"]}
        self.assertIn("r", per_roadmap)
        self.assertIn("rmap-b", per_roadmap)
        self.assertGreaterEqual(len(rs["recent_tasks"]), 1)
        # Events: capped at 10.
        self.assertLessEqual(len(data["latest_events"]["items"]), 10)
        # Diagnostics has a generated_at timestamp.
        self.assertIn("generated_at", data["diagnostics"])
        # The snapshot exposes the recommended CLI hints.
        self.assertIn("agentops status", " ".join(data["recommended_commands"]))


class UsageLedgerTests(unittest.TestCase):
    """End-to-end shape tests for the model-usage ledger.

    These tests seed the SQLite ``model_calls`` table through the
    :class:`StateStore` methods and verify the dashboard snapshot,
    the admin summary block, and the ``/api/usage`` HTTP surface.
    The HTTP test uses the same in-process server harness the existing
    ``/api/admin`` tests use, so the wire format is verified end to
    end without spawning a real Codex / OpenCode binary.
    """

    def _seed(self, store: StateStore) -> None:
        store.record_model_call(
            roadmap_id="r",
            task_id="T1",
            attempt_id="A1",
            provider="opencode",
            model="minimax/MiniMax-M3",
            purpose="executor",
            input_tokens=120,
            cached_tokens=15,
            output_tokens=22,
            cost_estimate=0.00012,
        )
        store.record_model_call(
            roadmap_id="r",
            task_id="T1",
            attempt_id="A1",
            provider="codex",
            model="codex-default",
            purpose="review",
            input_tokens=80,
            cached_tokens=0,
            output_tokens=30,
        )
        store.record_model_call(
            roadmap_id="r",
            task_id="T1",
            attempt_id="A1",
            provider="heuristic",
            model="heuristic",
            purpose="review",
        )

    def test_collect_usage_snapshot_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            snapshot = web.collect_usage_snapshot(store)
            self.assertEqual(snapshot["totals"]["known_calls"], 0)
            self.assertEqual(snapshot["totals"]["unknown_calls"], 0)
            # Totals report ``None`` for token fields when no call
            # exposed any usage; the dashboard renders them as
            # ``unknown`` instead of the misleading ``0``.
            self.assertIsNone(snapshot["totals"]["input_tokens"])
            self.assertIsNone(snapshot["totals"]["cached_tokens"])
            self.assertIsNone(snapshot["totals"]["output_tokens"])
            self.assertIsNone(snapshot["totals"]["total_tokens"])
            self.assertEqual(snapshot["by_purpose"], [])
            self.assertEqual(snapshot["by_model"], [])
            self.assertEqual(snapshot["latest_calls"], [])
            self.assertIn(
                "Missing token fields are not treated as zero.",
                snapshot["notes"],
            )

    def test_collect_usage_snapshot_known_and_unknown_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            self._seed(store)
            snapshot = web.collect_usage_snapshot(store)
            totals = snapshot["totals"]
            self.assertEqual(totals["known_calls"], 2)
            self.assertEqual(totals["unknown_calls"], 1)
            self.assertEqual(totals["input_tokens"], 200)
            self.assertEqual(totals["cached_tokens"], 15)
            self.assertEqual(totals["output_tokens"], 52)
            purposes = {row["purpose"]: row for row in snapshot["by_purpose"]}
            self.assertEqual(purposes["executor"]["calls"], 1)
            self.assertEqual(purposes["executor"]["known_calls"], 1)
            self.assertEqual(purposes["review"]["calls"], 2)
            self.assertEqual(purposes["review"]["known_calls"], 1)
            self.assertEqual(purposes["review"]["unknown_calls"], 1)
            self.assertEqual(len(snapshot["by_model"]), 3)
            # Latest calls: newest first, all three rows present.
            self.assertEqual(len(snapshot["latest_calls"]), 3)

    def test_admin_snapshot_includes_usage_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            self._seed(store)
            data = web.collect_admin_snapshot(store)
            summary = data["usage_summary"]
            self.assertIn("totals", summary)
            self.assertIn("by_purpose", summary)
            self.assertIn("by_model", summary)
            self.assertEqual(summary["totals"]["known_calls"], 2)
            self.assertEqual(summary["totals"]["unknown_calls"], 1)

    def _server(self, store: StateStore) -> tuple[int, Any, threading.Thread]:
        port = _free_port()
        server = web.make_server("127.0.0.1", port, state=store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return port, server, thread

    def _stop(self, server: Any, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def _http_get(self, port: int, path: str) -> tuple[int, dict[str, Any]]:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"error": "invalid JSON"}
        return resp.status, data

    def test_api_usage_endpoint_returns_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            self._seed(store)
            port, server, thread = self._server(store)
            try:
                status, data = self._http_get(port, "/api/usage?limit=10")
            finally:
                self._stop(server, thread)
            self.assertEqual(status, 200)
            self.assertIn("totals", data)
            self.assertEqual(data["totals"]["known_calls"], 2)
            self.assertEqual(len(data["latest_calls"]), 3)
            for call in data["latest_calls"]:
                # Sensitive fields MUST NOT leak.
                self.assertNotIn("prompt_body", call)
                self.assertNotIn("executor_log", call)

    def test_api_usage_filter_by_roadmap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            self._seed(store)
            store.record_model_call(
                roadmap_id="other",
                task_id="T9",
                attempt_id="A9",
                provider="opencode",
                model="minimax/MiniMax-M3",
                purpose="executor",
                input_tokens=1,
                cached_tokens=0,
                output_tokens=1,
            )
            port, server, thread = self._server(store)
            try:
                status, data = self._http_get(port, "/api/usage?roadmap=r")
            finally:
                self._stop(server, thread)
            self.assertEqual(status, 200)
            self.assertEqual(data["filter"]["roadmap_id"], "r")
            self.assertEqual(len(data["latest_calls"]), 3)
            self.assertEqual(data["totals"]["known_calls"], 2)

    def test_api_usage_endpoint_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite")
            store.init()
            port, server, thread = self._server(store)
            try:
                status, data = self._http_get(port, "/api/usage")
            finally:
                self._stop(server, thread)
            self.assertEqual(status, 200)
            self.assertEqual(data["totals"]["known_calls"], 0)
            self.assertEqual(data["totals"]["unknown_calls"], 0)
            self.assertEqual(data["latest_calls"], [])

    def test_render_index_html_has_usage_section(self) -> None:
        html = web.render_index_html()
        self.assertIn("Model usage", html)
        self.assertIn("/api/usage", html)
        self.assertIn("usage-purpose-rows", html)
        self.assertIn("usage-model-rows", html)
        self.assertIn("usage-latest-rows", html)
        self.assertIn("renderUsage", html)
        # Unknown must be rendered explicitly so the dashboard never
        # implies a measured zero.
        self.assertIn('"unknown"', html)
