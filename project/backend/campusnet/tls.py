"""HTTPS 上下文的唯一出口。

校园门户大量使用自签证书：内网地址拿不到公网 CA 签发的证书，门户也不会为
`10.x` / `192.168.x` 去申请一张。所以认证路径上必须跳过证书校验——这不是
「图省事」，是没有别的选择。

值得单独一个模块的理由是那段关校验的代码有顺序要求：Python 3.10 起，
`check_hostname` 为 True 时把 `verify_mode` 设成 `CERT_NONE` 会直接抛
`ValueError: Cannot set verify_mode to CERT_NONE when check_hostname is enabled`。
原来这段逻辑在探活、门户、SRUN、发现四处各写一遍，改错任何一处都是运行时
才炸的坑；收敛到一处之后，也一眼能看出全项目到底哪些地方在关校验。

`insecure=False` 保持 `create_default_context()` 的原样行为（加载系统 CA、
校验证书与主机名），供将来真需要校验的调用方使用。
"""

from __future__ import annotations

import ssl


def portal_context(insecure: bool = False) -> ssl.SSLContext:
    """建一个可直接交给 urllib 用的 HTTPS 上下文。

    返回的 context 同时适配两种用法：
    `urllib.request.HTTPSHandler(context=ctx)` 与
    `urllib.request.urlopen(..., context=ctx)`。
    """
    context = ssl.create_default_context()
    if insecure:
        # 顺序不能反，理由见模块开头的 ValueError 说明。
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
