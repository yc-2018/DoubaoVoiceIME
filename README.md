# 豆包语音输入（独立版）

把豆包输入法里的语音转文字能力单独抽出来的 Windows 小工具：按住快捷键说话，松开后文字直接输入到当前光标处，不用切换输入法。

## 功能

- 按住说话、松开输入，`Esc` 取消
- 实时转录浮层，边说边看
- 默认快捷键 `右 Alt`，托盘菜单里可改成任意组合键
- 输入结果校验：文字没进输入框时自动复制到剪贴板，不会丢
- 托盘图标与菜单：设置快捷键、开机自启、查看日志、退出
- 单实例运行，重复启动会提示
- 无控制台窗口，开机自启不打扰
- 绿色单文件，无需安装

## 使用

1. 从 [Releases](../../releases) 下载 `DoubaoVoice.exe`
2. 双击运行，出现托盘图标即已就绪
3. 在任意输入框里按住 `右 Alt`（或你设置的快捷键）说话，松开后自动输入；按 `Esc` 取消本次录音
4. 右击托盘图标可设置快捷键、开启开机自启、查看日志或退出

语音识别在豆包服务器完成，需要联网。如果听不到声音，检查“设置 → 隐私 → 麦克风”是否允许桌面应用访问麦克风。

## 构建

环境：Windows x64、Python 3.11。

```bat
pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean DoubaoVoice_new.spec
```

产物为 `dist\DoubaoVoice_new.exe`，重命名为 `DoubaoVoice.exe` 即可。推送 `v*` 标签时 GitHub Actions 会自动构建并发布 Release。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--device N` | 指定麦克风设备编号 |
| `--wav FILE` | 用 wav 文件代替麦克风（调试） |
| `--once` | 启动后识别一次并退出（配合 `--wav`） |
| `--inject unicode\|paste` | 文字注入方式，默认 unicode |
| `--no-inject` | 只识别不输入 |
| `--no-ui` | 不显示悬浮提示 |
| `--no-tray` | 不显示托盘图标 |
| `--verbose` | 记录每个中间识别结果 |

## 目录结构

```
app/voice_ime.py      主程序（键盘钩子、录音、ASR、文字注入、托盘、开机自启）
app/dll/              豆包 SAMI Core SDK
app/icon.ico          程序与托盘图标
DoubaoVoice_new.spec  PyInstaller 打包配置
tools/                逆向分析辅助脚本
```

运行日志和配置保存在 exe 同目录：`voice_ime.log`、`config.json`。

## 注意

- 非豆包官方项目。语音识别调用豆包输入法内置的 SDK 与凭据，账号与数据使用受豆包服务条款约束。
- 仅供个人学习使用，请勿分发 `app/dll` 下的 SDK 文件。
- 快捷键没反应时先看托盘菜单的“查看日志”，常见原因是组合键被其他软件占用。
