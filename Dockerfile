FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY restock_monitor.py .
ENV HEADLESS=1
CMD ["python", "-u", "restock_monitor.py"]
