# Personal Agent Benchmarks

我把亲历过的工程工作整理成一套个人 AI 考题。每题给出实际要完成的工作、考生能拿到的材料，以及别人怎样独立检查交付物。

| 题目 | 自主性 | 任务难度 | 主要能力 | 完成条件 |
| --- | --- | --- | --- | --- |
| [01 · 单轴伺服调试](challenges/plc-servo/README.md) | L3 独立诊断与验证 | D3 多项约束联动 | 故障定位、控制程序实现、试验设计、独立验收 | 双向点动中验后，用同一份程序和组态跑通位置、速度、转矩 |
| [02 · FreeCAD 绘图、画框与中文描述](challenges/freecad-demonstration/README.md) | L2 自主实现 | D2 常规组合 | 参数化建模、GUI 操作记录、目标定位、操作意图辨读 | 在 GUI 中画一件随动桥架，为一段五孔圆盘轨迹画框，为另一段双柱融合轨迹补中文 |
| [03 · 紧凑同轴行星舵机关节](challenges/joint-module/README.md) | L3 独立诊断与验证 | D4 复杂推导或权衡 | 机电集成、行星传动设计、装配顺序、受载验证 | 从零交付可拆检数字样机并证明它满足转矩、刚度、空回和装配要求 |

自主性衡量考生要自己决定哪些事。任务难度衡量在已有路线指引时，把结果做对仍有多难。两项单独标注，首题采用设计评级，待真实模型盲测校准。[分级标准与跨领域示例](RATINGS.md)

### 运行单轴伺服题

需要 Python 3.10 或更高版本，只用标准库。在仓库根目录运行：

```sh
cd challenges/plc-servo
python -I -B -X utf8 examiner/blind_exam.py --show
python -I -B -X utf8 examiner/selftest.py
```

第一条命令显示考生实际收到的开场，第二条让参考程序和有缺陷的程序分别参加考试。正式考生从独立的工具入口接题。要接模型、交卷或读取逐项成绩，见[工位说明](challenges/plc-servo/README.md)。

### 运行 FreeCAD 题

三个作业可以分别开考。建模作业提供 Windows 私有桌面 FreeCAD 工位。画框和中文描述附完整原图和动作，可以直接作答。[试卷与验收说明](challenges/freecad-demonstration/README.md)

### 关于公开版

伺服题的开工资料已重写为合成设备描述、交接单和初始组态，使用轻量虚拟轴。FreeCAD 题取自作者的 GUI 建模、录制和标注项目，收录两段原始截图序列与相应动作。

作者与维护者为 **OFishJ**。OpenAI Codex 协助实现工位、检验用例和说明。代码、文档和合成材料采用 [MIT 许可证](LICENSE)。FreeCAD 界面截图与录制材料的出处见第二题说明。

**English.** Hands-on agent benchmarks in servo commissioning, FreeCAD, and compact planetary joint design. The FreeCAD challenge includes GUI modeling, bounding-box annotation, and Chinese step descriptions, with separate candidate materials and examiner references.
