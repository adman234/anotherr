import logging
import os

import uvicorn

from anotherr import redact


def main():
    logging.basicConfig(level=os.environ.get("ANOTHERR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact.install()
    # log_config=None: uvicorn's loggers propagate to our (redacting) root handlers instead of
    # getting their own unfiltered ones - access logs would otherwise show ?apikey= from webhooks.
    uvicorn.run("anotherr.api:app", host=os.environ.get("ANOTHERR_HOST", "0.0.0.0"),
                port=int(os.environ.get("ANOTHERR_PORT", "9797")), log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
