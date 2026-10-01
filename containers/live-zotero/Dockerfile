FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils xvfb xauth fonts-dejavu-core \
    libgtk-3-0 libdbus-glib-1-2 libnss3 libasound2 libxt6 libx11-xcb1 libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# Match the development version; verify every downloaded Zotero/plugin artifact.
RUN curl --fail --location --retry 3 \
      https://download.zotero.org/client/release/9.0.6/Zotero-9.0.6_linux-x86_64.tar.xz -o /tmp/zotero.tar.xz \
    && echo '0db6e8f94bd0d84e862e6ef5c3e217030e173c0cd3c6dfbc836252c650fea3dd  /tmp/zotero.tar.xz' | sha256sum -c - \
    && mkdir -p /opt/zotero /opt/plugins \
    && tar -xJf /tmp/zotero.tar.xz --strip-components=1 -C /opt/zotero \
    && rm /tmp/zotero.tar.xz \
    && curl --fail --location --retry 3 \
      https://github.com/retorquere/zotero-better-bibtex/releases/download/debug-bridge/debug-bridge-3.0.1.xpi \
      -o /opt/plugins/debug-bridge.xpi \
    && echo '11fdabba319cd9014e633350718b9ee9ac0be113e414c86f575cf998e86bd3ba  /opt/plugins/debug-bridge.xpi' | sha256sum -c - \
    && curl --fail --location --retry 3 \
      https://github.com/wileyyugioh/zotmoov/releases/download/1.2.32/zotmoov-1.2.32-fx.xpi \
      -o /opt/plugins/zotmoov.xpi \
    && echo '7770dc2c62d2aaab2662fd7402afadd058387bff69578481ac039bfc050ecc2b  /opt/plugins/zotmoov.xpi' | sha256sum -c -

RUN pip install --no-cache-dir uv==0.11.28
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --extra mcp --extra test --locked
COPY tests ./tests
COPY benchmarks ./benchmarks
COPY containers ./containers

RUN useradd --create-home --uid 1000 user \
    && mkdir /work && chown user:user /work \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
USER user
ENV PATH="/app/.venv/bin:${PATH}" DISPLAY=:99
ENTRYPOINT ["python", "containers/live-zotero/run.py"]
