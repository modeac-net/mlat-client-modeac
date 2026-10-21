# mlat-client with Mode A/C forwarding, for running next to an ultrafeeder stack.
# Multi-arch friendly: builds on amd64 and arm64/armv7 (Raspberry Pi).

FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir setuptools pyasyncore \
 && python3 setup.py build_ext --inplace \
 && python3 tools/test_modeac.py

FROM python:3.12-slim
RUN pip install --no-cache-dir pyasyncore
WORKDIR /app
COPY --from=build /src/mlat /app/mlat
COPY --from=build /src/mlat-client /app/mlat-client
COPY --from=build /src/tools /app/tools
COPY --from=build /src/_modes*.so /app/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /app/mlat-client
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/entrypoint.sh"]
