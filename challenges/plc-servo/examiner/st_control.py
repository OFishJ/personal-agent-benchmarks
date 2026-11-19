"""Bounded, integer/boolean ST scan runner for the commissioning exercise."""
import ast
import operator
import re


OPS = {ast.Add:operator.add, ast.Sub:operator.sub, ast.USub:operator.neg, ast.UAdd:operator.pos,
       ast.Not:operator.not_, ast.Eq:operator.eq, ast.NotEq:operator.ne, ast.Gt:operator.gt,
       ast.GtE:operator.ge, ast.Lt:operator.lt, ast.LtE:operator.le}
WORDS = {"TRUE":"True","FALSE":"False","AND":"and","OR":"or","NOT":"not"}
INPUTS = {f"D{i}" for i in range(12)}


def expression(source):
    if re.search(r"[^A-Z0-9\s()+<>=-]",source):
        raise ValueError(f"表达式含不支持字符 {source}")
    source = re.sub(r"\b(TRUE|FALSE|AND|OR|NOT)\b",lambda m:WORDS[m[0]],source)
    source = re.sub(r"(?<![<>=!])=(?!=)","==",source.replace("<>","!="))
    root = ast.parse(source.strip(),mode="eval").body
    def build(n):
        if isinstance(n,ast.Constant) and type(n.value) in (bool,int):
            value = n.value
            if abs(value)>2147483647:
                raise ValueError("常数超出整型范围")
            return lambda _:value
        if isinstance(n,ast.Name) and re.fullmatch(r"[DM]\d{1,4}",n.id):
            return lambda mem:mem.get(n.id,0)
        if isinstance(n,ast.UnaryOp) and type(n.op) in OPS:
            arg,fn = build(n.operand),OPS[type(n.op)]
            return lambda mem:fn(arg(mem))
        if isinstance(n,ast.BinOp) and type(n.op) in (ast.Add,ast.Sub):
            left,right,fn = build(n.left),build(n.right),OPS[type(n.op)]
            return lambda mem:fn(left(mem),right(mem))
        if isinstance(n,ast.BoolOp) and type(n.op) in (ast.And,ast.Or):
            args = [build(v) for v in n.values]
            reduce = all if isinstance(n.op,ast.And) else any
            return lambda mem:reduce(arg(mem) for arg in args)
        if isinstance(n,ast.Compare) and len(n.ops)==1 and type(n.ops[0]) in OPS:
            left,right,fn = build(n.left),build(n.comparators[0]),OPS[type(n.ops[0])]
            return lambda mem:fn(left(mem),right(mem))
        raise ValueError(f"不支持的 ST 表达式 {source}")
    return build(root)


class Program:
    def __init__(self, source, inputs=INPUTS):
        if len(source)>65536:
            raise ValueError("首版单程序限 64 KiB")
        clean = re.sub(r"\(\*.*?\*\)"," ",source.upper(),flags=re.S)
        clean = re.sub(r"//[^\n]*"," ",clean)
        tokens = [part.strip() for part in re.split(r"(\b(?:ELSIF|END_IF|IF|THEN|ELSE)\b|;)",clean) if part.strip()]
        self.mem = {}
        self.code = []
        branches = []
        pos = 0
        def take():
            nonlocal pos
            if pos >= len(tokens):
                raise ValueError("ST 语句意外结束")
            item = tokens[pos]
            pos += 1
            return item
        def need(word):
            if take() != word:
                raise ValueError(f"需要 {word}")
        while pos < len(tokens):
            tok = take()
            if tok in ("IF","ELSIF","ELSE","END_IF"):
                if tok=="IF":
                    branches.append({"test":None,"ends":[]})
                elif not branches:
                    raise ValueError(f"{tok} 前缺 IF")
                frame=branches[-1]
                if tok in ("ELSIF","ELSE"):
                    if frame["test"] is None:
                        raise ValueError(f"ELSE 后不能再写 {tok}")
                    frame["ends"].append(len(self.code))
                    self.code.append(["jump",None,None])
                    self.code[frame["test"]][1]=len(self.code)
                    frame["test"]=None
                if tok in ("IF","ELSIF"):
                    frame["test"]=len(self.code)
                    self.code.append(["test",None,expression(take())])
                    need("THEN")
                if tok=="END_IF":
                    for patch in frame["ends"]+([frame["test"]] if frame["test"] is not None else []):
                        self.code[patch][1]=len(self.code)
                    branches.pop()
                    need(";")
            else:
                match = re.fullmatch(r"([DM]\d{1,4})\s*:=\s*(.+)",tok,re.S)
                if not match or match[1] in inputs:
                    raise ValueError(f"非法赋值或写入工位输入 {tok}")
                self.code.append(["set",match[1],expression(match[2])])
                need(";")
        if branches:
            raise ValueError("IF 缺 END_IF")

    def scan(self, registers, marker):
        self.mem.update({f"D{i}":n for i,n in enumerate(registers)})
        self.mem["D11"]=marker
        pc=0
        while pc<len(self.code):
            op,target,fn = self.code[pc]
            if op=="jump" or (op=="test" and not fn(self.mem)):
                pc=target
                continue
            if op=="set":
                value=int(fn(self.mem))
                self.mem[target]=int(bool(value)) if target.startswith("M") else ((value+32768)%65536-32768)
            pc+=1
        return [self.mem.get(k,0) for k in ("M100","D101","D102","D103")]
