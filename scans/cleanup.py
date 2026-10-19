from pathlib import Path


# ============================================================
# RETRY CLEANUP INSIDE THE LOCKED ANALYSIS DIRECTORY
# ============================================================

def is_generated_annotation(name: str) -> bool:
    if name.endswith(".tmp"):
        name = name[:-4]
    return name.endswith(("_pieces.png", "_dommages.png", "_critical.png"))


def remove_incomplete_outputs(analysis_dir: Path) -> None:

    # The caller holds both locks and has ruled out a completed analysis before deletion.

    generated = {"result.json", "report.pdf", "result.json.tmp", "report.pdf.tmp",
                 ".request.json", ".worker.log"}
    for path in analysis_dir.iterdir():
        if path.name in generated or (path.name.startswith(".scan-") and path.name.endswith(".tmp")):
            if path.is_file() or path.is_symlink():
                path.unlink()
    annotated = analysis_dir / "annotated"
    if not annotated.is_dir():
        return
    for path in annotated.iterdir():
        if is_generated_annotation(path.name) or (path.name.startswith(".scan-") and path.name.endswith(".tmp")):
            if path.is_file() or path.is_symlink():
                path.unlink()
