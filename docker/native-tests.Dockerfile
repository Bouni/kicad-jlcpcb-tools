# syntax=docker/dockerfile:1
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS ordinary
ENV DEBIAN_FRONTEND=noninteractive LC_CTYPE=C.UTF-8
# Bump this revision to refresh packages without changing the base image.
ARG DEPENDENCY_REVISION=1
RUN test -n "$DEPENDENCY_REVISION" \
    && apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends \
       ca-certificates git jq python3-pip python3-venv \
    && useradd --create-home --uid 1001 --shell /bin/bash runner \
    && rm -rf /var/lib/apt/lists/*
FROM ordinary AS wx
RUN apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends \
       python3-wxgtk4.0 xvfb xauth python3-pil python3-olefile \
       xfonts-base fonts-dejavu-core fonts-dejavu-mono fonts-urw-base35 x11-utils openbox \
       at-spi2-core libgtk-3-bin librsvg2-common libgl1-mesa-dri \
    && rm -rf /var/lib/apt/lists/*
FROM wx AS kicad
ARG KICAD_MAJOR
RUN case "$KICAD_MAJOR" in \
      7) ;; \
      8|9|10) apt-get -o APT::Update::Error-Mode=any update \
        && apt-get install -y --no-install-recommends software-properties-common \
        && add-apt-repository --yes --no-update "ppa:kicad/kicad-${KICAD_MAJOR}.0-releases" ;; \
      *) exit 1 ;; \
    esac \
    && apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends "kicad=${KICAD_MAJOR}.*" \
    && rm -rf /var/lib/apt/lists/*
