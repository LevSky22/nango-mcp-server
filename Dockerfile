FROM python:3.13-slim AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 nango-mcp
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER nango-mcp
WORKDIR /home/nango-mcp
ENV NANGO_MCP_TRANSPORT=http \
    NANGO_MCP_HTTP_HOST=0.0.0.0 \
    NANGO_MCP_HTTP_PORT=3000
EXPOSE 3000
ENTRYPOINT ["nango-mcp"]
