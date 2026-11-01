import logging
import os

import uvicorn


def main():
    logging.basicConfig(level=os.environ.get("ANOTHERR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("anotherr.api:app", host=os.environ.get("ANOTHERR_HOST", "0.0.0.0"),
                port=int(os.environ.get("ANOTHERR_PORT", "9797")), log_level="warning")


if __name__ == "__main__":
    main()
