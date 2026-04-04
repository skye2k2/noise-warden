from __future__ import annotations
import uvicorn
from app.config import load_settings
from app.engine import NoiseWardenEngine
from app.webapp import build_app


def main():
    settings = load_settings("config.yaml")
    engine = NoiseWardenEngine(settings)
    engine.start()
    app = build_app(engine)

    try:
        uvicorn.run(app, host=settings.app.host, port=settings.app.port, log_level=settings.app.log_level.lower())
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
