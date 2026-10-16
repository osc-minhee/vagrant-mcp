FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# 의존성 설치 
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# 보안: root 로 돌리지 않는다
RUN useradd -m app
USER app

EXPOSE 8000
CMD ["vagrant-mcp"]