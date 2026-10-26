"""Run one local API instance with a bounded deployment worker pool."""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run("ui.app:app", host="127.0.0.1", port=args.port, workers=1,
                proxy_headers=False, timeout_graceful_shutdown=10800)
