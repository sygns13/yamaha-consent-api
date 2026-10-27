# Mock de KIZUNA (solo local). Mismas versiones de FastAPI y Uvicorn que uv.lock.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /mock
RUN pip install "fastapi==0.162.0" "uvicorn==0.54.0"
COPY main.py ./

EXPOSE 8081
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
