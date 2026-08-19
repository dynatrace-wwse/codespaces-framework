FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask gunicorn
WORKDIR /srv
# check-tenant-setup.sh is the REAL file here; ../check-tenant-setup.sh is a
# symlink to it. That direction matters: the two used to be byte-identical
# copies, so a fix could land in one and silently miss the other — and the page
# is the copy SEs actually run. Docker COPY follows a symlink out of the build
# context into a dangling link, so the canonical file has to live in the context.
COPY app.py check-tenant-setup.sh ./
RUN chmod +x check-tenant-setup.sh && useradd -r -u 10001 -m -d /home/checker checker
USER 10001
ENV HOME=/home/checker
EXPOSE 8080
# Runs take ~30 s of outbound probes: threads, generous worker timeout.
CMD ["gunicorn", "-w", "2", "--threads", "8", "--timeout", "240", "-b", "0.0.0.0:8080", "app:app"]
