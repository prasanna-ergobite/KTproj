FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --default-timeout=1000 --retries=10 -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
