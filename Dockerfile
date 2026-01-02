# FROM quay.io/ceph/ceph:v19.2.1
FROM debian:bookworm

# empty by default, using official pypi
ARG LOCAL_PYPI_IP

# # encoding map missing from the quay image, we need this for certain pip packages
# COPY cp437.py /usr/lib64/python3.9/encodings/
# COPY cp1252.py /usr/lib64/python3.9/encodings/

# install essentials
RUN apt update && apt install python3 python3-pip python3-venv wget ssh -y

# install ceph commons (cli tools)
RUN wget -qO- https://download.ceph.com/keys/release.asc \
    | tee /etc/apt/trusted.gpg.d/ceph.asc
RUN echo "deb [signed-by=/etc/apt/trusted.gpg.d/ceph.asc] https://download.ceph.com/debian-squid bookworm main" > /etc/apt/sources.list.d/ceph.list
RUN apt update && apt install ceph-common -y

# need -e for \n to line break
RUN echo -e "Host *\n  StrictHostKeyChecking no" > /root/.ssh/config && \
    chmod 600 /root/.ssh/config

WORKDIR /app

# RUN yum install python3-pip git -y

# install requirements in seperate layer
COPY requirements.txt ./

RUN python3 -m venv /opt/fetcher

# when needed update like pve-cloud-controller tdd build 
RUN /opt/fetcher/bin/pip install ${LOCAL_PYPI_IP:+--index-url http://$LOCAL_PYPI_IP:8088/simple }${LOCAL_PYPI_IP:+--trusted-host $LOCAL_PYPI_IP }-r requirements.txt

# install the package
COPY pyproject.toml ./
COPY src/ ./src/

RUN /opt/fetcher/bin/pip install --no-deps .

ENV PYTHONUNBUFFERED=1

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]