"""Which REN operation drops the connection? Cycle each and watch."""
import time, traceback
import pyvisa
from pyvisa import constants
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")

for mode in constants.RENLineOperation:
    i = rm.open_resource(RN, open_timeout=5000); i.timeout = 8000
    lib, sess = i.visalib, i.session
    lib.gpib_control_ren(sess, constants.RENLineOperation.asrt_address)
    i.query("*IDN?")
    try:
        for n in range(12):
            lib.gpib_control_ren(sess, mode)
            i.query("*IDN?")
        print(f"  {mode.name:20s} survived 12 cycles")
    except Exception as e:
        print(f"  {mode.name:20s} DIED at cycle {n}: {type(e).__name__}: {str(e)[:50]}")
    finally:
        try: i.close()
        except Exception: pass
