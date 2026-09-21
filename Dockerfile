FROM python:3.12-slim

# openssh-client : le SDK docker passe par le binaire ssh (use_ssh_client)
# iputils-ping   : sonde ICMP, signal independant de l'API Proxmox
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client iputils-ping tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bifrost

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=root:root ssh_config /home/bifrost/.ssh/config
RUN chown -R bifrost:bifrost /home/bifrost/.ssh && chmod 700 /home/bifrost/.ssh \
 && chmod 600 /home/bifrost/.ssh/config

COPY bifrost/ /app/bifrost/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/bifrost
USER bifrost
VOLUME ["/data"]

# Verifie que la configuration se charge et que la prise repond. Ne modifie rien.
HEALTHCHECK --interval=60s --timeout=20s --start-period=20s --retries=3 \
  CMD python -c "from bifrost.config import load; from bifrost.infra.plug import PlugClient; \
import sys; c=load(); sys.exit(0 if PlugClient(c.plug).read().ok else 1)"

CMD ["python", "-m", "bifrost"]
