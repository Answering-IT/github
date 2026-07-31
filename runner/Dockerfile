# Runtime for the self-hosted GitHub Actions runner.
#
# The runner lives on the Jenkins EC2, which is Amazon Linux 2 with glibc 2.26. The
# runner bundles only node20 and node24, both of which need glibc 2.28 or newer, so
# every JavaScript action -- actions/checkout, setup-java, configure-aws-credentials,
# setup-terraform, github-script, upload-artifact -- fails natively with
#
#   /lib64/libc.so.6: version `GLIBC_2.28' not found
#
# The .NET listener itself does run on 2.26, which is why `run.sh` connects happily
# and then every job dies at checkout. Ubuntu 22.04 carries glibc 2.35, so running
# the runner inside this image fixes the whole class of failure without touching the
# host OS or Jenkins.
#
# The runner directory is mounted in rather than baked here, so the existing
# registration in .credentials is reused and no new registration token is needed.
FROM ubuntu:22.04

# Must match the host's ghrunner user and the host's docker group, or the mounted
# runner directory is unwritable and the mounted socket unusable. Override at build
# time if those ids differ.
ARG RUNNER_UID=1003
ARG RUNNER_GID=1003
ARG DOCKER_GID=992

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

# libicu is what the runner needs for globalization; with it present the
# DOTNET_SYSTEM_GLOBALIZATION_INVARIANT workaround is unnecessary.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
      libicu70 \
      locales \
      sudo \
      tar \
      unzip \
      zip \
 && locale-gen en_US.UTF-8 \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI only. The daemon is the host's, reached through the mounted socket, so
# builds and Testcontainers containers are siblings of this one rather than nested.
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu jammy stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# AWS CLI v2. Deploy.yml needs `ecr get-login-password`, which does not exist in v1 --
# and v1 on Python 2.7 is what the host carries for Jenkins. Installing v2 here keeps
# the two entirely separate.
RUN curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip \
 && aws --version

RUN groupadd -g "${RUNNER_GID}" ghrunner \
 && useradd -m -u "${RUNNER_UID}" -g "${RUNNER_GID}" -s /bin/bash ghrunner \
 && groupadd -g "${DOCKER_GID}" hostdocker \
 && usermod -aG hostdocker ghrunner

USER ghrunner
WORKDIR /home/ghrunner/actions-runner

# run.sh, not runsvc.sh: the latter wraps the listener in node, which is the very
# thing that cannot start. systemd supervises the container instead.
ENTRYPOINT ["./run.sh"]
