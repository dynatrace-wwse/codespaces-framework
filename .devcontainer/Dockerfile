# Pinned to noble: the floating `base:ubuntu` tag moved to Ubuntu 26.04 in 2026-10, which an
# unpinned rebuild would pick up silently (new Docker CLI, npm, libbrotli). Move the OS on purpose.
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

# Change to root only when running locally on MAC, otherwise leave it to vscode 
ARG USERNAME=vscode
ARG HELM_VERSION=v3.17.0
# Pinned to what v1.2 shipped. The unpinned lookups drifted (stable.txt) or failed silently: an
# unauthenticated GitHub API 403 left KIND_RELEASE empty and saved an XML error page as `kind`.
ARG KUBECTL_VERSION=v1.34.1
ARG KIND_VERSION=v0.30.0
# Multiarchitecture, plattform is passed on when building the image.
ARG TARGETPLATFORM
ARG BUILDPLATFORM
ARG TARGETARCH

RUN echo "Host is running on $BUILDPLATFORM and the binaries will be downloaded for '$TARGETARCH', building for $TARGETPLATFORM"

# Install Docker CLI (not daemon)
RUN apt update && apt install -y \
    docker.io \
    curl \
    ca-certificates \
    iptables \
    socat \
    ebtables \
    ethtool \
    conntrack \
    sudo \
    gh \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl (x86_64/amd64, aarch64/arm64)
RUN curl -fLO "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$TARGETARCH/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl

# Install Helm
RUN curl -fLO https://get.helm.sh/helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz \
    && tar -zxvf helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz \
    && mv linux-$TARGETARCH/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf helm-$HELM_VERSION-linux-$TARGETARCH.tar.gz linux-$TARGETARCH

# Install kind
RUN curl -fsSL -o /usr/local/bin/kind https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-$TARGETARCH \
    && chmod +x /usr/local/bin/kind \
    && kind version
    
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