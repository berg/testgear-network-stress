import time
import pyvisa
from pyvisa import constants
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")

def rate(inst, n=20):
    t0=time.time()
    for _ in range(n): inst.query("*IDN?")
    return n/(time.time()-t0)

a = rm.open_resource(RN, open_timeout=5000); a.timeout=10000
print("=== 1. does REN state explain the slowdown? ===")
a.visalib.gpib_control_ren(a.session, constants.RENLineOperation.asrt)
print(f"  REN asserted:            {rate(a):5.1f} queries/s")
a.visalib.gpib_control_ren(a.session, constants.RENLineOperation.deassert_gtl)
print(f"  REN deasserted (GTL):    {rate(a):5.1f} queries/s")
a.visalib.gpib_control_ren(a.session, constants.RENLineOperation.asrt)
print(f"  REN re-asserted:         {rate(a):5.1f} queries/s")

print("\n=== 2. does the gateway enforce exclusive locks? ===")
b = rm.open_resource(RN, open_timeout=5000); b.timeout=10000
b.query("*IDN?")
ia = a.visalib.sessions[a.session].interface
ib = b.visalib.sessions[b.session].interface
print(f"  A lock request  -> server said {ia.async_lock_request(2.0, '')!r}")
print(f"  B lock request  -> server said {ib.async_lock_request(2.0, '')!r}   <-- 'success' means not enforced")
print(f"  A lock_info (exclusive flag) -> {ia.async_lock_info()}")
print(f"  A release -> {ia.async_lock_release()!r}   B release -> {ib.async_lock_release()!r}")

print("\n=== 3. can B still do I/O while A holds an exclusive lock? ===")
print(f"  A lock -> {ia.async_lock_request(2.0, '')!r}")
try:
    print(f"  B query while A locked -> {b.query('*IDN?').strip()!r}  (a real lock would block this)")
except Exception as e:
    print(f"  B query while A locked -> blocked: {type(e).__name__}")
ia.async_lock_release()
a.close(); b.close()
