# Pinned to noble: the floating `base:ubuntu` tag moved to Ubuntu 26.04 in 2026-10, which an
# unpinned rebuild would pick up silently (new Docker CLI, npm, libbrotli). Move the OS on purpose.
# Pinned by release and digest (v0.4.35, built 2026-10-09): `ubuntu-24.04` floats inside noble, so
# a rebuild got whatever base the cache held (v1.3.x shipped the 2025-10-16 layer).
FROM mcr.microsoft.com/devcontainers/base:3.0.10-noble@sha256:21e9e3275736cb3bc33a2904b279a2ab292c51fedd53c2963463627d4586c3e6

# Change to root only when running locally on MAC, otherwise leave it to vscode 
ARG USERNAME=vscode
# Upstream-signed release binaries, pinned (v1.4). The unpinned lookups drifted (stable.txt) or
# failed silently: an unauthenticated GitHub API 403 left KIND_RELEASE empty and saved an XML error
# page as `kind`. Bump them with a new image tag; each build asserts the version it installed.
ARG HELM_VERSION=v3.22.1
ARG KUBECTL_VERSION=v1.37.1
ARG KIND_VERSION=v0.33.0
# Docker CLI: the newest 28.x. 29.x requires API >= 1.44 and refuses older daemons ("client
# version 1.52 is too new. Maximum supported API version is 1.43" on the GitHub Codespaces host);
# 28.x still negotiates down to 1.43. Do not move to 29.x until the Codespaces daemon does. 28.x
# gets no new upstream builds, so its Go stdlib findings stay until then. The docker.io package
# stays for the docker group the entrypoint maps; its daemon is never used (the socket is mounted).
ARG DOCKER_CLI_VERSION=28.5.2
# GitHub CLI from its release (checksum-verified). Ubuntu's `gh` is 2.45.0 built with go1.22.2.
ARG GH_VERSION=2.103.3
# Multiarchitecture, plattform is passed on when building the image.
ARG TARGETPLATFORM
ARG BUILDPLATFORM
ARG TARGETARCH

RUN echo "Host is running on $BUILDPLATFORM and the binaries will be downloaded for '$TARGETARCH', building for $TARGETPLATFORM"

# `upgrade` first: the base layer lags the Ubuntu security archive (v1.3.2 shipped openssl
# 3.0.13-0ubuntu3.6 and rsync 3.2.7-1ubuntu1.2, both with fixes out). No `docker.io`: up to v1.3.2
# it was installed only for its docker group, and it brought dockerd, containerd, runc, ctr and the
# docker-trust plugin, which nothing runs (the socket is mounted) but which carried most of the
# image's Go CRITICAL findings. The group is created directly; /entrypoint.sh maps its GID.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl \
    ca-certificates \
    iptables \
    socat \
    ebtables \
    ethtool \
    conntrack \
    sudo \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system docker \
    && ! command -v dockerd && ! command -v containerd

# Install GitHub CLI
RUN curl -fsSLO "https://github.com/cli/cli/releases/download/v$GH_VERSION/gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz" \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v$GH_VERSION/gh_${GH_VERSION}_checksums.txt" \
       | grep " gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz\$" | sha256sum -c - \
    && tar -xzf gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz \
    && install -o root -g root -m 0755 gh_${GH_VERSION}_linux_$TARGETARCH/bin/gh /usr/local/bin/gh \
    && rm -rf gh_${GH_VERSION}_linux_$TARGETARCH* \
    && gh --version | grep -q "^gh version $GH_VERSION "

# Install the pinned static Docker CLI (not the daemon), at both usual paths.
RUN case "$TARGETARCH" in amd64) A=x86_64 ;; arm64) A=aarch64 ;; *) echo "unsupported $TARGETARCH"; exit 1 ;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/$A/docker-$DOCKER_CLI_VERSION.tgz" | tar -xz -C /tmp docker/docker \
    && install -o root -g root -m 0755 /tmp/docker/docker /usr/local/bin/docker \
    && ln -sf /usr/local/bin/docker /usr/bin/docker \
    && rm -rf /tmp/docker \
    && test "$(docker version --format '{{.Client.Version}}' 2>/dev/null)" = "$DOCKER_CLI_VERSION"

# Install kubectl (x86_64/amd64, aarch64/arm64)
RUN curl -fLO "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$TARGETARCH/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && kubectl version --client | grep -qx "Client Version: $KUBECTL_VERSION"

# Install Helm
RUN curl -fLO https://get.helm.sh/helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz \
    && tar -zxvf helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz \
    && mv linux-$TARGETARCH/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz linux-$TARGETARCH \
    && helm version --template '{{.Version}}' | grep -qx "$HELM_VERSION"

# Install kind
RUN curl -fsSL -o /usr/local/bin/kind https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-$TARGETARCH \
    && chmod +x /usr/local/bin/kind \
    && kind version | grep -q "^kind $KIND_VERSION "
    
# Install Node.js and npm (multi-architecture support)
# Pinned so a rebuild is reproducible. 22.23.4 fixes CVE-2026-111497 (Brotli decoder OOB write);
# do not lower it. Bump to a newer 22.x patch with a new image tag, never by retagging.
ARG NODE_VERSION=22.23.4
# npm major held to v1.2's; @latest moved to npm 12. Move it on purpose with a new tag.
ARG NPM_MAJOR=11
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs=${NODE_VERSION}-1nodesource1 \
    && test "$(node -v)" = "v${NODE_VERSION}" \
    && npm install -g npm@${NPM_MAJOR} \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /

RUN chmod +x /entrypoint.sh

# In case the user is root, easy workaround when running locally on MAC so root can access the docker.socket of the hosts, otherwise user should be vscode.
# Change ownership carefully and use only locally. Container should run with --privileged
RUN if [ "$USERNAME" != "root" ]; then \
      chown -R $USERNAME:$USERNAME /home/$USERNAME; \
    fi

USER $USERNAME

# Entrypoint is on the mounted volume using the framework, post-create.sh
ENTRYPOINT ["/entrypoint.sh"]

CMD [ "sleep", "infinity" ]