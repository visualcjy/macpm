# Run macpm inside a container on a Linux GPU server.
#
# Build (needs network for the dependencies once; see README.txt for the
# offline wheelhouse variant):
#
#   docker build -f packaging/linux/Dockerfile -t macpm:test .
#
# Run with host hardware and process visibility:
#
#   docker run --rm -it --pid=host --privileged --network=host macpm:test
#
# --pid=host     per-process GPU time (DRM fdinfo) and other users' sockets
# --privileged   RAPL energy counters (root-only since kernel 5.10) and fdinfo
# --network=host public-IP lookup and the real interface list
#
# /sys is the host's /sys inside the container (Docker mounts it read-only), so
# temperatures, fans, VRAM and GPU utilisation are the real machine's values.
FROM python:3.12-slim

WORKDIR /opt/macpm

COPY pyproject.toml setup.py README.md LICENSE MANIFEST.in ./
COPY macpm ./macpm

# Install the runtime dependencies and the package itself.  For an
# air-gapped host, copy a wheelhouse in first and use ``--no-index``:
#   COPY wheels /wheels
#   RUN pip install --no-index --find-links /wheels --no-build-isolation .
RUN pip install --no-cache-dir --no-build-isolation .

# The TUI needs a real terminal; the probe works anywhere.
ENTRYPOINT ["python3", "-m", "macpm"]
