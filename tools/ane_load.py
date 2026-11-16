#!/usr/bin/env python3
"""Drive the Apple Neural Engine and print its utilisation while it runs.

A manual check for macpm's ANE gauge, not part of the monitor.  It builds a
small Core ML convolution program (an ``mlprogram`` -- the format the ANE
executes), predicts in a loop, and reads the same IOReport ``ANE/status``
channel macpm reads.

``coremltools`` is needed in the interpreter that runs this; macpm itself does
not depend on it::

    python3 -m venv /tmp/ane-venv
    /tmp/ane-venv/bin/pip install coremltools
    /tmp/ane-venv/bin/python tools/ane_load.py --seconds 20

Watch ``ANE Usage`` in macpm's default view while it runs: idle reads 0 %,
this should hold it near 100 %.  Work Apple schedules on the GPU (Vision
requests, for example) does not move this counter.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHAPE = (1, 3, 128, 128)


def build_program():
    """A conv/relu/pool stack: small, but shaped so the ANE accepts it."""
    import numpy as np
    from coremltools.converters.mil import Builder as mb

    @mb.program(input_specs=[mb.TensorSpec(shape=SHAPE)])
    def model(x):
        w1 = mb.const(val=(np.random.randn(64, 3, 3, 3) * 0.05).astype(np.float32))
        c1 = mb.conv(x=x, weight=w1, strides=[1, 1], pad_type="same")
        r1 = mb.relu(x=c1)
        p1 = mb.max_pool(x=r1, kernel_sizes=[2, 2], strides=[2, 2], pad_type="valid")
        w2 = mb.const(val=(np.random.randn(128, 64, 3, 3) * 0.05).astype(np.float32))
        c2 = mb.conv(x=p1, weight=w2, strides=[1, 1], pad_type="same")
        r2 = mb.relu(x=c2)
        return mb.max_pool(x=r2, kernel_sizes=[2, 2], strides=[2, 2], pad_type="valid")

    return model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a Core ML convolution loop to exercise the Apple Neural Engine",
    )
    parser.add_argument("--seconds", type=float, default=20.0,
                        help="how long to keep predicting (default 20)")
    args = parser.parse_args(argv)
    try:
        import coremltools as ct
        import numpy as np
    except ImportError as error:
        raise SystemExit(
            "coremltools is required for this check: "
            "python3 -m venv /tmp/ane-venv && "
            "/tmp/ane-venv/bin/pip install coremltools\n"
            f"({error})"
        )

    from macpm.ioreport import Subscription
    from macpm.metrics import ane_usage

    model = ct.convert(
        build_program(),
        convert_to="mlprogram",
        compute_units=ct.ComputeUnit.CPU_AND_NE,
        minimum_deployment_target=ct.target.macOS14,
    )
    sample = np.zeros(SHAPE, dtype=np.float32)
    model.predict({"x": sample})

    ane = Subscription("ANE")
    ane.delta()
    start = last = time.time()
    done = 0
    print("driving the ANE for %.0f s (Ctrl+C to stop)" % args.seconds)
    try:
        while time.time() - start < args.seconds:
            model.predict({"x": sample})
            done += 1
            now = time.time()
            if now - last >= 1.0:
                usage = ane_usage(ane.delta() or {})
                print("  %5.1f s  %5.0f predicts/s  ANE usage %s" % (
                    now - start, done / (now - start),
                    "n/a" if usage is None else "%.0f%%" % usage,
                ), flush=True)
                last = now
    except KeyboardInterrupt:
        pass
    finally:
        ane.close()
    print("total predictions:", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
