FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app

COPY . .

RUN python -m pip install --no-cache-dir -r requirements.txt
RUN python -m pip install --no-cache-dir aws_opentelemetry_distro_genai_beta>=0.1.2

ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1
ENV DOCKER_CONTAINER=1

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/mcp')" || exit 1

CMD ["opentelemetry-instrument", "python", "-m", "mcp_proxy.main"]
