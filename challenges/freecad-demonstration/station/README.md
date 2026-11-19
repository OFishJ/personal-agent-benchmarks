# Windows 本地 GUI 工位

已用 FreeCAD 1.1.0（Qt 6 / PySide6）及带 pywin32 的宿主 Python 测试这一套 GUI 后端。只用 B、C 时可直接读取试卷图片及 JSON。A 允许其他真实 FreeCAD GUI 录制工具。

在工位目录运行以下命令，用实际安装路径替换示例。每次选一个新运行目录；临时文件和图片写在该目录。考生应只能访问题面、工位和自己的提交，评测者保管 examiner。私有桌面隔离键鼠和窗口；若考生具备全盘文件权限，应另设受限账号或沙箱来保证答案不可读。

```powershell
python launch.py --exe 'D:/Apps/FreeCAD/bin/FreeCAD.exe' --run 'E:/exam-runs/run01'
python bgctl.py --run 'E:/exam-runs/run01' observe
```

Python 可以导入同目录 `bgctl.request(run, method, args)`。用 `observe` 返回的当前实际画面和可见控件定位，再调用 click、double_click、right_click、drag、scroll、type 或 hotkey。不同版本和显示布局的坐标会变化。可用方法拼写和参数以桥接源码中的实际分派表为准。`start_record` 接收 name 和 task，`stop_record` 结束建模记录，保存另记。默认 legacy-strict 键盘词表为 tab/delete/esc/enter；如选择桥接器支持的扩展键模式，应在交卷说明记录。失败请求先查 GUI 状态和回执，别重复落地的输入。

正式动作和主帧在运行目录的 records 下。请求、实际事件、抓图与辅助证据保留在运行目录，不作为考生宣称成功的替代。控制只作用在本次 Windows 私有桌面。收工时用 GUI 关闭本次考试实例；已完成记录先核实写盘。
