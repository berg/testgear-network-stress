"""What does ugpibd do with valid-but-empty vs out-of-range vs malformed addresses?"""
import time
import pyvisa
rm = pyvisa.ResourceManager("@py")
# GPIB primary addresses are 0-30; 31 is untalk/unlisten. Anything above is invalid.
for sub in ("hislip23","hislip0","hislip5","hislip30","hislip31","hislip99",
            "hislip255","hislip999","hislipabc","hislip"):
    rn = f"TCPIP0::127.0.0.1::{sub},4880::INSTR"
    t0 = time.time()
    try:
        i = rm.open_resource(rn, open_timeout=3000); i.timeout = 1500
        opened = time.time() - t0
        try:
            idn = i.query("*IDN?").strip()
            print(f"  {sub:11s} open {opened:.2f}s -> {idn[:38]!r}")
        except Exception as e:
            print(f"  {sub:11s} open {opened:.2f}s -> I/O {type(e).__name__} "
                  f"after {time.time()-t0-opened:.2f}s")
        i.close()
    except Exception as e:
        print(f"  {sub:11s} open FAILED after {time.time()-t0:.2f}s: {type(e).__name__}")
