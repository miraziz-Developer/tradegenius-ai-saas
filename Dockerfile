# TradeGenius worker: Telegram bot + supervisor (Linux Python) and MT5 engines
# (Windows Python + MetaTrader 5 under Wine). Wine prefix lives on the persistent
# disk (/data/wine) and is set up on first boot by docker/setup_wine.sh.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    WINEDEBUG=-all \
    WINEARCH=win64 \
    WINEPREFIX=/data/wine \
    WINEDLLOVERRIDES="mscoree,mshtml=" \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates wget gnupg2 xvfb xauth cabextract procps unzip \
        python3 python3-pip \
    && mkdir -pm755 /etc/apt/keyrings \
    && wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -qNP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-stable \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Installers are baked into the image; they are run once against the persistent prefix.
# Python uses the embeddable zip: the regular .exe installer (WiX Burn) is unreliable under Wine.
RUN mkdir -p /opt/installers \
    && wget -qO /opt/installers/python-3.11.9-embed-amd64.zip \
        https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip \
    && wget -qO /opt/installers/get-pip.py https://bootstrap.pypa.io/get-pip.py \
    && wget -qO /opt/installers/mt5setup.exe \
        https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

WORKDIR /app
COPY requirements.txt requirements-wine.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY tradegenius ./tradegenius
COPY engine ./engine
COPY docker ./docker
RUN chmod +x docker/*.sh

CMD ["/app/docker/entrypoint.sh"]
