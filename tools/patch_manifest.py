import pefile, sys, os

p = os.path.abspath(os.path.join(os.getcwd(), "work", "ime", "ImeService.exe"))
pe = pefile.PE(p)
target = None
for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
    if t.id != 24:
        continue
    for n in t.directory.entries:
        for l in n.directory.entries:
            target = (l.data.struct.OffsetToData, l.data.struct.Size)
off, size = target
new = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"></requestedExecutionLevel></requestedPrivileges></security></trustInfo><application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings><dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware><dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2,PerMonitor</dpiAwareness></windowsSettings></application></assembly>'''
new = new + b' ' * max(0, size - len(new))
assert len(new) == size, (len(new), size)
with open(p, 'rb') as f:
    data = bytearray(f.read())
data[off:off+size] = new
with open(p, 'wb') as f:
    f.write(data)
print("patched", p, hex(off), size)
