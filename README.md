# Personal Agent Benchmarks

我把亲历过的工程调试整理成一套个人 AI 考题。想测的是：拿到一份有缺口的交接材料后，考生能否找出设备为什么不动，写出控制程序，再用设备反馈证明它真的动对了。

| 题目 | 自主性 | 任务难度 | 主要能力 | 完成条件 |
| --- | --- | --- | --- | --- |
| [01 · 单轴伺服调试](challenges/plc-servo/README.md) | L3 独立诊断与验证 | D3 多项约束联动 | 故障定位、控制程序实现、试验设计、独立验收 | 双向点动中验后，用同一份程序和组态跑通位置、速度、转矩 |

自主性衡量考生要自己决定哪些事。任务难度衡量在已有路线指引时，把结果做对仍有多难。两项单独标注，首题采用设计评级，待真实模型盲测校准。[分级标准与跨领域示例](RATINGS.md)

### 先跑一次

需要 Python 3.10 或更高版本，只用标准库。在仓库根目录运行：

```sh
cd challenges/plc-servo
python -I -B -X utf8 examiner/blind_exam.py --show
python -I -B -X utf8 examiner/selftest.py
```

第一条命令显示考生实际收到的开场，第二条让参考程序和有缺陷的程序分别参加考试。正式考生从独立的工具入口接题。要接模型、交卷或读取逐项成绩，见[工位说明](challenges/plc-servo/README.md)。

### 关于公开版

题目来自个人的工程调试经历。为了让其他人能在一台普通电脑上复验，我把开工资料重写为合成设备描述、交接单和初始组态。公开版运行的是一个轻量虚拟轴，不需要 PLC、工程软件或虚拟机。

作者与维护者为 **OFishJ**。OpenAI Codex 协助实现虚拟工位、编写验收用例和整理文档。代码、文档和合成材料采用 [MIT 许可证](LICENSE)。

**English.** A personal collection of AI-agent tasks adapted from hands-on engineering work. The first challenge asks an agent to commission a simulated servo axis and demonstrate three operating modes. It runs locally with Python and includes a reproducible grader.
