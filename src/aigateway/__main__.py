"""CLI entry point: `aigateway --config gateway.yaml [--host 0.0.0.0] [--port 8080]`."""

from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aigateway", description="Extensible AI gateway (OpenAI-compatible)"
    )
    parser.add_argument("--config", required=True, help="Path to gateway YAML config")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--no-config-watcher", action="store_true", help="Disable hot config reload")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    from aigateway.api import create_app

    app = create_app(args.config, config_watcher=not args.no_config_watcher)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
