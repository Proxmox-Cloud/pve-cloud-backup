FROM debian:bookworm

# empty by default, using official pypi
ARG LOCAL_PYPI_IP

# install essentials
RUN apt update && apt install python3 python3-pip python3-venv wget ssh git -y

# install ceph commons (cli tools)
RUN wget -qO- https://download.ceph.com/keys/release.asc \
    | tee /etc/apt/trusted.gpg.d/ceph.asc
RUN echo "deb [signed-by=/etc/apt/trusted.gpg.d/ceph.asc] https://download.ceph.com/debian-squid bookworm main" > /etc/apt/sources.list.d/ceph.list
RUN apt update && apt install ceph-common -y

COPY --chmod=600 ssh-config /root/.ssh/config

WORKDIR /app

# install requirements in seperate layer
COPY requirements.txt ./

RUN python3 -m venv /opt/fetcher

RUN if [ -n "$LOCAL_PYPI_IP" ]; then \
        echo "Running tdd build"; \
        /opt/fetcher/bin/pip install --upgrade --upgrade-strategy eager --no-cache-dir --index-url http://$LOCAL_PYPI_IP:8088/simple --trusted-host $LOCAL_PYPI_IP -r requirements.txt; \
    else \
        echo "Running normal build"; \
        /opt/fetcher/bin/pip install -r requirements.txt; \
    fi

# install the package
COPY pyproject.toml ./
COPY src/ ./src/

RUN /opt/fetcher/bin/pip install --no-deps .

ENV PYTHONUNBUFFERED=1

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
