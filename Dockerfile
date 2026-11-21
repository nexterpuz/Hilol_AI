FROM python:3.12-slim
WORKDIR /srv
# Rasmdagi yozuvni o'qish uchun (yakshanba ish tartibi rasmi): Tesseract, rus va o'zbek (kirill) tillari
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-uzb-cyrl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY scripts scripts
COPY seed.json .
CMD ["python", "-m", "app.main"]
