"""Trusted launcher for a fresh PLC exam. JSONL by default; optional text model on loopback."""
import argparse
import http.client
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from blind_room import Room, load_packet


def spec(name, description, props, required=()):
    return {"type":"function","function":{"name":name,"description":description,
            "parameters":{"type":"object","properties":props,"required":list(required),"additionalProperties":False}}}


TEXT={"type":"string"}
INTEGER={"type":"integer"}
TOOLS=[
    spec("list_files","列出本轮工位内的文件。",{}),
    spec("read_file","按虚拟文件名分页读取。",{"path":TEXT,"offset":INTEGER,"limit":INTEGER,
         "format":{"type":"string","enum":["text","base64"]}},["path"]),
    spec("find_text","在一个工位文本文件中查找字面字符串。",{"path":TEXT,"text":TEXT,"offset":INTEGER},["path","text"]),
    spec("write_file","保存工作区中的 UTF-8 文本，整个替换同名工作副本。",{"path":TEXT,"text":TEXT},["path","text"]),
    spec("station","操作本轮虚拟工位。说明见《盲测工位.md》。",
         {"action":{"type":"string","enum":["discover","compile","download","observe","input","advance","reset","checkpoint"]},
          "mode":{"type":"string","enum":["jog","position","speed","torque"]},
          "rpm100":INTEGER,"position_mdeg":INTEGER,"torque_permille":INTEGER,"duration_ms":INTEGER,
          "stop":{"type":"boolean"},"start":{"type":"boolean"},"scans":INTEGER},["action"]),
    spec("submit","封存本轮作品和交付说明。",{"note":TEXT})
]


def opening(room):
    return {"messages":room.opening(),"tools":TOOLS}


def run_model(room, model, communicate):
    messages=room.opening()
    for _ in range(160):
        msg=communicate({"model":model,"messages":messages,"tools":TOOLS,"tool_choice":"auto","stream":False})
        calls=msg.get("tool_calls") or []
        if not calls:
            return {"submitted":room.closed,"text":msg.get("content") or ""}
        if not isinstance(calls,list) or len(calls)>32:
            raise ValueError("模型返回的工具调用数无效")
        # Keep only ordinary assistant text and these function calls. No session ID,
        # previous chat, hosted tool, local desktop connector or inherited instructions.
        clean=[]
        for c in calls:
            fn=c["function"]
            if c.get("type")!="function" or not all(isinstance(v,str) for v in (c["id"],fn["name"],fn["arguments"])):
                raise ValueError("模型响应格式不受支持")
            clean.append({"id":c["id"],"type":"function","function":{"name":fn["name"],"arguments":fn["arguments"]}})
        messages.append({"role":"assistant","content":msg.get("content") if isinstance(msg.get("content"),str) else None,
                         "tool_calls":clean})
        for call in clean:
            try:
                args=json.loads(call["function"]["arguments"])
                reply=room.call(call["function"]["name"],args)
            except (ValueError,RecursionError):
                reply={"ok":False,"error":"工具参数需要 JSON 对象"}
            messages.append({"role":"tool","tool_call_id":call["id"],"content":json.dumps(reply,ensure_ascii=False)})
            if room.closed:
                return reply
    return {"submitted":False,"reason":"本轮模型交互已达上限"}


def local_model(port):
    def request(body):
        wire=json.dumps(body,ensure_ascii=False).encode("utf-8")
        if len(wire)>4*1024*1024:
            raise ValueError("本轮上下文达到 4 MiB 上限")
        # A fixed host and path controlled only by the examiner. No redirects or OS proxy.
        conn=http.client.HTTPConnection("127.0.0.1",port,timeout=180)
        try:
            conn.request("POST","/v1/chat/completions",body=wire,headers={"Content-Type":"application/json"})
            response=conn.getresponse()
            if response.status!=200:
                raise RuntimeError(f"模型端点返回 HTTP {response.status}")
            data=response.read(1024*1024+1)
            if len(data)>1024*1024:
                raise ValueError("模型响应过大")
            return json.loads(data)["choices"][0]["message"]
        finally:
            conn.close()
    return request


def emit(data):
    print(json.dumps(data,ensure_ascii=False,separators=(",",":")),flush=True)


def serve(room):
    emit(opening(room))
    while not room.closed:
        line=sys.stdin.buffer.readline(160*1024+1)
        if not line:
            return
        if len(line)>160*1024:
            emit({"ok":False,"error":"消息过大，此次连接结束"})
            return
        try:
            req=json.loads(line)
            if type(req) is not dict or set(req)!={"tool","arguments"}:
                raise ValueError
            reply=room.call(req["tool"],req["arguments"])
        except (ValueError, RecursionError):
            reply={"ok":False,"error":"消息应为含 tool、arguments 的 JSON 对象"}
        emit(reply)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--preview",action="store_true",help="查看新考生收到的开场和工具清单")
    parser.add_argument("--show",action="store_true",help="以易读形式预览本场开工内容")
    parser.add_argument("--local-model",help="指定已运行的本地无状态模型名称")
    parser.add_argument("--port",type=int,default=1234,help="受信任的本机模型端口")
    args=parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        room=Room(load_packet())
        if args.show:
            for msg in room.opening():
                print(msg["content"])
            print("考生可用工具\n"+"、".join(t["function"]["name"] for t in TOOLS))
        elif args.preview:
            emit(opening(room))
        elif args.local_model:
            emit(run_model(room,args.local_model,local_model(args.port)))
        else:
            serve(room)
    except Exception:
        emit({"ok":False,"error":"考场启动或连接失败，请考官检查材料、交卷存储或模型端点。"})
        sys.exit(1)
