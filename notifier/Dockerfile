FROM python:3.12-slim
WORKDIR /app
# curl: carica_docsgpt.py lo usa per inviare gli zip (diversi GB) del ricaricamento mensile.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# def.finanze.it (CeRDEF, usato da downloader.py) non invia il certificato
# intermedio Sectigo OV R36: lo aggiungiamo al bundle CA di requests.
COPY sectigo-ov-r36.pem .
RUN cat "$(python -c 'import certifi; print(certifi.where())')" sectigo-ov-r36.pem > /app/ca-bundle.pem
ENV REQUESTS_CA_BUNDLE=/app/ca-bundle.pem
COPY notifier.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "notifier.py"]
