FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN mkdir -p /data/cas && chown -R 65532:65532 /data
USER 65532:65532
ENTRYPOINT ["replayscope"]
CMD ["--help"]

