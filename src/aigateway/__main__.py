"""CLI entry points.

    aigateway serve --config gateway.yaml [--host 0.0.0.0] [--port 8080]
    aigateway check gateway.yaml          # validate config, print summary, exit 0/1
"""

from __future__ import annotations

import argparse
import logging
import sys


def serve_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="aigateway serve", description="Run the AI gateway")
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
    return 0


def check_main(argv: list[str]) -> int:
    """Validate a gateway config end-to-end without starting the server."""
    parser = argparse.ArgumentParser(
        prog="aigateway check", description="Validate a gateway config (exit 0 = usable)"
    )
    parser.add_argument("config", help="Path to gateway YAML config")
    args = parser.parse_args(argv)

    from aigateway.config import load_config

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot load config: {exc}")
        return 1

    problems: list[str] = []
    providers = None
    router = None
    chain = None
    try:
        providers = config.build_providers()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"providers: {exc}")
    try:
        router = config.build_router()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"models: {exc}")
    try:
        chain = config.build_plugin_chain()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plugins: {exc}")

    # Warnings for production-readiness gaps.
    server = config.section("server") or {}
    if not server.get("admin_api_key"):
        problems.append("WARNING: server.admin_api_key not set — /metrics and /admin/* are open")
    shared = config.section("shared_state") or {}
    if shared.get("backend", "memory") == "memory":
        problems.append(
            "WARNING: shared_state.backend=memory — limits are per-process; use redis for N replicas"
        )

    if chain is not None:
        print(f"plugins ({len(chain.plugins)}):")
        for info in chain.describe():
            print(f"  - {info['name']} [{info['class']}] enabled={info['enabled']}")
    if router is not None:
        print(f"models ({len(router.model_names())}):")
        for name, route in router.routes.items():
            deployments = ", ".join(
                f"{d.provider}/{d.model}(w={d.weight},p={d.priority})" for d in route.deployments
            )
            fallbacks = f" fallbacks={route.fallbacks}" if route.fallbacks else ""
            print(f"  - {name}: {deployments}{fallbacks}")
    if providers:
        print(f"providers ({len(providers)}): {', '.join(sorted(providers))}")

    if problems:
        print()
        for problem in problems:
            print(f"  ! {problem}")
    hard_errors = [p for p in problems if not p.startswith("WARNING")]
    if hard_errors or router is None or chain is None or providers is None:
        print(f"\nRESULT: INVALID ({len(hard_errors)} error(s))")
        return 1
    print("\nRESULT: OK (config is usable)")
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "check":
        sys.exit(check_main(argv[1:]))
    sys.exit(serve_main(argv))


if __name__ == "__main__":
    main()
