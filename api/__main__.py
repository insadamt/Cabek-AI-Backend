import argparse

import uvicorn


# ============================================================
# LOCAL API LAUNCHER
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the API on a free local port.")
    parser.add_argument("--reload", action="store_true", help="Restart when Python files change.")
    arguments = parser.parse_args()

    # Port zero lets the OS reserve a free port without a check-then-bind race.

    uvicorn.run("api.main:app", host="127.0.0.1", port=0, reload=arguments.reload)


if __name__ == "__main__":
    main()
