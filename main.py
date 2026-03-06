"""Auton — Agent runtime server entry point."""

import logging

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)

from auton.api import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8420)
