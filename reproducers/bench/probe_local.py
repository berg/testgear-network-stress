import pyvisa
rm = pyvisa.ResourceManager("@py")
for sub in ("hislip0","hislip23","hislip0,23","gpib0,23","hislip1","inst0"):
    rn = f"TCPIP0::127.0.0.1::{sub},4880::INSTR" if "," not in sub else f"TCPIP0::127.0.0.1::{sub}::INSTR"
    try:
        inst = rm.open_resource(rn, open_timeout=4000); inst.timeout = 4000
        idn = inst.query("*IDN?").strip()
        print(f"  {rn}\n      OK -> {idn!r}")
        inst.close()
    except Exception as e:
        print(f"  {rn}\n      {type(e).__name__}: {str(e)[:80]}")
