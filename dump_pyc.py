import marshal, sys, types, dis

def walk(code, depth=0):
    pad = "  " * depth
    print(f"{pad}== {code.co_name} (line {code.co_firstlineno}) args={code.co_varnames[:code.co_argcount]}")
    print(f"{pad}   names: {code.co_names}")
    for c in code.co_consts:
        if isinstance(c, (str, int, float, tuple)) and c not in (None, 0, 1):
            print(f"{pad}   const: {c!r}")
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, depth + 1)

data = open(sys.argv[1], "rb").read()
code = marshal.loads(data[16:])   # 16-byte header on Python 3.7+
walk(code)
if len(sys.argv) > 2:
    dis.dis(code)