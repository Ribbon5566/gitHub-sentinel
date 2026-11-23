"""基础设施层：端口的真实（或可替代）实现。

依赖规则：只能 import domain / ports / 标准库 / 第三方包，**禁止** import application 或 api。
加一个新通知渠道就是在这里加一个文件，上层不需要知道。
"""
