FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir "fastapi==0.115.*" "uvicorn==0.32.*" \
 && useradd -u 1001 -m egress && mkdir -p /data && chown egress:egress /data
COPY egress.py /srv/egress.py
USER egress
EXPOSE 3128 8090
STOPSIGNAL SIGINT
CMD ["python", "/srv/egress.py"]
