# Base Ubuntu with Wine for running Windows MT5 headlessly
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEDEBUG=-all
ENV WINEPREFIX=/root/.wine
ENV DISPLAY=:99

# Install dependencies and Wine
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine64 \
        wine32 \
        xvfb \
        cabextract \
        wget \
        curl \
        ca-certificates \
        procps \
        unzip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Download and install Windows Python 3.11 inside Wine
RUN xvfb-run wineboot --init && \
    wget https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe -O /tmp/python-installer.exe && \
    xvfb-run wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 TargetDir="C:\\Python311" && \
    rm /tmp/python-installer.exe

# Install MT5 and Python packages inside Wine
RUN wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe -O /tmp/mt5setup.exe && \
    xvfb-run wine /tmp/mt5setup.exe /auto && \
    rm /tmp/mt5setup.exe

# Install required Python packages inside Wine's Python
RUN xvfb-run wine "C:\\Python311\\python.exe" -m pip install --no-cache-dir MetaTrader5 pandas numpy requests urllib3

# Copy strategy engine and entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
COPY strategy_engine.py /app/strategy_engine.py

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
