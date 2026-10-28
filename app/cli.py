from __future__ import annotations

import argparse

from app.config import settings
from app.db import init_db
from app.logging_config import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Configuration Change Monitoring System commands")
    parser.add_argument("command", choices=["init-db", "watcher", "worker", "mock-webhook"])
    args = parser.parse_args()
    configure_logging()

    if args.command == "init-db":
        settings.config_root.mkdir(parents=True, exist_ok=True)
        init_db()
        print("Database tables created/verified.")
    elif args.command == "watcher":
        from app.watcher import WatcherService
        WatcherService().run_forever()
    elif args.command == "worker":
        from app.notifier import DeliveryWorker
        try:
            DeliveryWorker().run_forever()
        except KeyboardInterrupt:
            print("Worker shutdown requested.")
    elif args.command == "mock-webhook":
        from app.mock_webhook import main as mock_main
        mock_main()


if __name__ == "__main__":
    main()
