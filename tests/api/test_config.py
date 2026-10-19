import os
import subprocess
import sys

from api.config import ApiSettings


# ============================================================
# LOCAL PRICING CONFIGURATION
# ============================================================

def test_local_pricing_configuration_is_inherited_by_workers(tmp_path, monkeypatch):
    monkeypatch.setattr("api.config.PROJECT_ROOT", tmp_path)
    for key in ("AUTOESTIMATE_TOKEN", "AUTOESTIMATE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        'AUTOESTIMATE_TOKEN="local-test-token"\n'
        'AUTOESTIMATE_BASE_URL=http://pricing.test/api/v1\n'
    )

    # Track dotenv changes so test credentials cannot leak into later tests.

    with monkeypatch.context() as environment:
        for key in ("AUTOESTIMATE_TOKEN", "AUTOESTIMATE_BASE_URL"):
            environment.setenv(key, "")
            environment.delenv(key)
        ApiSettings.from_environment()
        assert os.environ["AUTOESTIMATE_BASE_URL"] == "http://pricing.test/api/v1"
        child = subprocess.run(
            [sys.executable, "-c",
             "import os; assert os.environ['AUTOESTIMATE_TOKEN'] == 'local-test-token'"],
            capture_output=True, check=False,
        )
        assert child.returncode == 0


def test_deployment_environment_overrides_local_file(tmp_path, monkeypatch):
    monkeypatch.setattr("api.config.PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text('AUTOESTIMATE_TOKEN="local-test-token"\n')
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "deployment-test-token")

    ApiSettings.from_environment()

    assert os.environ["AUTOESTIMATE_TOKEN"] == "deployment-test-token"
