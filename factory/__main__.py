"""Start the factory:        python -m factory            (then open http://127.0.0.1:8000)
Sign in to YouTube once:   python -m factory youtube-login
"""
import argparse
import sys


def main():
    p = argparse.ArgumentParser(description="Run the Agent Factory and its control view.")
    p.add_argument("command", nargs="?", default="run", choices=["run", "youtube-login"])
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it from your phone on the same Wi-Fi")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    if args.command == "youtube-login":
        from . import config
        from .publish.youtube import UploadError, login
        try:
            token = login(config.load())
        except UploadError as e:
            sys.exit(str(e))
        print(f"Signed in. The factory can now upload to your channel (saved to {token}).")
        print("Set [publishing.youtube] live = true in config.toml to start uploading approved Shorts.")
        return

    import uvicorn
    from .server import create_app
    print(f"Agent Factory running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
