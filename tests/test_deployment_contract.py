from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = (
    "gnzsnz/ib-gateway:10.51.1e@sha256:"
    "85975f493bcacc8341cb42e6946c460868d165e5f64d9924b1958c8f03e7bee4"
)


class DeploymentContractTests(unittest.TestCase):
    def test_accounts_example_is_import_export_compatibility_only(self):
        example = (ROOT / "accounts.yaml.example").read_text(encoding="utf-8")
        self.assertIn("import/export compatibility format only", example)
        self.assertIn("SQLite is authoritative", example)
        self.assertNotIn("restart services to apply changes", example)
        self.assertNotIn("validated strictly at startup", example)

    def test_compose_has_exact_services_volumes_and_loopback_ports(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(compose["services"]), {"rebalancer", "ibkr-gateway"})
        self.assertEqual(set(compose["volumes"]), {"rebalancer_state", "ib_settings"})
        gateway = compose["services"]["ibkr-gateway"]
        self.assertEqual(gateway["image"], GATEWAY)
        self.assertEqual(
            gateway["pre_start"],
            [
                {
                    "command": [
                        "sh",
                        "-c",
                        "chown -R 1000:1000 /home/ibgateway/tws_settings",
                    ],
                    "user": "0:0",
                }
            ],
        )
        self.assertEqual(
            compose["services"]["rebalancer"]["ports"], ["127.0.0.1:18778:18778"]
        )
        for port in gateway["ports"]:
            self.assertTrue(port.startswith("127.0.0.1:") or port.startswith("${VNC_BIND_ADDRESS:-127.0.0.1}:"))
        mounts = str(compose["services"]["rebalancer"]["volumes"])
        self.assertNotIn("config.yaml", mounts)
        self.assertNotIn("accounts.yaml", mounts)
        self.assertNotIn("./data", mounts)
        self.assertNotIn("IB_USERNAME", compose["services"]["rebalancer"]["environment"])
        self.assertNotIn("ZEHNLABS_API_KEY", compose["services"]["ibkr-gateway"]["environment"])
        self.assertEqual(
            compose["services"]["rebalancer"]["environment"]["TRADING_MODE"],
            compose["services"]["ibkr-gateway"]["environment"]["TRADING_MODE"],
        )

    def test_image_and_railway_contract(self):
        dockerfile = (ROOT / "rebalancer" / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "rebalancer" / "entrypoint.sh").read_text(encoding="utf-8")
        railway = (ROOT / "docs" / "railway.md").read_text(encoding="utf-8")
        for required in (
            "fastapi",
            "uvicorn",
            "Jinja2",
            "python-multipart",
        ):
            self.assertIn(required, (ROOT / "rebalancer" / "requirements.txt").read_text())
        self.assertIn("gosu appuser python", dockerfile)
        self.assertIn("/healthz", dockerfile)
        self.assertIn("chown -R appuser:appuser /app/state", entrypoint)
        for required in (
            GATEWAY,
            "secret(32)",
            "RAILWAY_RUN_UID=0",
            "RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0",
            "RAILWAY_DEPLOYMENT_DRAINING_SECONDS=900",
            "restart policy: `Always`",
            "/healthz",
            "daily and weekly",
            "Status: not yet published",
            "source: `ghcr.io/zehnlabs-com/zehnlabs-rebalancer:<release>@sha256:<tested-manifest-digest>`",
            "image auto-updates: disabled",
            "`rebalancer-state`, mounted at `/app/state`",
            "`ib-settings`, mounted at `/home/ibgateway/tws_settings`",
            "IB_HOST=ibkr-gateway.railway.internal",
            "USER_NOTIFICATIONS_ENABLED=false",
            "USER_NOTIFICATIONS_CHANNEL=",
            "TWS_USERID=${{shared.IB_USERNAME}}",
            "TWS_PASSWORD=${{shared.IB_PASSWORD}}",
            "TWS_USERID_PAPER=${{shared.IB_USERNAME}}",
            "TWS_PASSWORD_PAPER=${{shared.IB_PASSWORD}}",
            "TWOFA_DEVICE=IB Key",
            "AUTO_RESTART_TIME=10:00 PM",
            "TWOFA_TIMEOUT_ACTION=restart",
            "RELOGIN_AFTER_TWOFA_TIMEOUT=yes",
            "VNC_SERVER_PASSWORD=${{shared.VNC_PASSWORD}}",
            "TIME_ZONE=America/New_York",
            "TZ=America/New_York",
            "JAVA_HEAP_SIZE=1024",
        ):
            self.assertIn(required, railway)
        self.assertNotIn("matching Compose", railway)
        self.assertNotIn("or the actual private service DNS", railway)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotRegex(readme, r"railway\.app/(?:template|new)")

    def test_historical_paper_plan_is_preserved_and_superseded(self):
        plan = (ROOT / "docs" / "v3-paper-test-plan.md").read_text(encoding="utf-8")
        self.assertTrue(plan.startswith("# Historical evidence — superseded"))
        self.assertIn("The original document is preserved unchanged below.", plan)
        for retained_heading in (
            "# Rebalancer v3 paper-acceptance plan",
            "## Weekend manual order",
            "## Acceptance matrix",
            "## Rollback",
        ):
            self.assertIn(retained_heading, plan)
        self.assertGreater(len(plan.splitlines()), 450)

    def test_validation_scripts_are_portable_and_complete(self):
        validate_path = ROOT / "tools" / "validate.sh"
        smoke_path = ROOT / "tools" / "smoke-test.sh"
        for script_path in (validate_path, smoke_path):
            raw = script_path.read_bytes()
            self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
            self.assertNotIn(b"\r\n", raw)

        validate = validate_path.read_text(encoding="utf-8")
        for required in (
            'PYTHONPATH="$repo_dir/rebalancer"',
            "python -m pytest -q tests",
            "python3 -m pytest -q tests",
            'docker compose --env-file "$compose_env" config --quiet',
            'bash "$repo_dir/tools/smoke-test.sh"',
        ):
            self.assertIn(required, validate)

        smoke = smoke_path.read_text(encoding="utf-8")
        for required in (
            "command -v python",
            "command -v python3",
            "command -v uv",
            "run_host_python",
            "export MSYS_NO_PATHCONV=1",
        ):
            self.assertIn(required, smoke)

    @unittest.skipIf(os.name == "nt", "runner selection executes in the Linux validation path")
    def test_validation_runner_skips_an_unusable_python_executable(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            log = fake_bin / "calls.log"

            def executable(name: str, body: str) -> None:
                path = fake_bin / name
                path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
                path.chmod(0o755)

            executable("uv", "exit 1")
            executable("uv.exe", "exit 1")
            executable("python", "exit 1")
            executable(
                "python3",
                'if [ "$1" = "-c" ]; then exit 0; fi\n'
                'printf "python3 %s\\n" "$*" >>"$VALIDATE_TEST_LOG"',
            )
            executable(
                "docker", 'printf "docker %s\\n" "$*" >>"$VALIDATE_TEST_LOG"'
            )
            executable(
                "bash", 'printf "bash %s\\n" "$*" >>"$VALIDATE_TEST_LOG"'
            )

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["VALIDATE_TEST_LOG"] = str(log)
            completed = subprocess.run(
                [bash, str(ROOT / "tools" / "validate.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("python3 -m pytest -q tests", calls)
            self.assertIn("docker compose", calls)
            self.assertIn("bash ", calls)
            self.assertNotIn("python -m pytest", calls)

    def test_removed_runtime_file_interfaces_are_absent(self):
        self.assertFalse((ROOT / "config.yaml").exists())
        self.assertFalse((ROOT / "tools" / "rebalance.sh").exists())
        self.assertFalse((ROOT / "tools" / "schedule.sh").exists())
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "rebalancer" / "app").rglob("*.py")
        )
        self.assertNotIn("get_config(", production)
        self.assertNotIn("load_config(", production)
        self.assertNotIn("scheduled.json", production)
        self.assertNotIn("manual-rebalance", production)

    def test_upgrade_guide_enforces_the_one_way_migration_contract(self):
        guide = (ROOT / "docs" / "upgrade-dashboard.md").read_text(encoding="utf-8")
        routes = (ROOT / "rebalancer" / "app" / "web" / "routes.py").read_text(
            encoding="utf-8"
        )

        import_posts = re.findall(r'@router\.post\("([^\"]*import[^\"]*)"', routes)
        self.assertEqual(import_posts, ["/accounts/import"])
        for required in (
            "one-time Account YAML import",
            "does not import it and uses exact immutable code defaults",
            "explicit acceptance of every lost customization",
            "recreate only intended work in the dashboard",
            "Do not replay a legacy manual-request file",
            "Keep legacy JSONL Run files unchanged",
            "they are not imported",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)


if __name__ == "__main__":
    unittest.main()
