FROM python:3.12-slim

# Tesseract is the OCR engine used for scanned invoices.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY samples ./samples
COPY scripts ./scripts

# Build-time smoke test: every demo invoice must reach its expected decision with this
# image's OCR engine. If it does not, the build fails instead of the demo.
RUN INVOICE_DESK_AI=off python scripts/run_samples.py --check

ENV INVOICE_DESK_DATA=/srv/data
EXPOSE 8000
# One worker on purpose: runs are queued in-process so two copies of an invoice cannot race.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
