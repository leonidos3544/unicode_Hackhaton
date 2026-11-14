import logging

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Access logs include query strings; use the structured middleware instead.
    uvicorn.run("app.api.app:app", host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    main()
