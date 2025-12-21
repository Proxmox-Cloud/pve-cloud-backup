FROM quay.io/ceph/ceph:v19.2.1

# empty by default, using official pypi
ARG LOCAL_PYPI_IP

# encoding map missing from the quay image, we need this for certain pip packages
COPY cp437.py /usr/lib64/python3.9/encodings/
COPY cp1252.py /usr/lib64/python3.9/encodings/

# need -e for \n to line break
RUN echo -e "Host *\n  StrictHostKeyChecking no" > /root/.ssh/config && \
    chmod 600 /root/.ssh/config

WORKDIR /app

RUN yum install python3-pip git -y

# install requirements in seperate layer
COPY requirements.txt ./

# when needed update like pve-cloud-controller tdd build 
RUN pip install ${LOCAL_PYPI_IP:+--index-url http://$LOCAL_PYPI_IP:8088/simple }${LOCAL_PYPI_IP:+--trusted-host $LOCAL_PYPI_IP }-r requirements.txt

# install the package
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-deps .

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3"]

