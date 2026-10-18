"""Kick Drops Miner – entrypoint."""
import logging
import os

import uvicorn

from kdm import config

if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    uvicorn.run("kdm.web:socket_app", host=os.environ.get("HOST", "0.0.0.0"), port=config.PORT,
                log_level="warning", proxy_headers=True)
