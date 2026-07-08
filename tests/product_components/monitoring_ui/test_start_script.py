from pathlib import Path


def test_powershell_start_redirects_backend_output_to_backend_log() -> None:
    script = Path("scripts/deployment/monitoring-ui/start.ps1").read_text()

    backend_command_line = next(
        line for line in script.splitlines() if line.startswith("$backendCommand =")
    )

    assert "src.product_components.monitoring_ui.backend 2>&1" in backend_command_line
    assert "Out-File -LiteralPath '$backendLog' -Append -Encoding utf8" in backend_command_line


def test_powershell_start_keeps_frontend_log_separate() -> None:
    script = Path("scripts/deployment/monitoring-ui/start.ps1").read_text()

    frontend_command_line = next(
        line for line in script.splitlines() if line.startswith("$frontendCommand =")
    )

    assert "Out-File -LiteralPath '$frontendLog' -Append -Encoding utf8" in frontend_command_line
