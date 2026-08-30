from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import argparse
import os

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE = ROOT / "_site"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--directory", default=str(DEFAULT_SITE))
    args = parser.parse_args()

    directory = Path(args.directory).resolve()
    os.chdir(directory)

    print(f"Open http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
