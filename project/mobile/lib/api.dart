/// 与后端打交道的那一小部分。
///
/// 这里只用免鉴权接口。带鉴权的（读网卡 MAC、改设置、关机）都交给 WebView 里的
/// 控制台去做——它自己带着会话 Cookie，APP 不必去碰鉴权这套东西。
library;

import 'dart:async';
import 'dart:io';

/// 后端探活。
///
/// `/api/health` 在免鉴权白名单里（`server.py:1230`），所以电脑刚开机、还没配对、
/// 会话过期都无所谓，它照样答。APP 靠它判断该显示控制台还是「电脑关机了」。
///
/// 探活失败不是异常——「连不上」正是这个函数要回答的问题之一。
Future<bool> ping(
  String baseUrl, {
  Duration timeout = const Duration(seconds: 4),
}) async {
  final client = HttpClient()..connectionTimeout = timeout;
  try {
    final request = await client
        .getUrl(Uri.parse('$baseUrl/api/health'))
        .timeout(timeout);
    final response = await request.close().timeout(timeout);
    await response.drain<void>();
    return response.statusCode == 200;
  } on SocketException {
    return false; // 连不上（关机、不在同一网段、地址写错）
  } on TimeoutException {
    return false; // 丢包挂起——校园网常见，按「不通」处理
  } on HttpException {
    return false;
  } on FormatException {
    return false; // 地址本身不合法
  } finally {
    client.close(force: true);
  }
}
