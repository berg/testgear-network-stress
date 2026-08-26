"""Reproduce the 09_remote_local flow and report exactly what the server says."""
import time, traceback
import pyvisa
from pyvisa import constants
from pyvisa_py.protocols import hislip
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
i = rm.open_resource(RN, open_timeout=5000); i.timeout = 5000
lib, sess = i.visalib, i.session
iface = i.visalib.sessions[sess].interface

def rate(n=6):
    t0 = time.time()
    for _ in range(n): i.query("*IDN?")
    return n / (time.time() - t0)

EXPECT = [
    (constants.RENLineOperation.deassert, "local"),
    (constants.RENLineOperation.asrt, "remote"),
    (constants.RENLineOperation.deassert_gtl, "local"),
    (constants.RENLineOperation.asrt_address, "remote"),
    (constants.RENLineOperation.asrt_llo, "remote"),
    (constants.RENLineOperation.asrt_address_llo, "remote"),
]
try:
    for mode, expected in EXPECT:
        opposite = (constants.RENLineOperation.deassert if expected == "remote"
                    else constants.RENLineOperation.asrt_address)
        lib.gpib_control_ren(sess, opposite)
        before = rate()
        lib.gpib_control_ren(sess, mode)
        after = rate()
        print(f"  {mode.name:20s} before={before:5.1f}/s after={after:5.1f}/s")
except hislip.HiSLIPServerError as e:
    print(f"\n  SERVER ERROR: fatal={e.fatal} control_code={e.control_code}")
    print(f"  description: {e.description}")
except Exception as e:
    print(f"\n  {type(e).__name__}: {e}")
    # dig for the underlying cause
    try:
        iface.receive(64)
    except hislip.HiSLIPServerError as inner:
        print(f"  underlying: fatal={inner.fatal} code={inner.control_code} {inner.description}")
    except Exception as inner:
        print(f"  underlying: {type(inner).__name__}: {inner}")
try: i.close()
except Exception: pass
