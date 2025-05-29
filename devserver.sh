#!/bin/sh
source .venv/bin/activate
python -m flask --app main:app run -p $PORT --debug