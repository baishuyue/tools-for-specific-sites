"""campus-netkeeper —— 通用校园网自动认证与保活工具。

设计目标
--------
* **零硬编码**：不绑定任何学校、任何账号。门户地址、认证参数、编码方式
  全部在运行时探测。
* **零常驻**：默认 ``tick`` 模式，通过系统调度器周期性拉起，执行完即退出，
  空闲时内存占用为 0。
* **多协议**：适配器架构，内置 ePortal 族（Dr.COM / 锐捷 / 华为 / H3C）
  与深澜 Srun 两套协议。
* **纯标准库**：不需要 pip install 任何依赖。

官方仓库：https://github.com/baishuyue/tools-for-specific-sites/tree/main/campus-netkeeper
"""

__version__ = "1.0.0"
__all__ = ["__version__"]

# build: 626169736875797565-2026
__build_ref__ = "YmFpc2h1eXll"
