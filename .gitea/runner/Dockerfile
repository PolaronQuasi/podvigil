FROM ubuntu:24.04

COPY --from=node:24.11.1-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=gitea/act_runner:0.2.13 /usr/local/bin/act_runner /usr/local/bin/act_runner
COPY --from=docker:28.4-cli /usr/local/bin/docker /usr/local/bin/docker

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        git \
        curl \
        unzip \
        xz-utils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && mkdir -p /opt/hostedtoolcache
ENV RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache

WORKDIR /data

ENTRYPOINT ["act_runner", "--config", "/data/config.yaml", "daemon"]
