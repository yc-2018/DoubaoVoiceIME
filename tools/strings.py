import re, sys, os

def extract(path, minlen=5):
    data = open(path, 'rb').read()
    out = []
    # ASCII
    for m in re.finditer(rb'[\x20-\x7e]{%d,}' % minlen, data):
        out.append((m.start(), 'A', m.group().decode('ascii')))
    # UTF-16LE
    for m in re.finditer(rb'(?:[\x20-\x7e]\x00){%d,}' % minlen, data):
        out.append((m.start(), 'W', m.group().decode('utf-16-le')))
    out.sort()
    return out

if __name__ == '__main__':
    path = sys.argv[1]
    pat = sys.argv[2] if len(sys.argv) > 2 else None
    ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    items = extract(path)
    if pat:
        rx = re.compile(pat, re.I)
        for i, (off, kind, s) in enumerate(items):
            if rx.search(s):
                if ctx:
                    lo = max(0, i - ctx); hi = min(len(items), i + ctx + 1)
                    print(f'--- 0x{off:x} [{kind}] ---')
                    for o2, k2, s2 in items[lo:hi]:
                        print(f'  0x{o2:x} {k2} {s2}')
                else:
                    print(f'0x{off:08x} {kind} {s}')
    else:
        for off, kind, s in items:
            print(f'0x{off:08x} {kind} {s}')
