"""Start the factory: python -m factory  (then open http://127.0.0.1:8000)"""
import argparse

import uvicorn

from .server import create_app


def main():
    p = argparse.ArgumentParser(description="Run the Agent Factory and its control view.")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it from your phone on the same Wi-Fi")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    print(f"Agent Factory running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
