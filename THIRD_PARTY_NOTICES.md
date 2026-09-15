# 第三方组件说明

本程序是独立的执行桥接层，不代表 OpenAI 官方产品或官方支持。桥接源码与官方 Codex 分开维护。

EXE 包含 Python 与第三方 Python 依赖。对应声明及许可证原件位于 THIRD_PARTY_LICENSES；requirements-lock.txt 记录本次 Windows 构建/测试环境，也包含开发工具。

0.1.10 新增 Playwright 1.62.0 及其随附 Node 驱动、greenlet 3.5.5、pyee 13.0.1。对应项目许可、Node 许可和驱动第三方声明已按原文件收录，共 45 份。Playwright 驱动通过管道控制用户已安装的 Tabbit；交付包不包含 Tabbit/Chromium 浏览器，也不包含 Tabbit AI 模型组件。Tabbit 为用户独立安装的软件，使用其自身的上游许可。

官方 Codex、官方 GUI Runtime、Git 和 ripgrep 没有捆绑进 EXE，仍从用户本机已安装组件动态发现；这些独立组件保留各自的上游许可。
