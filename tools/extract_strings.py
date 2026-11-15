"""Finner alle tekster merket med _() eller N_() i waypoint/*.py."""
import ast, glob, sys

def strings():
    out = []
    for f in sorted(glob.glob("waypoint/*.py")):
        with open(f) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "N_"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    s = node.args[0].value
                    if s not in out:
                        out.append(s)
    return out

if __name__ == "__main__":
    for s in strings():
        print(repr(s))
