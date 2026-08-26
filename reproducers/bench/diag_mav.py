"""Does MAV read correctly now that AsyncStatusQuery names the right message?"""
import sys, time
import pyvisa
RN = sys.argv[1]
rm = pyvisa.ResourceManager("@py")
i = rm.open_resource(RN, open_timeout=8000); i.timeout = 8000
i.query("*IDN?")
for _ in range(10):
    if i.query("SYST:ERR?").strip().startswith(("+0,","0,")): break

print(f"  stb with nothing pending      : {i.read_stb():#04x}")
i.write("*IDN?")                    # response queued, deliberately unread
time.sleep(0.4)
stb = i.read_stb()
print(f"  stb with a reply outstanding  : {stb:#04x}   MAV={'SET' if stb & 0x10 else 'clear'}")
try: i.read_raw()
except Exception: pass
print(f"  stb after draining it         : {i.read_stb():#04x}")
i.close()
