"""Check actual local capture prerequisites before the lighting owner displays READY.

Depends on the live scan supervisor, configured model backend, sample provider and
writable dossier storage. ESP32 availability is reported separately from app health.
"""

from tempfile import TemporaryFile


def check_capture_readiness(manager, captures):
    if not manager.available or manager.recovery_thread is None or not manager.recovery_thread.is_alive():
        raise RuntimeError("Scan supervisor is unavailable.")
    if captures.stopping.is_set() or captures.settings.tunnel_mode != "mock":
        raise RuntimeError("No operational capture provider is configured.")
    manager.backend.ensure_available(manager.settings.models_dir)
    root = manager.repository.dossiers_dir
    with TemporaryFile(dir=root) as probe:
        probe.write(b"cabek-readiness")
        probe.flush()

    # Verification layouts may contain phase subdirectories instead of a shared photo set.

    phases = ("before", "after") if all(
        (captures.provider.root / phase).is_dir() for phase in ("before", "after")
    ) else (None,)
    for phase in phases:
        for sample in captures.provider.plan_capture(phase):
            captures.provider.read_photo(sample["source"])
