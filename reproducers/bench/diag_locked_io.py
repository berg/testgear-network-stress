"""How does each server refuse I/O from a session that holds no lock?"""
import sys, time
import pyvisa
from pyvisa import constants
RN = sys.argv[1]
rm = pyvisa.ResourceManager("@py")
a = rm.open_resource(RN, open_timeout=8000); a.timeout = 4000
b = rm.open_resource(RN, open_timeout=8000); b.timeout = 4000
a.query("*IDN?"); b.query("*IDN?")
ia = a.visalib.sessions[a.session].interface

print(f"A takes an exclusive lock: {ia.async_lock_request(2.0, '')!r}")
for label, fn in (
    ("B write", lambda: b.visalib.write(b.session, b"*IDN?\n")),
    ("B read",  lambda: b.visalib.read(b.session, 4096)),
    ("B read_stb", lambda: b.visalib.read_stb(b.session)),
):
    t0 = time.time()
    try:
        r = fn()
        print(f"  {label:11s} -> {str(r)[:40]:42s} in {time.time()-t0:5.2f}s")
    except Exception as e:
        print(f"  {label:11s} -> {type(e).__name__}: {str(e)[:34]:34s} in {time.time()-t0:5.2f}s")
ia.async_lock_release()
print("A released; B should work now:")
try:
    print("  B query ->", b.query("*IDN?").strip()[:40])
except Exception as e:
    print("  B query ->", type(e).__name__, e)
a.close(); b.close()
