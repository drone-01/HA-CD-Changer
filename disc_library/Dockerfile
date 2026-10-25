FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

WORKDIR /opt/disc_library

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY www ./www

ENV DL_DATA_DIR=/data \
    DL_OPTIONS=/data/options.json \
    DL_WWW=/opt/disc_library/www

# Plain Python process; the Supervisor's init (init: true) is PID 1.
ENTRYPOINT []
CMD ["python3", "-m", "app.main"]
