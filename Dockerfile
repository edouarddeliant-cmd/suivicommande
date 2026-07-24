FROM python:3.12-slim

# poppler-utils fournit "pdftotext"/"pdftoppm" (lecture des proformas PDF)
# tesseract-ocr : OCR de secours pour les PDF sans couche texte (print-to-PDF vectoriel, scans)
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
