macpm on a Linux GPU server
===========================

Two ways to check compatibility without touching the host's Python:

1. Host Python, throwaway virtualenv (fastest, sees the host exactly as-is):

       python3 -m venv /tmp/macpm-venv
       . /tmp/macpm-venv/bin/activate
       pip install --no-index --find-links /path/to/wheels .   # or pip install .
       python3 -m macpm.probe            # inventory, no terminal needed
       python3 -m macpm.dump --capabilities
       macpm                              # the TUI

2. Container (isolated, same kernel and /sys):

       docker build -f packaging/linux/Dockerfile -t macpm:test .
       docker run --rm -it --pid=host --privileged --network=host macpm:test

   Offline build: fetch the wheels on a connected machine, copy them next to
   the Dockerfile's build context and uncomment the two wheelhouse lines in
   the Dockerfile:

       pip download -d wheels dashing psutil humanize setuptools wheel

   The container image does not change what the kernel exposes, so the venv
   path is usually enough for a compatibility check.

What to look for
----------------

* ``python3 -m macpm.probe`` lists every ``/sys/class/drm/card*`` with its
  driver and the counters it exposes.  A vendor accelerator (Iluvatar,
  Denglin, …) that shows no ``DRM cards`` is not visible to macpm at all;
  one that shows a card but no ``busy``/``files`` can only be displayed with
  ``n/a`` until its driver or vendor tool is wired in.
* ``macpm-dump --capabilities`` prints the same detection the TUI uses.
* Root adds RAPL power, all processes' GPU time (fdinfo) and every user's
  sockets; run the probe and macpm both ways to compare.

Notes
-----

* A container does not change what the kernel exposes: /sys is the host's and
  /proc follows ``--pid=host``.  Numbers inside and outside the container
  should match, which makes the container a safe way to try macpm on a
  production node.
* ``docker`` often needs root or membership in the docker group on compute
  nodes.  If the image cannot be built (no registry access), use the venv
  path instead.
