FROM python:3.10-alpine3.19

WORKDIR /usr/src
RUN apk update
RUN apk add smartmontools \
    && pip install prometheus_client PyYAML \
    # remove temporary files
    && rm -rf /root/.cache/ \
    && find / -name '*.pyc' -delete

COPY ./storcli64 /opt/storcli64

COPY ./smartprom.py /smartprom.py

EXPOSE 9902
ENTRYPOINT ["/usr/local/bin/python", "-u", "/smartprom.py"]

# HELP
# docker build -t vaa12345/prometheus-smartctl:test .
