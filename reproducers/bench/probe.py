import sys, time
import pyvisa
from pyvisa import constants

HOST = "192.168.81.74"
for sub in ("hislip0", "hislip1", "inst0"):
    rn = f"TCPIP0::{HOST}::{sub},4880::INSTR"
    rm = pyvisa.ResourceManager("@py")
    try:
        t0 = time.time()
        inst = rm.open_resource(rn, open_timeout=5000)
        inst.timeout = 5000
        idn = inst.query("*IDN?").strip()
        print(f"{sub:9s} OK  ({time.time()-t0:.2f}s)  {idn!r}")
        print("           max_msg_kb:", inst.get_visa_attribute(constants.ResourceAttribute.tcpip_hislip_max_message_kb))
        print("           stb:", hex(inst.read_stb()))
        inst.close()
    except Exception as e:
        print(f"{sub:9s} FAIL {type(e).__name__}: {e}")
    finally:
        rm.close()
