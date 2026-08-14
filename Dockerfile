# The fallback path: any host that takes a container rather than a Procfile.
# Same process the Procfile and render.yaml start, so all three routes run one command.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code edit does not re-resolve scikit-learn on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 8000, not 5000: macOS hands 5000 to the AirPlay receiver, and a container that publishes
# to it looks like the app failing rather than the port being taken.
ENV PORT=8000
EXPOSE 8000

# Shell form on purpose — $PORT has to be expanded at run time, because the host injects it.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
