FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd -u 1002 -m fwd
COPY fwd.py /srv/fwd.py
USER fwd
EXPOSE 8787
STOPSIGNAL SIGINT
CMD ["python", "/srv/fwd.py"]
